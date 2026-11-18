import ipaddress
import json
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from test_server import Fixture, strip_account_schema, strip_quota_schema, strip_expiry_schema
import agent_v3
import api
import authentication
import configurations
import database
import failover
import forwarding
import migrations
import quotas
import settings


class AccountTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        authentication.LOGIN_FAILS.clear()
        with self.db:
            database.set_setting(self.db, "must_change", "0")
        self.panel = database.setting(self.db, "panel_path")
        self.admin = self.login("admin", settings.DEFAULT_PASS)

    def request(self, method, route, token=None, body=None):
        data = json.dumps(body or {}).encode()
        headers = [("Content-Type", "application/json"), ("Content-Length", str(len(data)))]
        if token:
            headers.append(("Cookie", token))
        req = api.Request(method, route, headers, data, "127.0.0.1", panel_path=self.panel,
                          failover_engine=failover.Engine(now=0))
        req.route(method)
        status, content, outgoing = req.response
        return status, json.loads(content) if content.startswith(b"{") else content, dict(outgoing)

    def call_api(self, method, route, token, body=None, expect=200):
        status, content, _ = self.request(method, self.panel + "/api/" + route, token, body)
        self.assertEqual(status, expect, (route, content))
        return content

    def login(self, name, password):
        status, _, headers = self.request("POST", self.panel + "/api/login", body=dict(username=name, password=password))
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";")[0]

    def create(self, name, nodes):
        ident = self.call_api("POST", "accounts", self.admin, dict(username=name, password="temporary-password",
                                                                node_ids=nodes))["id"]
        return ident, self.login(name, "temporary-password")

    def account(self, ident, **fields):
        revision = self.db.execute("SELECT revision FROM accounts WHERE id=?", (ident,)).fetchone()[0]
        return self.call_api("PATCH", f"accounts/{ident}", self.admin, dict(revision=revision, **fields))

    def test_admin_can_replace_only_current_session_with_scoped_child_session(self):
        shared, private = self.node("shared"), self.node("private")
        aid, existing = self.create("member", [shared])
        route = f"accounts/{aid}/login"
        revision = dict(revision=1)
        with self.db:
            self.db.execute("UPDATE sessions SET expires=unixepoch()+600 WHERE account_id IS NULL")
        other_admin = self.login("admin", settings.DEFAULT_PASS)
        self.call_api("POST", route, None, revision, expect=401)
        self.call_api("POST", route, existing, revision, expect=403)
        self.call_api("POST", route, self.admin, dict(revision=2), expect=409)
        self.call_api("POST", f"accounts/{aid+100}/login", self.admin, revision, expect=404)
        status, _, headers = self.request("POST", self.panel + "/api/" + route, self.admin, revision)
        self.assertEqual(status, 200)
        switched = headers["Set-Cookie"].split(";")[0]
        self.assertNotIn(switched, (self.admin, existing, other_admin))
        self.call_api("GET", "me", self.admin, expect=401)
        me = self.call_api("GET", "me", switched)
        self.assertEqual((me["admin"], me["username"]), (False, "member"))
        self.assertEqual([n["id"] for n in self.call_api("GET", "nodes", switched)["nodes"]], [shared])
        self.call_api("GET", f"nodes/{private}", switched, expect=404)
        self.call_api("GET", "accounts", switched, expect=403)
        self.call_api("POST", route, switched, revision, expect=403)
        self.call_api("POST", "logout", switched)
        self.call_api("GET", "me", switched, expect=401)
        self.call_api("GET", "me", existing)
        self.assertTrue(self.call_api("GET", "me", other_admin)["admin"])

    def test_login_as_child_rejects_disabled_expired_and_deleting_accounts(self):
        aid, _ = self.create("member", [])
        self.account(aid, enabled=False)
        route = f"accounts/{aid}/login"
        self.call_api("POST", route, self.admin, dict(revision=2), expect=409)
        self.account(aid, enabled=True)
        with self.db:
            self.db.execute("UPDATE accounts SET expires_at=unixepoch() WHERE id=?", (aid,))
        self.call_api("POST", route, self.admin, dict(revision=3), expect=409)
        self.call_api("DELETE", f"accounts/{aid}", self.admin, dict(revision=3))
        self.call_api("POST", route, self.admin, dict(revision=4), expect=409)
        self.assertTrue(self.call_api("GET", "me", self.admin)["admin"])

    def test_delete_disabled_account_requires_admin_and_current_revision(self):
        aid, user = self.create("member", [])
        route = f"accounts/{aid}"
        self.call_api("DELETE", route, None, dict(revision=1), expect=401)
        self.call_api("DELETE", route, user, dict(revision=1), expect=403)
        self.account(aid, enabled=False)
        self.call_api("DELETE", route, self.admin, dict(revision=1), expect=409)
        self.assertFalse(self.call_api("GET", "accounts", self.admin)["accounts"][0]["expiring"])
        self.call_api("DELETE", route, self.admin, dict(revision=2))
        self.tick()
        self.assertEqual(self.call_api("GET", "accounts", self.admin)["accounts"], [])

    def report(self, node, rule, up, down=0, epoch="a" * 32):
        with database.atomic(self.db):
            agent_v3.accept_traffic(self.db, node, [dict(rule_id=rule, protocol="tcp", epoch=epoch,
                                                        up=str(up), down=str(down), up_packets="0", down_packets="0")])

    def test_hidden_grant_redacts_saved_and_preview_graphs_and_forwards_by_address_id(self):
        entry, middle = self.node("entry"), self.node("relay")
        main = database.get_node(self.db, middle)["addr"]
        alternate, local, gateway = "198.51.100.42", "10.10.3.5", "10.10.3.1"
        self.call("api_node_edit", dict(addresses=[
            dict(id="main", host=main, primary=True),
            dict(id="alt", name="私有线路标识", host=alternate, local=local, device="ens9", gateway=gateway),
        ]), middle)
        self.ready(entry, middle)
        aid, member = self.create("member", [entry, middle])
        _, other = self.create("other", [middle])
        self.account(aid, hidden_node_ids=[middle])
        self.assertEqual(self.call_api("GET", "accounts", self.admin)["accounts"][0]["hidden_node_ids"], [middle])
        row = self.call_api("GET", f"nodes/{middle}", member)["node"]
        self.assertTrue(row["addresses_hidden"])
        self.assertEqual((row["addr"], row["addresses"], row["name"]), ("", [], "relay"))
        self.assertEqual(self.call_api("GET", f"nodes/{middle}", other)["node"]["addr"], main)
        self.assertEqual(self.call_api("GET", f"nodes/{middle}", self.admin)["node"]["addr"], main)
        target = self.call_api("POST", "targets", member, dict(name="target", host="1.1.1.1", port=443))["id"]
        forbidden = self.graph([middle], target)
        forbidden["vertices"][0]["address"] = "main"
        for route, body in [("topology-preview", dict(layout_only=True, graph=forbidden)),
                            ("topologies", dict(name="forbidden", graph=forbidden))]:
            error = self.call_api("POST", route, member, body, expect=400)
            self.assertIn("只能用作中转", error["error"])
        graph = self.graph([entry, middle], target)
        graph["bindings"] = {f"v{entry}/v{middle}": {"to": {f"v{middle}": "alt"}}}
        preview = self.call_api("POST", "topology-preview", member, dict(graph=graph, arrange=True))
        self.assertEqual(preview["graph"]["address_book"][f"v{middle}"]["alt"]["name"], "地址 2")
        # Save the response graph from the editor, using its anonymized address book.
        tid = self.call_api("POST", "topologies", member, dict(name="relay link", graph=preview["graph"]))["id"]
        self.settle()
        raw = self.call_api("GET", f"topologies/{tid}", member)
        self.assertEqual(raw["link"]["configurations"][0]["state"], "active")
        self.assertEqual(self.db.execute("SELECT host FROM rules WHERE node_id=?", (entry,)).fetchone()[0], alternate)
        revision = raw["link"]["revision"]
        disaster = self.call_api("GET", f"topologies/{tid}/failover-preview?vertex=v{middle}&revision={revision}", member)
        configs = self.call_api("GET", "configs", member)
        for response in (preview, raw, disaster, configs, self.call_api("GET", "nodes", member)):
            text = json.dumps(response, ensure_ascii=False)
            for secret in (main, alternate, local, gateway, "ens9", "私有线路标识"):
                self.assertNotIn(secret, text)
        self.assertEqual(configs["configs"][0]["connection"]["host"], database.get_node(self.db, entry)["addr"])
        self.account(aid, node_ids=[])
        link = self.call_api("GET", f"topologies/{tid}", member)["link"]
        self.assertEqual(link["state"], "suspended")
        self.assertIsNone(link["configurations"][0]["port"])
        self.assertTrue(all(item["port"] is None for item in link["cleanup"]))
        revision = link["revision"]
        for route in ("topologies", "configs", f"topologies/{tid}/failover-preview?vertex=v{middle}&revision={revision}"):
            data = json.dumps(self.call_api("GET", route, member), ensure_ascii=False)
            for secret in (main, alternate, local, gateway, "ens9", "私有线路标识", database.get_node(self.db, entry)["addr"]):
                self.assertNotIn(secret, data)
        self.assertEqual(self.call_api("GET", f"nodes/{middle}", other)["node"]["addr"], main)

    def test_hide_requires_replacing_and_confirming_previous_entry(self):
        old, new = self.node("old"), self.node("new")
        self.ready(old, new)
        aid, member = self.create("member", [old, new])
        target = self.call_api("POST", "targets", member, dict(name="target", host="1.1.1.1", port=443))["id"]
        tid = self.call_api("POST", "topologies", member, dict(name="entry link", graph=self.graph([old], target)))["id"]
        self.settle()
        revision = self.db.execute("SELECT revision FROM accounts WHERE id=?", (aid,)).fetchone()[0]
        update = dict(revision=revision, hidden_node_ids=[old])
        self.call_api("PATCH", f"accounts/{aid}", self.admin, update, expect=409)
        self.assertFalse(database.hidden_nodes(self.db, aid))
        link = self.call_api("GET", f"topologies/{tid}", member)["link"]
        self.call_api("POST", f"topologies/{tid}", member,
                      dict(name=link["name"], graph=self.graph([new], target), revision=link["revision"]))
        self.call_api("PATCH", f"accounts/{aid}", self.admin, update, expect=409)
        self.settle()
        self.call_api("PATCH", f"accounts/{aid}", self.admin, update)
        self.assertEqual(database.hidden_nodes(self.db, aid), {old})

    def test_migrate_605_grants_keep_address_visibility(self):
        nid = self.node()
        aid, member = self.create("member", [nid])
        self.account(aid, max_configs=4, node_limits=[dict(node_id=nid, limit_gb=8)])
        with self.db:
            strip_expiry_schema(self.db)
            self.db.execute("ALTER TABLE account_nodes DROP COLUMN hide_addresses")
            self.db.execute("PRAGMA user_version=605")
        self.db.close()
        self.db = migrations.init_db()
        node = self.call_api("GET", f"nodes/{nid}", member)["node"]
        self.assertFalse(node["addresses_hidden"])
        self.assertTrue(node["addr"])
        self.assertEqual(node["traffic_quota"]["limit_gb"], 8)
        self.assertEqual(quotas.config_limit(self.db, aid), 4)

    def test_configuration_quota_counts_pending_endpoint_and_imports_atomically(self):
        node = self.node()
        aid, user = self.create("alpha", [node])
        self.account(aid, max_configs=2)
        ext = self.call_api("POST", "external-configs", user,
                            dict(uri="trojan://secret@example.com:443#external"))["id"]
        a, b = [self.call_api("POST", "targets", user, dict(name=host, host=host, port=443))["id"]
                for host in ("1.1.1.1", "8.8.8.8")]
        graph = self.graph([node], a)
        more = self.graph([node], a)
        more["vertices"] += [dict(id=f"t{b}", kind="target", target_id=b)]
        more["edges"] += [[f"v{node}", f"t{b}"]]
        self.call_api("POST", "topologies", user, dict(name="too many", graph=more), expect=409)
        self.assertEqual(self.call_api("GET", "topologies", user)["topologies"], [])
        tid = self.call_api("POST", "topologies", user, dict(name="one", graph=graph))["id"]
        link = self.call_api("GET", f"topologies/{tid}", user)["link"]
        body = dict(name="same", graph=graph, revision=link["revision"])
        self.call_api("POST", f"topologies/{tid}", user, {**body, "graph": more}, expect=409)
        self.assertEqual(self.call_api("GET", f"topologies/{tid}", user)["link"]["revision"], link["revision"])
        self.account(aid, max_configs=1)
        self.call_api("POST", f"topologies/{tid}", user, body)
        self.assertEqual(self.call_api("GET", "configs", user)["quota"], dict(used=2, limit=1))
        self.call_api("DELETE", f"external-configs/{ext}", user)
        self.call_api("POST", "external-configs", user,
                      dict(uri="trojan://secret@example.com:443#another"), expect=409)

    def test_flow_quota_stops_only_matching_configs_and_restores_port(self):
        shared, other = self.node("shared"), self.node("other")
        self.ready(shared, other)
        aid, alice = self.create("alice", [shared, other])
        bid, bob = self.create("bob", [shared])
        def link(user, node, host):
            target = self.call_api("POST", "targets", user, dict(name=host, host=host, port=443))["id"]
            return self.call_api("POST", "topologies", user, dict(name=host, graph=self.graph([node], target)))["id"]
        used = link(alice, shared, "1.1.1.1")
        safe = link(alice, other, "8.8.8.8")
        fellow = link(bob, shared, "8.8.4.4")
        self.settle()
        config, untouched, neighbor = [configurations.members(self.db, tid)[0]["id"] for tid in (used, safe, fellow)]
        old = self.db.execute("SELECT * FROM rules WHERE configuration_id=?", (config,)).fetchone()
        base = 100
        self.report(shared, old["id"], base)
        self.account(aid, node_limits=[dict(node_id=shared, limit_gb=1)])
        amount = base + quotas.GB - 1
        self.report(shared, old["id"], amount)
        self.report(shared, old["id"], amount - 2)
        self.tick()
        self.assertEqual(configurations.members(self.db, used)[0]["state"], "active")
        self.report(shared, old["id"], amount, down=1)
        self.report(shared, old["id"], amount, down=1)
        self.tick()
        raw = self.call_api("GET", f"topologies/{used}", alice)["link"]
        self.assertEqual(raw["state"], "quota_cleaning")
        self.assertIsNone(raw["configurations"][0]["connection"])
        self.assertEqual(raw["configurations"][0]["port"], old["port"])
        self.assertEqual({r[0] for r in self.db.execute("SELECT configuration_id FROM rules")}, {untouched, neighbor})
        # A final sample can arrive after the rule has been retired. It still
        # belongs to Alice, and a node cannot bill an ID issued to another node.
        self.report(shared, old["id"], amount + 12, down=1)
        self.report(other, old["id"], quotas.GB * 2)
        self.assertEqual(quotas.node_view(self.db, aid)[shared]["used"], str(quotas.GB + 12))
        self.assertNotIn(other, quotas.node_view(self.db, aid))
        self.assertEqual(quotas.node_view(self.db, bid), {})
        self.ack(shared)
        self.tick()
        self.assertEqual(self.call_api("GET", f"topologies/{used}", alice)["link"]["state"], "quota")
        self.account(aid, node_limits=[dict(node_id=shared, limit_gb=2)])
        self.settle()
        replacement = self.db.execute("SELECT * FROM rules WHERE configuration_id=?", (config,)).fetchone()
        self.assertEqual(replacement["port"], old["port"])
        self.assertNotEqual(replacement["id"], old["id"])
        self.assertEqual(quotas.node_view(self.db, aid)[shared]["used"], str(quotas.GB + 12))
        self.account(aid, node_limits=[dict(node_id=shared, limit_gb=1)])
        self.tick()
        self.ack(shared)
        self.tick()
        self.account(aid, node_ids=[other])
        admin_view = self.call_api("GET", "accounts", self.admin)["accounts"][0]
        self.assertEqual(admin_view["traffic"][str(shared)]["limit_gb"], 1)
        self.call_api("GET", f"nodes/{shared}", alice, expect=404)
        self.account(aid, node_ids=[shared, other], node_limits=[dict(node_id=shared, limit_gb=1, reset=True)])
        self.tick()
        self.assertEqual(self.call_api("GET", f"topologies/{used}", alice)["link"]["state"], "suspended")
        self.assertEqual(quotas.node_view(self.db, aid)[shared]["used"], "0")

    def test_quota_withdraws_inflight_switch_before_commit(self):
        entry, middle = self.node("entry"), self.node("middle")
        self.ready(entry, middle)
        aid, user = self.create("alpha", [entry, middle])
        target = self.call_api("POST", "targets", user, dict(name="target", host="1.1.1.1", port=443))["id"]
        tid = self.call_api("POST", "topologies", user, dict(name="one", graph=self.graph([entry, middle], target)))["id"]
        self.tick()
        stage = self.db.execute("SELECT * FROM rules").fetchone()
        self.assertEqual((stage["phase"], stage["node_id"]), ("stage", middle))
        self.account(aid, node_limits=[dict(node_id=middle, limit_gb=1)])
        self.ack(middle)
        self.report(middle, stage["id"], quotas.GB, epoch="b" * 32)
        self.tick()
        self.assertFalse(self.db.execute("SELECT 1 FROM jobs").fetchone())
        self.assertFalse(self.db.execute("SELECT 1 FROM rules").fetchone())
        self.assertEqual(configurations.members(self.db, tid)[0]["state"], "quota")
        self.assertIn(stage["port"], forwarding.reserved(self.db, middle))
        self.ack(middle)
        self.tick()
        self.account(aid, node_limits=[dict(node_id=middle, limit_gb=1, reset=True)])
        self.settle()

    def check_switch_quota(self, phase):
        entry, original, candidate = (self.node(name) for name in ("entry", "original", "candidate"))
        self.ready(entry, original, candidate)
        aid, user = self.create("member", [entry, original, candidate])
        target = self.call_api("POST", "targets", user, dict(name="target", host="1.1.1.1", port=443))["id"]
        tid = self.call_api("POST", "topologies", user, dict(name="link", graph=self.graph([entry, original], target)))["id"]
        self.settle()
        link = self.call_api("GET", f"topologies/{tid}", user)["link"]
        config = link["configurations"][0]
        def live():
            return [tuple(r) for r in self.db.execute(
                "SELECT id,node_id,port,host,tport FROM rules WHERE configuration_id=? AND phase='live' ORDER BY id",
                (config["id"],))]
        old_rules = live()
        self.call_api("POST", f"topologies/{tid}", user, dict(
            name="link", revision=link["revision"], graph=self.graph([entry, candidate], target)))
        self.tick()
        if phase == "commit":
            self.ack(candidate)
            self.tick()
            self.ack(entry)
        self.assertEqual(self.db.execute("SELECT phase FROM jobs").fetchone()[0], phase)
        self.account(aid, node_limits=[dict(node_id=candidate, limit_gb=0)])
        self.tick()
        self.assertEqual(live(), old_rules)
        if phase == "commit":
            self.assertEqual(self.db.execute("SELECT phase FROM jobs").fetchone()[0], "rollback")
            self.tick()
            self.ack(entry)
            self.tick()
        self.tick()
        current = self.call_api("GET", f"topologies/{tid}", user)["link"]["configurations"][0]
        self.assertEqual((current["state"], current["connection"]), ("quota_waiting", config["connection"]))
        self.assertEqual(live(), old_rules)
        self.assertFalse(self.db.execute("SELECT 1 FROM rules WHERE node_id=?", (candidate,)).fetchone())
        self.ack(candidate)
        self.account(aid, node_limits=[dict(node_id=candidate, limit_gb=1)])
        self.settle()
        self.assertEqual({r[1] for r in live()}, {entry, candidate})

    def test_new_path_quota_preserves_live_path_during_prepare(self):
        self.check_switch_quota("prepare")

    def test_new_path_quota_restores_live_ingress_during_commit(self):
        self.check_switch_quota("commit")

    def test_expiry_denies_access_then_waits_for_cleanup_across_restart(self):
        self.check_cleanup_across_restart(manual=False)

    def test_manual_deletion_revokes_access_then_waits_for_cleanup_across_restart(self):
        self.check_cleanup_across_restart(manual=True)

    def check_cleanup_across_restart(self, manual):
        shared, passive = self.node("shared"), self.node("passive")
        self.ready(shared)
        self.ready(passive, mode="passive")
        admin_link = self.save(self.graph([shared]))
        self.settle()
        aid, user = self.create("member", [shared, passive])
        target = self.call_api("POST", "targets", user, dict(name="target", host="8.8.8.8", port=443))["id"]
        tid = self.call_api("POST", "topologies", user, dict(name="relay", graph=self.graph([shared, passive], target)))["id"]
        self.settle()
        ext = self.call_api("POST", "external-configs", user, dict(uri="trojan://secret@example.com:443#external"))["id"]
        self.call_api("POST", "subscriptions", user, dict(name="subscription", external_configuration_ids=[ext]))
        url = self.call_api("GET", "subscriptions", user)["subscriptions"][0]["path"]
        admin_config = configurations.members(self.db, admin_link)[0]["id"]
        def admin_rules():
            return list(self.db.execute("SELECT * FROM rules WHERE configuration_id=?", (admin_config,)))
        rules = admin_rules()
        self.assertTrue(rules)
        self.assertEqual(self.request("GET", url)[0], 200)
        if manual:
            self.call_api("DELETE", f"accounts/{aid}", self.admin, dict(revision=1))
            self.call_api("DELETE", f"accounts/{aid}", self.admin, dict(revision=1))
        else:
            self.account(aid, expires_at=int(time.time()) + 3600)
            with self.db:
                self.db.execute("UPDATE accounts SET expires_at=unixepoch() WHERE id=?", (aid,))
        self.call_api("GET", "me", user, expect=401)
        self.call_api("POST", "login", None, dict(username="member", password="temporary-password"), expect=401)
        self.assertEqual(self.request("GET", url)[0], 404)
        self.call_api("PATCH", f"accounts/{aid}", self.admin, dict(revision=2, expires_at=None), expect=409)
        if not manual:
            self.assertTrue(self.tick())
        self.assertEqual(database.allowed_nodes(self.db, aid), set())
        self.assertEqual(admin_rules(), rules)
        self.ack(shared)
        self.tick()
        row = self.call_api("GET", "accounts", self.admin)["accounts"][0]
        self.assertEqual((row["expiring"], row["enabled"], row["cleanup_nodes"]), (True, False, 1))
        self.db.close()
        self.db = migrations.init_db()
        self.tick()
        self.assertTrue(self.db.execute("SELECT 1 FROM topologies WHERE id=?", (tid,)).fetchone())
        self.ack(passive)
        self.tick()
        for table, key, value in (("accounts","id",aid), ("item_groups","account_id",aid), ("sessions","account_id",aid),
                                  ("subscriptions","account_id",aid), ("traffic_rule_owners","account_id",aid),
                                  ("external_configurations","id",ext), ("topologies","id",tid), ("targets","id",target)):
            self.assertFalse(self.db.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (value,)).fetchone(), table)
        self.assertEqual(self.request("GET", url)[0], 404)
        self.assertEqual(admin_rules(), rules)
        self.assertEqual({r[0] for r in self.db.execute("SELECT id FROM nodes")}, {shared, passive})
        self.assertFalse(self.db.execute("PRAGMA foreign_key_check").fetchall())

    def test_expiry_optional_extend_clear_and_upgrade(self):
        nid = self.node()
        aid, member = self.create("member", [nid])
        self.account(aid, hidden_node_ids=[nid])
        with self.db:
            strip_expiry_schema(self.db)
            self.db.execute("PRAGMA user_version=606")
        self.db.close()
        self.db = migrations.init_db()
        def current():
            return self.call_api("GET", "accounts", self.admin)["accounts"][0]
        self.assertIsNone(current()["expires_at"])
        self.assertEqual(database.hidden_nodes(self.db, aid), {nid})
        now = int(time.time())
        for value in (True, str(now+100), now-1):
            self.call_api("PATCH", f"accounts/{aid}", self.admin,
                          dict(revision=current()["revision"], expires_at=value), expect=400)
        self.account(aid, expires_at=now+100)
        self.account(aid, expires_at=now+200)
        self.tick(now+101)
        self.assertEqual(current()["expires_at"], now+200)
        self.account(aid, expires_at=None)
        self.tick(now+201)
        self.assertEqual(self.call_api("GET", "me", member)["username"], "member")
        self.account(aid, enabled=False, expires_at=now+300)
        self.tick(now+300)
        self.assertEqual(self.call_api("GET", "accounts", self.admin)["accounts"], [])

    def test_migrate_604_preserves_sessions_and_counts_only_new_deltas(self):
        node = self.node()
        self.ready(node)
        aid, user = self.create("alpha", [node])
        target = self.call_api("POST", "targets", user, dict(name="target", host="1.1.1.1", port=443))["id"]
        self.call_api("POST", "topologies", user, dict(name="one", graph=self.graph([node], target)))
        self.settle()
        rule = self.db.execute("SELECT id FROM rules").fetchone()[0]
        self.report(node, rule, 1000)
        with self.db:
            strip_quota_schema(self.db)
            self.db.execute("PRAGMA user_version=604")
        self.db.close()
        self.db = migrations.init_db()
        self.assertEqual(self.call_api("GET", "configs", user)["quota"], dict(used=1, limit=None))
        self.assertEqual(quotas.node_view(self.db, aid), {})
        self.report(node, rule, 1050)
        self.report(node, rule, 1020)
        self.assertEqual(quotas.node_view(self.db, aid)[node]["used"], "50")
        self.assertEqual(database.get_node(self.db, node)["applied_generation"],
                         database.get_node(self.db, node)["desired_generation"])

    def test_scoped_resources_and_submission(self):
        shared, private = self.node("shared"), self.node("admin only")
        admin_target = self.target()
        admin_topology = self.save(self.graph([shared], admin_target))
        admin_config = configurations.members(self.db, admin_topology)[0]["id"]
        admin_external = self.call("api_external_save", dict(uri="trojan://secret@example.com:443#test"))["id"]
        aid, alice = self.create("alice", [shared])
        _, bob = self.create("bob", [shared])
        self.assertEqual(self.call_api("GET", "me", alice)["admin"], False)
        row = self.call_api("GET", f"nodes/{shared}", alice)["node"]
        self.assertNotIn("key", row)
        self.assertNotIn("rules", row)
        self.assertEqual([n["id"] for n in self.call_api("GET", "nodes", alice)["nodes"]], [shared])
        for method, path, status in [
            ("GET", "settings", 403), ("GET", f"nodes/{private}", 404), ("GET", "accounts", 403),
            ("PATCH", f"nodes/{shared}", 403), ("POST", f"nodes/{shared}/rekey", 403),
            ("PATCH", f"targets/{admin_target}", 404), ("GET", f"topologies/{admin_topology}", 404),
            ("POST", f"topologies/{admin_topology}/action", 404),
            ("GET", f"topologies/{admin_topology}/failover-preview", 404),
            ("PATCH", f"configs/{admin_config}/name", 404),
            ("DELETE", f"external-configs/{admin_external}", 404),
        ]:
            self.call_api(method, path, alice, expect=status)
        target = self.call_api("POST", "targets", alice, dict(name="alice target", host="8.8.4.4", port=443))["id"]
        self.assertEqual(self.call_api("GET", "targets", bob)["targets"], [])
        self.call_api("POST", "topology-preview", alice, dict(graph=self.graph([shared], admin_target)), expect=404)
        self.call_api("POST", "topology-preview", alice, dict(graph=self.graph([private], target)), expect=400)
        self.call_api("POST", "topology-preview", alice, dict(id=admin_topology, graph=self.graph([shared], target)), expect=404)
        admin_group = database.default_group(self.db, "targets")
        self.call_api("PATCH", f"groups/{admin_group}", alice, dict(name="take"), expect=404)
        self.call_api("PATCH", f"targets/{target}", alice, dict(group_id=admin_group), expect=400)
        for selection in [dict(configuration_ids=[admin_config]), dict(external_configuration_ids=[admin_external])]:
            self.call_api("POST", "subscriptions", alice, dict(name="wrong", **selection), expect=400)
        link = self.call_api("POST", "topologies", alice, dict(name="own", graph=self.graph([shared], target)))["id"]
        self.assertEqual([r["topology_id"] for r in self.call_api("GET", "configs", alice)["configs"]], [link])
        self.assertEqual([r["topology_id"] for r in self.call_api("GET", "configs", self.admin)["configs"]
                          if r["source"] == "topology"], [admin_topology])
        desired = [json.loads(configurations.members(self.db, tid)[0]["desired"])["port"] for tid in (link, admin_topology)]
        self.assertEqual(len(set(desired)), 2)
        graph = self.graph([shared], target)
        graph["ports"] = {f"v{shared}/t{target}": desired[1]}
        self.call_api("POST", "topologies", alice, dict(name="conflict", graph=graph), expect=400)
        prior = self.login("alice", "temporary-password")
        self.account(aid, password="new-temporary-password")
        for token in (alice, prior):
            self.call_api("GET", "me", token, expect=401)
        self.assertTrue(self.call_api("GET", "me", self.admin)["admin"])

    def test_revocation_cancels_stage_and_preserves_other_account_rules(self):
        first, last = self.node("passive entry"), self.node("passive next")
        self.ready(first, last, mode="passive")
        own = self.save(self.graph([first]))
        self.settle()
        own_id = configurations.members(self.db, own)[0]["id"]
        aid, token = self.create("team", [first, last])
        target = self.call_api("POST", "targets", token, dict(name="dns", host="8.8.8.8", port=443))["id"]
        tid = self.call_api("POST", "topologies", token, dict(name="team link", graph=self.graph([first, last], target)))["id"]
        self.tick()
        conf = configurations.members(self.db, tid)[0]["id"]
        rule = self.db.execute("SELECT * FROM rules WHERE configuration_id=?", (conf,)).fetchone()
        self.assertEqual((rule["phase"], rule["node_id"]), ("stage", last))
        self.ack(last)
        self.tick()
        self.assertEqual(self.db.execute("SELECT phase FROM jobs WHERE configuration_id=?", (conf,)).fetchone()[0], "commit")
        saved_ports = [tuple(r) for r in self.db.execute("SELECT node_id,port FROM rules WHERE configuration_id=?", (conf,))]
        self.account(aid, node_ids=[first])
        self.tick()
        self.assertFalse(self.db.execute("SELECT * FROM jobs").fetchall())
        self.assertEqual([r[0] for r in self.db.execute("SELECT configuration_id FROM rules")], [own_id])
        self.assertEqual({tuple(r) for r in self.db.execute("SELECT * FROM retired_ports")}, set(saved_ports))
        link = self.call_api("GET", f"topologies/{tid}", token)["link"]
        self.assertEqual(link["state"], "suspended")
        self.assertTrue(link["cleanup"])
        self.assertFalse(link["configurations"][0]["connection"])
        self.call_api("POST", f"topologies/{tid}/action", token, dict(action="retry", revision=link["revision"]), expect=409)
        self.account(aid, node_ids=[first, last])
        request = dict(name=link["name"], revision=link["revision"], graph=link["graph"], resume=True)
        self.call_api("POST", f"topologies/{tid}", token, request, expect=409)
        for node in (first, last):
            self.ack(node)
        self.tick()
        for node, port in saved_ports:
            self.assertNotIn(port, forwarding.reserved(self.db, node))
        self.call_api("POST", f"topologies/{tid}", token, request)
        self.settle()
        self.assertEqual(configurations.members(self.db, tid)[0]["id"], conf)

    def test_public_subscription_link_revokes_on_disable(self):
        aid, token = self.create("team", [])
        ext = self.call_api("POST", "external-configs", token, dict(uri="trojan://secret@example.com:443#team"))["id"]
        self.call_api("POST", "subscriptions", token, dict(name="team", external_configuration_ids=[ext]))
        sub = self.call_api("GET", "subscriptions", token)["subscriptions"][0]
        self.assertEqual(self.request("GET", sub["path"])[0], 200)
        self.account(aid, enabled=False)
        self.call_api("GET", "configs", token, expect=401)
        self.assertEqual(self.request("GET", sub["path"])[0], 404)
        self.account(aid, enabled=True)
        self.assertEqual(self.request("GET", sub["path"])[0], 404)

    def test_revocation_keeps_rollback_port_until_agent_acknowledges(self):
        nid = self.node("passive")
        self.ready(nid, mode="passive")
        aid, token = self.create("team", [nid])
        target = self.call_api("POST", "targets", token, dict(name="target", host="1.1.1.1", port=443))["id"]
        tid = self.call_api("POST", "topologies", token, dict(name="link", graph=self.graph([nid], target)))["id"]
        self.tick()
        self.tick()
        job = self.db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(job["phase"], "commit")
        port = json.loads(job["payload"])["new"][0]["port"]
        # During a rollback the staged ingress can still be active on the old generation.
        with self.db:
            self.db.execute("UPDATE jobs SET phase='rollback'")
            self.db.execute("DELETE FROM rules")
        self.account(aid, node_ids=[])
        self.assertIn(port, forwarding.reserved(self.db, nid))
        self.assertTrue(self.call_api("GET", f"topologies/{tid}", token)["link"]["cleanup"])
        self.ack(nid)
        self.tick()
        self.assertNotIn(port, forwarding.reserved(self.db, nid))

    def test_target_pin_used_by_active_rule_and_private_answers_rejected(self):
        nid = self.node()
        self.ready(nid)
        _, token = self.create("team", [nid])
        with self.db:
            database.set_setting(self.db, "public_url", "https://panel.example.com")
        good, private, panel = map(ipaddress.IPv4Address, ("8.8.8.8", "127.0.0.1", "1.1.1.1"))

        def resolve(hosts):
            lookup = {"safe.example.com": {good}, "mixed.example.com": {good, private},
                      "panel.example.com": {panel}, "alias.example.com": {panel}}
            return {h: lookup[h] if h in lookup else {ipaddress.IPv4Address(h)} for h in hosts}

        with patch("target_addresses.resolve_hosts", side_effect=resolve):
            for host in ("127.0.0.1", "mixed.example.com", "alias.example.com"):
                self.call_api("POST", "targets", token, dict(name="bad", host=host, port=443), expect=400)
            target = self.call_api("POST", "targets", token,
                                   dict(name="safe", host="safe.example.com", port=443))["id"]
        graph = self.graph([nid], target)
        graph["address_book"] = {f"t{target}": {"main": {"forward_ip": "127.0.0.1"}}}
        topology = self.call_api("POST", "topologies", token, dict(name="chain", graph=graph))["id"]
        self.settle()
        self.assertEqual(self.db.execute("SELECT host FROM rules").fetchone()[0], str(good))
        config = self.call_api("GET", f"topologies/{topology}", token)["link"]["configurations"][0]
        self.assertEqual(config["target_address"]["host"], "safe.example.com")

    def test_migrate_603_keeps_live_routes_and_existing_subscription(self):
        nid = self.node()
        self.ready(nid)
        tid = self.save(self.graph([nid]))
        self.settle()
        sub = self.call("api_subscription_save", dict(name="existing"))["id"]
        before = [tuple(r) for r in self.db.execute("SELECT * FROM rules")]
        node = dict(database.get_node(self.db, nid))
        with self.db:
            strip_account_schema(self.db)
            self.db.execute("PRAGMA user_version=603")
        self.db.close()
        self.db = migrations.init_db()
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], settings.DATABASE_VERSION)
        self.assertTrue(Path(settings.DB_PATH + ".pre-0.6.4.bak").is_file())
        self.assertEqual([tuple(r) for r in self.db.execute("SELECT * FROM rules")], before)
        self.assertEqual(dict(database.get_node(self.db, nid)), node)
        self.assertEqual(self.call("api_topologies")["topologies"][0]["id"], tid)
        self.assertEqual(self.call("api_subscriptions")["subscriptions"][0]["id"], sub)
        self.call_api("GET", "me", self.admin, expect=401)
