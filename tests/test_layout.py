"""Precise destructive migration, rollback, IDs and independent object grouping."""
import json
import os
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path
from test_server import Fixture, s
from test_imports import ss_uri, reality_uri


class MigrationTests(Fixture, unittest.TestCase):
    def legacy(self, version=202):
        self.db.close(); os.unlink(s.DB_PATH)
        c=s.init_legacy_db()
        self.db=c
        ng=s.default_group(c,'nodes');tg=s.default_group(c,'tunnel_nodes');cg=s.default_group(c,'chains')
        c.execute("INSERT INTO nodes(id,name,addr,key,first_seen,last_seen,group_id) VALUES (10,'F','192.0.2.1',?,1,1,?)",('ab'*32,ng))
        c.execute("INSERT INTO tunnel_nodes(id,name,addr,key,first_seen,group_id,kind) VALUES (20,'T','192.0.2.2',?,1,?,'tunnel')",('cd'*32,tg))
        rg=s.default_group(c,'routes');gg=s.default_group(c,'tunnels')
        c.execute("INSERT INTO routes(id,name,node_ids,group_id) VALUES (1,'kept route','[10]',?)",(rg,))
        for tid,proto in enumerate(('shadowsocks','vless-reality','socks5'),1):
            c.execute("INSERT INTO tunnels(id,name,node_id,protocol,port,params,group_id) VALUES (?, 'managed',20,?,?,'{}',?)",(tid,proto,11000+tid,gg))
            c.execute("INSERT INTO chains(id,remark,target_host,target_port,group_id,route_id,tunnel_id) VALUES (?,'removed','192.0.2.2',?,?,1,?)",(tid,11000+tid,cg,tid))
            c.execute('INSERT INTO hops VALUES (?,?,0,10,?)',(tid,tid,10000+tid))
        if version==202:
            for tid,uri in ((8,ss_uri()),(9,reality_uri())):
                t=s.target_config.import_uri(uri)
                c.execute("INSERT INTO tunnels(id,name,protocol,port,params,source,external_host,group_id) VALUES (?,?,?,?,?,'external',?,?)",(tid,t['name'],t['protocol'],t['port'],json.dumps(t['params']),t['host'],gg))
                c.execute("INSERT INTO chains(id,remark,target_host,target_port,group_id,route_id,tunnel_id) VALUES (?,'kept',?,?,?,1,?)",(tid,t['host'],t['port'],cg,tid))
                c.execute('INSERT INTO hops VALUES (?,?,0,10,?)',(tid,tid,10000+tid))
        for cid in (11,12):
            c.execute("INSERT INTO chains(id,remark,target_host,target_port,group_id,route_id) VALUES (?,'inline','example.com',443,?,1)",(cid,cg))
            c.execute('INSERT INTO hops VALUES (?,?,0,10,?)',(cid,cid,10000+cid))
        c.execute('INSERT INTO retired_ports VALUES (10,10019)');c.execute('INSERT INTO retired_ports VALUES (20,10020)')
        c.execute('INSERT INTO tunnel_presence VALUES (20,1,30)');c.execute('INSERT INTO windows VALUES (20,1,2)')
        s.set_setting(c,'next_hop_id','20');s.set_setting(c,'next_node_id','21')
        before=s.legacy_node_rules(c,s.get_node(c,10));c.commit()
        if version==201:
            # Rebuild the legacy non-null node table exactly as 201.
            c.execute('PRAGMA foreign_keys=OFF')
            c.executescript('''CREATE TABLE previous_tunnels(id INTEGER PRIMARY KEY,name TEXT NOT NULL,node_id INTEGER NOT NULL REFERENCES tunnel_nodes(id),protocol TEXT NOT NULL,port INTEGER NOT NULL,params TEXT NOT NULL,deleting INTEGER NOT NULL DEFAULT 0,group_id INTEGER NOT NULL DEFAULT 0,sort INTEGER NOT NULL DEFAULT 0,UNIQUE(node_id,port));
                INSERT INTO previous_tunnels SELECT id,name,node_id,protocol,port,params,deleting,group_id,sort FROM tunnels;
                DROP TABLE tunnels; ALTER TABLE previous_tunnels RENAME TO tunnels; PRAGMA user_version=201;''')
        return c,before

    def test_202_mixed_migration_exact_scope_and_idempotence(self):
        old,before=self.legacy();secret=old.execute('SELECT params FROM tunnels WHERE id=8').fetchone()[0]
        old.close();self.db=s.init_db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],300)
        self.assertEqual(s.node_rules(self.db,s.get_node(self.db,10)),before[3:])
        self.assertEqual(s.get_target(self.db,8)['params'],secret)
        self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM chains ORDER BY id')],[8,9,11,12])
        self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM hops ORDER BY id')],[8,9,11,12])
        self.assertEqual({r[0] for r in self.db.execute('SELECT port FROM retired_ports WHERE node_id=10')},{10001,10002,10003,10019})
        self.assertEqual(s.get_node(self.db,10)['desired_mode'],'active')
        self.assertNotEqual(s.get_chain(self.db,11)['target_id'],s.get_chain(self.db,12)['target_id'])
        self.assertFalse(self.db.execute("SELECT name FROM sqlite_master WHERE name IN ('tunnels','tunnel_nodes','tunnel_presence','all_nodes')").fetchall())
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())
        backup=Path(s.DB_PATH+'.pre-0.3.0.bak');data=backup.read_bytes();self.assertEqual(backup.stat().st_mode&0o777,0o600)
        self.db.close();self.db=s.init_db();self.assertEqual(backup.read_bytes(),data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM targets').fetchone()[0],4)

    def test_201_migration_path(self):
        c,_=self.legacy(201);c.close();self.db=s.init_db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],300)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM targets').fetchone()[0],2)

    def test_200_preserves_unchanged_applied_forwarding_and_removes_tunnel_nodes(self):
        self.db.close();os.unlink(s.DB_PATH)
        c=sqlite3.connect(s.DB_PATH);c.row_factory=sqlite3.Row;c.executescript(s.SCHEMA)
        for kind in ('nodes','chains'):
            c.execute('INSERT INTO item_groups(kind,name,builtin) VALUES (?,?,1)',(kind,'default'))
        c.execute("INSERT INTO nodes(id,name,addr,key,first_seen,last_seen,group_id,state) VALUES (10,'F','192.0.2.1',?,1,1,1,'ok')",('ab'*32,))
        c.execute("INSERT INTO nodes(id,name,addr,key,group_id) VALUES (20,'T','192.0.2.2',?,1)",('cd'*32,))
        c.execute("INSERT INTO chains(id,remark,target_host,target_port,group_id) VALUES (9,'kept','example.com',443,2)")
        c.execute('INSERT INTO hops VALUES (99,9,0,10,12345)')
        before=s.legacy_node_rules(c,c.execute('SELECT * FROM nodes WHERE id=10').fetchone())
        c.execute('UPDATE nodes SET reported_v=? WHERE id=10',(s.rules_version(before),));c.commit()
        s.migrate_v020(c);c.execute("UPDATE nodes SET kind='tunnel' WHERE id=20")
        c.execute("INSERT INTO tunnels(id,name,node_id,protocol,port,params) VALUES (6,'managed',20,'shadowsocks',12346,'{}')");c.commit();c.close()
        self.db=s.init_db();n=s.get_node(self.db,10)
        self.assertEqual(s.node_rules(self.db,n),before)
        self.assertEqual(n['desired_generation'],n['applied_generation'])
        self.assertEqual(s.get_chain(self.db,9)['remark'],'kept')
        self.assertFalse(self.db.execute('SELECT 1 FROM nodes WHERE id=20').fetchone())
        self.assertGreater(self.node('new'),20)

    def test_failure_rolls_back_202_and_keeps_foreign_keys(self):
        c,_=self.legacy()
        with patch.object(s,'node_rules',return_value=['wrong']):
            with self.assertRaises(s.UserError):s.migrate_v030(c)
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],202)
        self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM tunnels').fetchone()[0],5)
        self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name='targets'").fetchone())

    def test_info_never_creates_or_migrates(self):
        c,_=self.legacy();c.close()
        with patch('builtins.print'):
            s.cmd_info(None)
        self.db=sqlite3.connect(s.DB_PATH)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],202)


class LayoutTests(Fixture, unittest.TestCase):
    def test_each_scope_groups_orders_and_moves_without_rule_changes(self):
        a,b=self.node(),self.node('B');t,u=self.target(),self.target();r,q=self.route([a]),self.route([b]);c,d=self.config(r,target_id=t),self.config(q,target_id=u)
        before=s.mutation_snapshot(self.db)
        for kind,ids,method in [('nodes',[a,b],'api_node_edit'),('targets',[t,u],'api_target_save'),('routes',[r,q],'api_route_save'),('chains',[c,d],'api_config_save')]:
            default=s.default_group(self.db,kind);gid=self.call('api_group_add',dict(kind=kind,name='same'))['id']
            self.call(method,{'group_id':gid},ids[0])
            self.call('api_order',dict(groups=[gid,default],ids=ids[::-1]),kind)
            self.assertEqual(s.get_groups(self.db,kind)[0]['id'],gid)
            with self.assertRaises(s.UserError):self.call('api_order',dict(groups=[gid,default],ids=[ids[0],ids[0]]),kind)
            self.call('api_group_del',{},gid)
        after=s.mutation_snapshot(self.db)
        for nid in (a,b):
            self.assertEqual(before[nid][1:],after[nid][1:])
            self.assertEqual(before[nid][0]['desired_generation'],after[nid][0]['desired_generation'])

    def test_only_single_node_type_and_target_references(self):
        with self.assertRaises(s.UserError):self.node(kind='tunnel')
        n=self.node();r=self.route([n]);t=self.target();c=self.config(r,target_id=t)
        with self.assertRaises(s.UserError):self.call('api_target_del',{},t)
        with self.assertRaises(s.UserError):self.call('api_node_del',{},n)
        with self.assertRaises(s.UserError):self.call('api_config_save',dict(remark='inline',target_host='example.com',target_port=443))
        with self.assertRaises(s.UserError):self.call('api_config_save',dict(remark='direct',target_id=t,ports=[123]))
        self.assertEqual(self.call('api_config_save',dict(remark='direct',target_id=t))['assigned'],[])
