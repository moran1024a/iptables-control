"""Automatic recovery owns only failed intermediate paths, never manual work."""
import json
import time
import unittest

from test_server import Fixture
import configurations
import database
import execution
import failover


class FailoverTests(Fixture, unittest.TestCase):
    def network(self):
        self.e, self.p, self.f, self.b, self.c = [self.node(n) for n in ('entry', 'upstream', 'failed', 'backup', 'bypass')]
        self.ids = [self.e, self.p, self.f, self.b, self.c]
        self.t, self.u = self.target(), self.target()
        self.ready(*self.ids)
        for nid, value in ((self.f, 50), (self.b, 5), (self.c, 100)):
            self.call('api_node_edit', dict(weight=value), nid)
        g = self.graph([self.e, self.p, self.f], self.t)
        g['vertices'] += [dict(id=f'v{n}', kind='node', node_id=n) for n in (self.b, self.c)]
        g['vertices'].append(dict(id=f't{self.u}', kind='target', target_id=self.u))
        g['edges'] += [[f'v{a}', f'v{b}'] for a, b in ((self.p, self.b), (self.e, self.c))]
        g['edges'] += [[f'v{n}', f't{self.t}'] for n in (self.b, self.c)] + [[f'v{self.e}', f't{self.u}']]
        self.original = [f'v{n}' for n in (self.e, self.p, self.f)] + [f't{self.t}']
        self.ident = self.save(g, paths=[self.original], failover_enabled=True)
        self.settle()
        self.now = int(time.time())
        self.engine = failover.Engine(self.now)
        self.dead = {self.f}
        self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (self.now-61, self.f))
        self.db.commit()

    def config(self, target=None):
        return next(r for r in configurations.members(self.db, self.ident) if r['target'] == f't{target or self.t}')

    def step(self, seconds=10):
        self.now += seconds
        with database.atomic(self.db):
            for nid in self.ids:
                if nid not in self.dead:
                    self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (self.now, nid))
            self.engine.tick(self.db, self.now)

    def prime(self):
        for _ in range(4):
            self.step()

    def desired(self):
        return json.loads(self.config()['desired'])['graph_path']

    def test_weighted_suffix_preserves_other_pairs_identity_and_no_failback(self):
        self.network()
        before, other = self.config(), self.config(self.u)
        self.prime()
        self.assertEqual(self.desired(), [f'v{n}' for n in (self.e, self.p, self.b)] + [f't{self.t}'])
        self.assertEqual(self.config(self.u), other)
        chosen = self.config()
        self.assertEqual(chosen['id'], before['id'])
        self.assertEqual(json.loads(chosen['desired'])['port'], json.loads(before['desired'])['port'])
        self.assertEqual(chosen['applied'], before['applied'])
        # Execution needs only the new route online; an offline retired node can
        # keep its reserved ports without blocking confirmation of the new route.
        for _ in range(8):
            self.tick(self.now)
            for nid in self.ids:
                n = database.get_node(self.db, nid)
                if nid not in self.dead and n['applied_generation'] != n['desired_generation']:
                    self.ack(nid)
            self.step(1)
        self.assertEqual(self.config()['state'], 'active')
        self.assertEqual(json.loads(self.config()['failover'])['phase'], 'done')
        path = self.desired()
        self.dead.clear()
        self.ready(*self.ids)
        for _ in range(6):
            self.step()
        self.assertEqual(self.desired(), path)

    def test_backtracks_when_predecessor_has_no_available_suffix(self):
        self.network()
        self.dead.add(self.b)
        self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (self.now-61, self.b))
        self.prime()
        self.assertEqual(self.desired(), [f'v{self.e}', f'v{self.c}', f't{self.t}'])

    def test_pending_receipts_block_a_second_choice(self):
        self.network()
        self.prime()
        self.tick(self.now)
        chosen = self.config()['desired']
        self.assertTrue(self.db.execute('SELECT 1 FROM jobs').fetchone())
        self.dead.add(self.b)
        for _ in range(10):
            self.step()
            self.tick(self.now)
        self.assertEqual(self.config()['desired'], chosen)
        self.assertEqual(len(json.loads(self.config()['failover'])['attempts']), 1)
        self.assertEqual(self.config()['state'], 'waiting')

    def test_explicit_failure_can_wait_then_try_another_candidate(self):
        self.network()
        self.prime()
        self.tick(self.now)
        self.ack(self.b, False)
        self.tick(self.now)
        self.assertEqual(self.config()['state'], 'error')
        self.dead.add(self.c)
        self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (self.now-61, self.c))
        self.step()
        status = json.loads(self.config()['failover'])
        self.assertEqual(status['phase'], 'unavailable')
        self.assertEqual(len(status['attempts']), 1)
        self.dead.remove(self.c)
        for _ in range(4):
            self.step()
        self.assertEqual(self.desired(), [f'v{self.e}', f'v{self.c}', f't{self.t}'])
        self.assertEqual(len(json.loads(self.config()['failover'])['attempts']), 2)

    def test_manual_pending_work_and_disabled_topologies_are_untouched(self):
        self.network()
        original = self.config()['desired']
        self.db.execute("UPDATE configurations SET state='pending' WHERE id=?", (self.config()['id'],))
        self.prime()
        self.assertEqual(self.config()['desired'], original)
        self.db.execute("UPDATE configurations SET state='active' WHERE id=?", (self.config()['id'],))
        self.db.execute('UPDATE topologies SET failover_enabled=0')
        self.step()
        self.assertEqual(self.config()['desired'], original)

    def test_entry_failure_does_not_trigger_and_new_passive_backup_is_excluded(self):
        self.network()
        self.dead = {self.e}
        self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (self.now-61, self.e))
        self.prime()
        self.assertEqual(self.desired(), self.original)
        self.dead = {self.f}
        self.ready(self.b, self.c, mode='passive')
        self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (self.now-61, self.f))
        for _ in range(4):
            self.step()
        self.assertEqual(self.desired(), self.original)
        self.assertEqual(json.loads(self.config()['failover'])['phase'], 'unavailable')

    def test_candidate_failing_before_preparation_can_be_replaced(self):
        self.network()
        self.prime()
        self.assertFalse(self.db.execute('SELECT 1 FROM jobs').fetchone())
        self.dead.add(self.b)
        self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (self.now-61, self.b))
        self.step()
        self.assertEqual(self.desired(), [f'v{self.e}', f'v{self.c}', f't{self.t}'])

    def test_passive_entry_requires_wake_and_failed_candidates_are_bounded(self):
        self.network()
        self.ready(self.e, mode='passive')
        self.prime()
        self.assertEqual(self.desired(), self.original)
        self.db.execute('UPDATE nodes SET wake_enabled=1,wake_port=35000 WHERE id=?', (self.e,))
        self.step()
        self.assertNotEqual(self.desired(), self.original)
        config = self.config()
        status = json.loads(config['failover'])
        status.update(phase='retry', attempts=[self.original, self.desired(), ['another']])
        self.db.execute("UPDATE configurations SET state='error',failover=? WHERE id=?", (json.dumps(status), config['id']))
        self.step()
        self.assertEqual(json.loads(self.config()['failover'])['phase'], 'paused')

    def test_apply_error_is_not_runtime_failure_and_health_requires_stability(self):
        nid = self.node()
        self.ready(nid)
        engine = failover.Engine(100)
        n = dict(database.get_node(self.db, nid), state='error', runtime_state='unknown')
        for now in range(100, 141, 10):
            bad, good = engine.health({nid: dict(n, last_seen=now, runtime_at=now)}, now)
        self.assertNotIn(nid, bad)
        n.update(runtime_state='error', runtime_error='missing rules')
        for now in range(150, 181, 10):
            bad, good = engine.health({nid: dict(n, last_seen=now, runtime_at=now)}, now)
            self.assertEqual(nid in bad, now == 180)
        n.update(state='ok', runtime_state='ok')
        for now in range(190, 221, 10):
            bad, good = engine.health({nid: dict(n, last_seen=now, runtime_at=now)}, now)
            self.assertEqual(nid in good, now == 220)
        bad, good = engine.health({nid: dict(n, last_seen=280, runtime_at=280)}, 280)
        self.assertNotIn(nid, good)  # A reporting gap resets recovery stability.
        n.update(capabilities=json.dumps(['forwarding_health']), runtime_state='unknown')
        for now in range(300, 341, 10):
            bad, good = engine.health({nid: dict(n, last_seen=now, runtime_at=now)}, now)
        self.assertNotIn(nid, bad)
        self.assertNotIn(nid, good)  # Unknown inspection is neither a fault nor a verified backup.
