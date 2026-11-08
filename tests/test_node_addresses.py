import unittest
import json
from test_server import Fixture
import agent_v3, configurations, database, errors, node_addresses


class AddressTests(Fixture, unittest.TestCase):
    def test_correct_unapplied_source_then_protect_confirmed_address(self):
        a = self.node()
        self.ready(a)
        with self.db:
            agent_v3.save_snapshot(
                self.db,
                a,
                "inventory",
                dict(
                    network_complete=True,
                    interfaces=[dict(name="eth0", addresses=["10.0.0.1/24"])],
                ),
            )
        graph = self.graph([a])
        graph["bindings"] = {f"v{a}/{graph['vertices'][-1]['id']}": {"from": {f"v{a}": "main"}}}
        ident = self.save(graph)
        self.tick()
        self.assertEqual(configurations.members(self.db, ident)[0]["state"], "error")
        values = node_addresses.listing(self.db, database.get_node(self.db, a))
        values[0].update(local="10.0.0.1", device="eth0")
        self.call("api_node_edit", dict(addresses=values), a)
        self.assertEqual(configurations.members(self.db, ident)[0]["state"], "pending")
        self.settle()
        plan = json.loads(configurations.members(self.db, ident)[0]["applied"])["plan"]
        self.assertIn("10.0.0.1,eth0", plan["routing"][0])
        values[0]["local"] = "10.0.0.2"
        with self.assertRaises(errors.UserError):
            self.call("api_node_edit", dict(addresses=values), a)

    def test_primary_contact_and_local_source_are_independent(self):
        n = self.node()
        self.call("api_node_edit", dict(addr="203.0.113.1"), n)
        self.assertEqual(database.get_node(self.db, n)["addr"], "203.0.113.1")
        self.save(self.graph([n]))
        with self.db:
            agent_v3.save_snapshot(
                self.db,
                n,
                "inventory",
                dict(
                    network_complete=True,
                    interfaces=[dict(name="ens3", addresses=["10.0.0.1/24"])],
                ),
            )
        self.assertEqual(
            node_addresses.listing(self.db, database.get_node(self.db, n))[0]["host"],
            "203.0.113.1",
        )

    def test_inventory_resolves_only_matching_source(self):
        n = self.node()
        with self.db:
            agent_v3.save_snapshot(
                self.db,
                n,
                "inventory",
                dict(
                    network_complete=True,
                    interfaces=[dict(name="ens3", addresses=["192.0.2.1/24"])],
                ),
            )
        a = node_addresses.listing(self.db, database.get_node(self.db, n))[0]
        self.assertEqual(a["device"], "ens3")
        self.assertFalse(node_addresses.egress_problem(a, node_addresses.network(self.db, n)))
