"""Node/group isolation, direct tunnels and migration from the first 0.2.0 schema."""
import json
import os
import sqlite3
import unittest
from unittest.mock import patch
import test_server
from test_server import Fixture, s


class LayoutTests(Fixture, unittest.TestCase):
    def group(self,kind,name='shared name'):
        return self.call('api_group_add',dict(kind=kind,name=name))['id']

    def direct(self,t,**kw):
        return self.call('api_config_save',dict(remark='direct tunnel',route_id=None,target_type='tunnel',tunnel_id=t,**kw))['id']

    def test_physical_tables_and_global_ids(self):
        a=self.node();n=self.node('T','tunnel')
        self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM nodes')],[a])
        self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM tunnel_nodes')],[n])
        self.assertNotEqual(a,n)
        self.h.query={'kind':['forward']};self.assertEqual([x['id'] for x in self.call('api_nodes')['nodes']],[a])
        self.h.query={'kind':['tunnel']};self.assertEqual([x['id'] for x in self.call('api_nodes')['nodes']],[n])
        self.call('api_node_del',{},n);self.assertGreater(self.node('next','tunnel'),n)

    def test_cross_type_groups_rejected_and_deletion_isolated(self):
        f,t=self.group('nodes'),self.group('tunnel_nodes')
        a=self.call('api_node_add',dict(name='A',addr='192.0.2.1',group_id=f))['id']
        n=self.call('api_node_add',dict(name='T',addr='192.0.2.2',kind='tunnel',group_id=t))['id']
        with self.assertRaises(s.UserError):self.call('api_node_edit',{'group_id':f},n)
        with self.assertRaises(s.UserError):self.call('api_node_add',dict(name='X',addr='192.0.2.3',kind='tunnel',group_id=f))
        self.call('api_group_edit',{'name':'changed'},f)
        self.assertEqual(self.h.get_group(self.db,t)['name'],'shared name')
        self.call('api_group_del',{},f)
        self.assertEqual(s.get_node(self.db,a)['group_id'],s.default_group(self.db,'nodes'))
        self.assertEqual(s.get_node(self.db,n)['group_id'],t)

    def test_every_scope_supports_group_sort_and_move(self):
        a,b=self.node(),self.node('B');n,t=self.tunnel();n2=self.node('T2','tunnel')
        t2=self.call('api_tunnel_save',dict(name='t2',node_id=n,protocol='socks5'))['id']
        r1,r2=self.route([a],'r1'),self.route([b],'r2');c1,c2=self.config(r1),self.direct(t)
        before_forward=s.node_rules(self.db,s.get_node(self.db,a))
        before_tunnel=s.tunnel_snapshot(self.db,s.get_node(self.db,n))
        objects={'nodes':[a,b],'tunnel_nodes':[n,n2],'routes':[r1,r2],'tunnels':[t,t2],'chains':[c1,c2]}
        for kind,ids in objects.items():
            gid=self.group(kind);default=s.default_group(self.db,kind)
            if kind in ('nodes','tunnel_nodes'):self.call('api_node_edit',{'group_id':gid},ids[0])
            elif kind=='routes':self.call('api_route_save',{'group_id':gid},ids[0])
            elif kind=='tunnels':self.call('api_tunnel_save',{'name':'t','group_id':gid},ids[0])
            else:self.call('api_config_save',dict(remark='c',route_id=r1,target_type='direct',target_host='198.51.100.1',target_port=443,group_id=gid),ids[0])
            self.call('api_order',{'groups':[gid,default],'ids':ids[::-1]},kind)
            self.assertEqual(s.get_groups(self.db,kind)[0]['id'],gid)
            with self.assertRaises(s.UserError):self.call('api_order',{'groups':[gid,default],'ids':[ids[0],ids[0]]},kind)
            self.call('api_group_del',{},gid)
            self.assertEqual(self.db.execute(f'SELECT group_id FROM {kind} WHERE id=?',(ids[0],)).fetchone()[0],default)
        self.assertEqual(before_forward,s.node_rules(self.db,s.get_node(self.db,a)))
        self.assertEqual(before_tunnel,s.tunnel_snapshot(self.db,s.get_node(self.db,n)))

    def test_route_and_tunnel_lists_follow_saved_group_and_item_order(self):
        a=self.node();r1,r2,r3=[self.route([a],f'r{i}') for i in range(3)]
        for kind,ids in [('routes',[r1,r2,r3]),('tunnels',[self.tunnel()[1] for _ in range(3)])]:
            self.call('api_order',dict(groups=[s.default_group(self.db,kind)],ids=ids[::-1]),kind)
            data=self.call('api_'+kind)
            self.assertEqual([r['id'] for r in data[kind]],ids[::-1])
            self.assertIn('groups',data)

    def test_direct_without_forward_nodes_allocates_no_hops(self):
        n,t=self.tunnel('socks5',port=10003);c=self.direct(t)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM hops').fetchone()[0],0)
        data=self.call('api_configs')['chains'][0]
        self.assertIsNone(data['route_id']);self.assertIsNone(data['route_name'])
        self.assertEqual(data['connection']['host'],s.get_node(self.db,n)['addr'])
        self.assertEqual(data['connection']['port'],10003)
        self.assertNotIn('tcp_only',data['connection'])
        self.assertEqual(data['status'],'waiting')
        self.assertEqual(self.call('api_chain_del',{},c),{'deleted':True})
        self.assertIsNotNone(s.get_tunnel(self.db,t))

    def test_direct_requires_tunnel_and_has_no_forward_ports(self):
        n,t=self.tunnel()
        with self.assertRaises(s.UserError):self.direct(t,ports=[10001])
        with self.assertRaises(s.UserError):self.config(None)
        with self.assertRaises(s.UserError):self.direct(t,group_id=self.group('routes'))

    def test_switch_modes_keeps_old_port_until_removal_ack(self):
        a=self.node();self.seen(a);r=self.route([a]);n,t=self.tunnel()
        c=self.call('api_config_save',dict(remark='switch',route_id=r,target_type='tunnel',tunnel_id=t,ports=[10001]))['id']
        self.call('api_config_save',dict(remark='switch',route_id=None,target_type='tunnel',tunnel_id=t),c)
        self.assertFalse(self.db.execute('SELECT 1 FROM hops WHERE chain_id=?',(c,)).fetchone())
        self.assertTrue(self.db.execute('SELECT 1 FROM retired_ports WHERE node_id=? AND port=10001',(a,)).fetchone())
        self.assertTrue(self.db.execute('SELECT 1 FROM chain_pending WHERE chain_id=? AND node_id=?',(c,a)).fetchone())
        self.call('api_config_save',dict(remark='back',route_id=r,target_type='tunnel',tunnel_id=t),c)
        self.assertNotEqual(self.db.execute('SELECT port FROM hops WHERE chain_id=?',(c,)).fetchone()[0],10001)

    def test_direct_tracks_tunnel_address_and_port(self):
        n,t=self.tunnel(port=10001);c=self.direct(t)
        self.call('api_node_edit',{'addr':'192.0.2.99'},n)
        self.call('api_tunnel_save',{'name':'changed','port':10002},t)
        data=self.call('api_configs')['chains'][0]
        self.assertEqual((data['connection']['host'],data['connection']['port']),('192.0.2.99',10002))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM hops').fetchone()[0],0)

    def legacy(self):
        self.db.close();os.unlink(s.DB_PATH)
        old=sqlite3.connect(s.DB_PATH);old.row_factory=sqlite3.Row;old.executescript(s.SCHEMA)
        for kind in ('nodes','chains'):
            old.execute('INSERT INTO item_groups(kind,name,builtin) VALUES (?,?,1)',(kind,'default'))
        old.execute("INSERT INTO item_groups(id,kind,name,sort) VALUES (3,'nodes','both',5)")
        old.execute("INSERT INTO nodes(id,name,addr,key,group_id,first_seen) VALUES (10,'F','192.0.2.1',?,3,1)",('ab'*32,))
        old.execute("INSERT INTO nodes(id,name,addr,key,group_id,first_seen) VALUES (20,'T','192.0.2.2',?,3,1)",('cd'*32,))
        old.execute("INSERT INTO chains(id,remark,target_host,target_port,group_id) VALUES (1,'c','example.com',443,2)")
        old.execute('INSERT INTO hops(id,chain_id,seq,node_id,port) VALUES (99,1,0,10,10001)')
        old.commit();s.migrate_v020(old)
        old.execute("UPDATE nodes SET kind='tunnel', reported_v='old', state='ok', agent='0.1.0',core_version='1.14.4' WHERE id=20")
        params=s.tunnel_config.canonical(s.tunnel_config.new_credentials('shadowsocks'))
        old.execute("INSERT INTO tunnels(id,name,node_id,protocol,port,params) VALUES (6,'ss',20,'shadowsocks',10002,?)",(params,))
        old.execute('UPDATE chains SET tunnel_id=6 WHERE id=1')
        old.execute('INSERT INTO tunnel_presence VALUES (20,1,31)')
        old.execute('INSERT INTO retired_ports VALUES (20,10003)')
        old.execute('INSERT INTO chain_pending VALUES (1,10)')
        old.execute("INSERT INTO events(node_id,ts,text) VALUES (20,1,'connected')")
        old.commit();return old

    def test_schema200_migration_keeps_nodes_tunnels_rules_and_state(self):
        old=self.legacy()
        before=s.node_rules(old,old.execute('SELECT * FROM nodes WHERE id=10').fetchone())
        snapshot=s.tunnel_snapshot(old,old.execute('SELECT * FROM nodes WHERE id=20').fetchone())
        old.close();self.db=s.init_db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],201)
        self.assertEqual(s.node_rules(self.db,s.get_node(self.db,10)),before)
        self.assertEqual(s.tunnel_snapshot(self.db,s.get_node(self.db,20)),snapshot)
        self.assertEqual(s.get_node(self.db,20)['key'],'cd'*32)
        self.assertNotEqual(s.get_node(self.db,20)['group_id'],s.get_node(self.db,10)['group_id'])
        self.assertEqual(self.h.get_group(self.db,s.get_node(self.db,20)['group_id'])['name'],'both')
        self.assertEqual(tuple(self.db.execute('SELECT * FROM retired_ports').fetchone()),(20,10003))
        self.assertEqual(tuple(self.db.execute('SELECT * FROM tunnel_presence').fetchone()),(20,1,31))
        self.assertEqual(self.db.execute('SELECT text FROM events WHERE node_id=20').fetchone()[0],'connected')
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())
        self.assertTrue(os.path.exists(s.DB_PATH+'.pre-layout-201.bak'))
        self.assertGreater(self.node('new','tunnel'),20)
        self.db.close();self.db=s.init_db()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM tunnel_nodes').fetchone()[0],2)

    def test_failed_layout_migration_rolls_back_and_restores_foreign_keys(self):
        old=self.legacy()
        with patch.object(s,'node_rules',side_effect=[['before'],['after']]):
            with self.assertRaises(s.UserError):s.migrate_layout(old)
        self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0],200)
        self.assertEqual(old.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.assertEqual(old.execute('SELECT COUNT(*) FROM nodes').fetchone()[0],2)
        self.assertFalse(old.execute("SELECT 1 FROM sqlite_master WHERE name='tunnel_nodes'").fetchone())
        old.close();self.db=s.init_db()


class DirectHTTPTests(test_server.HTTPTests):
    # Reuse the authenticated local HTTP fixture without repeating inherited tests.
    test_tunnel_ack_enables_forwarding_and_deletion=None
    test_forward_sync_keeps_protocol_and_ack_gate=None
    test_auth_and_transport_required=None

    def test_direct_readiness_and_delete_leave_shared_tunnel(self):
        n,t=self.tunnel();c=self.call('api_config_save',dict(remark='direct',route_id=None,target_type='tunnel',tunnel_id=t))['id']
        self.assertEqual(self.call('api_configs')['chains'][0]['status'],'waiting')
        status,_=self.heartbeat(n);self.assertEqual(status,200)
        self.assertEqual(self.call('api_configs')['chains'][0]['status'],'applied')
        self.assertEqual(self.call('api_chain_del',{},c),{'deleted':True})
        self.assertIsNotNone(s.get_tunnel(self.db,t))
