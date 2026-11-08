import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
from test_server import Fixture, ROOT
import agent_v3, database, errors, migrations, node_sync, security, settings


class MigrationTests(Fixture, unittest.TestCase):
    def old(self):
        self.db.close()
        Path(settings.DB_PATH).unlink()
        c = sqlite3.connect(settings.DB_PATH)
        c.row_factory = sqlite3.Row
        c.executescript((ROOT / "tests/fixtures/0.5.3.sql").read_text())
        c.execute(
            "INSERT INTO item_groups(id,kind,name,builtin,sort) VALUES (9,'nodes','my nodes',1,5)"
        )
        c.execute(
            "INSERT INTO nodes(id,name,addr,key,group_id,sort,agent,installed_agent,desired_generation,applied_generation,desired_mode,auto_update,wake_enabled,wake_port,installation_id) VALUES (7,'N','198.18.0.7',?,9,19,'0.5.3','active',8,8,'active',1,0,39001,?)",
            ("ab" * 32, "cd" * 16),
        )
        addresses = [
            dict(
                id="main", name="", host="198.18.0.7", primary=True, local="", device="", gateway=""
            ),
            dict(
                id="lan",
                name="LAN",
                host="10.0.0.7",
                primary=False,
                local="10.0.0.7",
                device="eth0",
                gateway="",
            ),
        ]
        c.execute("INSERT INTO node_addresses VALUES (?,?)", (7, json.dumps(addresses)))
        c.execute("INSERT INTO retired_ports VALUES (7,10001)")
        c.execute(
            "INSERT INTO agent_switches VALUES (7,?,'active',8,'old','v=old',NULL,?)",
            ("ef" * 16, int(time.time())),
        )
        for k, v in dict(
            sync_path="/old-sync",
            panel_path="/old-panel",
            admin_user="owner",
            admin_hash=security.hash_password("secret-password"),
            must_change="0",
            public_url="https://panel.example",
            next_node_id="10",
            next_hop_id="901",
        ).items():
            database.set_setting(c, k, v)
        c.commit()
        c.close()

    def test_only_nodes_identity_settings_and_cleanup_survive_once(self):
        self.old()
        self.db = migrations.init_db()
        n = database.get_node(self.db, 7)
        self.assertEqual(
            (n["name"], n["key"], n["sort"], n["desired_generation"], n["auto_update"]),
            ("N", "ab" * 32, 19, 9, 1),
        )
        self.assertEqual(
            len(json.loads(self.db.execute("SELECT addresses FROM node_addresses").fetchone()[0])),
            2,
        )
        self.assertEqual(database.setting(self.db, "sync_path"), "/old-sync")
        self.assertEqual(database.setting(self.db, "admin_user"), "owner")
        self.assertTrue(
            security.verify_password("secret-password", database.setting(self.db, "admin_hash"))
        )
        for table in ("targets", "configurations", "topologies", "subscriptions", "probe_samples"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertTrue(self.db.execute("SELECT 1 FROM migration_nodes WHERE node_id=7").fetchone())
        self.assertEqual(self.db.execute("SELECT port FROM retired_ports").fetchone()[0], 10001)
        self.assertTrue(Path(settings.DB_PATH + ".pre-0.6.0.bak").is_file())
        self.db.close()
        self.db = migrations.init_db()
        self.assertEqual(database.get_node(self.db, 7)["desired_generation"], 9)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failed_import_leaves_old_database_intact(self):
        self.old()
        with patch.object(migrations, "import_nodes", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                migrations.init_db()
        self.db = database.db()
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 504)
        self.assertEqual(database.get_node(self.db, 7)["key"], "ab" * 32)
        self.assertFalse(Path(settings.DB_PATH + ".importing").exists())

    def test_existing_manager_transaction_can_advance_after_upgrade(self):
        self.old()
        self.db = migrations.init_db()
        for action in ("commit", "status"):
            with self.db:
                out = agent_v3.manage(
                    self.db,
                    database.get_node(self.db, 7),
                    dict(t=str(int(time.time())), manage=action, op="ef" * 16, target="active"),
                )
            self.assertIn("result=changed", out)
        self.ack(7)
        self.assertFalse(self.db.execute("SELECT 1 FROM migration_nodes").fetchone())
        self.assertFalse(self.db.execute("SELECT 1 FROM retired_ports").fetchone())

    def test_unsupported_schema_fails_without_migrating(self):
        with self.db:
            self.db.execute("PRAGMA user_version=300")
        self.db.close()
        with self.assertRaises(errors.UserError):
            migrations.init_db()
        self.db = database.db()
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 300)
