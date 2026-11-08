import unittest
from test_server import Fixture
import configurations
import database
import errors
import node_tags


class NodeTagTests(Fixture, unittest.TestCase):
    def test_multi_tags_shared_rename_remove_and_no_agent_change(self):
        a, b = self.node(), self.node()
        one = self.call('api_tag_save', dict(name='香港', color='#3b82f6'))['id']
        two = self.call('api_tag_save', dict(name='中转', color='#22a35a'))['id']
        before = [tuple(r) for r in self.db.execute('SELECT id,desired_generation,sort,group_id,weight FROM nodes')]
        self.call('api_node_edit', dict(tag_ids=[one, two]), a)
        self.call('api_node_edit', dict(tag_ids=[one]), b)
        self.assertEqual([t['id'] for t in self.call('api_node_get', {}, a)['node']['tags']], [one, two])
        self.call('api_tag_save', dict(name='区域 A', color='#123456'), one)
        self.assertEqual(node_tags.listing(self.db, b)[0]['name'], '区域 A')
        self.call('api_tag_delete', {}, one)
        self.assertEqual(node_tags.listing(self.db, b), [])
        self.assertEqual([t['id'] for t in node_tags.listing(self.db, a)], [two])
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT id,desired_generation,sort,group_id,weight FROM nodes')], before)

    def test_bad_tags_are_atomic_and_catalogue_follows_node_order(self):
        a, b, c = [self.node(n) for n in 'ABC']
        tag = self.call('api_tag_save', dict(name='label'))['id']
        self.call('api_node_edit', dict(tag_ids=[tag]), a)
        for ids in ([999], [True], ['1'], '1'):
            with self.subTest(ids=ids), self.assertRaises(errors.UserError):
                self.call('api_node_edit', dict(tag_ids=ids, weight=42), a)
        self.assertEqual(database.get_node(self.db, a)['weight'], 0)
        self.assertEqual(node_tags.listing(self.db, a)[0]['id'], tag)
        with self.assertRaises(errors.UserError):
            self.call('api_tag_save', dict(name='LABEL'))
        with self.assertRaises(errors.UserError):
            self.call('api_tag_save', dict(name='unsafe', color='red;display:none'))
        group = self.call('api_group_add', dict(kind='nodes', name='first'))['id']
        self.call('api_node_edit', dict(group_id=group), c)
        self.call('api_node_edit', dict(group_id=group), a)
        default = database.default_group(self.db, 'nodes')
        self.call('api_order', dict(groups=[group, default], ids=[c, a, b]), 'nodes')
        resources = configurations.resources(self.db)
        self.assertEqual([g['id'] for g in resources['node_groups']], [group, default])
        self.assertEqual([n['id'] for n in resources['nodes']], [c, a, b])
        self.assertEqual(resources['nodes'][1]['tags'][0]['id'], tag)
