"""External share links, configuration propagation and compatibility migrations."""
import base64
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from urllib.parse import parse_qs, quote, urlsplit

import test_server
from test_server import Fixture, s


def ss_uri(host='192.0.2.50', port=23456, key=b'k' * 16):
    password = base64.b64encode(key).decode()
    return f'ss://2022-blake3-aes-128-gcm:{quote(password, safe="")}@{host}:{port}#Imported%20SS'


def reality_uri(host='reality.example.com', port=443):
    key = base64.urlsafe_b64encode(b'p' * 32).decode().rstrip('=')
    return (f'vless://00000000-0000-4000-8000-000000000001@{host}:{port}'
            f'?security=reality&sni=tls.example.com&pbk={key}&sid=aabb&type=xhttp'
            '&path=%2Fmy%2Bpath&fp=firefox&flow=&extra=%7B%22mode%22%3A%22auto%22%7D#Reality')


class ImportTests(Fixture, unittest.TestCase):
    def imported(self, uri=None, **kw):
        return self.call('api_tunnel_save', dict(source='external', uri=uri or ss_uri(), **kw))['id']

    def direct(self, tid, route=None, **kw):
        return self.call('api_config_save', dict(remark='external', route_id=route,
                         target_type='tunnel', tunnel_id=tid, **kw))['id']

    def test_import_needs_no_agent_and_preserves_external_service(self):
        tid = self.imported()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM all_nodes').fetchone()[0], 0)
        tunnel = self.call('api_tunnels')['tunnels'][0]
        self.assertEqual(tunnel['status'], 'imported')
        self.assertIsNone(tunnel['agent_status'])
        c = self.direct(tid)
        data = self.call('api_configs')['chains'][0]
        self.assertEqual(data['status'], 'saved')
        self.assertTrue(data['ready'])
        self.assertEqual((data['connection']['host'], data['connection']['port']), ('192.0.2.50', 23456))
        self.assertFalse(data['pending'])
        self.assertFalse(data['hops'])
        with self.assertRaises(s.UserError):
            self.call('api_tunnel_del', {}, tid)
        self.assertTrue(self.call('api_chain_del', {}, c)['deleted'])
        self.assertIsNotNone(s.get_tunnel(self.db, tid))
        self.assertTrue(self.call('api_tunnel_del', {}, tid)['deleted'])
        self.assertFalse(self.db.execute('SELECT * FROM retired_ports').fetchall())

    def test_shared_external_endpoint_uses_independent_credentials(self):
        a, b = self.imported(), self.imported(ss_uri(key=b'b' * 16))
        self.direct(a); self.direct(b)
        records = self.call('api_configs')['chains']
        self.assertNotEqual(records[0]['connection']['password'], records[1]['connection']['password'])

    def test_endpoint_edits_update_all_configs_and_wait_for_forward_ack(self):
        nid = self.node(); route = self.route([nid]); tid = self.imported()
        direct = self.direct(tid); routed = self.direct(tid, route, ports=[10001])
        self.db.execute('DELETE FROM chain_pending'); self.db.commit()
        old_hops = [tuple(r) for r in self.db.execute('SELECT * FROM hops')]
        self.call('api_tunnel_save', {'uri': ss_uri('other.example.com', 34567)}, tid)
        configs = {c['id']: c for c in self.call('api_configs')['chains']}
        self.assertEqual(configs[direct]['connection']['host'], 'other.example.com')
        self.assertEqual(configs[direct]['status'], 'saved')
        self.assertEqual(configs[routed]['status'], 'applying')
        self.assertEqual(configs[routed]['connection']['host'], '192.0.2.1')
        self.assertEqual(configs[routed]['connection']['port'], 10001)
        self.assertIn('other.example.com 34567', s.node_rules(self.db, s.get_node(self.db, nid))[0])
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM hops')], old_hops)
        self.db.execute('DELETE FROM chain_pending'); self.db.commit()
        self.call('api_tunnel_save', {'uri': ss_uri('other.example.com', 34567, b'z' * 16)}, tid)
        self.assertFalse(self.db.execute('SELECT * FROM chain_pending').fetchall())
        self.assertEqual(self.call('api_configs')['chains'][0]['connection']['password'], base64.b64encode(b'z'*16).decode())

    def test_imported_tunnels_do_not_change_managed_core_and_groups_work(self):
        nid, managed = self.tunnel()
        snapshot = s.tunnel_snapshot(self.db, s.get_node(self.db, nid))
        tid = self.imported()
        group = self.call('api_group_add', {'kind': 'tunnels', 'name': 'external'})['id']
        self.call('api_tunnel_save', {'group_id': group, 'name': 'renamed'}, tid)
        self.assertEqual(s.get_tunnel(self.db, tid)['group_id'], group)
        self.assertEqual(s.tunnel_snapshot(self.db, s.get_node(self.db, nid)), snapshot)
        self.call('api_order', {'groups': [group, s.default_group(self.db, 'tunnels')], 'ids': [tid, managed]}, 'tunnels')
        self.assertEqual(self.call('api_tunnels')['tunnels'][0]['id'], tid)
        self.call('api_group_del', {}, group)
        self.assertEqual(s.get_tunnel(self.db, tid)['group_id'], s.default_group(self.db, 'tunnels'))
        with self.assertRaises(s.UserError): self.call('api_tunnel_save', {'source': 'agent'}, tid)
        with self.assertRaises(s.UserError): self.call('api_tunnel_save', {'source': 'external', 'uri': ss_uri()}, managed)
        with self.assertRaises(s.UserError): self.call('api_tunnel_save', {'port': 80}, tid)
        with self.assertRaises(s.UserError): self.imported(group_id=s.default_group(self.db, 'nodes'))

    def test_share_links_round_trip_without_losing_transport_or_secrets(self):
        for uri in (ss_uri(), reality_uri()):
            tid = self.imported(uri)
            t = s.get_tunnel(self.db, tid)
            original = s.tunnel_config.import_uri(uri)
            link = s.tunnel_config.connection(t, 'entry.example.com', 10001, '入口 + # 中文')['uri']
            parsed = s.tunnel_config.import_uri(link)
            self.assertEqual(parsed['host'], 'entry.example.com')
            self.assertEqual(parsed['port'], 10001)
            self.assertEqual(parsed['name'], '入口 + # 中文')
            self.assertEqual(parsed['params'], original['params'])
            if t['protocol'] == 'vless-reality':
                self.assertEqual(parse_qs(urlsplit(link).query, keep_blank_values=True),
                                 parse_qs(urlsplit(uri).query, keep_blank_values=True))

    def test_ss22_plain_and_legacy_base64_and_sip002_plugin(self):
        tc = s.tunnel_config
        password = base64.b64encode(b'k' * 16).decode()
        auth = '2022-blake3-aes-128-gcm:' + password
        b64 = base64.urlsafe_b64encode(auth.encode()).decode().rstrip('=')
        parsed = tc.import_uri('ss://' + b64 + '@192.0.2.50:23456')
        self.assertEqual(parsed['params']['password'], password)
        whole = base64.b64encode((auth + '@192.0.2.50:23456').encode()).decode()
        self.assertEqual(tc.import_uri('ss://' + whole)['params'], parsed['params'])
        special = base64.b64encode(b'\xff' * 16).decode()
        whole = base64.b64encode(('2022-blake3-aes-128-gcm:' + special + '@192.0.2.50:23456').encode()).decode()
        self.assertEqual(tc.import_uri('ss://' + whole)['params']['password'], special)
        tid = self.imported()
        export = self.call('api_tunnels')['tunnels'][0]['uri']
        self.assertTrue(export.startswith('ss://2022-blake3-aes-128-gcm:'))
        self.assertIn('%3D', export)
        legacy = base64.urlsafe_b64encode('aes-256-gcm:pass:with:@symbols'.encode()).decode().rstrip('=')
        uri = f'ss://{legacy}@192.0.2.60:1234/?plugin=v2ray-plugin%3Btls%3Bhost%3Dx.example.com#Plugin'
        imported = tc.import_uri(uri)
        self.assertEqual(imported['params']['password'], 'pass:with:@symbols')
        t = {'protocol': imported['protocol'], 'params': tc.canonical(imported['params'])}
        result = tc.import_uri(tc.connection(t, 'entry.example.com', 3456, 'Plugin')['uri'])
        self.assertEqual(result['params'], imported['params'])
        # Chained SS2022 identity keys are preserved.
        self.imported(ss_uri().replace('%3D%3D@', '%3D%3D%3A' + quote(password, safe='') + '@'))

    def test_invalid_import_is_atomic_and_keeps_existing_references(self):
        tid = self.imported(); self.direct(tid)
        before = tuple(s.get_tunnel(self.db, tid))
        invalid = ['vmess://abc', 'https://example.com', ss_uri().replace('23456', '0'),
                   ss_uri().replace('23456', '65536'), ss_uri().replace('192.0.2.50', '[::1]'),
                   'ss://invalid@192.0.2.1:1000', ss_uri().replace('a2tra2tra2tra2tra2traw', 'bad'),
                   reality_uri().replace('security=reality', 'security=tls'),
                   reality_uri().replace('sid=aabb', 'sid=a'), reality_uri() + '&bad=%zz',
                   reality_uri().replace('&sid=aabb', '&security=reality&sid=aabb'),
                   reality_uri().replace('pbk=', 'missing='), ss_uri() + '\nss://another']
        for uri in invalid:
            with self.subTest(uri=uri), self.assertRaises(s.UserError):
                self.call('api_tunnel_save', {'uri': uri}, tid)
            self.assertEqual(tuple(s.get_tunnel(self.db, tid)), before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM tunnels').fetchone()[0], 1)

    def test_schema201_migration_preserves_managed_tunnels_and_references(self):
        nid, tid = self.tunnel(); self.direct(tid)
        before = dict(s.get_tunnel(self.db, tid)); before.pop('source'); before.pop('external_host')
        snapshot = s.tunnel_snapshot(self.db, s.get_node(self.db, nid))
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.executescript('''
            CREATE TABLE old_tunnels (
                id INTEGER PRIMARY KEY,name TEXT NOT NULL,node_id INTEGER NOT NULL REFERENCES tunnel_nodes(id),
                protocol TEXT NOT NULL,port INTEGER NOT NULL,params TEXT NOT NULL,deleting INTEGER NOT NULL DEFAULT 0,
                group_id INTEGER NOT NULL DEFAULT 0,sort INTEGER NOT NULL DEFAULT 0,UNIQUE(node_id,port));
            INSERT INTO old_tunnels SELECT id,name,node_id,protocol,port,params,deleting,group_id,sort FROM tunnels;
            DROP TABLE tunnels;
            ALTER TABLE old_tunnels RENAME TO tunnels;
            PRAGMA user_version=201;
        ''')
        self.db.close(); self.db = s.init_db()
        current = dict(s.get_tunnel(self.db, tid))
        self.assertEqual(current.pop('source'), 'agent'); self.assertIsNone(current.pop('external_host'))
        self.assertEqual(current, before)
        self.assertEqual(s.tunnel_snapshot(self.db, s.get_node(self.db, nid)), snapshot)
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())
        self.assertTrue(Path(s.DB_PATH + '.pre-0.2.1.bak').is_file())
        self.imported()
        self.db.close(); self.db = s.init_db()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM tunnels').fetchone()[0], 2)

    def test_old_forward_only_groups_are_not_copied_to_tunnel_nodes(self):
        self.db.close(); os.unlink(s.DB_PATH)
        old = sqlite3.connect(s.DB_PATH); old.row_factory = sqlite3.Row; old.executescript(s.SCHEMA)
        for kind in ('nodes', 'chains'):
            old.execute('INSERT INTO item_groups(kind,name,builtin) VALUES (?,?,1)', (kind, 'default'))
        old.execute("INSERT INTO item_groups(kind,name) VALUES ('nodes','forward-only')")
        old.commit(); old.close(); self.db = s.init_db()
        self.assertEqual([g['name'] for g in s.get_groups(self.db, 'tunnel_nodes')], ['default'])
        tid = self.call('api_group_add', {'kind': 'tunnel_nodes', 'name': 'tunnel-only'})['id']
        fid = self.call('api_group_add', {'kind': 'nodes', 'name': 'new-forward'})['id']
        self.assertNotIn('new-forward', [g['name'] for g in s.get_groups(self.db, 'tunnel_nodes')])
        self.call('api_order', {'groups': [tid, s.default_group(self.db, 'tunnel_nodes')], 'ids': []}, 'tunnel_nodes')
        self.assertEqual(s.get_groups(self.db, 'nodes')[-1]['id'], fid)


class ImportHTTPTests(test_server.HTTPTests):
    test_tunnel_ack_enables_forwarding_and_deletion = None
    test_forward_sync_keeps_protocol_and_ack_gate = None
    test_auth_and_transport_required = None

    def test_external_config_only_waits_for_forward_ack(self):
        nid = self.node(); route = self.route([nid])
        tid = self.call('api_tunnel_save', {'source': 'external', 'uri': ss_uri()})['id']
        cid = self.call('api_config_save', dict(remark='route-import', route_id=route,
                        target_type='tunnel', tunnel_id=tid, ports=[10001]))['id']
        node = s.get_node(self.db, nid)
        want = s.rules_version(s.node_rules(self.db, node))
        ek, mk = s.derive(node['key'])
        plain = f't={int(time.time())}\nv={want}\nstate=ok\nagent=0.1.8\nau=1\n'.encode()
        message = s.aes(ek, plain)[8:]
        status, _ = self.req(s.Handler.sync_path, base64.b64encode(message + s.mac(mk, message)),
                             {'Content-Type': 'text/plain'})
        self.assertEqual(status, 200)
        self.assertEqual(self.call('api_configs')['chains'][0]['status'], 'applied')
