import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import database
import errors
import forwarding
import target_config
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
from test_server import Fixture


def ss_uri(host='192.0.2.50', port=23456, key=b'k' * 16):
    password = base64.b64encode(key).decode()
    return f'ss://2022-blake3-aes-128-gcm:{quote(password, safe="")}@{host}:{port}#Imported%20SS'


def reality_uri(host='reality.example.com', port=443):
    key = base64.urlsafe_b64encode(b'p' * 32).decode().rstrip('=')
    return (f'vless://00000000-0000-4000-8000-000000000001@{host}:{port}'
            f'?security=reality&sni=tls.example.com&pbk={key}&sid=aabb&type=xhttp'
            '&path=%2Fmy%2Bpath&fp=firefox&flow=&extra=%7B%22mode%22%3A%22auto%22%7D#Reality')


def enc_uri(host='enc.example.com', port=443):
    key = base64.urlsafe_b64encode(b'p' * 32).decode().rstrip('=')
    return (f'vless://00000000-0000-4000-8000-000000000001@{host}:{port}'
            f'?encryption=mlkem768x25519plus.native.1rtt.{key}&security=none&type=xhttp'
            '&path=%2Fmy%2Bpath&extra=%7B%22mode%22%3A%22auto%22%7D#ENC')


class ImportTests(Fixture, unittest.TestCase):
    def imported(self, uri=None, **kw):
        return self.call('api_target_save', dict(uri=uri or ss_uri(), **kw))['id']





    def test_target_groups_work(self):
        tid = self.imported()
        group = self.call('api_group_add', {'kind': 'targets', 'name': 'external'})['id']
        self.call('api_target_save', {'group_id': group, 'name': 'renamed'}, tid)
        self.assertEqual(database.get_target(self.db, tid)['group_id'], group)
        self.call('api_order', {'groups': [group, database.default_group(self.db, 'targets')], 'ids': [tid]}, 'targets')
        self.assertEqual(self.call('api_targets')['targets'][0]['id'], tid)
        self.call('api_group_del', {}, group)
        self.assertEqual(database.get_target(self.db, tid)['group_id'], database.default_group(self.db, 'targets'))
        with self.assertRaises(errors.UserError): self.call('api_target_save', {'port': 80}, tid)
        with self.assertRaises(errors.UserError): self.imported(group_id=database.default_group(self.db, 'nodes'))

    def test_share_links_round_trip_without_losing_transport_or_secrets(self):
        for uri in (ss_uri(), enc_uri()):
            tid = self.imported(uri)
            t = database.get_target(self.db, tid)
            original = target_config.import_uri(uri)
            link = target_config.connection(t, 'entry.example.com', 10001, '入口 + # 中文')['uri']
            parsed = target_config.import_uri(link)
            self.assertEqual(parsed['host'], 'entry.example.com')
            self.assertEqual(parsed['port'], 10001)
            self.assertEqual(parsed['name'], '入口 + # 中文')
            self.assertEqual(parsed['params'], original['params'])
            if t['type'] == 'vless-enc':
                self.assertEqual(parse_qs(urlsplit(link).query, keep_blank_values=True),
                                 {k: [v] for k, v in original['params']['share_query']})

    def test_ss22_plain_and_legacy_base64_and_sip002_plugin(self):
        tc = target_config
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
        export = self.call('api_targets')['targets'][0]['uri']
        self.assertTrue(export.startswith('ss://2022-blake3-aes-128-gcm:'))
        self.assertIn('%3D', export)
        legacy = base64.urlsafe_b64encode('aes-256-gcm:pass:with:@symbols'.encode()).decode().rstrip('=')
        uri = f'ss://{legacy}@192.0.2.60:1234/?plugin=v2ray-plugin%3Btls%3Bhost%3Dx.example.com#Plugin'
        imported = tc.import_uri(uri)
        self.assertEqual(imported['params']['password'], 'pass:with:@symbols')
        t = {'type': imported['protocol'], 'params': tc.canonical(imported['params'])}
        result = tc.import_uri(tc.connection(t, 'entry.example.com', 3456, 'Plugin')['uri'])
        self.assertEqual(result['params'], imported['params'])
        # Chained SS2022 identity keys are preserved.
        self.imported(ss_uri().replace('%3D%3D@', '%3D%3D%3A' + quote(password, safe='') + '@'))

    def test_invalid_import_is_atomic_and_keeps_existing_references(self):
        tid = self.imported(); self.save(self.graph([self.node()],tid))
        before = tuple(database.get_target(self.db, tid))
        invalid = ['vmess://abc', 'https://example.com', ss_uri().replace('23456', '0'),
                   ss_uri().replace('23456', '65536'), ss_uri().replace('192.0.2.50', '[::1]'),
                   'ss://invalid@192.0.2.1:1000', ss_uri().replace('a2tra2tra2tra2tra2traw', 'bad'),
                   reality_uri().replace('security=reality', 'security=tls'),
                   reality_uri().replace('sid=aabb', 'sid=a'), reality_uri() + '&bad=%zz',
                   reality_uri().replace('&sid=aabb', '&security=reality&sid=aabb'),
                   reality_uri().replace('pbk=', 'missing='), ss_uri() + '\nss://another']
        for uri in invalid:
            with self.subTest(uri=uri), self.assertRaises(errors.UserError):
                self.call('api_target_save', {'uri': uri}, tid)
            self.assertEqual(tuple(database.get_target(self.db, tid)), before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM targets').fetchone()[0], 1)
