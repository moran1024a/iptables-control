"""Weights and visual modifiers must not change forwarding identity or IP choices."""
import copy
import json
import unittest

from test_server import Fixture
import configurations
import database
import errors
import link_quality
import node_addresses
import topology


class WeightTests(Fixture, unittest.TestCase):
    def test_global_weights_and_direct_override_choose_maximum_sum(self):
        e, a, b = [self.node(n) for n in ('entry', 'A', 'B')]
        for nid, weight in ((e, 1000), (a, -8), (b, 4)):
            self.call('api_node_edit', dict(weight=weight), nid)
        g = self.graph([e, a])
        t = g['vertices'][-1]['id']
        g['vertices'] += [dict(id=f'v{b}', kind='node', node_id=b, weight=999), dict(id='d', kind='direct', weight=20)]
        g['edges'] = [[f'v{e}', 'd'], ['d', f'v{a}'], [f'v{a}', t], [f'v{e}', f'v{b}'], [f'v{b}', t]]
        checked = topology.validate(self.db, g)
        best = topology.find_path(checked)
        self.assertEqual(best, [f'v{e}', 'd', f'v{a}', t])
        self.assertEqual(topology.path_weight(checked, best), 12)  # Entry does not count.
        self.assertEqual(next(v['weight'] for v in checked['vertices'] if v['id'] == f'v{b}'), 4)
        self.assertEqual(topology.find_path(checked, forbidden=[best]), [f'v{e}', f'v{b}', t])
        for bad in (True, 1.5, '1', 100001, -100001):
            with self.assertRaises(errors.UserError):
                self.call('api_node_edit', dict(weight=bad), b)
        self.assertEqual(database.get_node(self.db, b)['weight'], 4)

    def test_weight_edits_preserve_existing_route_and_do_not_require_agent_sync(self):
        e, a, b = [self.node() for _ in range(3)]
        self.ready(e, a, b)
        g = self.graph([e, a])
        t = g['vertices'][-1]['id']
        g['vertices'].append(dict(id=f'v{b}', kind='node', node_id=b))
        g['edges'] += [[f'v{e}', f'v{b}'], [f'v{b}', t]]
        ident = self.save(g, paths=[[f'v{e}', f'v{a}', t]])
        self.settle()
        before = [tuple(r) for r in self.db.execute('SELECT * FROM rules')]
        generations = [tuple(r) for r in self.db.execute('SELECT id,desired_generation FROM nodes')]
        self.call('api_node_edit', dict(weight=100), b)
        self.save(g, ident)
        self.tick()
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM rules')], before)
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT id,desired_generation FROM nodes')], generations)
        self.assertEqual(json.loads(configurations.members(self.db, ident)[0]['desired'])['graph_path'], [f'v{e}', f'v{a}', t])
        refreshed = configurations.detail(self.db, ident)['graph']
        self.assertEqual(next(v['weight'] for v in refreshed['vertices'] if v['id'] == f'v{b}'), 100)

    def test_inserting_direct_preserves_rules_binding_port_and_quality_series(self):
        e, a = self.node(), self.node()
        addresses = node_addresses.listing(self.db, database.get_node(self.db, a))
        addresses.append(dict(id='alt', host='10.77.0.2', primary=False))
        self.call('api_node_edit', dict(addresses=addresses), a)
        self.ready(e, a)
        g = self.graph([e, a])
        edge = f'v{e}/v{a}'
        g['bindings'] = {edge: dict(to={f'v{a}': 'alt'})}
        ident = self.save(g)
        self.settle()
        nodes = {r['id']: dict(r) for r in self.db.execute('SELECT * FROM nodes')}
        config = configurations.members(self.db, ident)[0]
        probes = link_quality.config_sections(self.db, config, nodes)
        rules = [tuple(r) for r in self.db.execute('SELECT * FROM rules')]
        graph = json.loads(configurations.get_topology(self.db, ident)['graph'])
        graph['vertices'].append(dict(id='d', kind='direct', weight=-2))
        graph['edges'] = [x for x in graph['edges'] if x != [f'v{e}', f'v{a}']] + [[f'v{e}', 'd'], ['d', f'v{a}']]
        binding = graph['bindings'].pop(edge)
        graph['bindings'][f'v{e}/d'] = {'from': binding['from'], 'to': {}}
        graph['bindings'][f'd/v{a}'] = {'from': {}, 'to': binding['to']}
        self.save(graph, ident)
        self.tick()
        after = configurations.members(self.db, ident)[0]
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM rules')], rules)
        self.assertEqual(after['state'], 'active')
        self.assertIn('d', json.loads(after['applied'])['plan']['graph_path'])
        self.assertEqual(link_quality.config_sections(self.db, after, nodes), probes)
        preview = configurations.preview(self.db, dict(id=ident, graph=graph, prefix=[f'v{e}']))
        self.assertEqual(preview['options'], ['d'])
        # Fresh deployment of the same graph also emits only two physical rules.
        graph['ports'] = {}
        second = self.save(graph)
        self.settle()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM rules WHERE configuration_id IN (SELECT id FROM configurations WHERE topology_id=?)', (second,)).fetchone()[0], 2)

    def test_terminal_direct_does_not_require_an_extra_selection(self):
        e = self.node()
        g = self.graph([e])
        target = g['vertices'][-1]['id']
        g['vertices'].append(dict(id='d', kind='direct', weight=-5))
        g['edges'] = [[f'v{e}', 'd'], ['d', target]]
        result = configurations.preview(self.db, dict(graph=g, prefix=[f'v{e}']))
        self.assertTrue(result['complete'])
        self.assertEqual(result['options'], [])
        self.assertEqual(result['path'], [f'v{e}', 'd', target])

    def test_direct_cannot_hide_self_reference_or_branch_or_float(self):
        e, a = self.node(), self.node()
        g = self.graph([e, a])
        g['vertices'].append(dict(id='d', kind='direct', weight=0))
        with self.assertRaisesRegex(errors.UserError, '两端'):
            self.save(g)
        g['edges'] += [[f'v{e}', 'd'], ['d', f'v{a}']]
        self.assertTrue(topology.validate(self.db, g))
        bad = copy.deepcopy(g)
        bad['edges'].append(['d', g['vertices'][2]['id']])
        with self.assertRaisesRegex(errors.UserError, '上游'):
            self.save(bad)
        bad = copy.deepcopy(g)
        bad['vertices'][1]['node_id'] = e
        bad['edges'] = [[f'v{e}', 'd'], ['d', f'v{a}']]
        with self.assertRaisesRegex(errors.UserError, '同一台'):
            self.save(bad)

    def test_layers_follow_longest_root_path_ignore_direct_and_stay_stable(self):
        e, f, a, b, c, orphan = [self.node() for _ in range(6)]
        g = self.graph([e, a, b])
        t = g['vertices'][-1]['id']
        g['vertices'] += [dict(id=f'v{f}', kind='entry', node_id=f), dict(id=f'v{c}', kind='node', node_id=c), dict(id='d', kind='direct', weight=3), dict(id='orphan', kind='node', node_id=orphan)]
        g['edges'] += [[f'v{f}', f'v{c}'], [f'v{c}', 'd'], ['d', f'v{b}'], [f'v{e}', f'v{b}']]
        arranged = topology.arrange(topology.validate(self.db, g))
        by = {v['id']: v for v in arranged['vertices']}
        self.assertEqual([by[i]['layer'] for i in (f'v{e}', f'v{f}', f'v{a}', f'v{c}', f'v{b}', t)], [0, 0, 1, 1, 2, 3])
        self.assertIsNone(by['d']['layer'])
        self.assertIsNone(by['orphan']['layer'])
        self.assertEqual(topology.arrange(arranged), arranged)
        self.assertEqual(len(arranged['vertices']), len(g['vertices']))
