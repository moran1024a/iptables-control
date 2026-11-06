"""RC2 pair selection, history semantics, migration and WSS probe scheduling."""
import json
from pathlib import Path
import time
import unittest

from test_server import Fixture, s
import network_quality as q
import transport


class HistoryTests(unittest.TestCase):
    @staticmethod
    def row(slot, rtt=20, path='same', address='192.0.2.1', status='ok'):
        return dict(slot=slot, rtt=rtt if status == 'ok' else None, path_key=path,
                    path_label=path, resolved_ip=address, status=status, error='probe failed' if status != 'ok' else '')

    def test_delayed_successful_samples_do_not_break_hour_or_day_charts(self):
        for seconds in (900, 3600, 86400):
            with self.subTest(seconds=seconds):
                data = q.summarize([self.row(t) for t in range(0, seconds + 1, 12)], seconds, seconds)
                good = [p for p in data['points'] if p['mean'] is not None]
                self.assertTrue(good)
                self.assertTrue(all(not p['gap'] and p['mean'] == 20 for p in good))
                self.assertTrue(all(b['first_slot'] - a['last_slot'] == 12 for a, b in zip(good, good[1:])))

    def test_path_and_endpoint_switches_are_not_averaged_together(self):
        for field in ('path_key', 'resolved_ip'):
            with self.subTest(field=field):
                rows = [self.row(3540, 200), self.row(3550, 20), self.row(3560, 100)]
                rows[1][field] = 'other'
                points = [p for p in q.summarize(rows, 3600, 3600)['points'] if p['mean'] is not None]
                self.assertEqual([p['mean'] for p in points], [200, 20, 100])
                self.assertEqual([p['time'] for p in points], [3540, 3550, 3560])
                self.assertEqual([p['end'] for p in points], [3550, 3560, 3600])

    def test_partial_failure_is_preserved_after_a_successful_sample(self):
        rows = [self.row(3540), self.row(3550, address='', status='timeout'), self.row(3560, 30)]
        points = [p for p in q.summarize(rows, 3600, 3600)['points'] if p['mean'] is not None]
        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual((point['mean'], point['samples'], point['lost'], point['failures']), (25, 3, 1, 1))
        self.assertEqual((point['status'], point['error']), ('timeout', 'probe failed'))
        self.assertTrue(point['gap'])

    def test_boundary_bucket_excludes_samples_outside_visible_window(self):
        data = q.summarize([self.row(0, 1000), self.row(10, 20), self.row(20, 30), self.row(3610, 2000)], 3600, 3605)
        points = [p for p in data['points'] if p['mean'] is not None]
        self.assertEqual(len(points), 1)
        self.assertEqual((points[0]['time'], points[0]['mean'], points[0]['peak']), (5, 25, 30))
        self.assertTrue(all(5 <= p['time'] <= 3605 for p in data['points']))

    def test_real_outage_splits_a_bucket(self):
        points = [p for p in q.summarize([self.row(3540, 10), self.row(3580, 30)], 3600, 3600)['points'] if p['mean'] is not None]
        self.assertEqual([p['mean'] for p in points], [10, 30])
        self.assertEqual(points[1]['first_slot'] - points[0]['last_slot'], 40)


class QualityTests(Fixture, unittest.TestCase):
    def test_rc1_upgrade_keeps_rules_and_does_not_reassign_conflicting_port(self):
        a, b = self.node(ports='33000-34000'), self.node('B')
        route = self.route([a, b])
        # Simulate the pre-RC2 schema with an existing forwarding port at the new default.
        with self.db:
            self.db.execute('UPDATE nodes SET quality_port=0')
        self.config(route, [33445, 10001])
        before = s.node_rules(self.db, s.get_node(self.db, a))
        with self.db:
            self.db.execute('ALTER TABLE nodes DROP COLUMN quality_port')
            self.db.execute('ALTER TABLE nodes DROP COLUMN wake_enabled')
            self.db.execute('ALTER TABLE nodes DROP COLUMN wake_port')
            self.db.execute('DROP TABLE wake_jobs')
            for table in ('link_rules','link_retired','link_events','links','pools'):
                self.db.execute('DROP TABLE '+table)
            self.db.execute('PRAGMA user_version=400')
        Path(s.DB_PATH + '.pre-0.4.0-rc.2.bak').unlink()
        upgraded = s.init_db()
        self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0], s.DATABASE_VERSION)
        self.assertEqual(s.node_rules(upgraded, s.get_node(upgraded, a)), before)
        self.assertEqual(s.get_node(upgraded, a)['quality_port'], q.PORT)
        self.assertTrue(Path(s.DB_PATH + '.pre-0.4.0-rc.2.bak').is_file())
        upgraded.close()

    def test_probe_port_reserved_and_can_be_disabled(self):
        a = self.node(ports='33445')
        route = self.route([a])
        with self.assertRaises(s.UserError):
            self.config(route)
        self.call('api_node_edit', {'quality_port': 0}, a)
        self.config(route, [33445])

    def test_history_deduplicates_and_preserves_peak_gaps_and_sample_counts(self):
        a, b = self.node(), self.node('B')
        nodes = {n['id']: dict(n) for n in self.db.execute('SELECT * FROM nodes')}
        pair = q.ensure_pair(self.db, nodes, a, b)
        now = 1200
        q.record(self.db, pair['id'], 1000, 'ok', 10, resolved_ip='192.0.2.1')
        q.record(self.db, pair['id'], 1010, 'ok', 90, resolved_ip='192.0.2.1')
        q.record(self.db, pair['id'], 1020, 'timeout')
        q.record(self.db, pair['id'], 1030, 'offline')
        q.record(self.db, pair['id'], 1040, 'error')
        q.record(self.db, pair['id'], 1010, 'ok', 1000)
        data = q.history(self.db, pair['id'], 3600, now)
        self.assertEqual(data['summary']['samples'], 3)
        self.assertEqual(data['summary']['lost'], 1)
        self.assertEqual(data['summary']['peak'], 90)
        self.assertEqual(data['summary']['jitter'], 80)
        self.assertTrue(any(p['status'] == 'missing' and p['mean'] is None for p in data['points']))
        self.assertTrue(any(p['peak'] == 90 for p in data['points']))
        nodes[b]['addr'] = '192.0.2.2'
        self.assertNotEqual(q.ensure_pair(self.db, nodes, a, b)['id'], pair['id'])


class SchedulerTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = transport.Hub(s._module())
        self.a, self.b, self.c = self.node(), self.node('B'), self.node('C')
        self.route_id = self.route([self.a, self.b, self.c])
        self.route([self.a, self.b], 'shared')
        with self.db:
            self.db.execute("UPDATE nodes SET installed_agent='active',capabilities=?,last_seen=?,connection_state='connected'",
                            (json.dumps(['network_quality']), int(time.time())))
        self.messages = {n: [] for n in (self.a, self.b, self.c)}
        for n in self.messages:
            async def send(data, nid=n):
                self.messages[nid].append(data)
            self.hub.sessions[n] = dict(send=send, quality_capable=True, paused=False)

    def ready(self):
        for session in self.hub.sessions.values():
            session['quality_listener'] = dict(id=session['quality_setup_id'], port=session['quality_port'], error='')

    async def test_pair_dedup_budget_receipt_and_passive_stop(self):
        monitor = self.hub.quality
        await monitor.tick()
        self.ready()
        monitor.last.clear()
        await monitor.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM quality_pairs').fetchone()[0], 4)
        # The shared middle node has a single packet budget per second.
        self.assertEqual(len(monitor.pending), 1)
        op, task = next(iter(monitor.pending.items()))
        receipt = dict(id=op, status='ok', rtt=12.5, resolved_ip='192.0.2.1')
        await monitor.result(self.a, self.hub.sessions[self.b], receipt)
        self.assertIn(op, monitor.pending)
        # Use the next slot so the initial 'waiting' entry does not collide.
        task['at'] += 10
        await monitor.result(self.a, self.hub.sessions[self.a], receipt)
        self.assertNotIn(op, monitor.pending)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quality_samples WHERE status='ok'").fetchone()[0], 1)
        with self.db:
            self.db.execute("UPDATE nodes SET desired_mode='passive' WHERE id=?", (self.b,))
        monitor.last.clear()
        await monitor.tick()
        self.assertEqual(self.hub.sessions[self.a]['quality_port'], 0)
        self.assertEqual(len(monitor.pending), 0)

    async def test_icmp_runs_when_udp_listener_fails_and_keeps_separate_history(self):
        with self.db:
            self.db.execute("UPDATE nodes SET capabilities=?", (json.dumps(['network_quality', 'network_quality_icmp']),))
        monitor = self.hub.quality
        await monitor.tick()
        op, task = next(iter(monitor.pending.items()))
        self.assertEqual(task['pair']['protocol'], 'icmp-echo-v1')
        await monitor.result(self.a, self.hub.sessions[self.a], dict(id=op, status='ok', rtt=8.25))
        self.ready()
        self.hub.sessions[self.b]['quality_listener']['error'] = 'UDP port conflict'
        result = await monitor.view(dict(scope='routes', id=self.route_id, source=self.a, target=self.b))
        udp, icmp = result['series']
        self.assertEqual(udp['state'], 'error')
        self.assertEqual(icmp['state'], 'collecting')
        self.assertEqual(icmp['summary']['mean'], 8.25)
        self.assertIsNone(udp['summary']['mean'])
        self.assertEqual(icmp['pair']['port'], 0)
        self.assertNotEqual(udp['pair']['id'], icmp['pair']['id'])

    async def test_nonadjacent_lease_validation_and_retention(self):
        monitor = self.hub.quality
        view = await monitor.view(dict(scope='routes', id=self.route_id, source=self.a, target=self.c))
        self.assertFalse(view['adjacent'])
        self.call('api_order', dict(groups=[s.default_group(self.db, 'nodes')], ids=[self.c, self.b, self.a]), 'nodes')
        default_view = await monitor.view(dict(scope='routes', id=self.route_id))
        self.assertEqual([n['id'] for n in default_view['nodes']], [self.c, self.b, self.a])
        self.assertEqual((default_view['pair']['source'], default_view['pair']['target']), (self.a, self.b))
        self.assertTrue(default_view['adjacent'])
        await monitor.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM quality_pairs').fetchone()[0], 6)
        with self.assertRaises(s.UserError):
            await monitor.view(dict(scope='routes', id=self.route_id, source=self.a, target=999))
        with self.db:
            q.record(self.db, view['pair']['id'], int(time.time()) - 90000, 'ok', 10)
        monitor.extra.clear()
        monitor.cleanup_at = 0
        await monitor.tick()
        self.assertNotIn(view['pair']['id'], monitor.last)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM quality_samples WHERE slot<?', (int(time.time())-86400,)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
