"""Current database/API fixture. Forwarding tests use acknowledged node snapshots."""

import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import api
import configurations
import database
import errors
import execution
import forwarding
import migrations
import node_sync
import resources
import settings
import topology


class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings.DB_PATH = str(Path(self.temp.name) / "test.db")
        self.db = migrations.init_db()
        self.h = resources.Resources()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def call(self, method, body=None, *args):
        with database.atomic(self.db):
            return getattr(self.h, method)(self.db, body or {}, *args)

    def node(self, name="A", kind="forward", ports="10000-10100"):
        return self.call(
            "api_node_add",
            dict(
                name=name,
                addr="192.0.2." + str(int(database.setting(self.db, "next_node_id"))),
                kind=kind,
                ports=ports,
            ),
        )["id"]

    def target(self, host="198.51.100.1", port=443):
        return self.call("api_target_save", dict(name="target", host=host, port=port))["id"]

    def graph(self, ids, target=None):
        target = target or self.target()
        vs = [
            dict(id="v" + str(i), kind="entry" if j == 0 else "node", node_id=i)
            for j, i in enumerate(ids)
        ] + [dict(id="t" + str(target), kind="target", target_id=target)]
        return dict(
            version=2,
            routing_version=2,
            vertices=vs,
            edges=[[a["id"], b["id"]] for a, b in zip(vs, vs[1:])],
        )

    def save(self, graph, ident=None, **kw):
        body = dict(name="network", graph=graph, **kw)
        if ident:
            body.setdefault("revision", configurations.get_topology(self.db, ident)["revision"])
        return self.call("api_topology_save", body, ident)["id"]

    def ready(self, *ids, mode="active"):
        with self.db:
            for nid in ids:
                self.db.execute(
                    "UPDATE nodes SET installed_agent=?,desired_mode=?,reported_mode=?,agent=?,first_seen=?,last_seen=?,manual_required=0,state='ok',capabilities=?,connection_state='connected' WHERE id=?",
                    (
                        mode,
                        mode,
                        mode,
                        settings.VERSION,
                        int(time.time()),
                        int(time.time()),
                        json.dumps(
                            ["network_quality", "network_quality_icmp", "network_quality_tcp"]
                        ),
                        nid,
                    ),
                )
                self.db.execute(
                    "UPDATE nodes SET applied_generation=desired_generation,reported_v=? WHERE id=?",
                    (
                        forwarding.rules_version(
                            forwarding.node_rules(self.db, database.get_node(self.db, nid))
                        ),
                        nid,
                    ),
                )

    def ack(self, nid, ok=True):
        n = database.get_node(self.db, nid)
        data = dict(
            t=str(int(time.time())),
            proto="2",
            source="manual",
            phase="begin",
            op=secrets.token_hex(16),
            agent=settings.VERSION,
            mode=n["desired_mode"],
            state="ok",
            au="1",
        )
        with database.atomic(self.db):
            result = fields(node_sync.sync_report(self.db, n, data, "203.0.113.1"))
            data.update(
                {k: result[k] for k in ("op", "generation", "mode", "v")},
                phase="ack",
                state="ok" if ok else "error",
                err="" if ok else "端口冲突：test",
            )
            return fields(
                node_sync.sync_report(self.db, database.get_node(self.db, nid), data, "203.0.113.1")
            )["result"]

    def tick(self, now=None):
        with database.atomic(self.db):
            return execution.tick(self.db, now)

    def settle(self, limit=20):
        for _ in range(limit):
            self.tick()
            for n in self.db.execute("SELECT * FROM nodes").fetchall():
                if n["desired_generation"] != n["applied_generation"]:
                    Fixture.ack(self, n["id"])
            if (
                not self.db.execute(
                    "SELECT 1 FROM configurations WHERE state!='active' OR deleted=1"
                ).fetchone()
                and not self.db.execute("SELECT 1 FROM retirements").fetchone()
            ):
                return
        raise AssertionError(
            [dict(r) for r in self.db.execute("SELECT id,state,error FROM configurations")]
        )

    def seen(self, nid):
        self.ready(nid)


def fields(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class ServerTests(Fixture, unittest.TestCase):
    def request(self, path, body=None, method="POST"):
        with self.db:
            database.set_setting(self.db, "must_change", "0")
            self.db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?)",
                (hashlib.sha256(b"test-session").hexdigest(), int(time.time()) + 3600),
            )
        panel = database.setting(self.db, "panel_path")
        data = json.dumps(body or {}).encode()
        r = api.Request(
            method,
            panel + "/api/" + path,
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(data))),
                ("Cookie", settings.SESSION_COOKIE + "=test-session"),
            ],
            data,
            "127.0.0.1",
            panel_path=panel,
        )
        r.route(method)
        return r.response

    def test_lists_order_and_deleted_apis(self):
        a, b = self.node(), self.node("B")
        for kind in settings.KINDS:
            groups = [r["id"] for r in database.get_groups(self.db, kind)]
            code, data, _ = self.request(
                kind + "/order", dict(groups=groups, ids=[b, a] if kind == "nodes" else [])
            )
            self.assertEqual(code, 200, data)
        self.assertEqual([r["id"] for r in self.db.execute(database.ordered("nodes"))], [b, a])
        for path in ("pools", "chains", "links", "routes", "topology"):
            self.assertEqual(self.request(path, method="GET")[0], 404)
        for path in (
            "nodes",
            "targets",
            "configs",
            "topologies",
            "subscriptions",
            "settings",
            f"nodes/{a}",
        ):
            code, body, _ = self.request(path, method="GET")
            self.assertEqual(code, 200, body)

    def test_optimistic_revision_and_atomic_validation(self):
        a = self.node()
        g = self.graph([a])
        ident = self.save(g)
        before = self.db.execute("SELECT graph FROM topologies").fetchone()[0]
        code, _, _ = self.request(f"topologies/{ident}", dict(name="bad", graph=g, revision=99))
        self.assertEqual(code, 409)
        self.assertEqual(before, self.db.execute("SELECT graph FROM topologies").fetchone()[0])
        g["edges"].append(["missing", g["vertices"][0]["id"]])
        self.assertEqual(self.request("topologies", dict(name="bad", graph=g))[0], 400)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM topologies").fetchone()[0], 1)

    def test_rename_metadata_keeps_rules_and_subscription_name(self):
        a = self.node()
        self.ready(a)
        ident = self.save(self.graph([a]))
        self.settle()
        row = configurations.members(self.db, ident)[0]
        before = forwarding.node_rules(self.db, database.get_node(self.db, a))
        self.call("api_config_rename", dict(name="my name", previous_name=row["name"]), row["id"])
        self.assertEqual(configurations.configs(self.db)["configs"][0]["name"], "my name")
        self.assertEqual(before, forwarding.node_rules(self.db, database.get_node(self.db, a)))
