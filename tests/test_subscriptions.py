import base64
import json
import unittest
from urllib.parse import quote

from test_server import Fixture, s
import subscriptions
import transport


class SubscriptionTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.a = self.node()
        auth = base64.urlsafe_b64encode(b'aes-128-gcm:test-password').decode()
        self.target_id = self.call('api_target_save', {'uri': f'ss://{auth}@target.example:443#test'})['id']
        self.cid = self.config(self.route([self.a]), [10001], target_id=self.target_id)
        self.lid = self.db.execute('SELECT id FROM links WHERE chain_id=?', (self.cid,)).fetchone()[0]

    def create(self, ids=None):
        self.call('api_subscription_save', dict(name='test', link_ids=ids if ids is not None else [self.lid]))
        return self.call('api_subscriptions')['subscriptions'][-1]

    def test_formats_use_entry_and_keep_credentials(self):
        row = self.create()
        result, mime = subscriptions.export(s._module(), self.db, row['token'], 'v2ray')
        parsed = s.target_config.import_uri(base64.b64decode(result).decode())
        self.assertEqual((parsed['host'], parsed['port']), ('192.0.2.1', 10001))
        self.assertEqual(parsed['params']['password'], 'test-password')
        body, mime = subscriptions.export(s._module(), self.db, row['token'], 'clash')
        proxy = json.loads(body.split('  - ', 1)[1].splitlines()[0])
        self.assertEqual((proxy['server'], proxy['port']), ('192.0.2.1', 10001))
        self.assertEqual(proxy['password'], 'test-password')
        self.assertIn('proxy-groups:', body)
        self.assertNotIn('target.example', body)

    def test_subscription_independence_live_updates_and_reset(self):
        row, other = self.create(), self.create([])
        self.call('api_config_save', dict(remark='new name'), self.cid)
        body, _ = subscriptions.export(s._module(), self.db, row['token'], 'v2ray')
        self.assertIn('#new%20name', base64.b64decode(body).decode())
        self.assertEqual(subscriptions.export(s._module(), self.db, other['token'], 'v2ray')[0], '\n')
        self.call('api_subscription_save', dict(enabled=False), row['id'])
        self.assertIsNone(subscriptions.export(s._module(), self.db, row['token'], 'clash'))
        self.call('api_subscription_save', dict(enabled=True, reset_token=True), row['id'])
        self.assertIsNone(subscriptions.export(s._module(), self.db, row['token'], 'clash'))
        new = self.call('api_subscriptions')['subscriptions'][0]
        self.assertNotEqual(new['token'], row['token'])
        self.assertIsNotNone(subscriptions.export(s._module(), self.db, new['token'], 'clash'))
        self.call('api_subscription_delete', {}, row['id'])
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM links WHERE id=?', (self.lid,)).fetchone())

    def test_unsupported_endpoints_and_transports_are_visible_and_omitted(self):
        endpoint_cid = self.config(None)
        endpoint = self.db.execute('SELECT id FROM links WHERE chain_id=?', (endpoint_cid,)).fetchone()[0]
        row = self.create([endpoint, self.lid])
        self.assertEqual(row['counts'], {'v2ray': 1, 'clash': 1})
        cfg = next(x for x in self.call('api_subscriptions')['configs'] if x['id'] == endpoint)
        self.assertIn('缺少代理协议', cfg['error'])
        self.call('api_target_save', {'uri': 'ss://'+base64.b64encode(b'aes-128-gcm:test-password').decode()+'@target.example:443?plugin=unknown'}, self.target_id)
        row = self.call('api_subscriptions')['subscriptions'][0]
        self.assertEqual(row['counts'], {'v2ray': 1, 'clash': 0})
        body, _ = subscriptions.export(s._module(), self.db, row['token'], 'clash')
        self.assertIn('proxies: []', body)
        self.assertIn('REJECT', body)

    def test_reality_transport_and_quoted_values(self):
        key = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')
        uri = ('vless://11111111-1111-1111-1111-111111111111@target.example:443?security=reality&sni=sni.example'
               '&pbk='+key+'&sid=0123&type=ws&path='+quote('/a?b=1')+'&host=host.example&fp=chrome')
        self.call('api_target_save', {'uri': uri}, self.target_id)
        self.call('api_config_save', dict(remark='name: "quoted" # value'), self.cid)
        group = self.call('api_group_add', dict(kind='links', name='香港'))['id']
        with self.db:
            s.move_group(self.db, 'links', self.lid, group)
        row = self.create()
        body, _ = subscriptions.export(s._module(), self.db, row['token'], 'clash')
        proxy = json.loads(body.split('  - ', 1)[1].splitlines()[0])
        self.assertEqual(proxy['type'], 'vless')
        self.assertEqual(proxy['servername'], 'sni.example')
        self.assertEqual(proxy['reality-opts'], {'public-key': key, 'short-id': '0123'})
        self.assertEqual(proxy['ws-opts'], {'path': '/a?b=1', 'headers': {'Host': 'host.example'}})
        self.assertEqual(proxy['name'], '香港#name: "quoted" # value')
        body, _ = subscriptions.export(s._module(), self.db, row['token'], 'v2ray')
        parsed = s.target_config.import_uri(base64.b64decode(body).decode())
        self.assertEqual(parsed['name'], proxy['name'])
        self.assertEqual(s.target_config.import_uri(self.call('api_configs')['chains'][0]['connection']['uri'])['name'], proxy['name'])

    def test_public_route_without_panel_auth_and_revocation(self):
        row = self.create()
        old_panel, old_sync = s.Handler.panel_path, s.Handler.sync_path
        s.Handler.panel_path, s.Handler.sync_path = '/test-panel', '/test-sync'
        try:
            def request(path):
                h = transport.captured(s._module(), 'GET', path, [], b'', '127.0.0.1')
                h.route('GET')
                return h.response
            code, body, headers = request(row['path'] + '?format=clash')
            self.assertEqual(code, 200)
            self.assertIn(('Cache-Control', 'no-store'), headers)
            self.assertEqual(request('/sub/' + 'f'*64)[0], 404)
            self.assertEqual(request(row['path'] + '?format=invalid')[0], 404)
            self.call('api_subscription_save', dict(enabled=False), row['id'])
            self.assertEqual(request(row['path'])[0], 404)
        finally:
            s.Handler.panel_path, s.Handler.sync_path = old_panel, old_sync

    def test_link_order_drives_configs_and_export_without_rule_changes(self):
        cid2 = self.config(None, target_id=self.target_id)
        lid2 = self.db.execute('SELECT id FROM links WHERE chain_id=?', (cid2,)).fetchone()[0]
        self.call('api_config_save', dict(remark='second'), cid2)
        row = self.create([self.lid, lid2])
        group = self.call('api_group_add', dict(kind='links', name='first group'))['id']
        before = s.node_rules(self.db, s.get_node(self.db, self.a))
        with self.db:
            s.move_group(self.db, 'links', lid2, group)
        self.call('api_order', dict(groups=[group, s.default_group(self.db, 'links')], ids=[lid2, self.lid]), 'links')
        data = self.call('api_configs')
        self.assertEqual([c['link_id'] for c in data['chains']], [lid2, self.lid])
        self.assertEqual(data['chains'][0]['group_id'], group)
        body, _ = subscriptions.export(s._module(), self.db, row['token'], 'v2ray')
        uri = base64.b64decode(body).decode().splitlines()[0]
        self.assertEqual(s.target_config.import_uri(uri)['name'], 'first group#second')
        self.assertEqual(data['chains'][0]['connection']['uri'], uri)
        self.assertEqual(self.call('api_link_get', {}, lid2)['link']['connection']['uri'], uri)
        self.call('api_group_edit', dict(name='香港 # A'), group)
        body, _ = subscriptions.export(s._module(), self.db, row['token'], 'clash')
        self.assertEqual(json.loads(body.split('  - ', 1)[1].splitlines()[0])['name'], '香港 # A#second')
        self.call('api_group_del', {}, group)
        config = next(c for c in self.call('api_configs')['chains'] if c['link_id'] == lid2)
        self.assertEqual(s.target_config.import_uri(config['connection']['uri'])['name'], 'second')
        self.assertEqual(s.node_rules(self.db, s.get_node(self.db, self.a)), before)
        self.call('api_chain_del', {}, cid2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM subscription_links WHERE link_id=?', (lid2,)).fetchone()[0], 0)

    def test_v043_migration_preserves_rules_and_is_repeatable(self):
        before = s.node_rules(self.db, s.get_node(self.db, self.a))
        with self.db:
            for t in ('subscription_links', 'subscriptions', 'link_quality_samples'):
                self.db.execute('DROP TABLE '+t)
            self.db.execute('ALTER TABLE links DROP COLUMN group_id')
            self.db.execute('ALTER TABLE links DROP COLUMN sort')
            self.db.execute("DELETE FROM item_groups WHERE kind='links'")
            self.db.execute('PRAGMA user_version=403')
        self.db.close()
        self.db = s.init_db()
        self.assertEqual(s.node_rules(self.db, s.get_node(self.db, self.a)), before)
        s.migrate_v044(self.db)
        self.assertEqual(len(self.call('api_pool_list')['links']), 1)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
