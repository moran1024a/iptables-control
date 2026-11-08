"""0.2.0 regression tests: isolated SQLite databases and local HTTP only."""
import base64
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import agent_files
import api
import database
import errors
import forwarding
import http_harness
import migrations
import resources
import security
import settings



class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings.DB_PATH = str(Path(self.temp.name) / 'test.db')
        self.db = migrations.init_db()
        self.h = resources.Resources()
        self.h.source_ip = lambda: '127.0.0.1'

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def call(self, method, body=None, *args):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            return getattr(self.h, method)(self.db, body or {}, *args)

    def node(self, name='A', kind='forward', ports='10000-10020'):
        return self.call('api_node_add', dict(name=name, addr='192.0.2.1', kind=kind, ports=ports))['id']

    def route(self, ids, name='route'):
        return self.call('api_route_save', dict(name=name, node_ids=ids))['id']

    def target(self, host='198.51.100.1', port=443):
        return self.call('api_target_save', dict(name='target',host=host,port=port))['id']

    def config(self, route, ports=None, **kw):
        tid = kw.pop('target_id', None) or self.target()
        return self.call('api_config_save', dict(remark='config', route_id=route, ports=ports,
            target_id=tid, **kw))['id']

    def seen(self, nid):
        with self.db:
            self.db.execute(f'UPDATE {database.node_table(database.get_node(self.db,nid)["kind"])} SET first_seen = 1, last_seen = ? WHERE id = ?', (int(time.time()), nid))



class ServerTests(Fixture, unittest.TestCase):
    def panel_request(self, route, body, token='audit-session'):
        payload = json.dumps(body).encode()
        panel = database.setting(self.db, 'panel_path')
        request = api.Request('POST', panel + '/api/' + route,
            [('Content-Type', 'application/json'), ('Content-Length', str(len(payload))),
             ('Cookie', settings.SESSION_COOKIE + '=' + token)], payload, '127.0.0.1', panel_path=panel)
        request.route('POST')
        return request.response

    def test_panel_order_dispatches_resource_kind_and_persists_order(self):
        a, b = self.node(), self.node('B')
        with self.db:
            database.set_setting(self.db, 'must_change', '0')
            self.db.execute('INSERT INTO sessions VALUES (?, ?)',
                (hashlib.sha256(b'audit-session').hexdigest(), int(time.time()) + 86400))
        for kind in settings.KINDS:
            with self.subTest(kind=kind):
                groups = [r[0] for r in self.db.execute('SELECT id FROM item_groups WHERE kind=?', (kind,))]
                code, data, _ = self.panel_request(kind + '/order', dict(groups=groups, ids=[b, a] if kind=='nodes' else []))
                self.assertEqual(code, 200, data)
        self.assertEqual([r['id'] for r in self.db.execute(database.ordered('nodes'))], [b, a])

    def test_unicode_login_name_returns_authentication_error(self):
        self.assertEqual(self.panel_request('login', dict(username='管理员', password='incorrect'))[0], 401)

    def test_versions_are_independent(self):
        agent_files.load_agent()
        self.assertEqual(agent_files.AGENT[0], '0.2.1')
        self.assertEqual(agent_files.agent_update('0.2.1', 1), 'same')
        self.assertEqual(agent_files.agent_update('0.2.0', 1), 'pending')
        self.assertEqual(agent_files.agent_update('0.1.7', 1), 'pending')
        self.assertEqual(settings.VERSION, settings.COMPONENTS['server'])
        self.assertNotEqual(settings.VERSION, agent_files.AGENT[0])

    def test_route_allocates_no_ports(self):
        a, b = self.node(), self.node('B')
        self.route([a, b])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM hops').fetchone()[0], 0)
        self.assertEqual(forwarding.node_rules(self.db, database.get_node(self.db,a)), [])

    def test_fanout_keeps_ids_and_ports(self):
        a,b,c=self.node(),self.node('B'),self.node('C')
        r=self.route([a,b]);x=self.config(r,[10001,10002]);y=self.config(r,[10003,10004])
        before={h['id']:dict(h) for h in self.db.execute('SELECT * FROM hops')}
        self.seen(a);self.seen(b)
        result=self.call('api_route_save',{'name':'changed','node_ids':[a,b,c]},r)
        self.assertEqual(result['affected'],2)
        for hid,old in before.items():
            self.assertEqual(dict(self.db.execute('SELECT * FROM hops WHERE id=?',(hid,)).fetchone()),old)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM hops').fetchone()[0],6)
        self.assertEqual(self.db.execute('SELECT COUNT(DISTINCT port) FROM hops WHERE node_id=?',(c,)).fetchone()[0],2)

    def test_removed_never_connected_node_does_not_block_config(self):
        a,b=self.node(),self.node('B');r=self.route([a,b]);c=self.config(r)
        self.call('api_route_save',{'name':'one-hop','node_ids':[a]},r)
        pending=[row[0] for row in self.db.execute('SELECT node_id FROM chain_pending WHERE chain_id=?',(c,))]
        self.assertEqual(pending,[a])

    def test_fanout_conflict_rolls_back_every_config(self):
        a=self.node(); b=self.node('B',ports='10000')
        r=self.route([a]);self.config(r,[10001]);self.config(r,[10002])
        before=[tuple(h) for h in self.db.execute('SELECT * FROM hops')]
        with self.assertRaises(errors.UserError):
            self.call('api_route_save',{'name':'changed','node_ids':[a,b]},r)
        self.assertEqual([tuple(h) for h in self.db.execute('SELECT * FROM hops')],before)
        self.assertEqual(json.loads(database.get_route(self.db,r)['node_ids']),[a])
        self.assertEqual(database.get_route(self.db,r)['name'],'route')

    def test_removed_port_is_reserved_until_ack(self):
        a,b=self.node(),self.node('B');self.seen(b)
        r=self.route([a,b]);self.config(r,[10001,10002])
        self.call('api_route_save',{'name':'new','node_ids':[a]},r)
        r2=self.route([b])
        with self.assertRaises(errors.UserError):self.config(r2,[10002])
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM retired_ports WHERE node_id=? AND port=10002',(b,)).fetchone())

    def test_metadata_edit_preserves_rule_hash(self):
        a=self.node();r=self.route([a]);c=self.config(r,[10001])
        before=forwarding.node_rules(self.db,database.get_node(self.db,a))
        self.call('api_route_save',{'name':'rename','node_ids':[a]},r)
        self.call('api_config_save',dict(remark='rename',route_id=r,ports=[10001],target_id=database.get_chain(self.db,c)['target_id']),c)
        self.assertEqual(before,forwarding.node_rules(self.db,database.get_node(self.db,a)))





    def test_migration_preserves_resources_and_backup_clears_topology(self):
        self.db.close()
        os.unlink(settings.DB_PATH)
        old=sqlite3.connect(settings.DB_PATH);old.row_factory=sqlite3.Row
        old.executescript(migrations.SCHEMA)
        for kind in ('nodes','chains'):
            old.execute('INSERT INTO item_groups(kind,name,builtin) VALUES (?,?,1)',(kind,'group'))
        old.execute("INSERT INTO nodes(id,name,addr,key,first_seen,group_id,ports) VALUES (7,'old','192.0.2.7',?,1,1,'10000-65535')",('ab'*32,))
        old.execute("INSERT INTO nodes(id,name,addr,key,first_seen,group_id) VALUES (8,'removed','192.0.2.8',?,1,1)",('cd'*32,))
        old.execute("INSERT INTO chains(id,remark,target_host,target_port,group_id,sort) VALUES (9,'legacy','example.com',443,2,20)")
        old.execute('INSERT INTO hops(id,chain_id,seq,node_id,port) VALUES (81,9,0,7,12345)')
        old.execute('INSERT INTO chain_pending VALUES (9,8)')
        database.set_setting(old,'window','60');database.set_setting(old,'sync_path','/oldsync');database.set_setting(old,'panel_path','/oldpanel')
        old.commit();before=forwarding.legacy_node_rules(old,old.execute('SELECT * FROM nodes WHERE id=7').fetchone());old.close()
        self.db=migrations.init_db()
        self.assertEqual(forwarding.node_rules(self.db,database.get_node(self.db,7)),[])
        self.assertEqual(database.get_node(self.db,7)['key'],'ab'*32)
        self.assertEqual(database.setting(self.db,'sync_path'),'/oldsync')
        self.assertFalse(self.db.execute('SELECT * FROM chain_pending').fetchone())
        self.assertEqual(self.db.execute('SELECT port FROM retired_ports WHERE node_id=7').fetchone()[0],12345)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM links').fetchone()[0],0)
        backup=sqlite3.connect(settings.DB_PATH+'.pre-0.2.0.bak')
        self.assertEqual(backup.execute('SELECT port FROM hops WHERE id=81').fetchone()[0],12345);backup.close()
        self.db.close();self.db=migrations.init_db()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM links').fetchone()[0],0)

    def test_failed_migration_is_atomic(self):
        self.db.close();os.unlink(settings.DB_PATH)
        old=sqlite3.connect(settings.DB_PATH);old.executescript(migrations.SCHEMA)
        # Trigger a conflict after the first ALTER, exercising transactional DDL rollback.
        old.execute('CREATE TABLE routes(id INTEGER PRIMARY KEY)');old.commit();old.close()
        with self.assertRaises(sqlite3.OperationalError):migrations.init_db()
        check=sqlite3.connect(settings.DB_PATH)
        self.assertNotIn('kind',[r[1] for r in check.execute('PRAGMA table_info(nodes)')]);check.close()
        self.db=sqlite3.connect(settings.DB_PATH)


class HTTPTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        http_harness.Handler.panel_path=database.setting(self.db,'panel_path')
        http_harness.Handler.sync_path=database.setting(self.db,'sync_path')
        self.server=http_harness.LimitedHTTPServer(('127.0.0.1',0),http_harness.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();super().tearDown()

    def req(self,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        hs=headers or {}
        c.request('POST' if body is not None else 'GET',path,body,hs)
        r=c.getresponse();data=r.read();c.close();return r.status,data



    def test_forward_sync_keeps_protocol_and_ack_gate(self):
        n=self.node();self.seen(n);r=self.route([n]);c=self.config(r,[10001]);node=database.get_node(self.db,n)
        want=forwarding.rules_version(forwarding.node_rules(self.db,node));ek,mk=security.derive(node['key'])
        def sync(state):
            plain=f't={int(time.time())}\nv={want}\nstate={state}\nagent=0.1.8\nau=1\n'.encode()
            msg=security.aes(ek,plain)[8:];tag=security.mac(mk,msg)
            code,raw=self.req(http_harness.Handler.sync_path,base64.b64encode(msg+tag),{'Content-Type':'text/plain'})
            raw=base64.b64decode(raw);self.assertEqual(code,200)
            self.assertEqual(raw[-32:],security.mac(mk,tag+raw[:-32]))
            return security.aes(ek,b'Salted__'+raw[:-32],True).decode()
        self.assertEqual(sync('error'),'same\n')
        self.assertTrue(self.db.execute('SELECT 1 FROM chain_pending WHERE chain_id=?',(c,)).fetchone())
        self.assertEqual(sync('ok'),'same\n')
        self.assertFalse(self.db.execute('SELECT 1 FROM chain_pending WHERE chain_id=?',(c,)).fetchone())


    def encrypted(self, key, payload):
        ek,mk=security.derive(key);msg=security.aes(ek,payload.encode())[8:];tag=security.mac(mk,msg)
        code,raw=self.req(http_harness.Handler.sync_path,base64.b64encode(msg+tag),{'Content-Type':'text/plain'})
        if code!=200:return code,raw
        raw=base64.b64decode(raw);self.assertEqual(raw[-32:],security.mac(mk,tag+raw[:-32]))
        return code,security.aes(ek,b'Salted__'+raw[:-32],True).decode()

    def test_auth_body_limits_and_removed_endpoints(self):
        self.assertEqual(self.req(http_harness.Handler.panel_path+'/api/targets')[0],401)
        self.assertEqual(self.req('/obsolete/tunnel/config')[0],404)
        self.assertEqual(self.req(http_harness.Handler.sync_path,'not-base64',{'Content-Type':'text/plain'})[0],404)
        self.assertEqual(self.req(http_harness.Handler.sync_path,'x'*(settings.MAX_BODY+1),{'Content-Type':'text/plain'})[0],404)
        n=self.node();key=database.get_node(self.db,n)['key']
        text=f't={int(time.time())}\nstate=ok\nstate=ok\n'
        self.assertEqual(self.encrypted(key,text)[0],404)
        self.assertIsNone(database.get_node(self.db,n)['first_seen'])

    def test_deleted_node_receipt_can_be_retried_without_config_access(self):
        n=self.node();self.seen(n);key=database.get_node(self.db,n)['key']
        self.call('api_node_del',{},n)
        base=f't={int(time.time())}\nstate=ok\nproto=2\nsource=manual\nop={"ab"*16}\nagent=0.2.0\nau=0\n'
        code,out=self.encrypted(key,base+'phase=begin\nmode=active\nv=\ngeneration=0\n')
        self.assertEqual(code,200)
        f=dict(line.split('=',1) for line in out.splitlines() if '=' in line)
        ack=base+f'phase=ack\nmode={f["mode"]}\nv={f["v"]}\ngeneration={f["generation"]}\n'
        self.assertIn('result=deleted',self.encrypted(key,ack)[1])
        self.assertIn('result=deleted',self.encrypted(key,ack)[1])
        self.assertEqual(self.encrypted(key,base+'phase=begin\n')[0],404)

    def test_idle_partial_headers_timeout_without_db_lock(self):
        previous=http_harness.Handler.timeout;http_harness.Handler.timeout=0.15
        client=socket.create_connection(('127.0.0.1',self.port),timeout=2)
        try:
            client.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\nX-Slow: ')
            with self.db:self.db.execute('BEGIN IMMEDIATE')
            self.assertEqual(client.recv(1),b'')
        finally:client.close();http_harness.Handler.timeout=previous

    def test_connection_cap_rejects_and_releases_slots(self):
        for _ in range(self.server.max_connections):self.assertTrue(self.server.slots.acquire(False))
        try:
            with self.assertRaises((OSError,http.client.HTTPException)):
                self.req(http_harness.Handler.panel_path+'/')
        finally:
            for _ in range(self.server.max_connections):self.server.slots.release()
        self.assertEqual(self.req(http_harness.Handler.panel_path+'/')[0],200)



if __name__ == '__main__':unittest.main()
