import base64
import json
import unittest
from urllib.parse import quote
from test_server import Fixture
from test_imports import ss_uri, enc_uri, reality_uri
import configurations, database, errors, proxy_formats, subscriptions, target_config

UUID = '00000000-0000-4000-8000-000000000001'


def examples():
    vmess = dict(v='2',ps='VMess',add='vm.example.com',port='443',id=UUID,aid='0',
                 scy='auto',net='ws',type='none',host='ws.example.com',path='/socket',tls='tls',sni='tls.example.com')
    return [ss_uri(), 'vmess://'+base64.b64encode(json.dumps(vmess).encode()).decode(),
            enc_uri().split('&security=')[0]+'&security=none#ENC',
            'trojan://p%40ss%3Aword@trojan.example.com:443?sni=tls.example.com#Trojan',
            'hy2://auth%3Asecret@hy.example.com?obfs=salamander&obfs-password=obfs&insecure=0#HY2',
            f'tuic://{UUID}%3Ap%40ss@tuic.example.com:443?congestion_control=bbr&udp_relay_mode=native#TUIC',
            'anytls://p%3Ass@tls.example.com:443?sni=s.example.com#AnyTLS',
            'socks5://user:p%40ss@socks.example.com:1080#SOCKS5',
            'https://user:p%3Ass@proxy.example.com:443?sni=tls.example.com#HTTP']


class FormatTests(unittest.TestCase):
    def test_nine_families_round_trip_rename_and_convert(self):
        for uri in examples():
            with self.subTest(uri=uri.split('://')[0]):
                node = proxy_formats.parse(uri)
                renamed = proxy_formats.parse(proxy_formats.share(node,'名称 + # &'))
                self.assertEqual(renamed['name'],'名称 + # &')
                self.assertEqual((node['host'],node['port']),(renamed['host'],renamed['port']))
                proxy = proxy_formats.mihomo(renamed,renamed['name'])
                self.assertEqual(proxy['server'],node['host'])
                self.assertEqual(proxy['name'],'名称 + # &')
        self.assertEqual(len({proxy_formats.parse(u)['protocol'] for u in examples()}),9)

    def test_enc_preserves_keys_transports_and_original_tls_identity(self):
        uri = enc_uri().split('&security=')[0]+'&security=tls&type=ws&path=%2Fsock#ENC'
        parsed = target_config.import_uri(uri)
        target = dict(type=parsed['protocol'],host=parsed['host'],port=parsed['port'],params=json.dumps(parsed['params']))
        node = proxy_formats.parse(target_config.connection(target,'entry.example.com',10001,'test')['uri'])
        self.assertEqual(dict(node['params']['share_query'])['sni'],'enc.example.com')
        proxy = proxy_formats.mihomo(node,'test')
        self.assertEqual(proxy['servername'],'enc.example.com')
        self.assertEqual(proxy['encryption'],dict(parsed['params']['share_query'])['encryption'])
        self.assertEqual(proxy['ws-opts']['path'],'/sock')
        self.assertEqual(proxy['ws-opts']['headers']['Host'],'enc.example.com')
        # An additional target IP must retain the same TLS identity and Host header.
        alias = dict(target, host='192.0.2.88', port=8443)
        second = proxy_formats.parse(target_config.connection(alias,'entry.example.com',10002,'other')['uri'])
        self.assertEqual(dict(second['params']['share_query'])['sni'],'enc.example.com')

    def test_external_reality_ipv6_and_unknown_fields_are_preserved(self):
        node = proxy_formats.parse(reality_uri())
        with self.assertRaises(ValueError): target_config.import_uri(reality_uri())
        with self.assertRaises(ValueError): proxy_formats.mihomo(node,'test')
        self.assertEqual(proxy_formats.parse(proxy_formats.share(node,'renamed'))['params'],node['params'])
        ipv6 = proxy_formats.parse('socks5://[2001:db8::1]:1080#IPv6')
        self.assertEqual(proxy_formats.mihomo(ipv6,'test')['server'],'2001:db8::1')
        self.assertIn('[2001:db8::1]',proxy_formats.share(ipv6,'renamed'))

    def test_invalid_input_is_rejected_without_network_or_type_errors(self):
        for uri in ['file:///etc/passwd','https://example.com/subscription?token=x','ss://invalid',
                    'vmess://W10=', 'vmess://eyJpZCI6MSwiaWQiOjJ9',
                    f'vless://{UUID}@example.com:443?encryption=invalid',
                    f'vless://{UUID}@example.com:443?security=tls&security=none',
                    'http://example.com:0','http://example.com:65536',
                    'anytls://p@example.com:443?insecure=maybe',
                    'http://example.com#bad%00name', 'http://example.com\nhttp://evil.test',
                    'ss://'+'a'*8192]:
            with self.subTest(uri=uri[:50]), self.assertRaises(ValueError): proxy_formats.parse(uri)

    def test_security_options_cannot_be_silently_discarded(self):
        for uri in ['hy2://p@example.com?pinSHA256=bad', 'anytls://p@example.com:443?future_security=1',
                    'https://example.com?insecure=1&skip-cert-verify=0']:
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                proxy_formats.mihomo(proxy_formats.parse(uri),'test')
        node = proxy_formats.parse('hy2://p@example.com?pinSHA256='+'ab'*32)
        self.assertEqual(proxy_formats.mihomo(node,'test')['fingerprint'],'ab'*32)

    def test_encoded_credentials_and_malformed_vmess_types(self):
        for scheme in ('https','socks5'):
            node=proxy_formats.parse(f'{scheme}://user%3Aname:p%40ss%3Aword@example.com:443#test')
            self.assertEqual((node['params']['username'],node['params']['password']),('user:name','p@ss:word'))
        for ident in (True, 1, None):
            uri='vmess://'+base64.b64encode(json.dumps(dict(add='example.com',port=443,id=ident)).encode()).decode()
            with self.assertRaises(ValueError):proxy_formats.parse(uri)
        node=proxy_formats.parse('SOCKS5://example.com:1080')
        self.assertTrue(proxy_formats.share(node,'test').startswith('socks5://'))


class ExternalTests(Fixture, unittest.TestCase):
    def add(self, uri=None, **kw):
        return self.call('api_external_save',dict(uri=uri or examples()[0],**kw))['id']

    def test_ids_names_revisions_and_deletion_leave_nodes_unchanged(self):
        n = self.node(); before=dict(database.get_node(self.db,n))
        e1,e2=self.add(),self.add()
        self.call('api_external_delete',{},e2)
        self.assertGreater(self.add(),e2)
        sid=self.call('api_subscription_save',dict(name='test',external_configuration_ids=[e1]))['id']
        self.call('api_external_save',dict(name='Renamed',revision=1),e1)
        with self.assertRaises(errors.UserError): self.call('api_external_save',dict(name='stale',revision=1),e1)
        row=subscriptions.get(self.db,sid)
        link=base64.b64decode(subscriptions.export(self.db,row['token'],'v2ray')[0]).decode().strip()
        self.assertEqual(proxy_formats.parse(link)['name'],'Renamed')
        self.call('api_external_delete',{},e1)
        self.assertEqual(subscriptions.listing(self.db)['subscriptions'][0]['external_configuration_ids'],[])
        self.assertEqual(dict(database.get_node(self.db,n)),before)
        for table in ('topologies','configurations','rules','jobs','probe_tasks'):
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_mixed_selection_and_format_counts_with_duplicate_numeric_ids(self):
        n=self.node();self.ready(n)
        t=self.call('api_target_save',dict(uri=ss_uri()))['id']
        topo=self.save(self.graph([n],t));self.settle()
        cid=configurations.members(self.db,topo)[0]['id']
        ids=[self.add(uri,name='same') for uri in examples()]
        self.assertEqual(cid,ids[0])
        self.call('api_config_rename',dict(name='same',previous_name=configurations.members(self.db,topo)[0]['name']),cid)
        sid=self.call('api_subscription_save',dict(name='Mixed',configuration_ids=[cid],external_configuration_ids=ids))['id']
        listing=subscriptions.listing(self.db)
        self.assertEqual(listing['subscriptions'][0]['counts'],dict(clash=10,v2ray=9))
        token=subscriptions.get(self.db,sid)['token']
        clash=subscriptions.export(self.db,token,'clash')[0]
        names=[json.loads(line[4:])['name'] for line in clash.split('proxy-groups:')[0].splitlines() if line.startswith('  - ')]
        self.assertEqual(len(names),len(set(names)))
        self.assertIn('same [E0001]',names)
        self.call('api_subscription_save',dict(enabled=False),sid)
        self.assertIsNone(subscriptions.export(self.db,token,'clash'))
        self.call('api_subscription_save',dict(enabled=True,configuration_ids=[]),sid)
        self.assertEqual(len(subscriptions.listing(self.db)['subscriptions'][0]['external_configuration_ids']),9)

    def test_groups_order_and_invalid_selection_are_atomic(self):
        a,b=self.add(name='A'),self.add(name='B')
        group=self.call('api_group_add',dict(kind='external_configurations',name='Later'))['id']
        self.call('api_external_save',dict(group_id=group,revision=1),b)
        default=database.default_group(self.db,'external_configurations')
        self.call('api_order',dict(groups=[group,default],ids=[b,a]),'external_configurations')
        self.assertEqual([x['id'] for x in self.call('api_configs')['configs']],[b,a])
        sid=self.call('api_subscription_save',dict(name='test',external_configuration_ids=[a,b]))['id']
        self.assertEqual([x['id'] for x in subscriptions.listing(self.db)['configs']],[b,a])
        with self.assertRaises(errors.UserError):
            self.call('api_subscription_save',dict(name='bad',external_configuration_ids=[True]),sid)
        self.assertEqual(subscriptions.get(self.db,sid)['name'],'test')
        self.call('api_group_del',{},group)
        self.assertEqual(self.db.execute('SELECT group_id FROM external_configurations WHERE id=?',(b,)).fetchone()[0],default)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])
