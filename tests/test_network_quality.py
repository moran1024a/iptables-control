import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import database
import errors
import forwarding
import migrations
import settings
"""RC2 pair selection, history semantics, migration and WSS probe scheduling."""
import json
from pathlib import Path
import time
import unittest

from test_server import Fixture
import network_quality as q
import transport


class HistoryTests(unittest.TestCase):
    def test_sparse_dispatched_samples_remain_continuous_and_week_is_bounded(self):
        rows = [dict(self.row(t, rtt=20+t%3), epoch='run', previous_slot=t-28 if t else None)
                for t in range(0, q.RETENTION+1, 28)]
        for seconds in q.HISTORY_STEPS:
            data = q.summarize(rows, seconds, q.RETENTION)
            good = [p for p in data['points'] if p['mean'] is not None]
            self.assertTrue(all(p['connect'] and not p['gap'] for p in good[1:]))
            self.assertIsNotNone(data['summary']['jitter'])
        self.assertEqual(len(q.summarize(rows, q.RETENTION, q.RETENTION)['points']), 337)
        broken = [dict(self.row(100), epoch='first', previous_slot=None),
                  dict(self.row(128), epoch='second', previous_slot=100)]
        self.assertFalse(q.continuous(*broken))
        broken[1].update(epoch='first', previous_slot=114)
        self.assertFalse(q.continuous(*broken))

    def test_many_path_changes_cannot_expand_the_week_without_bound(self):
        rows = [self.row(t, path=str(t)) for t in range(0, q.RETENTION+1, 10)]
        data = q.summarize(rows, q.RETENTION, q.RETENTION)
        self.assertLessEqual(len(data['points']), 337*16)
        self.assertTrue(data['points'][0]['endpoint_change'])
        self.assertTrue(data['points'][0]['gap'])

    @staticmethod
    def row(slot, rtt=20, path='same', address='192.0.2.1', status='ok'):
        return dict(slot=slot, rtt=rtt if status == 'ok' else None, path_key=path,
                    path_label=path, resolved_ip=address, status=status, error='probe failed' if status != 'ok' else '')

    def test_delayed_successful_samples_do_not_break_hour_or_day_charts(self):
        for seconds in q.HISTORY_STEPS:
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
                self.assertEqual([p['end'] for p in points], [3550, 3560, 3580])

    def test_partial_failure_is_preserved_after_a_successful_sample(self):
        rows = [self.row(3540), self.row(3550, address='', status='timeout'), self.row(3560, 30)]
        data = q.summarize(rows, 3600, 3600)
        points = [p for p in data['points'] if p['first_slot'] is not None]
        self.assertEqual([p['status'] for p in points], ['ok', 'timeout', 'ok'])
        self.assertEqual([p['mean'] for p in points], [20, None, 30])
        self.assertEqual([p['gap'] for p in points], [False, True, False])
        self.assertEqual([p['end'] for p in points], [3550, 3560, 3580])
        self.assertEqual(points[1]['error'], 'probe failed')
        self.assertEqual((data['summary']['samples'], data['summary']['lost'], data['summary']['mean']), (3, 1, 25))

    def test_boundary_bucket_excludes_samples_outside_visible_window(self):
        data = q.summarize([self.row(0, 1000), self.row(10, 20), self.row(20, 30), self.row(3610, 2000)], 3600, 3605)
        points = [p for p in data['points'] if p['mean'] is not None]
        self.assertEqual(len(points), 1)
        self.assertEqual((points[0]['time'], points[0]['mean'], points[0]['peak']), (10, 25, 30))
        self.assertTrue(all(5 <= p['time'] <= 3605 for p in data['points']))
        self.assertEqual(data['latest']['slot'], 20)

    def test_real_outage_splits_a_bucket(self):
        points = [p for p in q.summarize([self.row(3540, 10), self.row(3580, 30)], 3600, 3600)['points'] if p['mean'] is not None]
        self.assertEqual([p['mean'] for p in points], [10, 30])
        self.assertEqual(points[1]['first_slot'] - points[0]['last_slot'], 40)
        self.assertEqual(points[0]['end'], 3560)

    def test_week_aggregates_healthy_runs_and_retains_sparse_failures(self):
        rows = [self.row(t, rtt=100 if t == 150 else 20) for t in range(0, q.RETENTION + 1, 10)]
        data = q.summarize(rows, q.RETENTION, q.RETENTION)
        self.assertEqual(data['step'], 1800)
        self.assertEqual(len(data['points']), 337)
        self.assertEqual(data['points'][0]['peak'], 100)
        # One timeout per five minutes must not invalidate the surrounding runs.
        rows = [self.row(t, status='timeout' if t % 300 == 150 else 'ok') for t in range(0, 3600, 10)]
        data = q.summarize(rows, q.RETENTION, 3600)
        good = [p for p in data['points'] if p['mean'] is not None]
        self.assertTrue(all(not p['gap'] and p['last_slot'] > p['first_slot'] for p in good))
        self.assertEqual(sum(p['lost'] for p in data['points']), 12)






if __name__ == '__main__':
    unittest.main()
