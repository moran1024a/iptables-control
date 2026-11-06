"""Transaction boundaries and in-place upgrade of a populated 0.4.6 database."""
import sqlite3
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import Fixture
import migrations
import node_sync
import probe_store
import settings


class ModularityTests(Fixture, unittest.TestCase):
    def test_outer_rollback_includes_resource_rules_generation_and_waits(self):
        nid = self.node()
        self.config(self.route([nid]), [10001])
        before = node_sync.mutation_snapshot(self.db)
        waits = list(self.db.execute('SELECT * FROM chain_pending'))
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.h.api_node_edit(self.db, dict(addr='203.0.113.55'), nid)
                self.assertTrue(self.db.in_transaction)
                raise RuntimeError('rollback')
        self.assertEqual(node_sync.mutation_snapshot(self.db), before)
        self.assertEqual(list(self.db.execute('SELECT * FROM chain_pending')), waits)
        with patch.object(node_sync, 'finish_mutation', side_effect=RuntimeError('version failure')):
            with self.assertRaisesRegex(RuntimeError, 'version failure'):
                self.call('api_node_edit', dict(addr='203.0.113.55'), nid)
        self.assertEqual(node_sync.mutation_snapshot(self.db), before)

    def test_404_upgrade_backup_cutoff_idempotence_and_resources(self):
        nid = self.node()
        self.config(self.route([nid]), [10001])
        before = node_sync.mutation_snapshot(self.db)
        with self.db:
            for table in ('probe_bindings', 'probe_samples', 'probe_tasks'):
                self.db.execute('DROP TABLE ' + table)
            self.db.execute("DELETE FROM settings WHERE k='probe_cutover'")
            self.db.execute('PRAGMA user_version=404')
        backup = Path(settings.DB_PATH + '.pre-0.4.7.bak')
        backup.unlink()  # replace the empty fixture's backup with this populated upgrade
        self.db.close()
        now = int(time.time())
        with patch.object(migrations.time, 'time', return_value=now):
            self.db = migrations.init_db()
        self.assertEqual(probe_store.cutoff(self.db), now)
        self.assertEqual(node_sync.mutation_snapshot(self.db), before)
        with sqlite3.connect(backup) as old:
            self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0], 404)
            self.assertEqual(old.execute('SELECT COUNT(*) FROM hops').fetchone()[0], 1)
        self.db.close()
        with patch.object(migrations.time, 'time', return_value=now + 100):
            self.db = migrations.init_db()
        self.assertEqual(probe_store.cutoff(self.db), now)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 405)
