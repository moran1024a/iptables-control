import json
import secrets
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from test_server import Fixture, ROOT, fields
from test_imports import reality_uri, ss_uri
import configurations, database, migrations, node_sync, proxy_formats, settings, target_config


class RC7MigrationTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        groups = [tuple(r) for r in self.db.execute("SELECT * FROM item_groups WHERE kind!='external_configurations'")]
        values = [tuple(r) for r in self.db.execute('SELECT * FROM settings')]
        self.db.close()
        Path(settings.DB_PATH).unlink()
        self.db = database.db()
        self.db.executescript((ROOT/'tests/fixtures/0.6.0-rc.6.sql').read_text())
        self.db.executemany('INSERT INTO item_groups VALUES (?,?,?,?,?)',groups)
        self.db.executemany('INSERT INTO settings VALUES (?,?)',values)
        self.db.execute('PRAGMA user_version=602')
        self.db.commit()

    def reality(self):
        parsed = proxy_formats.parse(reality_uri())
        p = parsed['params']
        q = dict(p['share_query'])
        p.update(server_name=q['sni'],public_key=q['pbk'],short_id=q['sid'])
        with self.db:
            return self.db.execute("INSERT INTO targets(name,type,host,port,params,group_id) VALUES ('Reality','vless-reality',?,?,?,?)",
                                   (parsed['host'],parsed['port'],json.dumps(p),database.default_group(self.db,'targets'))).lastrowid

    def upgrade(self):
        self.db.close()
        self.db = migrations.init_db()

    def test_live_multi_target_cleanup_preserves_other_configuration_and_samples(self):
        a,b=self.node('entry'),self.node('middle');self.ready(a,b)
        real=self.reality();ss=self.call('api_target_save',dict(uri=ss_uri()))['id']
        graph=self.graph([a,b],real)
        graph['vertices'] += [dict(id='modifier',kind='direct',weight=0),dict(id=f't{ss}',kind='target',target_id=ss)]
        graph['edges'] = [[f'v{a}',f'v{b}'],[f'v{b}','modifier'],['modifier',f't{real}'],[f'v{b}',f't{ss}']]
        topo=self.save(graph);self.settle()
        configs=configurations.members(self.db,topo)
        doomed=next(x for x in configs if json.loads(x['desired'])['target']==real)
        keep=next(x for x in configs if x['id']!=doomed['id'])
        rules=[dict(r) for r in self.db.execute('SELECT * FROM rules WHERE configuration_id=?',(keep['id'],))]
        retired={(r['node_id'],r['port']) for r in self.db.execute('SELECT * FROM rules WHERE configuration_id=?',(doomed['id'],))}
        with self.db:
            self.db.execute("INSERT INTO subscriptions VALUES (1,'test','token',1)")
            self.db.executemany('INSERT INTO subscription_configurations VALUES (1,?)',[(doomed['id'],),(keep['id'],)])
            self.db.execute("INSERT INTO probe_tasks VALUES ('shared','{}')")
            self.db.execute("INSERT INTO probe_samples(task_id,slot,status,rtt) VALUES ('shared',1,'ok',5)")
            for config in configs:
                self.db.execute("INSERT INTO probe_bindings(scope,owner_id,section,protocol,task_id,path_key,path_label,status,note,started) VALUES ('topologies',?,?,'tcp-connect-v1','shared','path','label','ok','',1)",(topo,f"config:{config['id']}:full"))
        self.upgrade()
        self.assertIsNone(self.db.execute('SELECT 1 FROM targets WHERE id=?',(real,)).fetchone())
        self.assertEqual([dict(r) for r in self.db.execute('SELECT * FROM rules')],rules)
        self.assertEqual(dict(self.db.execute('SELECT * FROM configurations').fetchone()),keep)
        self.assertEqual({tuple(r) for r in self.db.execute('SELECT * FROM retired_ports')},retired)
        newgraph=json.loads(configurations.get_topology(self.db,topo)['graph'])
        self.assertNotIn('modifier',[v['id'] for v in newgraph['vertices']])
        self.assertEqual(self.call('api_configs')['configs'][0]['id'],keep['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM probe_samples').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM probe_bindings WHERE ended IS NULL').fetchone()[0],1)
        for nid in (a,b):self.ack(nid)
        self.assertFalse(self.db.execute('SELECT 1 FROM retired_ports').fetchone())
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.upgrade()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM item_groups WHERE kind="external_configurations"').fetchone()[0],1)

    def test_each_inflight_phase_keeps_all_port_receipts(self):
        for phase in ('prepare','commit','rollback'):
            with self.subTest(phase=phase):
                # Every subcase starts from the same actual RC6 schema.
                if phase!='prepare':
                    self.tearDown();self.setUp()
                a,b=self.node(),self.node();self.ready(a,b)
                real=self.reality();topo=self.save(self.graph([a,b],real));self.tick()
                job=self.db.execute('SELECT * FROM jobs').fetchone()
                payload=json.loads(job['payload'])
                payload['old']=[dict(payload['new'][0],id=9999,port=10099)]
                with self.db:
                    self.db.execute('UPDATE jobs SET phase=?,payload=?',(phase,json.dumps(payload)))
                wanted={(r['node_id'],r['port']) for r in payload['new']+payload['old']}
                self.upgrade()
                for table in ('targets','topologies','configurations','jobs','rules'):
                    self.assertEqual(self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
                self.assertEqual({tuple(r) for r in self.db.execute('SELECT * FROM retired_ports')},wanted)
                self.assertTrue(Path(settings.DB_PATH+'.pre-0.6.0-rc.7.bak').is_file())

    def test_passive_late_ack_cannot_release_cleanup_ports(self):
        a=self.node();self.ready(a,mode='passive')
        real=self.reality();self.save(self.graph([a],real));self.settle()
        data=dict(t=str(int(time.time())),proto='2',source='manual',phase='begin',op=secrets.token_hex(16),
                  agent=settings.VERSION,mode='passive',state='ok',au='1')
        with self.db:
            result=fields(node_sync.sync_report(self.db,database.get_node(self.db,a),data,'203.0.113.1'))
        data.update({k:result[k] for k in ('op','generation','mode','v')},phase='ack')
        self.upgrade()
        with self.db:
            reply=node_sync.sync_report(self.db,database.get_node(self.db,a),data,'203.0.113.1')
        self.assertIn('result=changed',reply)
        self.assertTrue(self.db.execute('SELECT 1 FROM retired_ports').fetchone())
        self.assertEqual(database.get_node(self.db,a)['desired_mode'],'passive')
        self.ack(a)
        self.assertFalse(self.db.execute('SELECT 1 FROM retired_ports').fetchone())

    def test_new_valid_intent_survives_old_reality_snapshot(self):
        a=self.node();self.ready(a)
        real=self.reality();topo=self.save(self.graph([a],real));self.settle()
        old=configurations.members(self.db,topo)[0]
        parsed=target_config.import_uri(ss_uri())
        with self.db:
            self.db.execute("UPDATE targets SET type='shadowsocks',params=? WHERE id=?",(json.dumps(parsed['params']),real))
        self.upgrade()
        current=configurations.members(self.db,topo)[0]
        self.assertEqual((current['id'],current['desired'],current['name']),(old['id'],old['desired'],old['name']))
        self.assertIsNone(current['applied'])
        self.assertEqual(current['state'],'pending')
        self.settle()
        self.assertTrue(self.call('api_configs')['configs'][0]['connection']['uri'].startswith('ss://'))

    def test_migration_failure_is_atomic(self):
        real=self.reality()
        self.db.close()
        with patch.object(node_sync,'finish_mutation',side_effect=RuntimeError('test rollback')):
            with self.assertRaises(RuntimeError):migrations.init_db()
        self.db=database.db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],602)
        self.assertEqual(database.get_target(self.db,real)['type'],'vless-reality')
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='external_configurations'").fetchone())
        self.upgrade()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],603)
