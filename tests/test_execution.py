import copy
import json
import time
import unittest
from test_server import Fixture
import configurations, database, errors, execution, forwarding, node_addresses


class ExecutionTests(Fixture, unittest.TestCase):
    def test_active_prepare_timeout_keeps_original_rules_and_reserves_ports(self):
        a, b, c = [self.node(x) for x in 'ABC']
        self.ready(a, b, c)
        target = self.target()
        ident = self.save(self.graph([a, b], target))
        self.settle()
        before = configurations.members(self.db, ident)[0]
        self.save(self.graph([a, c], target), ident)
        now = int(time.time())
        self.tick(now)
        job = json.loads(self.db.execute('SELECT payload FROM jobs').fetchone()[0])
        self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (now-61, c))
        self.db.commit()
        self.tick(now+61)
        after = configurations.members(self.db, ident)[0]
        self.assertEqual(after['applied'], before['applied'])
        self.assertEqual(after['state'], 'error')
        self.assertTrue(execution.applied_rules_present(self.db, after))
        self.assertFalse(self.db.execute('SELECT 1 FROM jobs').fetchone())
        self.assertTrue(self.db.execute('SELECT 1 FROM retired_ports WHERE node_id=? AND port=?', (c, job['new'][1]['port'])).fetchone())

    def test_commit_timeout_waits_for_rollback_receipt_before_retiring_downstream(self):
        a, b, c = [self.node(x) for x in 'ABC']
        self.ready(a, b, c)
        target = self.target()
        ident = self.save(self.graph([a, b], target))
        self.settle()
        before = configurations.members(self.db, ident)[0]
        self.save(self.graph([a, c], target), ident)
        now = int(time.time())
        self.tick(now)
        self.ack(c)
        self.tick(now+1)
        self.assertEqual(self.db.execute('SELECT phase FROM jobs').fetchone()[0], 'commit')
        self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (now-61, a))
        self.db.commit()
        self.tick(now+62)
        self.assertEqual(self.db.execute('SELECT phase FROM jobs').fetchone()[0], 'rollback')
        for nid in (b, c):
            self.assertTrue(self.db.execute('SELECT 1 FROM rules WHERE node_id=?', (nid,)).fetchone())
        self.assertEqual(configurations.members(self.db, ident)[0]['applied'], before['applied'])
        self.ack(a)
        self.tick(now+63)
        self.assertFalse(self.db.execute('SELECT 1 FROM jobs').fetchone())
        self.assertTrue(execution.applied_rules_present(self.db, configurations.members(self.db, ident)[0]))
        self.assertFalse(self.db.execute('SELECT 1 FROM rules WHERE node_id=?', (c,)).fetchone())

    def test_passive_preparation_keeps_waiting_for_manual_sync(self):
        a, b = self.node(), self.node()
        self.ready(a)
        self.ready(b, mode='passive')
        ident = self.save(self.graph([a,b]))
        now = int(time.time())
        self.tick(now)
        self.tick(now+3600)
        self.assertEqual(self.db.execute('SELECT phase FROM jobs').fetchone()[0], 'prepare')
        self.assertEqual(configurations.members(self.db,ident)[0]['state'], 'waiting')

    def test_view_keeps_confirmed_ip_and_replaced_node_until_ack(self):
        a, b, c = [self.node(x) for x in "ABC"]
        values = node_addresses.listing(self.db, database.get_node(self.db, b))
        values.append(dict(id="alt", host="10.0.0.2", primary=False))
        self.call("api_node_edit", dict(addresses=values), b)
        self.ready(a, b, c)
        graph = self.graph([a, b])
        edge = f"v{a}/v{b}"
        graph["bindings"] = {edge: {"to": {f"v{b}": "main"}}}
        ident = self.save(graph)
        self.settle()
        graph = json.loads(configurations.get_topology(self.db, ident)["graph"])
        graph["bindings"][edge]["to"][f"v{b}"] = "alt"
        self.save(graph, ident)
        detail = configurations.detail(self.db, ident)
        view = detail["configurations"][0]
        self.assertEqual(view["active_graph"]["bindings"][edge]["to"][f"v{b}"], "main")
        self.assertEqual(view["pending_graph"]["bindings"][edge]["to"][f"v{b}"], "alt")
        graph["bindings"][edge]["to"][f"v{b}"] = "main"
        graph["vertices"][1]["node_id"] = c
        self.save(graph, ident)
        detail = configurations.detail(self.db, ident)
        view = detail["configurations"][0]
        nodes = {v["id"]: v for v in detail["view_graph"]["vertices"]}
        self.assertEqual(nodes[view["active_path"][1]]["node_id"], b)
        self.assertEqual(nodes[view["pending_path"][1]]["node_id"], c)
        self.assertNotEqual(view["active_path"][1], view["pending_path"][1])
        self.settle()
        self.assertFalse(
            any(
                v.get("runtime_only")
                for v in configurations.detail(self.db, ident)["view_graph"]["vertices"]
            )
        )

    def test_restore_removed_path_rebuilds_rules_before_exporting(self):
        a, b = self.node(), self.node()
        self.ready(a, b)
        ident = self.save(self.graph([a, b]))
        self.settle()
        graph = json.loads(configurations.get_topology(self.db, ident)["graph"])
        removed = dict(graph, edges=[])
        self.save(removed, ident)
        self.tick()
        self.assertFalse(self.db.execute("SELECT 1 FROM rules").fetchone())
        self.save(graph, ident)
        self.assertIsNone(configurations.configs(self.db)["configs"][0]["connection"])
        self.tick()
        self.assertNotEqual(configurations.members(self.db, ident)[0]["state"], "active")
        self.ack(a)
        self.ack(b)
        self.settle()
        config = configurations.members(self.db, ident)[0]
        self.assertTrue(execution.applied_rules_present(self.db, config))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 2)

    def test_restoring_cannot_take_another_configs_reassigned_port(self):
        a, b, c, d = [self.node(x) for x in "ABCD"]
        self.ready(a, b, c, d)
        ident = self.save(self.graph([a, b]))
        self.settle()
        graph = json.loads(configurations.get_topology(self.db, ident)["graph"])
        port = next(iter(graph["ports"].values()))
        self.save(dict(graph, edges=[]), ident)
        self.tick()
        self.ack(a)  # B has not confirmed cleanup, so the old config still exists.
        other = self.graph([a])
        other["ports"] = {f"v{a}/{other['vertices'][-1]['id']}": port}
        second = self.save(other)
        for _ in range(4):
            self.tick()
            self.ack(a)
        self.assertEqual(configurations.members(self.db, second)[0]["state"], "active")
        graph["vertices"][1]["node_id"] = c
        with self.assertRaises(errors.UserError):
            self.save(graph, ident)
        # A corrected port and an unrelated topology can both finish normally.
        graph["ports"] = {key: 10100 if port != 10100 else 10000 for key in graph["ports"]}
        self.save(graph, ident)
        self.save(self.graph([d]))
        self.settle()
        self.assertEqual(len(configurations.configs(self.db)["configs"]), 3)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM rules WHERE node_id=?", (a,)).fetchone()[0], 2
        )

    def branch(self):
        a, b, c = [self.node(x) for x in "ABC"]
        self.ready(a, b, c)
        g = self.graph([a, b])
        t = g["vertices"][-1]["id"]
        g["vertices"].append(dict(id=f"v{c}", kind="node", node_id=c))
        g["edges"] += [[f"v{a}", f"v{c}"], [f"v{c}", t]]
        ident = self.save(g)
        self.settle()
        return ident, a, b, c, t

    def switch(self, ident, path):
        g = json.loads(configurations.get_topology(self.db, ident)["graph"])
        return self.save(g, ident, switch_only=True, paths=[path])

    def test_prepare_ack_commit_ack_retire(self):
        ident, a, b, c, t = self.branch()
        before = configurations.members(self.db, ident)[0]
        old = json.loads(before["applied"])
        self.switch(ident, [f"v{a}", f"v{c}", t])
        self.tick()
        self.assertEqual(self.db.execute("SELECT phase FROM jobs").fetchone()[0], "prepare")
        self.assertEqual(
            configurations.members(self.db, ident)[0]["applied"], before["applied"]
        )
        self.assertEqual(
            self.db.execute("SELECT host FROM rules WHERE node_id=?", (a,)).fetchone()[0],
            database.get_node(self.db, b)["addr"],
        )
        self.ack(c)
        self.tick()
        self.assertEqual(self.db.execute("SELECT phase FROM jobs").fetchone()[0], "commit")
        self.assertEqual(
            configurations.members(self.db, ident)[0]["applied"], before["applied"]
        )
        self.ack(a)
        self.tick()
        after = json.loads(configurations.members(self.db, ident)[0]["applied"])
        self.assertEqual(after["plan"]["graph_path"], [f"v{a}", f"v{c}", t])
        self.assertEqual(after["plan"]["port"], old["plan"]["port"])
        self.assertTrue(
            self.db.execute("SELECT 1 FROM retired_ports WHERE node_id=?", (b,)).fetchone()
        )
        self.ack(b)
        self.tick()
        self.assertFalse(self.db.execute("SELECT 1 FROM retirements").fetchone())

    def test_downstream_failure_keeps_applied_and_can_retry(self):
        ident, a, b, c, t = self.branch()
        old = configurations.members(self.db, ident)[0]["applied"]
        self.switch(ident, [f"v{a}", f"v{c}", t])
        self.tick()
        self.ack(c, False)
        self.tick()
        r = configurations.members(self.db, ident)[0]
        self.assertEqual(r["state"], "error")
        self.assertEqual(r["applied"], old)
        self.ack(c)
        self.call(
            "api_topology_action",
            dict(
                action="retry", revision=configurations.get_topology(self.db, ident)["revision"]
            ),
            ident,
        )
        self.settle()
        self.assertEqual(
            json.loads(configurations.members(self.db, ident)[0]["applied"])["plan"][
                "graph_path"
            ][1],
            f"v{c}",
        )

    def test_failed_entry_rolls_back_before_freeing_downstream(self):
        ident, a, b, c, t = self.branch()
        old = configurations.members(self.db, ident)[0]["applied"]
        self.switch(ident, [f"v{a}", f"v{c}", t])
        self.tick()
        self.ack(c)
        self.tick()
        self.ack(a, False)
        self.tick()
        self.assertEqual(self.db.execute("SELECT phase FROM jobs").fetchone()[0], "rollback")
        self.assertTrue(self.db.execute("SELECT 1 FROM rules WHERE node_id=?", (c,)).fetchone())
        self.assertEqual(configurations.members(self.db, ident)[0]["applied"], old)
        self.ack(a)
        self.tick()
        self.assertFalse(
            self.db.execute("SELECT 1 FROM rules WHERE node_id=?", (c,)).fetchone()
        )
        self.assertEqual(configurations.members(self.db, ident)[0]["state"], "error")

    def test_new_save_during_prepare_supersedes_job(self):
        ident, a, b, c, t = self.branch()
        self.switch(ident, [f"v{a}", f"v{c}", t])
        self.tick()
        self.switch(ident, [f"v{a}", f"v{b}", t])
        self.tick()
        self.assertFalse(self.db.execute("SELECT 1 FROM jobs").fetchone())
        self.settle()
        self.assertEqual(
            json.loads(configurations.members(self.db, ident)[0]["applied"])["plan"][
                "graph_path"
            ][1],
            f"v{b}",
        )

    def test_new_save_during_commit_waits_for_receipt_then_continues(self):
        ident, a, b, c, t = self.branch()
        self.switch(ident, [f"v{a}", f"v{c}", t])
        self.tick()
        self.ack(c)
        self.tick()
        self.switch(ident, [f"v{a}", f"v{b}", t])
        self.tick()
        self.assertEqual(self.db.execute("SELECT phase FROM jobs").fetchone()[0], "commit")
        self.ack(a)
        self.tick()
        self.settle()
        self.assertEqual(
            json.loads(configurations.members(self.db, ident)[0]["applied"])["plan"][
                "graph_path"
            ][1],
            f"v{b}",
        )

    def test_layout_save_during_prepare_keeps_job(self):
        ident, a, b, c, t = self.branch()
        self.switch(ident, [f"v{a}", f"v{c}", t])
        self.tick()
        job = self.db.execute("SELECT payload FROM jobs").fetchone()[0]
        g = json.loads(configurations.get_topology(self.db, ident)["graph"])
        g["vertices"][0]["x"] += 50
        self.save(g, ident)
        self.tick()
        self.assertEqual(self.db.execute("SELECT payload FROM jobs").fetchone()[0], job)
        self.settle()

    def test_shared_nodes_serialized_but_offline_other_path_does_not_block(self):
        a, b, c, d = [self.node(x) for x in "ABCD"]
        self.ready(a, b, c, d)
        one = self.save(self.graph([a, b]))
        two = self.save(self.graph([a, c]))
        three = self.save(self.graph([d]))
        with self.db:
            self.db.execute("UPDATE nodes SET last_seen=1 WHERE id=?", (b,))
        self.tick()
        jobs = [dict(r) for r in self.db.execute("SELECT * FROM jobs")]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(configurations.members(self.db, one)[0]["state"], "waiting")
        self.ready(b)
        self.settle()
        self.assertEqual(
            len(
                {
                    json.loads(r[0])["port"]
                    for r in self.db.execute(
                        "SELECT desired FROM configurations WHERE topology_id IN (?,?)",
                        (one, two),
                    )
                }
            ),
            2,
        )

    def test_deleting_topology_keeps_identity_until_cleanup_ack(self):
        a, b = self.node(), self.node()
        self.ready(a, b)
        ident = self.save(self.graph([a, b]))
        self.settle()
        self.call("api_topology_delete", {}, ident)
        self.tick()
        self.assertTrue(self.db.execute("SELECT 1 FROM topologies").fetchone())
        with self.assertRaises(errors.UserError):
            self.call("api_node_del", {}, a)
        self.ack(a)
        self.ack(b)
        self.tick()
        self.assertFalse(self.db.execute("SELECT 1 FROM topologies").fetchone())
        self.assertFalse(self.db.execute("SELECT 1 FROM configurations").fetchone())
        self.call("api_node_del", {}, a)

    def test_passive_never_needs_online_but_requires_receipt(self):
        a, b = self.node(), self.node()
        self.ready(a)
        self.ready(b, mode="passive")
        ident = self.save(self.graph([a, b]))
        self.tick()
        self.tick(int(time.time()) + 31)
        self.assertEqual(configurations.members(self.db, ident)[0]["state"], "waiting")
        self.assertFalse(configurations.members(self.db, ident)[0]["applied"])
        self.ack(b)
        self.tick()
        self.ack(a)
        self.tick()
        self.assertEqual(configurations.members(self.db, ident)[0]["state"], "active")

    def test_target_change_during_prepare_uses_latest_target(self):
        a, b = self.node(), self.node()
        self.ready(a, b)
        target = self.target()
        ident = self.save(self.graph([a, b], target))
        self.tick()
        self.call("api_target_save", dict(port=8443), target)
        self.tick()
        self.ack(b)
        self.settle()
        applied = json.loads(configurations.members(self.db, ident)[0]["applied"])
        self.assertEqual(applied["target"]["port"], 8443)

    def test_save_during_first_install_keeps_reserved_ingress(self):
        a, b = self.node(), self.node()
        self.ready(a, b)
        ident = self.save(self.graph([a, b]))
        self.tick()
        g = json.loads(configurations.get_topology(self.db, ident)["graph"])
        self.save(g, ident)
        self.ack(b)
        self.tick()  # Initial ingress has no applied snapshot yet.
        self.save(g, ident)
        self.settle()
        self.assertEqual(configurations.members(self.db, ident)[0]["state"], "active")

    def test_inventory_resolution_does_not_repeat_deployment(self):
        import agent_v3

        a, b = self.node(), self.node()
        self.ready(a, b)
        g = self.graph([a, b])
        g["bindings"] = {f"v{a}/v{b}": {"from": {f"v{a}": "main"}}}
        ident = self.save(g)
        self.tick()
        self.assertEqual(configurations.members(self.db, ident)[0]["state"], "waiting")
        with self.db:
            agent_v3.save_snapshot(
                self.db,
                a,
                "inventory",
                dict(
                    network_complete=True,
                    interfaces=[dict(name="ens3", addresses=["192.0.2.1/24"])],
                ),
            )
        self.settle()
        before = [tuple(r) for r in self.db.execute("SELECT * FROM rules")]
        self.tick()
        self.tick()
        self.assertEqual(before, [tuple(r) for r in self.db.execute("SELECT * FROM rules")])
        self.assertFalse(self.db.execute("SELECT 1 FROM jobs").fetchone())
        graph = json.loads(configurations.get_topology(self.db, ident)["graph"])
        path = json.loads(configurations.members(self.db, ident)[0]["desired"])["graph_path"]
        self.save(graph, ident, switch_only=True, paths=[path])
        self.tick()
        self.assertFalse(self.db.execute("SELECT 1 FROM jobs").fetchone())

    def test_failed_deletion_can_retry_and_keeps_applied_until_ack(self):
        a, b = self.node(), self.node()
        self.ready(a, b)
        ident = self.save(self.graph([a, b]))
        self.settle()
        applied = configurations.members(self.db, ident)[0]["applied"]
        self.call("api_topology_delete", {}, ident)
        self.tick()
        self.ack(b, False)
        self.assertEqual(configurations.members(self.db, ident)[0]["applied"], applied)
        self.assertTrue(configurations.detail(self.db, ident)["sync"]["retry"])
        self.call(
            "api_topology_action",
            dict(
                action="retry", revision=configurations.get_topology(self.db, ident)["revision"]
            ),
            ident,
        )
        self.ack(a)
        self.ack(b)
        self.tick()
        self.assertFalse(self.db.execute("SELECT 1 FROM configurations").fetchone())
