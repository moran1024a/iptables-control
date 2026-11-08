"""One probe task per physical measurement, independently bound to each view."""

import asyncio
import json
import time
import types
import unittest
from test_server import Fixture
import configurations, database, link_quality, network_quality as q, probe_store, probes


class QualityTests(Fixture, unittest.TestCase):
    def monitor(self):
        sessions = {
            n["id"]: dict(
                quality_capable=True,
                quality_setup_id="ready",
                quality_listener=dict(port=n["quality_port"], id="ready"),
                paused=False,
            )
            for n in self.db.execute("SELECT * FROM nodes")
        }

        async def db(fn):
            with self.db:
                return fn(self.db)

        return probes.Monitor(types.SimpleNamespace(sessions=sessions, database=db))

    def test_shared_topologies_deduplicate_and_keep_full_and_subsegments(self):
        a, b, c = [self.node(x) for x in "ABC"]
        self.ready(a, b, c)
        t = self.target()
        one = self.save(self.graph([a, b, c], t))
        two = self.save(self.graph([a, b, c], t))
        self.settle()
        monitor = self.monitor()
        nodes, demands = monitor.plan(self.db, 1000)
        self.assertEqual(len(demands), 18)
        with self.db:
            _, tasks = monitor.bind(self.db, 1000)
        # B->target TCP, B->C UDP/ICMP, C->target TCP/ICMP.
        self.assertEqual(len(tasks), 5)
        self.assertTrue(all(len(consumers) >= 2 for consumers in tasks.values()))
        sections = link_quality.sections(self.db, configurations.get_topology(self.db, one), nodes)
        self.assertEqual(len(sections), 3)
        self.assertEqual([s["kind"] for s in sections], ["全段", "子段", "子段"])
        ids = set(tasks)
        with self.db:
            for key in ids:
                probe_store.record(
                    self.db, key, 1001, "ok", 21, resolved_ip="192.0.2.1", epoch="test"
                )
        spec = next(s for s in demands if s["protocol"] == "tcp-connect-v1" and s["source"] == b)
        history = probe_store.link_rows(self.db, spec, 900, 1100)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["rtt"], 21)
        _, again = monitor.bind(self.db, 1100)
        self.assertEqual(set(again), ids)

    def test_unapplied_topology_has_unknown_charts_without_probe(self):
        a, b = self.node(), self.node()
        ident = self.save(self.graph([a, b]))
        monitor = self.monitor()
        _, tasks = monitor.bind(self.db, 1000)
        self.assertEqual(tasks, {})
        result = asyncio.run(link_quality.view(monitor, dict(id=ident, range=900), 1100))
        self.assertEqual(len(result["sections"]), 2)
        self.assertTrue(
            all(
                s["state"] == "waiting" for section in result["sections"] for s in section["series"]
            )
        )

    def test_retention_and_path_bindings_do_not_mix(self):
        a, b = self.node(), self.node()
        self.ready(a, b)
        ident = self.save(self.graph([a, b]))
        self.settle()
        monitor = self.monitor()
        _, tasks = monitor.bind(self.db, 1)
        key = next(iter(tasks))
        spec = tasks[key][0]
        with self.db:
            probe_store.record(self.db, key, 2, "ok", 10)
            probe_store.record(self.db, key, q.RETENTION + 3, "ok", 20)
            probe_store.cleanup(self.db, 3)
        rows = probe_store.link_rows(self.db, spec, 0, q.RETENTION + 4)
        self.assertEqual([r["rtt"] for r in rows], [20])
