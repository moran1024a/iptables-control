import copy
import json
import unittest
from test_server import Fixture
import configurations, database, errors, execution, forwarding, topology


class TopologyTests(Fixture, unittest.TestCase):
    def test_sparse_independent_first_hops_and_offline_save(self):
        a, b, c, d = [self.node(x) for x in "ABCD"]
        t, u = self.target(), self.target("example.com")
        g = self.graph([a, c], t)
        g["vertices"] += self.graph([b, d], u)["vertices"]
        g["edges"] += self.graph([b, d], u)["edges"]
        g["edges"].append(["v" + str(a), "v" + str(d)])
        ident = self.save(g)
        configs = configurations.members(self.db, ident)
        self.assertEqual(
            {(r["entry"], r["target"]) for r in configs},
            {(f"v{a}", f"t{t}"), (f"v{a}", f"t{u}"), (f"v{b}", f"t{u}")},
        )
        self.assertFalse(self.db.execute("SELECT 1 FROM rules").fetchone())
        self.tick()
        self.assertEqual(
            {r["state"] for r in configurations.members(self.db, ident)}, {"waiting"}
        )
        self.assertEqual(len(configurations.configs(self.db)["configs"]), 3)

    def test_disconnected_nodes_are_saved_with_warning(self):
        a, b = self.node(), self.node()
        g = self.graph([a])
        g["vertices"].append(dict(id="unused", kind="node", node_id=b))
        ident = self.save(g)
        self.assertEqual(len(configurations.detail(self.db, ident)["warnings"]), 1)
        g["edges"] = []
        self.save(g, ident)
        self.assertEqual(len(configurations.detail(self.db, ident)["warnings"]), 3)

    def test_cycle_self_reference_bad_ip_and_format_rejected(self):
        a, b = self.node(), self.node()
        g = self.graph([a, b])
        bad = copy.deepcopy(g)
        bad["edges"].append([f"v{b}", f"v{a}"])
        with self.assertRaises(errors.UserError):
            self.save(bad)
        bad = copy.deepcopy(g)
        bad["version"] = 1
        with self.assertRaises(errors.UserError):
            self.save(bad)
        bad = copy.deepcopy(g)
        bad["bindings"] = {f"v{a}/v{b}": {"to": {f"v{b}": "gone"}}}
        with self.assertRaises(errors.UserError):
            self.save(bad)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM topologies").fetchone()[0], 0)

    def test_layout_and_adding_alternative_do_not_switch(self):
        a, b, c = [self.node(x) for x in "ABC"]
        self.ready(a, b, c)
        g = self.graph([a, b])
        ident = self.save(g)
        self.settle()
        old = configurations.members(self.db, ident)[0]
        before = list(self.db.execute("SELECT * FROM rules"))
        g["vertices"].append(dict(id=f"v{c}", kind="node", node_id=c, x=15, y=15))
        g["edges"] += [[f"v{a}", f"v{c}"], [f"v{c}", g["vertices"][2]["id"]]]
        self.save(g, ident)
        self.tick()
        new = configurations.members(self.db, ident)[0]
        self.assertEqual(old["id"], new["id"])
        self.assertEqual(old["name"], new["name"])
        self.assertEqual(
            execution.operational(json.loads(old["desired"])),
            execution.operational(json.loads(new["desired"])),
        )
        self.assertEqual(
            [tuple(r) for r in before],
            [tuple(r) for r in self.db.execute("SELECT * FROM rules")],
        )
        self.assertFalse(self.db.execute("SELECT 1 FROM jobs").fetchone())
        self.assertFalse(configurations.detail(self.db, ident)["warnings"])

    def test_explicit_egress_checked_only_during_execution(self):
        a, b = self.node(), self.node()
        self.ready(a, b)
        g = self.graph([a, b])
        g["bindings"] = {f"v{a}/v{b}": {"from": {f"v{a}": "main"}}}
        ident = self.save(g)
        self.tick()
        r = configurations.members(self.db, ident)[0]
        self.assertEqual(r["state"], "waiting")
        self.assertIn("出口", r["error"])
        # Unbound public receiving addresses work with automatic source selection.
        g.pop("bindings")
        self.save(g, ident)
        self.settle()

    def test_unique_ports_and_invalid_requested_ports(self):
        a = self.node(ports="10000-10001")
        g = self.graph([a])
        one = self.save(g)
        second = self.save(g)
        ports = [
            json.loads(r[0])["port"]
            for r in self.db.execute("SELECT desired FROM configurations")
        ]
        self.assertEqual(set(ports), {10000, 10001})
        with self.assertRaises(errors.UserError):
            self.save(g)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM topologies").fetchone()[0], 2)
        self.save(g, one)
        self.assertEqual(
            json.loads(configurations.members(self.db, one)[0]["desired"])["port"], ports[0]
        )

    def test_preview_ignores_online_and_only_changes_reachable_targets(self):
        a, b, c = [self.node(x) for x in "ABC"]
        t, u = self.target(), self.target()
        g = self.graph([a, b], t)
        g["vertices"] += [
            dict(id=f"v{c}", kind="node", node_id=c),
            dict(id=f"t{u}", kind="target", target_id=u),
        ]
        g["edges"] += [[f"v{a}", f"v{c}"], [f"v{c}", f"t{u}"]]
        ident = self.save(g)
        r = configurations.preview(self.db, dict(id=ident, graph=g, required=[f"v{c}"]))
        self.assertEqual(len(r["paths"]), 1)
        self.assertEqual(r["paths"][0]["target"], f"t{u}")
        r = configurations.preview(self.db, dict(id=ident, graph=g, prefix=[f"v{a}"]))
        self.assertEqual(set(r["options"]), {f"v{b}", f"v{c}"})

    def test_duplicate_selection_and_removing_referenced_resources_are_rejected(self):
        a, b = self.node(), self.node()
        g = self.graph([a, b])
        ident = self.save(g)
        r = configurations.get_topology(self.db, ident)
        graph = json.loads(r["graph"])
        path = list(topology.pairs(graph).values())[0]
        with self.assertRaises(errors.UserError):
            self.save(graph, ident, paths=[path, path], switch_only=True)
        self.call("api_node_edit", dict(addr="192.0.2.99"), a)
        with self.assertRaises(errors.UserError):
            self.call("api_node_del", {}, b)
        with self.assertRaises(errors.UserError):
            self.call("api_target_del", {}, int(path[-1][1:]))

    def test_parallel_paths_do_not_expand_exponentially(self):
        a, target = self.node(), self.target()
        layers = [[a]] + [[self.node(), self.node()] for _ in range(15)]
        vertices = [
            dict(id=f"v{n}", kind="entry" if n == a else "node", node_id=n)
            for layer in layers
            for n in layer
        ]
        vertices.append(dict(id=f"t{target}", kind="target", target_id=target))
        edges = [
            [f"v{x}", f"v{y}"]
            for left, right in zip(layers, layers[1:])
            for x in left
            for y in right
        ]
        edges += [[f"v{x}", f"t{target}"] for x in layers[-1]]
        ident = self.save(dict(version=2, routing_version=2, vertices=vertices, edges=edges))
        graph = json.loads(configurations.get_topology(self.db, ident)["graph"])
        self.assertEqual(len(configurations.members(self.db, ident)), 1)
        required = f"v{layers[10][1]}"
        self.assertIn(required, topology.find_path(graph, required=[required]))

    def test_search_keeps_physical_node_history_for_duplicate_instances(self):
        a, x, y = self.node(), self.node(), self.node()
        target = self.target()
        graph = self.graph([a], target)
        graph["vertices"] += [
            dict(id=name, kind="node", node_id=n)
            for name, n in [("a_x", x), ("b_y", y), ("merge", self.node()), ("last_x", x)]
        ]
        graph["edges"] = [
            [f"v{a}", "a_x"],
            [f"v{a}", "b_y"],
            ["a_x", "merge"],
            ["b_y", "merge"],
            ["merge", "last_x"],
            ["last_x", f"t{target}"],
        ]
        ident = self.save(graph)
        plan = json.loads(configurations.members(self.db, ident)[0]["desired"])
        self.assertIn("b_y", plan["graph_path"])
        self.assertNotIn("a_x", plan["graph_path"])
