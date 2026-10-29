"""Different bar resolutions, accurate history cutover and independent retention."""
import secrets
import unittest
from unittest.mock import patch
from test_server import Fixture, s


class StatusBarTests(Fixture, unittest.TestCase):
    NOW = 1_800_000_000  # Aligned to both 20- and 60-second windows.

    def setUp(self):
        clock = patch.object(s.time, 'time', return_value=self.NOW)
        clock.start()
        self.addCleanup(clock.stop)
        super().setUp()

    def report(self, nid, offset, source='auto', state='ok', op=None):
        now = self.NOW + offset
        data = dict(t=str(now), state=state, v='', agent='0.2.0', au='0', proto='2',
                    source=source, phase='begin' if source == 'manual' else 'poll',
                    op=op or secrets.token_hex(16), mode='active', generation='0')
        with self.db:
            return s.sync_report(self.db, s.get_node(self.db, nid), data, '127.0.0.1', now)

    def test_active_20_second_buckets_preserve_faults_and_show_missing_reports(self):
        nid = self.node()
        for offset, state in ((1, 'ok'), (5, 'error'), (19, 'ok'), (20, 'ok'), (60, 'ok')):
            self.report(nid, offset, state=state)
        bar = s.node_bar(self.db, s.get_node(self.db, nid), self.NOW + 61)
        self.assertEqual(bar['window'], 20)
        self.assertEqual(len(bar['levels']), 60)
        self.assertEqual(bar['window'] * len(bar['levels']), 1200)
        self.assertEqual(bar['start'] + 59 * 20, self.NOW + 60)
        self.assertEqual(bar['levels'][-4:], ['fault', 'ok', 'lost', 'ok'])
        self.assertEqual(set(bar['levels'][:-4]), {'none'})

    def test_passive_minute_counts_survive_mode_switches_and_exclude_retries(self):
        nid = self.node()
        self.call('api_node_edit', {'desired_mode': 'passive'}, nid)
        token = secrets.token_hex(16)
        self.report(nid, 1, 'manual')
        self.report(nid, 10)  # The automatic pause request is not manual activity.
        self.report(nid, 25, 'manual')
        self.report(nid, 45, 'manual', op=token)
        self.report(nid, 46, 'manual', op=token)
        bar = s.node_bar(self.db, s.get_node(self.db, nid), self.NOW + 61)
        self.assertEqual((bar['window'], len(bar['levels'])), (60, 60))
        self.assertEqual(bar['levels'][-2:], ['ok', 'white'])
        self.assertEqual(bar['counts'][-2:], [3, 0])
        self.assertEqual(bar['times'][-2:], [self.NOW + 45, None])
        self.call('api_node_edit', {'desired_mode': 'active'}, nid)
        self.assertEqual(s.node_bar(self.db, s.get_node(self.db, nid), self.NOW + 61)['window'], 20)
        self.call('api_node_edit', {'desired_mode': 'passive'}, nid)
        self.assertEqual(s.node_bar(self.db, s.get_node(self.db, nid), self.NOW + 61), bar)

    def test_existing_300_history_is_retained_without_inventing_fine_samples(self):
        nid = self.node()
        with self.db:
            self.db.execute('DROP TABLE active_windows')
            self.db.execute("DELETE FROM settings WHERE k='active_window_start'")
            self.db.execute('UPDATE nodes SET first_seen=?,last_seen=? WHERE id=?',
                            (self.NOW - 1000, self.NOW - 1, nid))
            self.db.execute('INSERT INTO windows VALUES (?,?,1,2,?)',
                            (nid, self.NOW // 60 - 1, self.NOW - 1))
        old = [tuple(r) for r in self.db.execute('SELECT * FROM windows')]
        self.db.close()
        with patch.object(s.time, 'time', return_value=self.NOW + 7):
            self.db = s.init_db()
        bar = s.node_bar(self.db, s.get_node(self.db, nid), self.NOW + 8)
        self.assertEqual(set(bar['levels']), {'none'})
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM windows')], old)
        self.report(nid, 21)
        bar = s.node_bar(self.db, s.get_node(self.db, nid), self.NOW + 61)
        self.assertEqual(bar['levels'][-4:], ['none', 'ok', 'lost', 'none'])
        rows = [tuple(r) for r in self.db.execute('SELECT * FROM active_windows')]
        self.db.close()
        self.db = s.init_db()
        self.assertEqual(s.setting(self.db, 'active_window_start'), str(self.NOW + 7))
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM active_windows')], rows)
        self.call('api_node_edit', {'desired_mode': 'passive'}, nid)
        bar = s.node_bar(self.db, s.get_node(self.db, nid), self.NOW + 61)
        self.assertEqual(sum(bar['counts']), 2)
        self.assertEqual(bar['levels'].count('ok'), 1)

    def test_retention_and_node_deletion_cover_both_resolutions(self):
        nid = self.node()
        for table, width in (('windows', 60), ('active_windows', 20)):
            cutoff = self.NOW // width
            with self.db:
                self.db.execute(f'INSERT INTO {table}(node_id,win,level) VALUES (?,?,2)', (nid, cutoff - 1))
                self.db.execute(f'INSERT INTO {table}(node_id,win,level) VALUES (?,?,2)', (nid, cutoff))
        with patch.object(s.time, 'time', return_value=self.NOW + s.RETENTION):
            s.cleanup(self.db)
        for table, width in (('windows', 60), ('active_windows', 20)):
            self.assertEqual([r[0] for r in self.db.execute(f'SELECT win FROM {table}')], [self.NOW // width])
        self.call('api_node_del', {}, nid)
        for table in ('windows', 'active_windows'):
            self.assertFalse(self.db.execute(f'SELECT 1 FROM {table}').fetchone())
