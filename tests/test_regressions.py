"""Regression checks for defects fixed in 0.6.3."""
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from test_server import Fixture
from test_failover import FailoverTests
from test_imports import ss_uri
import agent_v3
import configurations
import database
import errors
import failover
import http_harness
import proxy_formats
import settings
import subscriptions
import transport


class LostNodeTests(Fixture, unittest.TestCase):
    def settle_without(self, lost):
        for _ in range(20):
            self.tick()
            for n in self.db.execute("SELECT * FROM nodes").fetchall():
                if n["id"] == lost:
                    continue
                if n["desired_generation"] != n["applied_generation"]:
                    Fixture.ack(self, n["id"])
                self.db.execute("UPDATE nodes SET last_seen=? WHERE id=?", (int(time.time()), n["id"]))
            self.db.commit()

    def delete(self, nid, force):
        self.h.query = {"force": ["1"]} if force else {}
        try:
            return self.call("api_node_del", {}, nid)
        finally:
            self.h.query = {}

    def test_rerouted_lost_node_can_be_force_deleted(self):
        a, b, c = self.node(), self.node(), self.node()
        self.ready(a, b, c)
        target = self.target()
        ident = self.save(self.graph([a, b], target))
        self.settle()
        self.save(self.graph([a, c], target), ident)
        self.settle_without(b)
        self.assertEqual(configurations.members(self.db, ident)[0]["state"], "active")
        with self.assertRaises(errors.UserError) as caught:
            self.delete(b, False)
        self.assertEqual((caught.exception.status, caught.exception.field), (409, "force"))
        self.assertEqual(self.delete(b, True), {"deleted": True})
        self.assertFalse(self.db.execute("SELECT 1 FROM retirements").fetchone())
        self.tick()
        self.assertEqual(configurations.members(self.db, ident)[0]["state"], "active")

    def test_lost_node_does_not_keep_a_deleted_topology_forever(self):
        a, b = self.node(), self.node()
        self.ready(a, b)
        ident = self.save(self.graph([a, b]))
        self.settle()
        self.call("api_topology_delete", {}, ident)
        self.settle_without(b)
        self.assertTrue(self.db.execute("SELECT 1 FROM topologies").fetchone())
        with self.assertRaises(errors.UserError) as caught:
            self.delete(b, False)
        self.assertEqual(caught.exception.field, "force")
        self.assertEqual(self.delete(b, True), {"deleted": True})
        self.tick()
        self.assertFalse(self.db.execute("SELECT 1 FROM topologies").fetchone())
        self.assertFalse(self.db.execute("SELECT 1 FROM configurations").fetchone())

    def test_force_never_removes_a_node_that_a_link_still_uses(self):
        a, b = self.node(), self.node()
        self.ready(a, b)
        self.save(self.graph([a, b]))
        self.settle()
        with self.assertRaises(errors.UserError) as caught:
            self.delete(b, True)
        self.assertEqual((caught.exception.status, caught.exception.field), (409, None))
        self.assertTrue(self.db.execute("SELECT 1 FROM rules WHERE node_id=?", (b,)).fetchone())


class RestartFailoverTests(FailoverTests):
    def test_server_downtime_is_not_counted_as_node_downtime(self):
        self.network()
        # Every agent was healthy until the server stopped 45 seconds ago.
        self.db.execute("UPDATE nodes SET last_seen=?", (self.now - 45,))
        self.db.commit()
        self.engine = failover.Engine(self.now)
        self.dead = {self.f}
        original = self.desired()
        for _ in range(44):
            self.step(1)
        self.dead = set()  # The slow node reconnects within the configured wait.
        for _ in range(60):
            self.step(1)
        self.assertEqual(self.desired(), original)

    def test_node_still_missing_after_restart_is_replaced_after_the_wait(self):
        self.network()
        self.db.execute("UPDATE nodes SET last_seen=?", (self.now - 45,))
        self.db.commit()
        self.engine = failover.Engine(self.now)
        original = self.desired()
        for _ in range(55):
            self.step(1)
        self.assertEqual(self.desired(), original)
        for _ in range(15):
            self.step(1)
        self.assertNotEqual(self.desired(), original)


for _name in [n for n in vars(FailoverTests) if n.startswith("test_")]:
    setattr(RestartFailoverTests, _name, None)  # Run only the restart cases here.


class ExportTests(Fixture, unittest.TestCase):
    def export(self, name):
        n = self.node()
        self.ready(n)
        t = self.call("api_target_save", dict(uri=ss_uri()))["id"]
        ident = self.save(self.graph([n], t))
        self.settle()
        c = configurations.members(self.db, ident)[0]
        self.call("api_config_rename", dict(name=name, previous_name=c["name"]), c["id"])
        s = self.call("api_subscription_save", dict(name="client", configuration_ids=[c["id"]]))["id"]
        return subscriptions.export(self.db, subscriptions.get(self.db, s)["token"], "clash")[0]

    def test_clash_keeps_emoji_readable_for_yaml_parsers(self):
        body = self.export("\U0001F1ED\U0001F1F0 香港 01")
        self.assertIn("\U0001F1ED\U0001F1F0 香港 01", body)
        self.assertNotIn("\\ud83c", body)

    def test_clash_renames_every_builtin_policy_name(self):
        for name in ("PASS", "COMPATIBLE", "REJECT-DROP"):
            with self.subTest(name=name):
                self.setUp()
                self.assertNotIn(f'"name":"{name}"', self.export(name))
                self.tearDown()

    def test_hysteria2_port_hopping_uses_mihomo_ranges(self):
        node = proxy_formats.parse("hy2://p@example.com:443?mport=5000-6000,7000#hop")
        self.assertEqual(proxy_formats.mihomo(node, "hop")["ports"], "5000-6000,7000")

    def test_malformed_links_are_user_errors(self):
        for uri in ("ss:abc", "vmess:abc", "socks:abc"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                proxy_formats.parse(uri)

    def test_sip002_standard_base64_userinfo_with_slash(self):
        node = proxy_formats.parse("ss://YWVzLTI1Ni1nY206YWI/@192.0.2.4:8388#slash")
        self.assertEqual((node["host"], node["port"]), ("192.0.2.4", 8388))

    def test_oversized_and_zero_identifiers_are_rejected(self):
        with self.assertRaises(errors.UserError):
            self.call("api_node_add", dict(name="n", addr="192.0.2.9", kind="forward", ports="10000-10010", group_id=10**30))
        with self.assertRaises(errors.UserError) as caught:
            self.call("api_tag_save", dict(name="zero"), 0)
        self.assertEqual(caught.exception.status, 404)
        self.assertFalse(self.db.execute("SELECT 1 FROM tags").fetchone())


class HandoverTests(Fixture, unittest.TestCase):
    def test_failed_handover_keeps_the_installed_version(self):
        nid = self.node()
        self.ready(nid, mode="passive")
        with self.db:
            self.db.execute("UPDATE nodes SET agent='0.5.9' WHERE id=?", (nid,))
        stamp = lambda: str(int(time.time()))
        with patch.object(agent_v3, "artifact", return_value=(Path("/dev/null"), "0" * 64)):
            with database.atomic(self.db):
                out = agent_v3.fields(agent_v3.manage(self.db, database.get_node(self.db, nid), dict(
                    t=stamp(), manage="prepare", target="passive", arch="amd64", agent="")))
            with database.atomic(self.db):
                agent_v3.manage(self.db, database.get_node(self.db, nid), dict(
                    t=stamp(), manage="failed", op=out["op"], target="passive", agent=out["version"], error="failed"))
        self.assertEqual(database.get_node(self.db, nid)["agent"], "0.5.9")


class FirstStartTests(unittest.TestCase):
    def test_interrupted_first_start_can_be_repeated(self):
        import migrations, security, tempfile
        with tempfile.TemporaryDirectory() as root, patch.object(settings, "DB_PATH", root + "/ictl.db"):
            with patch.object(security, "reset_admin", side_effect=RuntimeError("interrupted")):
                with self.assertRaises(RuntimeError):
                    migrations.init_db()
            conn = migrations.init_db()
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], settings.DATABASE_VERSION)
            self.assertTrue(database.setting(conn, "panel_path"))
            conn.close()


class UpdateNoticeTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        http_harness.Handler.sync_path = database.setting(self.db, "sync_path")
        http_harness.Handler.panel_path = database.setting(self.db, "panel_path")
        self.client = TestClient(TestServer(transport.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_agent_that_cannot_update_is_still_served(self):
        nid = self.node()
        self.ready(nid)
        with self.db:
            self.db.execute("UPDATE nodes SET auto_update=1 WHERE id=?", (nid,))
        key = database.get_node(self.db, nid)["key"]
        ws = await self.client.ws_connect(
            http_harness.Handler.sync_path + "/v3/ws", headers={"Authorization": "Bearer " + key})
        await ws.send_json(dict(type="hello", protocol=3, version="0.6.0", generation=0, hash="",
                                installation_id="a" * 32, auto_update=True))
        session = (await ws.receive_json(timeout=3))["session"]
        self.assertEqual((await ws.receive_json(timeout=3))["type"], "update")
        # The updater did not take over: the agent gets its configuration again.
        with patch.object(transport, "UPDATE_GRACE", 0):
            await ws.send_json(dict(type="heartbeat", session=session))
            self.assertIn((await ws.receive_json(timeout=3))["type"], ("config", "ready"))
        await ws.close()

    async def test_rejected_requests_do_not_wake_the_scheduler(self):
        hub = self.client.server.app["hub"]
        with patch.object(hub, "notify") as notify:
            for _ in range(5):
                response = await self.client.post("/wp-login.php", data=b"x")
                self.assertGreaterEqual(response.status, 400)
            notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
