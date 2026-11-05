import json
import time
import unittest
from unittest.mock import patch

from test_server import Fixture, s
import link_quality as lq
import network_quality as q
import transport


class LinkQualityTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ids = [self.node(n) for n in ('入口A', 'B', 'C')]
        self.cid = self.config(self.route(self.ids), [10001, 10002, 10003])
        self.lid = self.db.execute('SELECT id FROM links WHERE chain_id=?', (self.cid,)).fetchone()[0]
        with self.db:
            # Only the new link scheduler owns these measurements.
            self.db.execute('UPDATE chains SET route_id=NULL')
            self.db.execute('DELETE FROM routes')
            for i, nid in enumerate(self.ids):
                self.db.execute("UPDATE nodes SET addr=?, installed_agent='active', capabilities=?, state='ok', applied_generation=desired_generation WHERE id=?",
                                (f'192.0.2.{i+1}', json.dumps(['network_quality', 'network_quality_icmp', 'network_quality_tcp']), nid))
        self.hub = transport.Hub(s._module())
        self.messages = []
        for nid in self.ids:
            async def send(data, nid=nid):
                self.messages.append((nid, data))
            self.hub.sessions[nid] = dict(send=send, paused=False, quality_capable=True)

    def plan(self):
        nodes = {r['id']: dict(r) for r in self.db.execute('SELECT * FROM nodes')}
        link = self.db.execute('SELECT * FROM links WHERE id=?', (self.lid,)).fetchone()
        return lq.sections(s._module(), self.db, link, nodes)

    async def test_full_route_uses_next_forwarding_port_not_target_directly(self):
        sections = self.plan()
        self.assertEqual(len(sections), 3)
        self.assertEqual(sections[0]['label'], 'B → C → target')
        full = sections[0]['probes'][0]
        self.assertEqual((full['source'], full['host'], full['port']), (self.ids[1], '192.0.2.3', 10003))
        rule = self.db.execute('SELECT id FROM hops WHERE node_id=?', (self.ids[1],)).fetchone()[0]
        self.assertEqual(full['rule_id'], rule)
        self.assertIsNone(full['reason'])
        self.assertIsNotNone(sections[0]['probes'][2]['reason'])  # ping must not pretend to follow C
        tail = sections[2]['probes'][0]
        self.assertEqual((tail['source'], tail['host'], tail['port']), (self.ids[2], '198.51.100.1', 443))
        self.assertNotIn(self.ids[0], [p['source'] for sec in sections for p in sec['probes']])

    async def test_direct_keeps_two_unknown_segments_and_full_bypasses_it(self):
        with self.db:
            row = self.db.execute('SELECT plan FROM links WHERE id=?', (self.lid,)).fetchone()
            plan = json.loads(row[0])
            plan['stages'].insert(1, dict(id='direct', kind='pool', pool_id=1, direct=True, order=[0], disabled=[], default=0))
            self.db.execute('UPDATE links SET plan=?,selected=? WHERE id=?', (json.dumps(plan), json.dumps({'direct': 0}), self.lid))
        sections = self.plan()
        self.assertEqual(len(sections), 4)
        self.assertEqual(sections[0]['label'], 'B → C → target')
        for sec in sections[1:3]:
            self.assertTrue(all(p['reason'] == ('unknown', 'direct') for p in sec['probes']))

    async def test_passive_sender_unknown_but_passive_transit_is_allowed(self):
        with self.db:
            self.db.execute("UPDATE nodes SET desired_mode='passive',installed_agent='passive' WHERE id=?", (self.ids[2],))
        view = await self.hub.quality.view(dict(scope='links', id=self.lid))
        self.assertEqual(len(view['sections']), 3)
        self.assertEqual(view['sections'][0]['series'][0]['state'], 'collecting')
        self.assertEqual(view['sections'][2]['series'][0]['state'], 'unknown')

    async def test_scheduler_budget_and_late_results_are_not_misattributed(self):
        monitor = self.hub.quality
        now = int(time.time())
        with patch.object(q.time, 'time', return_value=now):
            await monitor.tick()
        probes = [(n, m) for n, m in self.messages if m['type'] == 'quality_probe']
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0][0], self.ids[1])
        self.assertEqual(probes[0][1]['protocol'], 'tcp-connect-v1')
        op = probes[0][1]['id']
        await monitor.result(self.ids[1], self.hub.sessions[self.ids[0]], dict(id=op, status='ok', rtt=12))
        self.assertIn(op, monitor.links.pending)
        await monitor.result(self.ids[1], self.hub.sessions[self.ids[1]], dict(id=op, status='ok', rtt=12, resolved_ip='192.0.2.3'))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM link_quality_samples WHERE status='ok'").fetchone()[0], 1)
        self.messages.clear()
        with patch.object(q.time, 'time', return_value=now+10):
            await monitor.tick()
        op, task = next(iter(monitor.links.pending.items()))
        nid = task['specs'][0]['source']
        with self.db:
            self.db.execute("UPDATE nodes SET addr='203.0.113.3' WHERE id=?", (nid,))
        await monitor.result(nid, self.hub.sessions[nid], dict(id=op, status='ok', rtt=500))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM link_quality_samples WHERE rtt=500').fetchone()[0], 0)

    async def test_history_keeps_switch_gap_and_current_statistics(self):
        now = int(time.time()) // 10 * 10
        spec = self.plan()[0]['probes'][0]
        with self.db:
            lq.record(self.db, dict(spec, path_key='old', path_label='B → D → target'), now-20, 'ok', 90)
            lq.record(self.db, spec, now-10, 'ok', 20)
            lq.record(self.db, spec, now, 'ok', 25)
        data = await self.hub.quality.view(dict(scope='links', id=self.lid))
        series = data['sections'][0]['series'][0]
        self.assertEqual(series['summary']['mean'], 22.5)
        self.assertEqual(series['summary']['jitter'], 5)
        self.assertEqual({p['path_key'] for p in series['points'] if p['mean'] is not None}, {'old', spec['path_key']})

    async def test_disabled_probe_does_not_send_traffic_and_prunes_old_samples(self):
        now = int(time.time())
        spec = self.plan()[0]['probes'][0]
        with self.db:
            self.db.execute('UPDATE nodes SET quality_port=0')
            lq.record(self.db, spec, now-90000, 'ok', 10)
        await self.hub.quality.tick()
        self.assertFalse([m for _, m in self.messages if m['type'] == 'quality_probe'])
        self.assertFalse(self.db.execute('SELECT 1 FROM link_quality_samples WHERE slot<?', (now-86400,)).fetchone())

    async def test_single_stage_full_and_tail_share_one_wire_probe(self):
        with self.db:
            self.db.execute('DELETE FROM hops WHERE node_id=?', (self.ids[2],))
            row = self.db.execute('SELECT plan FROM links WHERE id=?', (self.lid,)).fetchone()
            plan = json.loads(row[0]); plan['stages'].pop()
            self.db.execute('UPDATE links SET plan=? WHERE id=?', (json.dumps(plan), self.lid))
        await self.hub.quality.tick()
        tasks = list(self.hub.quality.links.pending.values())
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(tasks[0]['specs']), 2)
