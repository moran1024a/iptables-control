import json
import unittest

from test_server import Fixture
from test_imports import ss_uri, enc_uri
import configurations
import database
import errors
import execution
import link_quality
import target_config
import target_addresses


class TargetAddressTests(Fixture, unittest.TestCase):
    def create_routes(self):
        e, f, a, b = [self.node(n) for n in ('entry A', 'entry B', 'middle A', 'middle B')]
        self.ready(e, f, a, b)
        tid = self.call('api_target_save', dict(uri=ss_uri(), addresses=[dict(id='alternate', name='内网', host='10.0.0.4', port=8443)]))['id']
        g = self.graph([e, a], tid)
        second = self.graph([f, b], tid)
        g['vertices'] += second['vertices'][:-1]
        g['edges'] += second['edges']
        g['bindings'] = {f'v{b}/t{tid}': {'to': {f't{tid}': 'alternate'}}}
        ident = self.save(g)
        self.settle()
        return ident, tid, g

    def test_independent_addresses_keep_identity_credentials_and_probe_endpoints(self):
        ident, tid, graph = self.create_routes()
        configs = configurations.members(self.db, ident)
        self.assertEqual(len(configs), 2)
        nodes = {r['id']: dict(r) for r in self.db.execute('SELECT * FROM nodes')}
        endpoints = []
        for config in configs:
            applied = json.loads(config['applied'])
            last = applied['rules'][-1]
            endpoints.append((last['host'], last['tport']))
            target = execution.applied_target(self.db, config)
            self.assertEqual(target['id'], tid)
            self.assertEqual(target['params'], database.get_target(self.db, tid)['params'])
            sections = link_quality.config_sections(self.db, config, nodes)
            for section in sections:
                tcp = next(p for p in section['probes'] if p['protocol'] == 'tcp-connect-v1')
                self.assertEqual((tcp['host'], tcp['port']), (last['host'], last['tport']))
                self.assertEqual(tcp['identity']['endpoint'], (last['host'], last['tport']))
            exported = target_config.import_uri(configurations.view(self.db, config)['connection']['uri'])
            self.assertEqual(exported['params'], target_config.import_uri(ss_uri())['params'])
        self.assertEqual(endpoints, [('192.0.2.50', 23456), ('10.0.0.4', 8443)])
        item = self.call('api_targets')['targets'][0]
        self.assertEqual(target_config.import_uri(item['uri'])['host'], '192.0.2.50')
        self.assertEqual([a['id'] for a in item['addresses']], ['main', 'alternate'])
        summaries = configurations.configs(self.db)['configs']
        self.assertTrue(all(c['topology_name'] == 'network' and c['topology_id'] == ident for c in summaries))
        self.assertEqual(summaries[0]['target_address'], dict(id='main', name='默认', host='192.0.2.50', port=23456))
        self.assertEqual(summaries[1]['target_address'], dict(id='alternate', name='内网', host='10.0.0.4', port=8443))

    def test_address_edit_only_updates_users_and_preserves_confirmed_snapshot(self):
        ident, tid, graph = self.create_routes()
        original = configurations.members(self.db, ident)
        other = original[0]
        config = original[1]
        self.call('api_target_save', dict(addresses=[dict(id='alternate', host='10.0.0.5', port=9443)]), tid)
        after = configurations.members(self.db, ident)
        self.assertEqual(after[0]['state'], 'active')
        self.assertEqual(after[1]['state'], 'pending')
        self.assertEqual(execution.applied_target(self.db, after[1])['host'], '10.0.0.4')
        self.assertEqual(target_addresses.resolve(self.db, json.loads(after[1]['desired']))['host'], '10.0.0.5')
        self.assertEqual(after[1]['applied'], config['applied'])
        self.assertEqual(configurations.view(self.db, after[1])['target_address'],
                         dict(id='alternate', name='内网', host='10.0.0.4', port=8443))
        self.settle()
        final = configurations.members(self.db, ident)
        self.assertEqual(json.loads(final[0]['applied'])['rules'], json.loads(other['applied'])['rules'])
        self.assertEqual(execution.applied_target(self.db, final[1])['host'], '10.0.0.5')
        self.assertEqual(json.loads(final[1]['applied'])['rules'][-1]['tport'], 9443)
        self.assertEqual(configurations.view(self.db, final[1])['target_address'],
                         dict(id='alternate', name='10.0.0.5:9443', host='10.0.0.5', port=9443))

    def test_endpoint_label_and_source_rename_without_reapplying_rules(self):
        ident, tid, graph = self.create_routes()
        before = configurations.members(self.db, ident)[1]
        self.call('api_target_save', dict(addresses=[dict(id='alternate', name='备用接入', host='10.0.0.4', port=8443)]), tid)
        self.call('api_topology_save', dict(graph=graph, name='新链路名称',
                                          revision=configurations.get_topology(self.db, ident)['revision']), ident)
        row = configurations.members(self.db, ident)[1]
        self.assertEqual(row['state'], 'active')
        self.assertEqual(row['applied'], before['applied'])
        summary = configurations.configs(self.db)['configs'][1]
        self.assertEqual(summary['topology_name'], '新链路名称')
        self.assertEqual(summary['target_address']['name'], '备用接入')

    def test_selected_address_survives_direct_and_cannot_be_deleted_while_used(self):
        ident, tid, graph = self.create_routes()
        with self.assertRaisesRegex(errors.UserError, '引用'):
            self.call('api_target_save', dict(addresses=[]), tid)
        last, target = graph['edges'][-1]
        graph['vertices'].append(dict(id='modifier', kind='direct', weight=2))
        graph['edges'].remove([last, target])
        graph['edges'] += [[last, 'modifier'], ['modifier', target]]
        graph['bindings'] = {f'modifier/{target}': {'to': {target: 'alternate'}}}
        self.save(graph, ident)
        self.settle()
        config = configurations.members(self.db, ident)[1]
        self.assertEqual(execution.applied_target(self.db, config)['host'], '10.0.0.4')
        graph['bindings'][f'modifier/{target}']['to'][target] = 'main'
        self.save(graph, ident)
        with self.assertRaisesRegex(errors.UserError, '引用'):
            self.call('api_target_save', dict(addresses=[]), tid)
        self.settle()
        self.call('api_target_save', dict(addresses=[]), tid)
        self.assertEqual(len(self.call('api_targets')['targets'][0]['addresses']), 1)

    def test_validation_and_reality_credentials(self):
        tid = self.call('api_target_save', dict(uri=enc_uri()))['id']
        self.call('api_target_save', dict(addresses=[dict(id='same-ip', host='192.0.2.8', port=443), dict(id='other-port', host='192.0.2.8', port=8443)]), tid)
        before = self.call('api_targets')['targets'][0]
        self.assertEqual(target_config.import_uri(before['uri'])['params'], target_config.import_uri(enc_uri())['params'])
        for values in ([dict(id='main', host='192.0.2.2', port=443)], [dict(id='bad', host='192.0.2.2', port=0)], [dict(id='dup', host=before['host'], port=before['port'])]):
            with self.subTest(values=values), self.assertRaises(errors.UserError):
                self.call('api_target_save', dict(addresses=values), tid)
        self.assertEqual(self.call('api_targets')['targets'][0]['addresses'], before['addresses'])
