import copy
import json
from pathlib import Path
import time
import unittest
from unittest import mock

from test_server import Fixture
import test_pools
import database
import errors
import forwarding
import migrations
import node_addresses
import pools
import topology
import topology_groups
import settings
import subscriptions
import target_config


class MultiTopologyTests(Fixture, unittest.TestCase):
    def ack(self, nid, ok=True):
        test_pools.PoolTests.ack(self, nid, ok)
        with self.db: self.db.execute("UPDATE nodes SET agent='0.5.1-rc.1' WHERE id=?", (nid,))
    tick = test_pools.PoolTests.tick
    get = test_pools.PoolTests.get

    def setUp(self):
        super().setUp()
        self.ids = [self.node(name, ports='10000-10100') for name in ('A','B','M','N')]
        self.targets = [self.target(), self.target()]
        for nid in self.ids:
            with self.db:
                self.db.execute("UPDATE nodes SET installed_agent='active',agent='0.5.1-rc.1',protocol_version=2,first_seen=1,last_seen=?,state='ok' WHERE id=?", (int(time.time()),nid))
                node_addresses.save(self.db,database.get_node(self.db,nid),[dict(id='public',name='公网',host=f'192.0.2.{nid}',device='eth0'),dict(id='private',name='内网',host=f'10.0.0.{nid}',device='eth1')])
            self.ack(nid)
        self.graph=dict(version=2,groups=[dict(id='pool',name='middle',locked=None)],vertices=[
            dict(id='a',kind='entry',node_id=self.ids[0],address='public'),
            dict(id='b',kind='entry',node_id=self.ids[1],address='public'),
            dict(id='m',kind='node',node_id=self.ids[2],group='pool'),
            dict(id='n',kind='node',node_id=self.ids[3],group='pool'),
            dict(id='c',kind='target',target_id=self.targets[0]),
            dict(id='d',kind='target',target_id=self.targets[1])],
            edges=[['a','pool'],['b','pool'],['pool','c'],['pool','d']],
            bindings={
                'a/pool':dict(**{'from':{'a':'public'},'to':{'m':'public','n':'public'}}),
                'b/pool':dict(**{'from':{'b':'public'},'to':{'m':'public','n':'public'}}),
                'pool/c':dict(**{'from':{'m':'private','n':'private'},'to':{}}),
                'pool/d':dict(**{'from':{'m':'private','n':'private'},'to':{}})})

    def save(self, graph=None, path=None, ident=None):
        body=dict(name='multi',graph=graph or self.graph,path=path or ['a','m','c'],apply=True)
        if ident: body['revision']=self.get(ident)['revision']
        return self.call('api_graph_save',body,ident)['id']

    def finish(self, ident):
        for _ in range(8):
            for nid in self.ids: self.ack(nid)
            self.tick()
            if not self.db.execute('SELECT 1 FROM link_batches WHERE root_id=?',(ident,)).fetchone() and not self.get(ident)['job'] and self.get(ident)['state']!='pending': return
        self.fail('batch did not finish')

    def test_explicit_nodes_have_independent_reachability(self):
        graph=topology.flatten(self.graph)
        graph['edges'].remove(['a','n'])
        graph=topology.validate(self.db,graph)
        preview=topology_groups.preview(self.db,graph,dict(required=['n']))
        self.assertEqual({p['entry'] for p in preview['paths']},{'b'})
        self.assertEqual(preview['skipped'][0]['entry'],'a')
        self.assertEqual(graph['bindings']['b/n'],{'from':{'b':'public'},'to':{'n':'public'}})
        self.assertNotIn('a/n',graph['bindings'])
        self.assertNotIn('groups',graph)
        self.assertFalse(any('group' in v for v in graph['vertices']))

    def test_pool_metadata_migration_preserves_pending_switch_and_ports(self):
        graph=copy.deepcopy(self.graph)
        graph['ports']={'a/c':10011,'a/d':10012,'b/c':10013,'b/d':10014}
        root=self.save(graph);self.finish(root)
        info=self.get(root)
        self.call('api_graph_save',dict(name='未发布',graph=info['graph'],apply=False,
            revision=info['revision']),root)
        info=self.get(root)
        self.call('api_graph_save',dict(name=info['name'],graph=info['active_graph'],
            path=['a','n','c'],apply=True,switch_only=True,revision=info['revision']),root)

        def legacy(value):
            if isinstance(value,list):return [legacy(v) for v in value]
            if not isinstance(value,dict):return value
            result={k:legacy(v) for k,v in value.items()}
            if 'vertices' in result and 'edges' in result:
                candidates={v['id'] for v in result['vertices'] if v['id'] in ('m','n')}
                if candidates:
                    result['groups']=[dict(id='pool',name='旧池',locked=None)]
                    for v in result['vertices']:
                        if v['id'] in candidates:v['group']='pool'
                    edges,bindings=[],{}
                    for a,b in result['edges']:
                        edge=['pool' if i in candidates else i for i in (a,b)]
                        if edge not in edges:edges.append(edge)
                        mapping=bindings.setdefault('/'.join(edge),{'from':{},'to':{}})
                        for side in ('from','to'):mapping[side].update(result.get('bindings',{}).get(a+'/'+b,{}).get(side,{}))
                    result.update(edges=edges,bindings=bindings)
            if result.get('previous_applied'):
                result['previous_applied']=json.dumps(legacy(json.loads(result['previous_applied'])))
            return result

        tables=(('links','id',('plan','applied','job','failure_scope','retained')),
                ('link_graphs','link_id',('draft',)),
                ('topology_canvases','root_id',('graph','applied_graph')),
                ('link_batches','root_id',('graph',)))
        with self.db:
            for table,key,columns in tables:
                for row in self.db.execute(f'SELECT * FROM {table}').fetchall():
                    for column in columns:
                        if row[column]:self.db.execute(f'UPDATE {table} SET {column}=? WHERE {key}=?',
                            (json.dumps(legacy(json.loads(row[column]))),row[key]))
            self.db.execute('PRAGMA user_version=501')
        rules={n:forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in self.ids}
        generations=[tuple(r) for r in self.db.execute('SELECT id,desired_generation FROM nodes')]
        ports={m['id']:pools.runtime_plan(m)['port'] for m in topology_groups.members(self.db,root)}
        pending={m['id']:json.loads(m['job'])['new'] for m in topology_groups.members(self.db,root) if m['job']}
        self.db.close();self.db=migrations.init_db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],settings.DATABASE_VERSION)
        self.assertTrue(Path(settings.DB_PATH+'.pre-node-topology.bak').exists())
        self.assertEqual(rules,{n:forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in self.ids})
        self.assertEqual(generations,[tuple(r) for r in self.db.execute('SELECT id,desired_generation FROM nodes')])
        self.assertEqual(pending,{m['id']:json.loads(m['job'])['new'] for m in topology_groups.members(self.db,root) if m['job']})
        for table,_,columns in tables:
            for row in self.db.execute(f'SELECT * FROM {table}'):
                for column in columns:
                    if row[column]:self.assertNotIn('"pool"',row[column],(table,column))
        info=self.get(root)
        self.assertEqual(info['draft']['name'],'未发布')
        self.assertEqual(info['active_graph']['bindings']['a/m'],{'from':{'a':'public'},'to':{'m':'public'}})
        self.finish(root)
        for member in topology_groups.members(self.db,root):
            plan=pools.runtime_plan(member)
            self.assertEqual(plan['port'],ports[member['id']])
            self.assertEqual(plan['graph_path'][1],'n' if member['entry']=='a' else 'm')
        self.assertFalse(self.get(root)['pending_changes'])
        self.db.close();self.db=migrations.init_db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],settings.DATABASE_VERSION)

    def test_four_configs_keep_ports_and_change_selected_entry_together(self):
        root=self.save();self.finish(root)
        configs=topology_groups.members(self.db,root)
        self.assertEqual(len(configs),4)
        ports={r['id']:pools.runtime_plan(r)['port'] for r in configs}
        self.assertEqual(len({(pools.runtime_plan(r)['entry'],ports[r['id']]) for r in configs}),4)
        for r in configs:
            self.assertEqual(pools.runtime_plan(r)['stages'][0]['node_id'],self.ids[2])
        self.save(path=['a','n','c'],ident=root);self.finish(root)
        for r in topology_groups.members(self.db,root):
            self.assertEqual(pools.runtime_plan(r)['port'],ports[r['id']])
            self.assertEqual(pools.runtime_plan(r)['stages'][0]['node_id'],self.ids[3] if r['entry']=='a' else self.ids[2])
        self.assertEqual(len(self.call('api_pool_list')['links']),1)
        self.assertEqual(self.call('api_link_get',{},root)['link']['state'],'active')

    def test_configuration_names_are_independent_metadata(self):
        self.call('api_target_save',dict(uri='ss://YWVzLTEyOC1nY206cGFzc3dvcmQ=@target.example:443'),self.targets[0])
        root=self.save();self.finish(root)
        configs={c['configuration_id']:c for c in self.call('api_configs')['chains']}
        self.assertEqual(len(configs),4)
        for lid,config in configs.items():
            self.assertEqual(config['remark'],f'配置 {lid:04d}')
            self.assertEqual(config['configuration_code'],f'C{lid:04d}')
        member=next(m for m in topology_groups.members(self.db,root) if m['target']=='c')
        config=configs[member['id']]
        self.call('api_subscription_save',dict(name='订阅',link_ids=[member['id']]))
        token=self.call('api_subscriptions')['subscriptions'][0]['token']

        def runtime():
            return (
                [tuple(r) for r in self.db.execute('SELECT id,chain_id,revision,plan,applied,job FROM links ORDER BY id')],
                [tuple(r) for r in self.db.execute('SELECT id,desired_generation FROM nodes ORDER BY id')],
                {n:forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in self.ids},
                [tuple(r) for r in self.db.execute('SELECT * FROM subscription_links')])
        before=runtime()
        name='香港专线 # 备用'
        self.call('api_config_rename',dict(name=name,previous_name=config['remark']),config['id'])
        self.assertEqual(runtime(),before)
        node=self.call('api_node_get',{},self.ids[0])
        self.assertEqual(next(c['remark'] for c in node['chains'] if c['id']==config['id']),name)
        self.assertEqual(next(r['chain_remark'] for r in node['rules'] if r['chain_id']==config['id']),name)
        target=next(t for t in self.call('api_targets')['targets'] if t['id']==self.targets[0])
        self.assertEqual(next(c['remark'] for c in target['configs'] if c['id']==config['id']),name)
        with self.assertRaisesRegex(errors.UserError,'名称已变化'):
            self.call('api_config_rename',dict(name='过期改名',previous_name=config['remark']),config['id'])
        for invalid in ('', 'a'*65, 'bad\x00name'):
            with self.assertRaises(errors.UserError):
                self.call('api_config_rename',dict(name=invalid,previous_name=name),config['id'])

        info=self.get(root)
        group=self.call('api_group_add',dict(kind='links',name='不会导出的分组'))['id']
        self.call('api_graph_save',dict(name='新的链路名称',group_id=group,graph=info['graph'],
            path=['a','n','c'],apply=True,revision=info['revision']),root)
        self.finish(root)
        after={c['configuration_id']:c for c in self.call('api_configs')['chains']}
        self.assertEqual(set(after),set(configs))
        for lid,c in after.items():
            self.assertEqual(c['id'],configs[lid]['id'])
            self.assertEqual(c['connection']['port'],configs[lid]['connection']['port'])
            self.assertEqual(c['remark'],name if lid==member['id'] else configs[lid]['remark'])
        self.assertEqual(target_config.import_uri(after[member['id']]['connection']['uri'])['name'],name)
        detail=next(c for c in self.get(root)['configurations'] if c['id']==member['id'])
        self.assertEqual(detail['name'],name)
        body,_=subscriptions.export(self.db,token,'clash')
        self.assertEqual(json.loads(body.split('  - ',1)[1].splitlines()[0])['name'],name)

    def test_configuration_name_migration_preserves_runtime_and_bindings(self):
        root=self.save();self.finish(root)
        legacy=self.config(None)
        self.call('api_config_save',dict(remark='已有手动名称'),legacy)
        ids=[m['id'] for m in topology_groups.members(self.db,root)]
        self.call('api_subscription_save',dict(name='保留绑定',link_ids=ids))
        self.save(path=['a','n','c'],ident=root)
        with self.db:
            self.db.execute('ALTER TABLE links DROP COLUMN config_name')
            self.db.execute('PRAGMA user_version=502')
        tables=('links','chains','link_batches','subscription_links')
        before={t:[dict(r) for r in self.db.execute('SELECT * FROM '+t)] for t in tables}
        rules={n:forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in self.ids}
        self.db.close();self.db=migrations.init_db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],settings.DATABASE_VERSION)
        self.assertTrue(Path(settings.DB_PATH+'.pre-config-names.bak').exists())
        for t in tables:
            after=[{k:v for k,v in dict(r).items() if k!='config_name'} for r in self.db.execute('SELECT * FROM '+t)]
            self.assertEqual(before[t],after)
        self.assertEqual(rules,{n:forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in self.ids})
        for member in topology_groups.members(self.db,root):
            self.assertEqual(member['config_name'],f"配置 {member['id']:04d}")
        config=next(c for c in self.call('api_configs')['chains'] if c['id']==legacy)
        self.assertEqual(config['remark'],'已有手动名称')
        self.call('api_config_rename',dict(name='新的自定名称',previous_name=config['remark']),legacy)
        self.db.close();self.db=migrations.init_db()
        self.finish(root)
        config=next(c for c in self.call('api_configs')['chains'] if c['id']==legacy)
        self.assertEqual(config['remark'],'新的自定名称')
        self.assertEqual([r[0] for r in self.db.execute('SELECT link_id FROM subscription_links ORDER BY link_id')],ids)

    def test_explicit_source_is_on_wire_and_export_uses_ingress(self):
        root=self.save();self.finish(root)
        rules=forwarding.node_rules(self.db,database.get_node(self.db,self.ids[2]))
        self.assertTrue(all(f'192.0.2.{self.ids[2]},10.0.0.{self.ids[2]},eth0,eth1,-,-' in r for r in rules))
        info=self.call('api_link_get',{},root)['link']
        self.assertEqual(info['connection']['host'],f'192.0.2.{self.ids[0]}')

    def test_runtime_switch_confirms_every_configuration_and_keeps_retry_on_new_path(self):
        graph=copy.deepcopy(self.graph)
        graph['ports']={'a/c':10011,'a/d':10012,'b/c':10013,'b/d':10014}
        root=self.save(graph);self.finish(root)
        original_ports={r['id']:pools.runtime_plan(r)['port'] for r in topology_groups.members(self.db,root)}
        for middle in ('n','m','n'):
            for nid in self.ids:self.ack(nid)
            self.tick()
            before=self.get(root)
            self.call('api_graph_save',dict(name=before['name'],group_id=before['group_id'],
                graph=before['active_graph'],path=['a',middle,'c'],revision=before['revision'],apply=True,switch_only=True),root)
            self.assertEqual(self.get(root)['active_path'],before['active_path'])
            self.finish(root)
            info=self.get(root)
            self.assertEqual(info['active_path'],['a',middle,'c'])
            self.assertEqual(info['state'],'active')
            self.assertFalse(info['pending_changes'])
            for member in topology_groups.members(self.db,root):
                plan=pools.runtime_plan(member)
                self.assertEqual(plan['port'],original_ports[member['id']])
                self.assertFalse(pools.pending_plan(member,self.db))
                self.assertEqual(json.loads(member['plan'])['graph_path'],plan['graph_path'])
                self.assertEqual(plan['graph'],topology.validate(self.db,plan['graph']))
        self.call('api_link_action',dict(action='retry',revision=self.get(root)['revision']),root)
        self.finish(root)
        self.assertEqual(self.get(root)['active_path'],['a','n','c'])
        self.assertFalse(self.get(root)['pending_changes'])

    def test_retry_node_receipts_without_a_switch_keeps_paths_and_rules(self):
        root=self.save();self.finish(root)
        before=self.get(root)
        plans={m['id']:m['applied'] for m in topology_groups.members(self.db,root)}
        rules={nid:forwarding.node_rules(self.db,database.get_node(self.db,nid)) for nid in self.ids}
        # The middle node is shared by all four configurations; the spare is not used.
        failed=self.ids[2]
        with self.db:
            self.db.execute('UPDATE nodes SET desired_generation=desired_generation+1 WHERE id=?',(failed,))
        self.ack(failed,False)
        generations={nid:database.get_node(self.db,nid)['desired_generation'] for nid in self.ids}
        info=self.get(root)
        self.assertEqual(info['state'],'error')
        self.assertTrue(info['sync']['retry'])
        self.assertIsNone(info['job'])
        result=self.call('api_link_action',dict(action='retry',revision=info['revision']),root)
        self.assertEqual(result['resynced'],1)
        for nid in self.ids:
            self.assertEqual(database.get_node(self.db,nid)['desired_generation'],generations[nid]+(nid==failed))
            self.assertEqual(forwarding.node_rules(self.db,database.get_node(self.db,nid)),rules[nid])
        self.assertEqual({m['id']:m['applied'] for m in topology_groups.members(self.db,root)},plans)
        self.assertFalse(self.db.execute('SELECT 1 FROM link_batches WHERE root_id=?',(root,)).fetchone())
        self.ack(failed)
        self.assertEqual(self.get(root)['state'],'active')
        self.assertEqual(self.get(root)['graph'],before['graph'])

    def test_retry_shared_node_queues_one_passive_wake(self):
        root=self.save();self.finish(root)
        nid=self.ids[0]
        with self.db:
            self.db.execute("UPDATE nodes SET installed_agent='passive',desired_mode='passive',state='error',wake_enabled=1,wake_port=61024,desired_generation=desired_generation+1 WHERE id=?",(nid,))
        generation=database.get_node(self.db,nid)['desired_generation']
        self.call('api_link_action',dict(action='retry',revision=self.get(root)['revision']),root)
        job=self.db.execute('SELECT * FROM wake_jobs WHERE node_id=?',(nid,)).fetchone()
        self.assertEqual(job['generation'],generation+1)
        self.assertEqual(job['state'],'waiting')
        self.assertEqual(job['attempts'],0)

    def test_node_retry_does_not_publish_a_previous_failed_edit(self):
        root=self.save();self.finish(root)
        graph=copy.deepcopy(self.graph)
        for entry in ('a','b'):graph['bindings'][entry+'/pool']['to']['m']='private'
        self.save(graph,ident=root);self.ack(self.ids[2],False);self.tick()
        for nid in self.ids:self.ack(nid)
        self.tick()
        # A later receipt failure must not reapply the earlier structural edit.
        nid=self.ids[0]
        with self.db:self.db.execute('UPDATE nodes SET desired_generation=desired_generation+1 WHERE id=?',(nid,))
        self.ack(nid,False)
        before=self.get(root)
        generation=database.get_node(self.db,nid)['desired_generation']
        rules={n:forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in self.ids}
        self.call('api_link_action',dict(action='retry',revision=before['revision']),root)
        after=self.get(root)
        self.assertEqual(database.get_node(self.db,nid)['desired_generation'],generation+1)
        self.assertEqual(after['graph'],before['graph'])
        self.assertEqual(after['active_graph'],before['active_graph'])
        self.assertTrue(after['pending_changes'])
        self.assertIsNone(after['job'])
        self.assertEqual({n:forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in self.ids},rules)

    def test_job_retries_share_one_receipt_generation(self):
        root=self.save();self.finish(root)
        self.switch_paths(root,[['a','n','c'],['a','n','d']])
        self.ack(self.ids[3]);self.tick()
        self.ack(self.ids[0],False)
        generation=database.get_node(self.db,self.ids[0])['desired_generation']
        self.call('api_link_action',dict(action='retry',revision=self.get(root)['revision']),root)
        self.assertEqual(database.get_node(self.db,self.ids[0])['desired_generation'],generation+1)
        self.finish(root)
        self.assertTrue(all(c['active_path'][1]=='n' for c in self.get(root)['configurations'] if c['entry']=='a'))

    def test_preview_prunes_branches_that_cannot_include_the_required_node(self):
        ids=self.ids[:]
        for i in range(28):
            nid=self.node('branch-'+str(i));ids.append(nid)
            with self.db:
                self.db.execute("UPDATE nodes SET installed_agent='active',agent='0.5.1-rc.1',protocol_version=2,first_seen=1,last_seen=?,state='ok' WHERE id=?",(int(time.time()),nid))
                node_addresses.save(self.db,database.get_node(self.db,nid),[dict(id='public',name='公网',host=f'192.0.2.{nid}',device='eth0')])
            self.ack(nid)
        graph=dict(version=2,groups=[],vertices=[
            dict(id='entry',kind='entry',node_id=ids[0],address='public'),
            dict(id='target',kind='target',target_id=self.targets[0])],edges=[],bindings={})
        previous=['entry']
        for i in range(15):
            layer=[f'n{i}a',f'n{i}b']
            graph['vertices'] += [dict(id=vid,kind='node',node_id=ids[1+i*2+j]) for j,vid in enumerate(layer)]
            graph['edges'] += [[a,b] for a in previous for b in layer]
            previous=layer
        graph['vertices'].append(dict(id='requested',kind='node',node_id=ids[31]))
        graph['edges'] += [[a,'target'] for a in previous]+[['entry','requested'],['requested','target']]
        for a,b in graph['edges']:
            graph['bindings'][a+'/'+b]={'from':{a:'public'},'to':{b:'public'} if b!='target' else {}}
        result=self.call('api_graph_preview',dict(graph=graph,required=['requested']))
        self.assertEqual([p['path'] for p in result['paths']],[['entry','requested','target']])
        incompatible=self.call('api_graph_preview',dict(graph=graph,required=['requested','n0a']))
        self.assertEqual(incompatible['paths'],[])
        continued=self.call('api_graph_preview',dict(graph=graph,prefix=['entry','requested'],required=['requested']))
        self.assertEqual(continued['options'],[])
        self.assertTrue(continued['complete'])

    def test_old_switch_metadata_does_not_look_like_an_unapplied_edit(self):
        graph=copy.deepcopy(self.graph)
        graph['ports']={'a/c':10011,'a/d':10012,'b/c':10013,'b/d':10014}
        root=self.save(graph);self.finish(root)
        info=self.get(root)
        full=info['active_graph']
        with self.db:
            for member in topology_groups.members(self.db,root):
                applied=json.loads(member['applied'])
                for key in ('address_book','bindings','ports'):
                    applied['plan']['graph'][key]=full[key]
                self.db.execute('UPDATE links SET applied=? WHERE id=?',(json.dumps(applied),member['id']))
            self.db.execute('UPDATE topology_canvases SET graph=? WHERE root_id=?',(json.dumps(full,indent=2),root))
        info=self.get(root)
        self.assertFalse(info['pending_changes'])
        self.assertEqual(info['state'],'active')
        self.assertEqual({c['state'] for c in info['configurations']},{'active'})
        # The formatting and stale metadata must not preserve an obsolete path.
        self.call('api_graph_save',dict(name=info['name'],group_id=info['group_id'],graph=full,
            path=['a','n','c'],revision=info['revision'],apply=True,switch_only=True),root)
        self.finish(root)
        info=self.get(root)
        self.assertEqual(info['graph_path'],['a','n','c'])
        self.assertFalse(info['pending_changes'])

    def test_runtime_switch_preserves_a_real_failed_address_edit(self):
        root=self.save();self.finish(root)
        graph=copy.deepcopy(self.graph)
        for entry in ('a','b'):graph['bindings'][entry+'/pool']['to']['m']='private'
        self.save(graph=graph,ident=root)
        self.ack(self.ids[2],False);self.tick()
        self.assertTrue(self.get(root)['pending_changes'])
        desired=self.get(root)['graph']
        for nid in self.ids:self.ack(nid)
        self.tick()
        info=self.get(root)
        self.call('api_graph_save',dict(name=info['name'],group_id=info['group_id'],graph=info['active_graph'],
            path=['a','n','c'],revision=info['revision'],apply=True,switch_only=True),root)
        self.finish(root)
        info=self.get(root)
        self.assertEqual(info['active_path'],['a','n','c'])
        self.assertTrue(info['pending_changes'])
        self.assertEqual(info['graph'],desired)

    def test_complete_branches_are_projected_only_to_their_endpoints(self):
        for sources, targets in ((('a',),('c','d')), (('a','b'),('c',)), (('a',),('c',))):
            with self.subTest(sources=sources,targets=targets):
                graph=copy.deepcopy(self.graph)
                next(v for v in graph['vertices'] if v['id']=='n')['group']=None
                for source in sources:
                    graph['edges'].append([source,'n'])
                    graph['bindings'][source+'/n']={'from':{source:'public'},'to':{'n':'public'}}
                for target in targets:
                    graph['edges'].append(['n',target])
                    graph['bindings']['n/'+target]={'from':{'n':'private'},'to':{}}
                # Preview and save must agree that this is a complete canvas.
                topology.preview(self.db,dict(graph=graph))
                root=self.save(graph);self.finish(root)
                info=self.get(root)
                self.assertEqual(info['state'],'active')
                self.assertIn('n',{v['id'] for v in info['graph']['vertices']})
                self.assertEqual(len(info['configurations']),4)
                for member in topology_groups.members(self.db,root):
                    projected=pools.runtime_plan(member)['graph']
                    present=member['entry'] in sources and member['target'] in targets
                    self.assertEqual('n' in {v['id'] for v in projected['vertices']},present)
                    self.assertEqual('n' in projected['address_book'],present)
                    self.assertEqual(projected,topology.validate(self.db,projected))
                    self.assertFalse(pools.pending_plan(member,self.db))

    def test_disconnected_node_is_still_rejected_with_its_name(self):
        graph=copy.deepcopy(self.graph)
        next(v for v in graph['vertices'] if v['id']=='n')['group']=None
        with self.assertRaises(errors.UserError) as caught:self.save(graph)
        self.assertIn('N（入口无法到达；无法到达目标）',str(caught.exception))
        self.assertEqual(caught.exception.field,'vertex:n')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM links').fetchone()[0],0)

    def test_missing_mapping_and_nonshared_route_rejected(self):
        graph=copy.deepcopy(self.graph);graph['bindings']['a/pool']['to'].pop('m')
        with self.assertRaises(errors.UserError):self.save(graph=graph)
        graph=copy.deepcopy(self.graph);graph['edges'].remove(['b','pool'])
        with self.assertRaises(errors.UserError):self.save(graph=graph)

    def test_all_downstream_receipts_precede_all_ingress_changes(self):
        root=self.save()
        for nid in self.ids[:2]:
            self.assertEqual(forwarding.node_rules(self.db,database.get_node(self.db,nid)),[])
        self.tick()
        self.assertEqual({json.loads(r['job'])['phase'] for r in topology_groups.members(self.db,root)}, {'prepare'})
        self.ack(self.ids[2]);self.tick()
        self.assertEqual({json.loads(r['job'])['phase'] for r in topology_groups.members(self.db,root)}, {'commit'})

    def test_removing_original_entry_keeps_other_configurations(self):
        root=self.save();self.finish(root)
        old={r['target']: (r['id'],pools.runtime_plan(r)['port']) for r in topology_groups.members(self.db,root) if r['entry']=='b'}
        graph=copy.deepcopy(self.graph)
        graph['vertices']=[v for v in graph['vertices'] if v['id']!='a']
        graph['edges']=[e for e in graph['edges'] if e[0]!='a']
        self.save(graph=graph,path=['b','m','c'],ident=root);self.finish(root)
        for r in topology_groups.members(self.db,root):
            self.assertEqual((r['id'],pools.runtime_plan(r)['port']),old[r['target']])
        self.assertEqual(len(self.call('api_link_get',{},root)['link']['configurations']),2)
        self.assertEqual(len(self.call('api_pool_list')['links']),1)

    def test_ports_and_address_references_are_enforced(self):
        graph=copy.deepcopy(self.graph);graph['ports']={'a/c':10011,'a/d':10012,'b/c':10013,'b/d':10014}
        root=self.save(graph=graph);self.finish(root)
        self.assertEqual({pools.runtime_plan(r)['port'] for r in topology_groups.members(self.db,root)},{10011,10012,10013,10014})
        with self.assertRaises(errors.UserError):
            self.call('api_node_edit',dict(addresses=[]),self.ids[0])

    def test_failed_preparation_preserves_all_existing_rules(self):
        root=self.save();self.finish(root)
        before={nid:forwarding.node_rules(self.db,database.get_node(self.db,nid)) for nid in self.ids[:3]}
        self.save(path=['a','n','c'],ident=root)
        self.ack(self.ids[3],False);self.tick()
        self.assertFalse(self.db.execute('SELECT 1 FROM link_batches').fetchone())
        for nid, rules in before.items():
            self.assertEqual(forwarding.node_rules(self.db,database.get_node(self.db,nid)),rules)

    def test_quality_keeps_configuration_bindings_and_reuses_measurements(self):
        import link_quality
        root=self.save();self.finish(root)
        nodes={r['id']:dict(r) for r in self.db.execute('SELECT * FROM nodes')}
        sections=link_quality.sections(self.db,pools.get(self.db,'links',root),nodes)
        self.assertEqual(len(sections),8)  # full + terminal hop for each configuration
        self.assertEqual(len({s['id'] for s in sections}),8)
        # Both target objects resolve to the same endpoint, so their probes also share tasks.
        self.assertEqual(len({json.dumps(p['identity'],sort_keys=True) for s in sections for p in s['probes']}),3)
        for section in sections:
            for probe in section['probes']:
                self.assertIn('source_route',probe)

    def test_sort_and_groups_use_canvas_not_hidden_configurations(self):
        root=self.save();self.finish(root)
        group=self.call('api_group_add',dict(kind='links',name='group'))['id']
        with self.db: database.move_group(self.db,'links',root,group)
        visible=self.call('api_pool_list')
        self.call('api_order',dict(groups=[r['id'] for r in visible['groups']],ids=[root]),'links')
        configs=self.call('api_configs')['chains']
        self.assertEqual({c['group_id'] for c in configs},{group})
        self.assertEqual({c['link_id'] for c in configs},{root})

    def test_middle_failure_does_not_invoke_old_whole_canvas_failover(self):
        root=self.save();self.finish(root)
        with self.db:self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(int(time.time())-61,self.ids[2]))
        self.tick()
        self.assertFalse(self.db.execute('SELECT 1 FROM link_batches WHERE root_id=?',(root,)).fetchone())
        self.assertTrue(all(pools.runtime_plan(r)['stages'][0]['node_id']==self.ids[2] for r in topology_groups.members(self.db,root)))
        self.assertFalse(self.get(root)['pending_changes'])

    def switch_paths(self, root, paths):
        info=self.get(root)
        return self.call('api_graph_save',dict(name=info['name'],graph=info['active_graph'],
            paths=paths,revision=info['revision'],apply=True,switch_only=True),root)

    def separate_branch(self):
        graph=copy.deepcopy(self.graph)
        next(v for v in graph['vertices'] if v['id']=='n')['group']=None
        graph['edges'] += [['a','n'],['n','c'],['n','d']]
        graph['bindings'].update({'a/n':{'from':{'a':'public'},'to':{'n':'public'}},
            'n/c':{'from':{'n':'private'},'to':{}},'n/d':{'from':{'n':'private'},'to':{}}})
        return graph

    def with_first_hop(self, graph=None, entries=('a',)):
        graph = topology.flatten(graph or self.separate_branch())
        nid = self.node('共同首跳', ports='10000-10100'); self.ids.append(nid)
        with self.db:
            self.db.execute("UPDATE nodes SET installed_agent='active',agent='0.5.1',protocol_version=2,first_seen=1,last_seen=?,state='ok' WHERE id=?", (int(time.time()), nid))
            node_addresses.save(self.db, database.get_node(self.db, nid), [dict(id='public', host=f'192.0.2.{nid}', device='eth0')])
        self.ack(nid)
        graph['vertices'].append(dict(id='first', kind='node', node_id=nid))
        for entry in entries:
            for source, target in list(graph['edges']):
                if source != entry: continue
                graph['edges'].remove([source, target])
                binding = graph['bindings'].pop(source+'/'+target)
                if ['first', target] not in graph['edges']: graph['edges'].append(['first', target])
                graph['bindings']['first/'+target] = {'from': {'first':'public'}, 'to':binding['to']}
            graph['edges'].append([entry, 'first'])
            graph['bindings'][entry+'/first'] = {'from':{entry:'public'}, 'to':{'first':'public'}}
        return graph

    def test_entries_need_no_common_middle_path(self):
        graph=self.separate_branch()
        graph['edges'].remove(['a','pool'])
        root=self.save(graph,path=['a','n','c']);self.finish(root)
        self.assertEqual({(c['entry'],c['target']):c['active_path'][1] for c in self.get(root)['configurations']},
            {('a','c'):'n',('a','d'):'n',('b','c'):'m',('b','d'):'m'})

    def test_partial_branch_preview_and_switch_leave_other_entry_untouched(self):
        root=self.save(self.separate_branch());self.finish(root)
        before=self.get(root)
        b_rules=forwarding.node_rules(self.db,database.get_node(self.db,self.ids[1]))
        response=topology.preview(self.db,dict(id=root,graph=before['active_graph'],required=['n']))
        self.assertEqual({(p['entry'],p['target']) for p in response['paths']},{('a','c'),('a','d')})
        self.assertEqual({p['entry'] for p in response['skipped']},{'b'})
        change=self.switch_paths(root,[p['path'] for p in response['paths']])
        self.assertEqual(change['changed'],2)
        pending=self.get(root)['configurations']
        self.assertTrue(all(c['active_path'][1]=='m' for c in pending))
        self.assertTrue(all(c['pending_path'][1]=='n' for c in pending if c['entry']=='a'))
        self.assertTrue(all(not c['pending_path'] for c in pending if c['entry']=='b'))
        self.finish(root)
        self.assertEqual(forwarding.node_rules(self.db,database.get_node(self.db,self.ids[1])),b_rules)
        self.assertEqual([(c['id'],c['port']) for c in before['configurations']],[(c['id'],c['port']) for c in self.get(root)['configurations']])

    def test_final_forwarding_node_completes_the_entry_without_mutating_preview(self):
        root=self.save(self.separate_branch());self.finish(root)
        before=self.get(root);graph=before['active_graph']
        for prefix,expected in ((['a'],{'m','n'}),(['b'],{'m'}),(['a','n'],set())):
            response=topology.preview(self.db,dict(id=root,graph=graph,prefix=prefix))
            self.assertEqual(set(response['options']),expected)
            self.assertEqual(response['complete'],len(prefix)==2)
        self.assertEqual([p['path'] for p in response['paths']],[['a','n','c'],['a','n','d']])
        # Full paths from older clients remain valid.
        response=topology.preview(self.db,dict(id=root,graph=graph,prefix=['a','n','d']))
        self.assertTrue(response['complete'])
        self.assertEqual([p['path'] for p in response['paths']],[['a','n','c'],['a','n','d']])
        self.assertEqual(before,self.get(root))
        self.switch_paths(root,[p['path'] for p in response['paths']]);self.finish(root)
        self.assertEqual([c['active_path'] for c in self.get(root)['configurations']],
            [['a','n','c'],['a','n','d'],['b','m','c'],['b','m','d']])

    def test_target_suffix_keeps_optional_middle_branches_selectable(self):
        graph=topology.flatten(self.graph)
        graph['edges'].append(['n','m'])
        graph['bindings']['n/m']={'from':{'n':'private'},'to':{'m':'public'}}
        root=self.save(graph,path=['a','n','m','c']);self.finish(root)
        before=self.get(root);graph=before['active_graph']
        body=dict(id=root,graph=graph,prefix=['a','n'])
        response=topology.preview(self.db,body)
        self.assertFalse(response['complete'])
        self.assertEqual(response['options'],['m'])
        self.assertEqual(response['path'],['a','n','m','c'])
        final=topology.preview(self.db,dict(body,finish=True))
        self.assertTrue(final['complete'])
        self.assertEqual([p['path'] for p in final['paths']],[['a','n','c'],['a','n','d']])
        self.assertEqual(self.get(root),before)
        # An absent target edge must never be invented during completion.
        graph=copy.deepcopy(graph);graph['edges'].remove(['n','d'])
        partial=topology.preview(self.db,dict(body,graph=graph,finish=True))
        self.assertTrue(partial['complete'])
        self.assertEqual([p['path'] for p in partial['paths']],[['a','n','c']])
        self.assertEqual(partial['skipped'][0]['targets'],['d'])
        last=topology.preview(self.db,dict(body,prefix=['a','n','m']))
        self.assertTrue(last['complete'])
        self.assertEqual(last['options'],[])

    def test_direct_entry_completes_all_targets_without_a_target_click(self):
        graph=copy.deepcopy(self.graph)
        graph['vertices']=[v for v in graph['vertices'] if v['kind']!='node']
        graph.pop('groups');graph['edges']=[[a,b] for a in ('a','b') for b in ('c','d')]
        graph['bindings']={a+'/'+b:{'from':{a:'public'},'to':{}} for a,b in graph['edges']}
        response=topology.preview(self.db,dict(graph=graph,prefix=['b']))
        self.assertTrue(response['complete'])
        self.assertEqual(response['options'],[])
        self.assertEqual([p['path'] for p in response['paths']],[['b','c'],['b','d']])

    def test_partial_failure_keeps_success_and_retries_only_failed_entry(self):
        graph=copy.deepcopy(self.graph)
        for target in ('c','d'):
            graph['edges'].append(['a',target])
            graph['bindings']['a/'+target]={'from':{'a':'public'},'to':{}}
        root=self.save(graph);self.finish(root)
        self.switch_paths(root,[['a','c'],['a','d'],['b','n','c'],['b','n','d']])
        self.ack(self.ids[3],False);self.tick()
        for _ in range(4):
            self.ack(self.ids[0]);self.ack(self.ids[2]);self.tick()
        configs=self.get(root)['configurations']
        success=next(c for c in configs if c['entry']=='a' and c['target']=='c')
        failed=next(c for c in configs if c['entry']=='b' and c['target']=='d')
        self.assertEqual(success['active_path'],['a','c'])
        self.assertEqual(failed['active_path'],['b','m','d'])
        self.assertEqual(failed['failed_path'],['b','n','d'])
        self.assertFalse(self.db.execute('SELECT 1 FROM link_batches').fetchone())
        success_rules=pools.rows(self.db,success['id'])
        self.ack(self.ids[3]);self.tick()
        self.call('api_link_action',dict(action='retry',revision=self.get(root)['revision']),root)
        batch=self.db.execute('SELECT members FROM link_batches').fetchone()
        self.assertEqual(set(json.loads(batch[0])),{c['id'] for c in configs if c['entry']=='b'})
        self.finish(root)
        self.assertEqual(pools.rows(self.db,success['id']),success_rules)
        self.assertEqual(next(c for c in self.get(root)['configurations'] if c['id']==failed['id'])['active_path'],['b','n','d'])

    def test_runtime_cancel_waits_for_entry_and_rejects_stale_request(self):
        root=self.save();self.finish(root)
        revision=self.get(root)['revision']
        self.switch_paths(root,[['a','n','c'],['a','n','d']]);self.ack(self.ids[3]);self.tick()
        self.call('api_link_action',dict(action='cancel',revision=self.get(root)['revision']),root)
        config=next(c for c in self.get(root)['configurations'] if c['entry']=='a' and c['target']=='d')
        self.assertEqual(config['active_path'],['a','m','d'])
        self.assertIsNotNone(pools.get(self.db,'links',config['id'])['job'])
        with self.assertRaises(errors.UserError):
            self.call('api_link_action',dict(action='retry',revision=revision),root)
        self.finish(root)
        self.assertEqual(next(c for c in self.get(root)['configurations'] if c['id']==config['id'])['active_path'],['a','m','d'])

    def test_retry_can_join_an_existing_switch_without_stalling(self):
        root=self.save();self.finish(root)
        self.switch_paths(root,[['a','n','c'],['a','n','d']])
        self.ack(self.ids[3],False);self.tick()
        failed=next(c for c in self.get(root)['configurations'] if c['entry']=='a' and c['target']=='c')
        self.ack(self.ids[3]);self.tick()
        self.switch_paths(root,[['b','n','c'],['b','n','d']])
        self.ack(self.ids[3]);self.tick()  # N is ready; B is still awaiting its ingress receipt.
        self.call('api_link_action',dict(action='retry',revision=self.get(root)['revision']),root)
        batch=json.loads(self.db.execute('SELECT members FROM link_batches').fetchone()[0])
        self.assertIn(failed['id'],batch)
        self.assertEqual(len(batch),4)
        self.finish(root)
        self.assertEqual({(c['entry'],c['target']) for c in self.get(root)['configurations'] if 'n' in c['active_path']},{('a','c'),('a','d'),('b','c'),('b','d')})

    def test_selecting_current_path_discards_a_failed_switch(self):
        root=self.save();self.finish(root)
        self.switch_paths(root,[['a','n','c'],['a','n','d']]);self.ack(self.ids[3],False);self.tick()
        self.assertTrue(self.get(root)['configurations'][0]['failed_path'])
        self.switch_paths(root,[['a','m','c'],['a','m','d']])
        self.assertFalse(self.get(root)['configurations'][0]['failed_path'])
        self.call('api_link_action',dict(action='retry',revision=self.get(root)['revision']),root)
        self.assertFalse(self.get(root)['job'])
        self.assertEqual(self.get(root)['active_path'],['a','m','c'])

    def test_quality_view_follows_selected_configuration(self):
        import asyncio
        import transport
        root=self.save();self.finish(root)
        self.switch_paths(root,[['a','n','c']]);self.finish(root)
        monitor=transport.Hub().quality
        configs=self.get(root)['configurations']
        for config in configs:
            view=asyncio.run(monitor.view(dict(scope='links',id=root,configuration=config['id'])))
            self.assertEqual(len(view['configurations']),4)
            self.assertEqual(view['configuration'],config['id'])
            self.assertTrue(all(s['id'].startswith(f"config:{config['id']}:") for s in view['sections']))
            self.assertEqual(view['sections'][0]['label'],('N' if config['active_path'][1]=='n' else 'M')+' → target')
        with self.assertRaises(errors.UserError):
            asyncio.run(monitor.view(dict(scope='links',id=root,configuration=99999)))

    def test_previous_shared_quality_history_is_rebound_without_copying_samples(self):
        root=self.save();self.finish(root)
        with self.db:
            self.db.execute("INSERT INTO probe_bindings(scope,owner_id,section,protocol,path_key,path_label,status,note,started) VALUES ('links',?,'target:c:full','tcp-connect-v1','history','M → target','unknown','test',100)",(root,))
            database.set_setting(self.db,'independent_path_history','')
            node_addresses.migrate(self.db)
        copied=[dict(r) for r in self.db.execute("SELECT * FROM probe_bindings WHERE section LIKE 'config:%'")]
        members=[m for m in topology_groups.members(self.db,root) if m['target']=='c']
        self.assertEqual({r['section'] for r in copied},{f"config:{m['id']}:full" for m in members})
        self.assertTrue(all(r['started']==100 and r['path_key']=='history' for r in copied))
        with self.db:node_addresses.migrate(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM probe_bindings').fetchone()[0],3)

    def test_removed_endpoints_remain_live_when_preparation_fails(self):
        root=self.save();self.finish(root)
        before={nid:forwarding.node_rules(self.db,database.get_node(self.db,nid)) for nid in self.ids[:3]}
        graph=copy.deepcopy(self.graph)
        graph['vertices']=[v for v in graph['vertices'] if v['id']!='a']
        graph['edges']=[e for e in graph['edges'] if e[0]!='a']
        self.save(graph=graph,path=['b','n','c'],ident=root)
        self.ack(self.ids[3],False);self.tick()
        self.assertEqual(len(topology_groups.members(self.db,root)),4)
        for nid,rules in before.items():
            self.assertEqual(forwarding.node_rules(self.db,database.get_node(self.db,nid)),rules)

    def test_cancel_restores_all_entries_before_old_path_cleanup(self):
        root=self.save();self.finish(root)
        self.save(path=['a','n','c'],ident=root)
        self.ack(self.ids[3]);self.tick()
        self.call('api_link_action',dict(action='cancel',revision=self.get(root)['revision']),root)
        self.assertEqual(self.db.execute('SELECT phase FROM link_batches WHERE root_id=?',(root,)).fetchone()[0],'rollback')
        self.finish(root)
        self.assertEqual(self.get(root)['active_path'],['a','m','c'])
        self.assertTrue(all(pools.runtime_plan(r)['stages'][0]['node_id']==self.ids[2] for r in topology_groups.members(self.db,root)))

    def test_old_graph_upgrade_retains_configuration_and_subscription(self):
        graph=copy.deepcopy(self.graph);graph['version']=1
        graph['vertices']=[v for v in graph['vertices'] if v['id'] not in ('b','d')]
        graph['edges']=[e for e in graph['edges'] if 'b' not in e and 'd' not in e]
        root=self.save(graph);self.finish(root)
        old=pools.get(self.db,'links',root);port=pools.runtime_plan(old)['port'];chain=old['chain_id']
        subscription=self.call('api_subscription_save',dict(name='saved',link_ids=[root]))['id']
        self.save(ident=root);self.finish(root)
        updated=pools.get(self.db,'links',root)
        self.assertEqual(updated['chain_id'],chain)
        self.assertEqual(pools.runtime_plan(updated)['port'],port)
        self.assertEqual(self.db.execute('SELECT link_id FROM subscription_links WHERE subscription_id=?',(subscription,)).fetchone()[0],root)

    def test_delete_canvas_waits_for_all_configuration_cleanup(self):
        root=self.save();self.finish(root)
        self.call('api_link_delete',{},root)
        self.assertEqual(len(self.call('api_pool_list')['links']),1)
        for _ in range(5):
            for nid in self.ids:self.ack(nid)
            self.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM links').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM link_members').fetchone()[0],0)

    def test_batch_resumes_after_database_reopen(self):
        import migrations
        root=self.save()
        self.db.close();self.db=migrations.init_db()
        self.finish(root)
        self.assertEqual(len(topology_groups.members(self.db,root)),4)
        self.assertEqual(self.get(root)['state'],'active')

    def test_automatic_switch_requires_wake_on_every_passive_entry(self):
        root=self.save();self.finish(root)
        with self.db:
            self.db.execute("UPDATE nodes SET desired_mode='passive',wake_enabled=0 WHERE id=?",(self.ids[1],))
            self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(int(time.time())-61,self.ids[2]))
        self.tick()
        self.assertFalse(self.db.execute('SELECT 1 FROM link_batches').fetchone())

    def test_new_canvas_requires_an_entry(self):
        with self.assertRaises(errors.UserError):
            self.save(dict(version=2,vertices=[dict(id='c',kind='target',target_id=self.targets[0])],groups=[],edges=[]),['c'])

    def test_address_inventory_and_labels_do_not_block_independent_switch(self):
        root=self.save();self.finish(root)
        values=node_addresses.listing(self.db,database.get_node(self.db,self.ids[2]))
        values[0]['name']='新的显示名称'
        values.append(dict(id='extra',name='备用地址',host='203.0.113.22',device='eth2'))
        self.call('api_node_edit',dict(addresses=values),self.ids[2])
        info=self.get(root)
        self.call('api_graph_save',dict(name=info['name'],graph=info['active_graph'],path=['a','n','c'],
            revision=info['revision'],apply=True,switch_only=True),root)
        self.finish(root)
        self.assertFalse(self.get(root)['pending_changes'])
        for nid in self.ids:self.ack(nid)
        with self.db:self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(int(time.time())-61,self.ids[3]))
        self.tick()
        self.assertFalse(self.db.execute('SELECT 1 FROM link_batches WHERE root_id=?',(root,)).fetchone())
        info=self.get(root)
        self.call('api_graph_save',dict(name=info['name'],graph=info['active_graph'],path=['a','m','c'],
            revision=info['revision'],apply=True,switch_only=True),root)
        self.finish(root)
        self.assertEqual(self.get(root)['active_path'],['a','m','c'])
        self.assertFalse(self.get(root)['pending_changes'])
        values[0]['host']='203.0.113.23'
        with self.assertRaisesRegex(errors.UserError,'引用'):
            self.call('api_node_edit',dict(addresses=values),self.ids[2])

    def test_switch_cannot_smuggle_new_address_binding(self):
        root=self.save();self.finish(root);info=self.get(root)
        graph=copy.deepcopy(info['active_graph'])
        graph['bindings']['a/m']['to']['m']='private'
        with self.assertRaisesRegex(errors.UserError,'运行拓扑已变化'):
            self.call('api_graph_save',dict(name=info['name'],graph=graph,path=['a','m','c'],
                revision=info['revision'],apply=True,switch_only=True),root)

    def test_promoting_primary_address_keeps_live_paths_rules_and_credentials(self):
        root=self.save();self.finish(root)
        before=self.get(root)
        nodes={nid:dict(database.get_node(self.db,nid)) for nid in self.ids}
        rules={nid:forwarding.node_rules(self.db,node) for nid,node in nodes.items()}
        values=node_addresses.listing(self.db,nodes[self.ids[2]])
        for a in values:a['primary']=a['id']=='private'
        self.call('api_node_edit',dict(addresses=values),self.ids[2])
        after=self.get(root)
        self.assertFalse(after['pending_changes'])
        self.assertEqual(after['active_graph'],before['active_graph'])
        self.assertFalse(self.db.execute('SELECT 1 FROM link_batches').fetchone())
        self.assertEqual(database.get_node(self.db,self.ids[2])['addr'],values[1]['host'])
        for nid,node in nodes.items():
            now=database.get_node(self.db,nid)
            self.assertEqual(now['key'],node['key'])
            self.assertEqual(now['desired_generation'],node['desired_generation'])
            self.assertEqual(forwarding.node_rules(self.db,now),rules[nid])
        self.switch_paths(root,[['a','n','c']]);self.finish(root)

    def test_unresolved_bound_address_waits_for_inventory_before_publish(self):
        nid=self.ids[2]
        values=node_addresses.listing(self.db,database.get_node(self.db,nid))
        values[0]['device']=''
        values[1]['device']=''
        self.call('api_node_edit',dict(addresses=values),nid)
        root=self.call('api_graph_save',dict(name='draft',graph=self.graph,apply=False))['id']
        with self.assertRaisesRegex(errors.UserError,'待识别'):
            self.save(ident=root)
        import agent_v3
        with self.db:
            agent_v3.save_snapshot(self.db,nid,'inventory',dict(interfaces=[dict(name='eth0',addresses=[f'192.0.2.{nid}/24'])]))
        values=node_addresses.listing(self.db,database.get_node(self.db,nid))
        values[1]['device']='eth1'
        self.call('api_node_edit',dict(addresses=values),nid)
        self.save(ident=root);self.finish(root)
        self.assertFalse(self.get(root)['pending_changes'])

    def test_failed_new_target_remains_protected_and_dangling_canvas_can_be_opened(self):
        root=self.save();self.finish(root)
        target=self.target(host='203.0.113.99')
        graph=copy.deepcopy(self.graph)
        graph['vertices'].append(dict(id='e',kind='target',target_id=target))
        graph['edges'].append(['pool','e'])
        graph['bindings']['pool/e']={'from':{'m':'private','n':'private'},'to':{}}
        self.save(graph,ident=root);self.ack(self.ids[2],False);self.tick()
        with self.assertRaisesRegex(errors.UserError,'引用'):
            self.call('api_target_del',{},target)
        self.assertTrue(self.get(root)['pending_changes'])
        # Previously released RCs could leave this dangling canvas reference.
        with self.db:self.db.execute('DELETE FROM targets WHERE id=?',(target,))
        info=self.get(root)
        self.assertEqual(len(info['configurations']),6)
        self.assertTrue(all(c['name'] and c['code'] for c in info['configurations']))
        self.assertEqual(len(self.call('api_pool_list')['links']),1)

    def test_resource_edit_cannot_reapply_failed_canvas(self):
        root=self.save();self.finish(root)
        graph=copy.deepcopy(self.graph)
        for entry in ('a','b'):graph['bindings'][entry+'/pool']['to']['m']='private'
        self.save(graph,ident=root);self.ack(self.ids[2],False);self.tick()
        for nid in self.ids:self.ack(nid)
        self.tick()
        old=dict(database.get_target(self.db,self.targets[0]))
        with self.assertRaisesRegex(errors.UserError,'未生效'):
            self.call('api_target_save',dict(name='new',host='198.51.100.55'),self.targets[0])
        self.assertEqual(dict(database.get_target(self.db,self.targets[0])),old)
        self.assertFalse(self.db.execute('SELECT 1 FROM link_batches').fetchone())
        self.assertTrue(self.get(root)['pending_changes'])
        self.assertEqual(pools.runtime_plan(pools.get(self.db,'links',root))['hop_addresses'][1],f'192.0.2.{self.ids[2]}')

    def test_route_metadata_survives_transactions_and_is_collected_afterwards(self):
        root=self.save()
        # The new ingress rule exists only in the job until the commit phase.
        ingress=json.loads(pools.get(self.db,'links',root)['job'])['new'][0]['id']
        with self.db:node_addresses.cleanup_routes(self.db)
        self.assertTrue(node_addresses.rule_route(self.db,ingress))
        self.finish(root)
        for middle in ('n','m','n'):
            for nid in self.ids:self.ack(nid)
            self.tick()
            self.save(path=['a',middle,'c'],ident=root);self.finish(root)
        for _ in range(3):
            for nid in self.ids:self.ack(nid)
            self.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM rule_routes WHERE rule_id NOT IN (SELECT id FROM link_rules) AND rule_id NOT IN (SELECT id FROM hops)').fetchone()[0],0)
        self.call('api_link_delete',{},root)
        for _ in range(5):
            for nid in self.ids:self.ack(nid)
            self.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM rule_routes').fetchone()[0],0)

    def test_partial_target_branch_switches_only_reachable_configurations(self):
        graph = self.with_first_hop()
        graph['edges'].remove(['n','d'])
        root = self.save(graph, path=['a','first','m','c']); self.finish(root)
        before = self.get(root)
        configs={c['id']:c for c in before['configurations']}
        selected=next(c for c in configs.values() if c['entry']=='a' and c['target']=='c')
        untouched={m['id']:dict(m) for m in topology_groups.members(self.db,root) if m['id']!=selected['id']}
        rules={lid:pools.rows(self.db,lid) for lid in untouched}
        response = topology.preview(self.db, dict(id=root, graph=before['active_graph'], required=['n']))
        self.assertEqual([p['path'] for p in response['paths']],[['a','first','n','c']])
        self.assertEqual(sum(s['count'] for s in response['skipped']),3)
        choices = topology.preview(self.db, dict(id=root, graph=before['active_graph'], prefix=['a','first']))
        self.assertIn('n', choices['options'])
        terminal=topology.preview(self.db,dict(id=root,graph=before['active_graph'],prefix=['a','first','n']))
        self.assertTrue(terminal['complete'])
        self.assertEqual([p['path'] for p in terminal['paths']],[['a','first','n','c']])
        self.assertEqual(self.get(root), before)
        result=self.switch_paths(root,[p['path'] for p in response['paths']])
        self.assertEqual(result['changed'],1)
        self.finish(root)
        after=self.get(root)
        self.assertEqual(next(c for c in after['configurations'] if c['id']==selected['id'])['active_path'],['a','first','n','c'])
        for c in after['configurations']:
            self.assertEqual((c['id'],c['port'],c['name']),(configs[c['id']]['id'],configs[c['id']]['port'],configs[c['id']]['name']))
        for m in topology_groups.members(self.db,root):
            if m['id'] in untouched:
                self.assertEqual(dict(m),untouched[m['id']])
                self.assertEqual(pools.rows(self.db,m['id']),rules[m['id']])
        # Publishing again must preserve each target's selected route.
        response=topology.preview(self.db,dict(id=root,graph=after['active_graph'],prefix=['a']))
        self.assertEqual({p['target']:p['path'] for p in response['paths']}, {'c':['a','first','n','c'],'d':['a','first','m','d']})
        self.call('api_graph_save',dict(name=after['name'],graph=after['active_graph'],revision=after['revision'],apply=True),root)
        self.finish(root)
        self.assertEqual([c['active_path'] for c in self.get(root)['configurations']],[c['active_path'] for c in after['configurations']])
        with self.assertRaises(errors.UserError):
            self.switch_paths(root, [['a','first','m','c'], ['a','first','n','d']])
        # Truly disconnected entry/target pairs still cannot be published.
        graph['edges'].remove(['m','c'])
        with self.assertRaisesRegex(errors.UserError, '无法到达目标'):
            topology.validate(self.db, graph)

    def test_exclusive_targets_need_no_common_final_node(self):
        graph=self.with_first_hop(self.graph, entries=('a','b'))
        graph['edges'].remove(['m','c']);graph['edges'].remove(['n','d'])
        root=self.save(graph,path=['a','first','n','c']);self.finish(root)
        configs=self.get(root)['configurations']
        self.assertEqual(len(configs),4)
        for c in configs:
            self.assertEqual(c['active_path'],[c['entry'],'first','n' if c['target']=='c' else 'm',c['target']])

    def test_target_gateway_is_followed_only_for_its_target(self):
        graph=topology.flatten(self.graph)
        graph['edges']=[['a','m'],['b','m'],['m','n'],['n','c'],['m','d']]
        graph['bindings']['m/n']={'from':{'m':'private'},'to':{'n':'public'}}
        root=self.save(graph,path=['a','m','n','c']);self.finish(root)
        response=topology.preview(self.db,dict(id=root,graph=self.get(root)['active_graph'],required=['m']))
        for p in response['paths']:
            expected=[p['entry'],'m','n','c'] if p['target']=='c' else [p['entry'],'m','d']
            self.assertEqual(p['path'],expected)
            self.assertTrue(p['unchanged'])

    def test_partial_target_failure_and_retry_leave_other_targets_unchanged(self):
        graph=self.with_first_hop();graph['edges'].remove(['n','d'])
        root=self.save(graph,path=['a','first','m','c']);self.finish(root)
        original={c['id']:c['active_path'] for c in self.get(root)['configurations']}
        self.switch_paths(root,[['a','first','n','c']]);self.ack(self.ids[3],False);self.tick()
        failed=self.get(root)['configurations']
        self.assertEqual({c['id']:c['active_path'] for c in failed},original)
        self.assertEqual([(c['entry'],c['target']) for c in failed if c['failed_path']],[('a','c')])
        for nid in self.ids: self.ack(nid)
        self.call('api_link_action',dict(action='retry',revision=self.get(root)['revision']),root)
        self.finish(root)
        for c in self.get(root)['configurations']:
            expected=['a','first','n','c'] if (c['entry'],c['target'])==('a','c') else original[c['id']]
            self.assertEqual(c['active_path'],expected)

    def test_pending_entry_does_not_block_other_entry_switch_or_confirmation(self):
        root = self.save(); self.finish(root)
        self.switch_paths(root, [['a','n','c'], ['a','n','d']]); self.ack(self.ids[3]); self.tick()
        info = self.get(root)
        response = topology.preview(self.db, dict(id=root, graph=info['active_graph'], required=['n']))
        self.assertEqual({p['entry'] for p in response['paths']}, {'b'})
        self.assertEqual({p['entry'] for p in response['skipped']}, {'a'})
        result = self.switch_paths(root, [['b','n','c'], ['b','n','d']])
        self.assertEqual(result['changed'], 2)
        self.ack(self.ids[3]); self.tick()
        self.ack(self.ids[1]); self.tick()
        configs = self.get(root)['configurations']
        self.assertTrue(all(c['active_path'][1]=='n' and not c['switching'] for c in configs if c['entry']=='b'))
        self.assertTrue(all(c['active_path'][1]=='m' and c['switching'] for c in configs if c['entry']=='a'))
        self.finish(root)
        self.assertTrue(all(c['active_path'][1]=='n' for c in self.get(root)['configurations']))

    def test_graph_only_changes_share_entry_barrier_and_rollback(self):
        graph = copy.deepcopy(self.graph)
        graph['vertices'].append(dict(id='alias', kind='node', node_id=self.ids[2]))
        graph['edges'] += [['a','alias'], ['alias','c'], ['alias','d']]
        graph['bindings'].update({'a/alias':{'from':{'a':'public'},'to':{'alias':'public'}},
            'alias/c':{'from':{'alias':'private'},'to':{}},
            'alias/d':{'from':{'alias':'public'},'to':{}}})
        root = self.save(graph); self.finish(root)
        before = self.get(root)
        self.switch_paths(root, [['a','alias','c'], ['a','alias','d']])
        self.assertTrue(all(c['active_path'][1]=='m' for c in self.get(root)['configurations']))
        rows = [m for m in topology_groups.members(self.db,root) if m['entry']=='a']
        self.assertEqual(sum(bool(json.loads(m['job']).get('metadata_only')) for m in rows), 1)
        self.ack(self.ids[2], False); self.tick()
        self.assertTrue(all(c['active_path'][1]=='m' for c in self.get(root)['configurations']))
        self.assertEqual([c['connection'] for c in self.get(root)['configurations']], [c['connection'] for c in before['configurations']])
        self.ack(self.ids[2]); self.tick()
        self.call('api_link_action',dict(action='retry',revision=self.get(root)['revision']),root)
        self.finish(root)
        self.assertTrue(all(c['active_path'][1]=='alias' for c in self.get(root)['configurations'] if c['entry']=='a'))
        for m in topology_groups.members(self.db,root):
            self.assertEqual(len(pools.rows(self.db,m['id'])), 2)

    def test_distinct_target_paths_still_allow_only_one_path_per_configuration(self):
        root = self.save(self.with_first_hop(),path=['a','first','m','c']); self.finish(root)
        result=self.switch_paths(root, [['a','first','m','c'], ['a','first','n','d']])
        self.assertEqual(result['changed'],1)
        self.finish(root)
        paths={(c['entry'],c['target']):c['active_path'] for c in self.get(root)['configurations']}
        self.assertEqual(paths[('a','c')],['a','first','m','c'])
        self.assertEqual(paths[('a','d')],['a','first','n','d'])
        before = self.get(root)
        with self.assertRaisesRegex(errors.UserError, '只能选择一条路径'):
            self.switch_paths(root, [['a','first','m','c'], ['a','first','n','c']])
        self.assertEqual(self.get(root), before)

    def test_explicit_switch_does_not_expand_to_other_targets(self):
        root = self.save(self.with_first_hop(),path=['a','first','m','c']); self.finish(root)
        result = self.switch_paths(root, [['a','first','n','c']])
        self.assertEqual(result['changed'], 1)
        self.finish(root)
        paths = {(c['entry'], c['target']): c['active_path'] for c in self.get(root)['configurations']}
        self.assertEqual(paths[('a', 'c')], ['a','first','n','c'])
        self.assertEqual(paths[('a', 'd')], ['a','first','m','d'])

    def test_switch_rejects_split_first_hops_without_changing_rules(self):
        root = self.save(); self.finish(root)
        before = self.get(root)
        rules = {n:forwarding.node_rules(self.db, database.get_node(self.db, n)) for n in self.ids}
        for paths in ([['a','n','c']], [['a','m','c'], ['a','n','d']]):
            result = self.switch_paths(root, paths)
            self.assertEqual(result['changed'], 0)
            self.assertIn('同一入口只能使用一个首跳', result['skipped'][0]['reason'])
            self.assertEqual(self.get(root), before)
            self.assertEqual(rules, {n:forwarding.node_rules(self.db, database.get_node(self.db, n)) for n in self.ids})
        with self.assertRaisesRegex(errors.UserError, '同一入口只能使用一个首跳'):
            self.call('api_graph_save', dict(name=before['name'], graph=before['active_graph'],
                revision=before['revision'], apply=True, paths=[['a','m','c'],['a','n','d']]), root)
        self.assertEqual(self.get(root), before)

    def test_unusable_first_hop_is_not_offered_or_published(self):
        graph = self.separate_branch(); graph['edges'].remove(['n','d'])
        root = self.save(graph); self.finish(root)
        graph = self.get(root)['active_graph']
        before = self.get(root)
        preview = topology.preview(self.db, dict(id=root, graph=graph, prefix=['a']))
        self.assertEqual(preview['options'], ['m'])
        for selection in (dict(required=['n']), dict(prefix=['a','n'])):
            preview = topology.preview(self.db, dict(id=root, graph=graph, **selection))
            self.assertEqual(preview['paths'], [])
            self.assertIn('统一首跳', preview['skipped'][0]['reason'])
        with self.assertRaisesRegex(errors.UserError, '统一首跳'):
            self.save(graph, path=['a','n','c'], ident=root)
        self.assertEqual(self.get(root), before)
        # Each target is reachable on its own, but no first hop covers both.
        graph['edges'].remove(['m','c'])
        graph['edges'].append(['b','n'])
        graph['bindings']['b/n'] = {'from':{'b':'public'},'to':{'n':'public'}}
        with self.assertRaisesRegex(errors.UserError, '统一首跳'):
            self.save(graph, path=['a','n','c'])

    def test_publish_uses_requested_first_hop_for_unlisted_targets(self):
        graph = topology.flatten(self.graph)
        graph['edges'].remove(['n','d']); graph['edges'].append(['n','m'])
        graph['bindings']['n/m'] = {'from':{'n':'private'},'to':{'m':'public'}}
        root = self.save(graph, path=['a','n','c']); self.finish(root)
        paths = {c['target']:c['active_path'] for c in self.get(root)['configurations'] if c['entry']=='a'}
        self.assertEqual(paths, {'c':['a','n','c'], 'd':['a','n','m','d']})

    def test_full_preview_finds_common_hop_when_old_downstream_is_offline(self):
        graph = self.with_first_hop(self.graph)
        graph['edges'] = [[e, n] for e in ('a','b') for n in ('m','n')] + [
            ['m','c'],['m','first'],['first','d'],['n','c'],['n','d']]
        graph['bindings'] = {a+'/'+b:{'from':{a:'public'},'to':{b:'public'} if b not in ('c','d') else {}}
                             for a, b in graph['edges']}
        root = self.save(graph); self.finish(root)
        with self.db:
            self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?', (int(time.time())-3600, self.ids[-1]))
        info = self.get(root)
        preview = topology.preview(self.db, dict(id=root, graph=info['active_graph']))
        self.assertEqual(preview['skipped'], [])
        self.assertEqual(len(preview['paths']), 4)
        self.assertEqual({p['path'][1] for p in preview['paths']}, {'n'})

    def test_new_first_hop_also_moves_targets_outside_clicked_branch(self):
        graph = self.with_first_hop()
        graph['edges'] = [['a','m'],['b','m'],['m','c'],['m','d'],
                          ['a','first'],['first','n'],['first','d'],['n','c']]
        graph['bindings']['a/m'] = {'from':{'a':'public'},'to':{'m':'public'}}
        graph['bindings']['first/d'] = {'from':{'first':'public'},'to':{}}
        root = self.save(graph); self.finish(root)
        preview = topology.preview(self.db, dict(id=root,graph=self.get(root)['active_graph'],required=['n']))
        self.assertEqual([p['path'] for p in preview['paths']], [['a','first','n','c'],['a','first','d']])
        result = self.switch_paths(root, [p['path'] for p in preview['paths']])
        self.assertEqual(result['changed'], 2)
        self.finish(root)
        self.assertEqual({c['active_path'][1] for c in self.get(root)['configurations'] if c['entry']=='a'}, {'first'})

    def test_legacy_split_is_reported_and_repaired_only_after_confirmation(self):
        root = self.save(); self.finish(root)
        with mock.patch.object(topology_groups, 'check_entry_hop'):
            self.switch_paths(root, [['a','n','c']])
        self.finish(root)
        before = self.get(root)
        self.assertEqual(before['entry_conflicts'], ['a'])
        preview = topology.preview(self.db, dict(id=root, graph=before['active_graph'], required=['n']))
        self.assertEqual(self.get(root), before)
        result = self.switch_paths(root, [p['path'] for p in preview['paths'] if p['entry']=='a'])
        self.assertEqual(result['changed'], 1)
        self.assertEqual(self.get(root)['entry_conflicts'], ['a'])
        self.finish(root)
        self.assertEqual(self.get(root)['entry_conflicts'], [])
        self.assertEqual({c['active_path'][1] for c in self.get(root)['configurations'] if c['entry']=='a'}, {'n'})

    def test_retry_preserves_each_failed_target_path(self):
        root = self.save(self.with_first_hop(),path=['a','first','m','c']); self.finish(root)
        self.switch_paths(root, [['a','first','m','c'], ['a','first','n','d']]); self.finish(root)
        wanted = [['a','first','n','c'], ['a','first','m','d']]
        self.switch_paths(root, wanted)
        self.ack(self.ids[3], False); self.tick()
        failed = [c for c in self.get(root)['configurations'] if c['entry'] == 'a']
        self.assertEqual([c['failed_path'] for c in failed], wanted)
        for nid in self.ids: self.ack(nid)
        self.call('api_link_action', dict(action='retry', revision=self.get(root)['revision']), root)
        self.finish(root)
        self.assertEqual([c['active_path'] for c in self.get(root)['configurations'] if c['entry'] == 'a'], wanted)

    def test_capacity_failure_rolls_back_entire_entry_request(self):
        root = self.save(); self.finish(root)
        with self.db:
            self.db.execute("UPDATE nodes SET ports='10100-10100' WHERE id=?", (self.ids[3],))
        before = {n: forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in self.ids}
        result = self.switch_paths(root, [['a','n','c'], ['a','n','d']])
        self.assertEqual(result['changed'], 0)
        self.assertEqual([s['entry'] for s in result['skipped']], ['a'])
        self.assertFalse(self.get(root)['job'])
        self.assertEqual(before, {n: forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in self.ids})

    def test_failed_target_edit_keeps_applied_names_and_exports(self):
        root = self.save(); self.finish(root)
        old = self.get(root)
        target = self.target('203.0.113.77', 8443)
        self.call('api_target_save', dict(name='NEW-TARGET'), target)
        graph = copy.deepcopy(self.graph)
        next(v for v in graph['vertices'] if v['id']=='c')['target_id'] = target
        self.save(graph=graph, ident=root); self.ack(self.ids[2], False); self.tick()
        info = self.get(root)
        self.assertTrue(info['pending_changes'])
        self.assertEqual([c['name'] for c in info['configurations']], [c['name'] for c in old['configurations']])
        self.assertEqual([c['connection'] for c in info['configurations']], [c['connection'] for c in old['configurations']])

    def test_live_address_inventory_sizes_layout_without_republishing(self):
        root = self.save(); self.finish(root)
        old = self.get(root)['active_graph']
        values = node_addresses.listing(self.db, database.get_node(self.db, self.ids[2]))
        values += [dict(id='extra'+str(i),name='extra',host='203.0.113.'+str(i+1),device='eth2') for i in range(7)]
        self.call('api_node_edit', dict(addresses=values), self.ids[2])
        info = self.get(root)
        vertices = {v['id']: v for v in info['view_graph']['vertices']}
        self.assertGreaterEqual(vertices['n']['y'], vertices['m']['y'] + 62 + 24*len(values))
        self.assertEqual(info['active_graph'], old)
        self.assertFalse(info['pending_changes'])

    def test_canvas_direct_retains_unknown_quality_sections(self):
        import link_quality
        graph = copy.deepcopy(self.graph)
        graph['vertices'] = [v for v in graph['vertices'] if v['id'] not in ('b','d')]
        graph['vertices'].append(dict(id='bypass',kind='direct'))
        graph['groups'] = []
        for v in graph['vertices']: v.pop('group',None)
        graph['edges'] = [['a','m'],['m','bypass'],['bypass','n'],['n','c']]
        graph['bindings'] = {'a/m':{'from':{'a':'public'},'to':{'m':'public'}},
            'm/bypass':{'from':{'m':'private'},'to':{}},'bypass/n':{'from':{},'to':{'n':'private'}},
            'n/c':{'from':{'n':'public'},'to':{}}}
        root = self.save(graph, path=['a','m','bypass','n','c']); self.finish(root)
        nodes = {n['id']: dict(n) for n in self.db.execute('SELECT * FROM nodes')}
        sections = link_quality.sections(self.db, pools.get(self.db,'links',root), nodes)
        self.assertEqual(len(sections), 4)
        self.assertEqual(sections[0]['label'], 'M → N → target')
        for section in sections[1:3]:
            self.assertTrue(all(p['reason']==('unknown','direct') for p in section['probes']))

    def test_detail_does_not_expand_other_canvases(self):
        from unittest.mock import patch
        root=self.save();self.finish(root)
        other=self.save();self.finish(other)
        with patch.object(pools,'detail',wraps=pools.detail) as detail:
            info=self.call('api_link_get',{},root)
            self.assertEqual(detail.call_count,1)
            self.assertEqual(info['link']['id'],root)
        with patch('node_state.node_status',wraps=pools.node_state.node_status) as status:
            self.call('api_link_get',{},root)
            self.assertEqual(status.call_count,len(self.ids))
