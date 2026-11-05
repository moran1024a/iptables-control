"""0.3.x upgrades retain resources, not obsolete topology or execution history."""
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch

from test_server import Fixture, s


class Upgrade03Tests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.nid = self.node('old node')
        self.tid = self.target()
        self.cid = self.config(self.route([self.nid]), [10001], target_id=self.tid)
        self.route([self.nid], 'unused template')
        with self.db:
            self.db.execute("UPDATE nodes SET first_seen=1,last_seen=2,desired_generation=8,applied_generation=8,state='ok',reported_v='old' WHERE id=?", (self.nid,))
            self.db.execute('INSERT INTO retired_ports VALUES (?,10002)', (self.nid,))
            self.db.execute("INSERT INTO sync_ops VALUES (?,?,'auto',8,'active','old','old rules',9999999999,NULL)", (self.nid, 'a'*32))
            self.db.execute('INSERT INTO windows(node_id,win,level) VALUES (?,1,2)', (self.nid,))
            for table in ('link_events','link_retired','link_rules','links','pools','quality_samples','quality_pairs','wake_jobs',
                          'node_snapshots','traffic_epochs','metric_windows','metric_hours','archived_nodes','agent_switches'):
                self.db.execute('DROP TABLE ' + table)
            for column in ('quality_port','wake_enabled','wake_port','installed_agent','backend','installation_id','boot_id','capabilities','connection_state'):
                self.db.execute('ALTER TABLE nodes DROP COLUMN ' + column)
            self.db.execute('PRAGMA user_version=300')
        self.node_before = dict(s.get_node(self.db, self.nid))
        self.target_before = dict(s.get_target(self.db, self.tid))
        self.settings_before = dict(self.db.execute('SELECT k,v FROM settings'))
        self.groups_before = [tuple(r) for r in self.db.execute("SELECT * FROM item_groups WHERE kind IN ('nodes','targets') ORDER BY id")]
        Path(s.DB_PATH + '.pre-0.4.0.bak').unlink()

    def test_resources_survive_empty_snapshot_clears_old_rules_after_ack(self):
        self.db.close(); self.db = s.init_db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 403)
        node = dict(s.get_node(self.db, self.nid))
        for key in ('id','name','note','addr','key','ports','desired_mode','group_id','sort','auto_update'):
            self.assertEqual(node[key], self.node_before[key])
        self.assertEqual(dict(s.get_target(self.db, self.tid)), self.target_before)
        self.assertEqual([tuple(r) for r in self.db.execute("SELECT * FROM item_groups WHERE kind IN ('nodes','targets') ORDER BY id")], self.groups_before)
        for key in ('admin_user','admin_hash','panel_path','sync_path','next_hop_id'):
            self.assertEqual(s.setting(self.db,key),self.settings_before[key])
        for table in ('chains','hops','routes','links','pools','sync_ops','windows','active_windows','events'):
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)
        self.assertEqual(node['desired_generation'],9)
        self.assertIsNone(node['last_seen'])
        self.assertEqual(s.node_rules(self.db,node),[])
        self.assertEqual({r[0] for r in self.db.execute('SELECT port FROM retired_ports')},{10001,10002})
        self.assertEqual(self.h.api_nodes(self.db,{})['nodes'][0]['status'],'offline')
        before=sqlite3.connect(s.DB_PATH+'.pre-0.4.0.bak')
        self.assertEqual(before.execute('SELECT COUNT(*) FROM chains').fetchone()[0],1)
        before.close()
        fields=dict(t=str(int(time.time())),proto='2',source='auto',phase='poll',op='b'*32,agent='0.2.1',au='0',
                    mode='active',state='ok',generation='8',v='old')
        with self.db:
            out=s.agent_v3.fields(s.sync_report(self.db,s.get_node(self.db,self.nid),fields,'127.0.0.1'))
            self.assertEqual(out['action'],'apply')
            self.assertEqual(out['v'],s.rules_version([]))
            self.assertTrue(self.db.execute('SELECT 1 FROM retired_ports').fetchone())
            fields.update(phase='ack',op=out['op'],generation=out['generation'],v=out['v'])
            receipt=s.agent_v3.fields(s.sync_report(self.db,s.get_node(self.db,self.nid),fields,'127.0.0.1'))
            self.assertEqual(receipt['result'],'confirmed')
        self.assertFalse(self.db.execute('SELECT 1 FROM retired_ports').fetchone())
        # Starting an already-upgraded server must not wipe newly created data.
        self.config(self.route([self.nid]),[10003],target_id=self.tid)
        self.db.close(); self.db=s.init_db()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM chains').fetchone()[0],1)

    def test_failure_rolls_back_resource_reset_and_schema(self):
        reset=s.agent_v3.reset_v03
        def fail(server,conn):
            reset(server,conn)
            raise RuntimeError('injected migration failure')
        self.db.close()
        with patch.object(s.agent_v3,'reset_v03',side_effect=fail):
            with self.assertRaises(RuntimeError): s.init_db()
        self.db=s.db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],300)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM chains').fetchone()[0],1)
        self.assertEqual(dict(s.get_node(self.db,self.nid)),self.node_before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sync_ops').fetchone()[0],1)
