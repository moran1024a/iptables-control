"""Graph compilation through actual rule transactions, receipts and migrations."""
import copy
import json
from pathlib import Path
import time
import unittest
import hashlib
import shutil
import subprocess
import threading

from test_server import Fixture
import http_harness
import agent_v3
import node_sync
import node_addresses
import test_pools as legacy_tests
import agent_files
import database
import errors
import forwarding
import migrations
import pools
import topology
import settings
import link_quality


class TopologyTests(Fixture, unittest.TestCase):
    ack = legacy_tests.PoolTests.ack
    tick = legacy_tests.PoolTests.tick
    get = legacy_tests.PoolTests.get
    finish = legacy_tests.PoolTests.finish

    def setUp(self):
        super().setUp()
        self.old_agent = agent_files.AGENT
        agent_files.load_agent()
        self.entry,self.a,self.b,self.c = [self.node(n,ports='10000-10100') for n in ('entry','a','b','c')]
        self.target_id=self.target()
        for nid in (self.entry,self.a,self.b,self.c):
            with self.db:
                self.db.execute("UPDATE nodes SET installed_agent='active',agent='0.4.7',addr=?,protocol_version=2,first_seen=1,last_seen=?,state='ok' WHERE id=?",(f'192.0.2.{nid}',int(time.time()),nid))
            self.ack(nid)
        self.graph=dict(version=1,groups=[dict(id='pool',name='池',locked=None)],vertices=[
            dict(id='entry',kind='entry',node_id=self.entry,x=40,y=100),
            dict(id='a',kind='node',node_id=self.a,group='pool',x=300,y=100),
            dict(id='b',kind='node',node_id=self.b,group='pool',x=300,y=200),
            dict(id='c',kind='node',node_id=self.c,x=550,y=100),
            dict(id='target',kind='target',target_id=self.target_id,x=800,y=100)],
            edges=[['entry','a'],['entry','b'],['a','target'],['b','c'],['c','target']])

    def tearDown(self):
        agent_files.AGENT=self.old_agent
        super().tearDown()

    def save(self,path=None,ident=None,**kw):
        body=dict(name='graph',graph=self.graph,port=10001,apply=path is not None,path=path)
        if ident: body['revision']=self.get(ident)['revision']
        body.update(kw)
        return self.call('api_graph_save',body,ident)['id']

    def test_branch_choice_and_complete_path_validation(self):
        g=topology.validate(self.db,self.graph)
        r=topology.preview(self.db,dict(graph=g,prefix=['entry'],required=['b']))
        self.assertEqual(r['options'],['b'])
        self.assertEqual(r['path'],['entry','b','target'])
        self.assertEqual(g['edges'],[['entry','a'],['entry','b'],['a','target'],['b','target'],
                                    ['a','c'],['b','c'],['c','target']])
        self.assertNotIn('groups',g)
        self.assertFalse(any('group' in v for v in g['vertices']))
        r=topology.preview(self.db,dict(graph=g,required=['b'],preferred=['entry','a','c','target']))
        self.assertEqual(r['path'],['entry','b','c','target'])
        with self.assertRaises(errors.UserError):self.save(['entry','c','target'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM links').fetchone()[0],0)

    def test_draft_is_inert_and_preserves_live_path(self):
        lid=self.save(['entry','a','target']);self.finish(lid)
        before=[forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in (self.entry,self.a,self.b,self.c)]
        generations=[r[0] for r in self.db.execute('SELECT desired_generation FROM nodes')]
        self.graph['edges'].remove(['a','target'])
        self.save(ident=lid)
        self.assertIsNotNone(self.get(lid)['draft'])
        self.assertEqual(before,[forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in (self.entry,self.a,self.b,self.c)])
        self.assertEqual(generations,[r[0] for r in self.db.execute('SELECT desired_generation FROM nodes')])
        self.assertEqual(self.get(lid)['active_path'],['entry','a','target'])

    def test_batched_choice_preserves_continuation_and_stops_at_new_branch(self):
        complete=topology.preview(self.db,dict(graph=self.graph,required=['b'],
            preferred=['entry','a','c','target'],advance=True))
        self.assertEqual(complete['prefix'],['entry','b','c','target'])
        self.assertTrue(complete['complete'])
        decision=topology.preview(self.db,dict(graph=self.graph,required=['b'],advance=True))
        self.assertEqual(decision['prefix'],['entry','b'])
        self.assertEqual(decision['options'],['c'])
        self.assertFalse(decision['complete'])
        final=topology.preview(self.db,dict(graph=self.graph,prefix=['entry','b','c']))
        self.assertEqual(final['prefix'],['entry','b','c','target'])
        self.assertTrue(final['complete'])
        self.assertEqual(final['options'],[])
        direct=topology.preview(self.db,dict(graph=self.graph,prefix=['entry','b'],finish=True,
            preferred=['entry','b','c','target']))
        self.assertEqual(direct['path'],['entry','b','target'])
        self.assertTrue(direct['complete'])

    def test_same_rule_branch_switch_does_not_revert_when_retrying(self):
        self.graph['vertices'].append(dict(id='alias',kind='node',node_id=self.a,x=300,y=420))
        self.graph['edges'] += [['entry','alias'],['alias','target']]
        lid=self.save(['entry','a','target']);self.finish(lid)
        generations=[r[0] for r in self.db.execute('SELECT desired_generation FROM nodes')]
        self.save(['entry','alias','target'],lid,switch_only=True,graph=self.get(lid)['active_graph'])
        switched=self.get(lid)
        self.assertIsNone(switched['job'])
        self.assertEqual(switched['active_path'],['entry','alias','target'])
        self.assertEqual(switched['plan']['graph_path'],switched['active_path'])
        self.call('api_link_action',dict(action='retry',revision=switched['revision']),lid)
        self.assertEqual(self.get(lid)['active_path'],switched['active_path'])
        self.assertEqual(generations,[r[0] for r in self.db.execute('SELECT desired_generation FROM nodes')])

    def test_invalid_pool_reference_returns_validation_error(self):
        for reference in ([],{},7,'entry','missing'):
            graph=copy.deepcopy(self.graph);graph['vertices'][1]['group']=reference
            with self.subTest(reference=reference),self.assertRaises(errors.UserError):
                topology.validate(self.db,graph)

    def test_pool_edges_share_all_members_and_apply_arranges_without_reordering_path(self):
        self.graph['edges']=[['entry','pool'],['pool','target'],['pool','c'],['c','target']]
        # Editing is free-form and saving a draft preserves those coordinates.
        self.graph['vertices'][1].update(x=900,y=300)
        self.graph['vertices'][2].update(x=50,y=180)
        lid=self.save()
        self.assertEqual(self.get(lid)['draft']['graph']['vertices'][1]['x'],900)
        graph=topology.validate(self.db,self.graph)
        for member in ('a','b'):
            for path in (['entry',member,'target'],['entry',member,'c','target']):
                self.assertEqual(topology.check_path(graph,path),path)
        self.save(['entry','a','c','target'],lid);self.finish(lid)
        active=self.get(lid)['active_graph'];vertices={v['id']:v for v in active['vertices']}
        self.assertEqual(vertices['a']['x'],vertices['b']['x'])
        self.assertLess(vertices['b']['y'],vertices['a']['y'])
        self.assertLess(vertices['entry']['x'],vertices['a']['x'])
        self.assertLess(vertices['a']['x'],vertices['c']['x'])
        self.assertLess(vertices['c']['x'],vertices['target']['x'])
        self.assertEqual(self.get(lid)['active_path'],['entry','a','c','target'])
        self.save(['entry','b','target'],lid,switch_only=True,graph=active);self.finish(lid)
        self.assertEqual(self.get(lid)['active_graph'],active)

    def test_pool_to_pool_expansion_and_empty_pool_cycle(self):
        g=copy.deepcopy(self.graph)
        g['groups'].append(dict(id='second',name='第二池',locked=None))
        g['vertices'][3]['group']='second'
        extra=copy.deepcopy(g['vertices'][3]);extra['id']='d';g['vertices'].append(extra)
        g['edges']=[['entry','pool'],['pool','second'],['second','target']]
        graph=topology.validate(self.db,g)
        self.assertTrue({('a','c'),('a','d'),('b','c'),('b','d')} <= set(topology.expanded_edges(graph)))
        for a in ('a','b'):
            for b in ('c','d'):self.assertEqual(topology.check_path(graph,['entry',a,b,'target']),['entry',a,b,'target'])
        g['groups'].append(dict(id='empty',name='空池',locked=None))
        g['edges'] += [['second','empty'],['empty','second']]
        with self.assertRaises(errors.UserError):topology.validate(self.db,g,complete=False)

    def test_layout_aligns_connected_nodes_and_keeps_parallel_lanes(self):
        g=copy.deepcopy(self.graph)
        g['vertices'] += [dict(id='d',kind='node',node_id=self.b,x=300,y=500),
                          dict(id='e',kind='node',node_id=self.c,x=600,y=500)]
        g['edges']=[['entry','pool'],['pool','c'],['c','target'],['entry','d'],['d','e'],['e','target']]
        original=copy.deepcopy(g)
        layout=topology.arrange(topology.validate(self.db,g))
        self.assertEqual(g,original)
        vertices={v['id']:v for v in layout['vertices']}
        self.assertNotIn('groups',layout)
        self.assertEqual(vertices['entry']['y'],vertices['b']['y'])
        self.assertEqual(vertices['c']['y'],(vertices['a']['y']+vertices['b']['y'])/2)
        self.assertEqual(vertices['d']['y'],vertices['e']['y'])
        self.assertEqual(vertices['d']['x'],vertices['a']['x'])
        self.assertGreaterEqual(vertices['d']['y'],vertices['b']['y']+86+48)
        self.assertAlmostEqual(vertices['target']['y'],(vertices['c']['y']+vertices['e']['y'])/2,delta=.5)
        self.assertEqual(topology.arrange(layout),layout)
        for path in (['entry','a','c','target'],['entry','d','e','target']):
            self.assertEqual(topology.check_path(layout,path),path)
        # Parallel targets stay centred around the nodes feeding them.
        g['vertices'].append(dict(id='target2',kind='target',target_id=self.target_id,x=900,y=300))
        g['edges'] += [['c','target2'],['e','target2']]
        vertices={v['id']:v for v in topology.arrange(g)['vertices']}
        self.assertAlmostEqual((vertices['target']['y']+vertices['target2']['y'])/2,
                               (vertices['c']['y']+vertices['e']['y'])/2,delta=.5)

    def test_free_node_layout_survives_draft_without_pool_geometry(self):
        self.graph['groups'][0].update(x=-850,y=-420,width=520,height=380)
        self.graph['vertices'][1].update(x=-820,y=-370)
        self.graph['vertices'][2].update(x=-810,y=-280)
        lid=self.save()
        draft=self.get(lid)['draft']['graph']
        self.assertNotIn('groups',draft)
        self.assertEqual(draft['vertices'][1]['x'],-820)
        self.assertEqual(draft['vertices'][2]['y'],-280)
        self.save(['entry','a','target'],lid,graph=draft);self.finish(lid)
        graph=self.get(lid)['active_graph']
        self.assertFalse(any('group' in v for v in graph['vertices']))
        self.assertEqual({tuple(e) for e in graph['edges']},{tuple(e) for e in draft['edges']})

    def test_manual_switch_keeps_draft_and_confirms_only_after_receipts(self):
        lid=self.save(['entry','a','target']);self.finish(lid)
        self.save(ident=lid,name='未发布')
        self.save(['entry','b','c','target'],lid,switch_only=True,graph=self.get(lid)['active_graph'])
        self.assertEqual(self.get(lid)['active_path'],['entry','a','target'])
        self.finish(lid)
        after=self.get(lid)
        self.assertEqual(after['active_path'],['entry','b','c','target'])
        self.assertEqual(after['draft']['name'],'未发布')
        self.assertFalse(after['pending_changes'])
        self.assertEqual([h['node_id'] for h in after['hops']],[self.entry,self.b,self.c])

    def test_cycles_disconnected_and_pool_serialization_rejected(self):
        for edge in (['c','b'],['a','b'],['target','entry']):
            g=copy.deepcopy(self.graph);g['edges'].append(edge)
            with self.subTest(edge=edge),self.assertRaises(errors.UserError):topology.validate(self.db,g)
        g=copy.deepcopy(self.graph);g['edges'].remove(['c','target'])
        with self.assertRaises(errors.UserError):topology.validate(self.db,g)
        g=copy.deepcopy(self.graph);g['vertices'][3]['node_id']=self.b
        self.assertIsNone(topology.find_path(g,required=['b','c']))

    def test_constraints_and_random_failover_of_standalone_node(self):
        # a is not a pool member: its failure still triggers graph failover.
        self.graph['vertices'][1].pop('group');self.graph['vertices'][2].pop('group');self.graph['groups']=[]
        lid=self.save(['entry','a','target']);self.finish(lid)
        with self.db:self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(int(time.time())-61,self.a))
        self.tick()
        self.assertEqual(self.get(lid)['job']['plan']['graph_path'],['entry','b','c','target'])
        self.finish(lid)
        self.ack(self.a);self.tick()
        self.assertEqual(self.get(lid)['active_path'],['entry','b','c','target'])
        self.assertIsNone(self.get(lid)['job'])

    def test_lock_prevents_alternative_and_recovery_clears_alarm(self):
        self.graph['groups'][0]['locked']='a'
        graph=topology.flatten(self.graph)
        self.assertTrue(next(v for v in graph['vertices'] if v['id']=='b')['disabled'])
        self.assertNotIn('disabled',self.graph['vertices'][2])
        lid=self.save(['entry','a','target']);self.finish(lid)
        with self.db:self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(int(time.time())-61,self.a))
        self.tick();self.assertIsNone(self.get(lid)['job']);self.assertIn('灾备等待',self.get(lid)['error'])
        self.ack(self.a);self.tick();self.assertEqual(self.get(lid)['state'],'active')

    def test_unknown_commit_waits_instead_of_starting_another_path(self):
        lid=self.save(['entry','a','target']);self.finish(lid)
        self.save(['entry','b','c','target'],lid,switch_only=True,graph=self.get(lid)['active_graph'])
        self.ack(self.b);self.ack(self.c);self.tick()
        job=self.get(lid)['job'];self.assertEqual(job['phase'],'commit')
        self.tick(now=int(time.time())+61)
        self.assertEqual(self.get(lid)['job']['new'],job['new'])
        self.assertEqual(self.get(lid)['active_path'],['entry','a','target'])

    def test_cancel_and_restart_preserve_runtime(self):
        lid=self.save(['entry','a','target']);self.finish(lid)
        self.save(['entry','b','c','target'],lid,switch_only=True,graph=self.get(lid)['active_graph'])
        self.db.close();self.db=migrations.init_db()
        self.assertEqual(self.get(lid)['job']['plan']['graph_path'],['entry','b','c','target'])
        self.call('api_link_action',dict(action='cancel',revision=self.get(lid)['revision']),lid)
        self.finish(lid)
        self.assertEqual(self.get(lid)['active_path'],['entry','a','target'])

    def test_stale_revision_and_illegal_runtime_graph_rejected(self):
        lid=self.save(['entry','a','target']);self.finish(lid)
        with self.assertRaises(errors.UserError):self.save(['entry','b','c','target'],lid,revision=0)
        changed=copy.deepcopy(self.graph);changed['edges'].append(['entry','target'])
        with self.assertRaises(errors.UserError):self.save(['entry','target'],lid,switch_only=True,graph=changed)

    def test_inactive_candidates_and_drafts_protect_resources(self):
        lid=self.save(['entry','a','target']);self.finish(lid)
        with self.assertRaises(errors.UserError):self.call('api_node_del',{},self.b)
        self.call('api_node_edit',dict(desired_mode='passive'),self.b)
        self.assertEqual(database.get_node(self.db,self.b)['desired_mode'],'passive')
        tid=self.target(host='other.example')
        self.graph['vertices'][-1]['target_id']=tid;self.save(ident=lid)
        with self.assertRaises(errors.UserError):self.call('api_target_del',{},tid)

    def test_quality_follows_runtime_and_only_explicit_candidates(self):
        lid=self.save(['entry','b','c','target']);self.finish(lid)
        _,edges=pools.quality_paths(self.db,lid)
        self.assertEqual(edges,{(self.entry,self.a),(self.entry,self.b),(self.a,self.c),(self.b,self.c)})
        nodes={r['id']:dict(r) for r in self.db.execute('SELECT * FROM nodes')}
        link=pools.get(self.db,'links',lid)
        sections=link_quality.sections(self.db,link,nodes)
        self.assertEqual(len(sections),3)
        self.assertEqual(sections[0]['probes'][0]['route'],[self.b,self.c])

    def test_resource_update_after_switch_keeps_selected_route(self):
        lid=self.save(['entry','a','target']);self.finish(lid)
        self.save(['entry','b','c','target'],lid,switch_only=True,graph=self.get(lid)['active_graph']);self.finish(lid)
        self.call('api_target_save',dict(host='203.0.113.9'),self.target_id);self.finish(lid)
        self.assertEqual(self.get(lid)['active_path'],['entry','b','c','target'])
        self.assertEqual([s['node_id'] for s in self.get(lid)['applied_plan']['stages']],[self.b,self.c])
        self.assertFalse(self.get(lid)['pending_changes'])

    def test_migration_preserves_rule_bytes_and_live_transaction(self):
        pool=self.call('api_pool_save',dict(name='old pool',members=[self.a,self.b]))['id']
        lid=self.call('api_link_save',dict(name='old',plan=dict(entry=self.entry,target=self.target_id,port=10001,
            stages=[dict(id='p',kind='pool',pool_id=pool)])))['id'];self.finish(lid)
        self.call('api_link_action',dict(action='switch',stage='p',member=self.b,revision=self.get(lid)['revision']),lid)
        before=[forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in (self.entry,self.a,self.b,self.c)]
        job=self.get(lid)['job']
        with self.db:self.db.execute('PRAGMA user_version=406')
        self.db.close();self.db=migrations.init_db()
        self.assertTrue(Path(settings.DB_PATH+'.pre-0.5.0.bak').exists())
        self.assertEqual(before,[forwarding.node_rules(self.db,database.get_node(self.db,n)) for n in (self.entry,self.a,self.b,self.c)])
        self.assertEqual(self.get(lid)['job']['new'],job['new'])
        self.finish(lid)
        self.assertEqual(self.get(lid)['active_path'],['entry','p-'+str(self.b),'target'])
        graph=self.get(lid)['active_graph']
        plan=topology.compile_plan(topology.validate(self.db,graph),['entry','p-'+str(self.a),'target'],10001)
        self.assertEqual(plan['stages'][0]['legacy_section'],'p')

    def test_passive_candidate_cannot_enter_pool_or_automatic_unwoken_path(self):
        self.graph['vertices'][2].pop('group');self.graph['groups']=[];self.graph['vertices'][1].pop('group')
        with self.db:self.db.execute("UPDATE nodes SET desired_mode='passive',installed_agent='passive' WHERE id=?",(self.b,))
        graph=topology.validate(self.db,self.graph)
        self.assertIsNone(topology.find_path(graph,required=['b'],allowed=topology.ready_path(self.db,graph,None,automatic=True)))
        self.assertIsNotNone(topology.find_path(graph,required=['b'],allowed=topology.ready_path(self.db,graph,None)))

    @unittest.skipUnless(shutil.which('google-chrome') or shutil.which('chromium'), 'Chrome unavailable')
    def test_canvas_with_real_http_api(self):
        lid=self.save(['entry','a','target']);self.finish(lid)
        with self.db:
            for nid in (self.entry,self.a,self.b,self.c):
                node_addresses.save(self.db,database.get_node(self.db,nid),[
                    dict(id='public',name='公网',host=f'192.0.2.{nid}',device='eth0'),
                    dict(id='private',name='内网',host=f'10.8.0.{nid}',device='eth1')])
        token='topology-browser-test'
        with self.db:
            database.set_setting(self.db,'must_change','0')
            self.db.execute('INSERT INTO sessions VALUES (?,?)',(hashlib.sha256(token.encode()).hexdigest(),int(time.time())+86400*7))
        exercise=r'''
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const wait=async fn=>{for(let i=0;i<250;i++){if(fn())return;await pause(25);}throw Error('Timeout: '+fn+' '+(graphSession?.error||''));};
const assert=(x,m)=>{if(!x)throw Error(m);};
const click=selector=>{const el=document.querySelector(selector);assert(el,'missing '+selector);el.focus();el.click();};
window.addEventListener('error',e=>fetch('/panel/test-result',{method:'POST',body:'FAIL '+e.message}));
window.addEventListener('unhandledrejection',e=>fetch('/panel/test-result',{method:'POST',body:'FAIL '+e.reason}));
(async()=>{try {
 await wait(()=>document.querySelector('[data-graph-node="b"]'));
 assert(!document.querySelector('[data-nav=pools]'),'obsolete pool navigation');
 assert(!document.querySelector('.graph-port'),'runtime exposes editing pins');
 assert(document.querySelectorAll('[data-graph-line]').length===7,'legacy connections were not expanded to node edges');
 assert(!document.querySelector('.graph-pool'),'runtime still contains a pool');
 const flowNode=poolView.nodes.find(n=>n.id===poolView.link.hops[0].node_id),flowStatus=flowNode.status;
 assert(document.querySelector('.graph-route-flow'),'healthy path has no flow animation');
 flowNode.status='offline';drawGraph();
 assert(!document.querySelector('.graph-route-flow'),'offline path still shows flowing traffic');
 assert(document.querySelector('.graph-route-line'),'offline path lost its confirmed route');
 flowNode.status=flowStatus;drawGraph();
 assert(document.querySelector('.graph-route-flow'),'recovered path animation did not resume');
 const wireClear=(source,target,barriers)=>{
  const boxes=new Map([source,target,...barriers].map(b=>[b.id,b]));
  const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',graphCurve(source,target,boxes));
  const length=path.getTotalLength();assert(Number.isFinite(length)&&length>0,'invalid wire geometry');
  for(let t=0;t<=length;t+=2){const p=path.getPointAtLength(t);for(const b of barriers)assert(!(p.x>b.x-3&&p.x<b.x+b.width+3&&p.y>b.y-3&&p.y<b.y+b.height+3),'wire crosses '+b.id);}
  assert(!path.getAttribute('d').includes('C'),'long bezier remained');
 };
 const a={id:'a',x:0,y:100,width:220,height:86},b={id:'b',x:780,y:100,width:220,height:86};
 const barriers=[{id:'obstacle',x:350,y:70,width:220,height:130},{id:'upper',x:300,y:-60,width:300,height:70}];
 wireClear(a,b,[]);wireClear(a,b,barriers);wireClear(b,a,barriers);
 click('[data-graph-node="b"] .graph-node-body');
 await wait(()=>!graphBusy&&!graphSession);
 assert(document.querySelector('#modal').hidden,'switch unnecessarily opens a modal');
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 assert(poolView.link.active_path.join(',')==='entry,b,target','one-click switch did not preserve downstream');
 click('[data-graph-node="b"] .graph-node-body');await wait(()=>!graphBusy&&graphSession?.kind==='choose');
 click('[data-action=graph-autocomplete]');await wait(()=>!graphBusy&&!graphSession);
 assert(poolView.link.active_path.join(',')==='entry,b,target','legacy autocomplete changed its selected path');
 click('[data-action=link-edit]');
 assert(graphSession.kind==='edit','editor missing');
 assert(graphSession.graph.version===2,'existing topology did not enter the current editor format');
 assert(graphSession.graph.ports['entry/target']===10001,'conversion lost the existing ingress port');
 assert(document.querySelector('[data-graph-node="a"] .graph-port[data-address="private"]'),'existing topology hides extra IPs');
 assert(!document.querySelector('[data-explicit-routing]'),'legacy routing toggle still visible');
 assert(!document.querySelector('[data-action=graph-ports-edit]'),'combination port editor still visible');
 assert(document.querySelector('[data-graph-node="a"] .graph-port'),'node IP controls missing');
 assert(!document.querySelector('[data-graph-group]'),'editor still contains a pool');
 const saved=structuredClone(graphSession.graph);
 click('[data-action=graph-add-open]');
 assert(document.querySelector('.graph-library')&&document.querySelector('#modal').hidden,'adding uses a modal');
 assert([...document.querySelectorAll('[data-action=graph-library-kind]')].map(el=>el.dataset.kind).join(',')==='entry,node,target','unexpected component kinds');
 const nodeID=poolView.nodes[0].id;graphAddResource('node',nodeID);
 const added=graphSession.graph.vertices.at(-1);
 const drag=(id,x,y)=>{
  const el=document.querySelector(`[data-graph-node="${id}"] .graph-node-body`),r=el.getBoundingClientRect(),scale=topologyBinding.view.scale;
  el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:7,clientX:r.x+20,clientY:r.y+15}));
  document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:7,clientX:r.x+20+x*scale,clientY:r.y+15+y*scale}));
  document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:7}));
 };
 const start={x:added.x,y:added.y};
 const otherBefore=JSON.stringify(graphSession.graph.vertices.filter(v=>v.id!==added.id));
 drag(added.id,-500-added.x,-250-added.y);await pause(10);
 assert(added.x===-500&&added.y===-250,'node drag constrained coordinates');
 assert(JSON.stringify(graphSession.graph.vertices.filter(v=>v.id!==added.id))===otherBefore,'moving one node changed other nodes');
 ACTIONS['graph-undo']();assert(graphSession.graph.vertices.at(-1).x===start.x&&graphSession.graph.vertices.at(-1).y===start.y,'undo node move failed');
 graphSession.graph=saved;graphSession.library=false;graphSession.selected=null;graphSession.edge=null;drawGraph();topologyBinding.fit();
 // Selecting a different node must display its own resource in the inspector.
 click('[data-graph-node="a"] .graph-node-body');
 assert(document.querySelector('[data-graph-resource]').value===String(saved.vertices.find(v=>v.id==='a').node_id),'wrong resource for a');
 click('[data-graph-node="b"] .graph-node-body');
 assert(document.querySelector('[data-graph-resource]').value===String(saved.vertices.find(v=>v.id==='b').node_id),'resource leaked between inspectors');
 // Layout freezes both form fields and resource mutations until its response arrives.
 graphSession.library=true;drawGraph();
 const layoutAPI=api;let releaseLayout;
 api=async(...args)=>{if(args[1]==='topology-preview')await new Promise(resolve=>releaseLayout=resolve);return layoutAPI(...args);};
 const vertexCount=graphSession.graph.vertices.length;
 const layingOut=ACTIONS['graph-check']();
 assert(document.querySelector('[data-action=graph-library-add]').disabled&&document.querySelector('[data-graph-field=name]').disabled,'layout leaves editor fields active');
 graphAddResource('node',nodeID);
 assert(graphSession.graph.vertices.length===vertexCount,'layout allowed a mutation that could be lost');
 releaseLayout();await layingOut;api=layoutAPI;
 assert(graphSession.graph.vertices.length===vertexCount&&!document.querySelector('[data-graph-field=name]').disabled,'layout did not preserve and unlock the editor');
 graphSession.library=false;graphSession.selected=null;drawGraph();
 // Layout is available for unfinished drafts and does not mutate the running route.
 graphSession.graph.vertices.push({id:'loose',kind:'node',node_id:nodeID,x:500,y:900,disabled:false});
 await ACTIONS['graph-check']();assert(!graphSession.error,'cannot arrange an unfinished draft: '+graphSession.error);
 graphSession.selected='loose';ACTIONS['graph-remove']();
 const runtime=(await api('GET','links/1')).link.active_path.join(',');
 assert(runtime==='entry,b,target','editing changed live path');
 ACTIONS['canvas-fullscreen']();drawGraph();assert(topologyBinding.view.fullscreen,'fullscreen lost');ACTIONS['canvas-fullscreen']();
 // Build a new dual-ingress topology using visible IP pins and the add shelf.
 for(const n of poolView.nodes)await api('PATCH',`nodes/${n.id}`,{addresses:[{id:'public',name:'公网',host:n.addr,device:'eth0'},{id:'private',name:'内网',host:`10.8.0.${n.id}`,device:'eth1'}]});
 await fetch('/panel/test-ack',{method:'POST'});
 graphSession=null;location.hash='#/routes/new';await wait(()=>graphSession?.kind==='edit'&&!graphSession.id);
 graphSession.name='多入口共享网络';const nodes=poolView.nodes,tid=poolView.targets[0].id;
 graphAddResource('entry',nodes[0].id);const e1=graphSession.graph.vertices.at(-1);
 graphAddResource('entry',nodes[3].id);const e2=graphSession.graph.vertices.at(-1);
 graphAddResource('node',nodes[1].id);const m1=graphSession.graph.vertices.at(-1);
 graphAddResource('node',nodes[2].id);const m2=graphSession.graph.vertices.at(-1);
 graphAddResource('target',tid);const t1=graphSession.graph.vertices.at(-1);
 graphAddResource('target',tid);const t2=graphSession.graph.vertices.at(-1);
 for(const e of [e1,e2])graphChooseAddress(e.id,'in','public');
 for(const m of [m1,m2]){graphChooseAddress(m.id,'in','public');graphChooseAddress(m.id,'out','private');}
 for(const e of [e1,e2])for(const m of [m1,m2]){graphConnect(e.id,'out','public');graphConnect(m.id,'in','public');}
 for(const m of [m1,m2])for(const t of [t1,t2]){graphConnect(m.id,'out','private');graphConnect(t.id,'in');}
 assert(graphSession.graph.bindings[`${m1.id}/${t1.id}`].from[m1.id]==='private','node output selection lost');
 assert(graphSession.graph.bindings[`${e1.id}/${m2.id}`].to[m2.id]==='public','node input selection lost');
 await ACTIONS['graph-check']();assert(!graphSession.error,'layout rejected IP topology');
 // Cancelling a drag must not rewrite the source's existing outgoing bindings.
 const beforeWire=JSON.stringify(graphSession.graph.bindings);
 const port=document.querySelector(`[data-graph-node="${e1.id}"] .graph-port.out[data-address="private"]`),pr=port.getBoundingClientRect();
 port.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:9,clientX:pr.x+5,clientY:pr.y+5}));
 document.dispatchEvent(new PointerEvent('pointercancel',{bubbles:true,pointerId:9}));await pause(10);
 assert(JSON.stringify(graphSession.graph.bindings)===beforeWire,'cancelled wire changed existing IP bindings');
 // Selecting an existing wire must not capture the next outgoing-pin click.
 const firstEdge=graphSession.graph.edges.findIndex(([a,b])=>a===m1.id&&b===t1.id);
 graphSession.edge=firstEdge;drawGraph();
 const pin=document.querySelector(`[data-graph-node="${m1.id}"] .graph-port.out[data-address="public"]`),rect=pin.getBoundingClientRect();
 pin.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:10,clientX:rect.x+5,clientY:rect.y+5}));
 document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:10}));await pause(10);
 assert(graphSession.connect===m1.id&&graphSession.edge===null,'selected wire swallowed new connection');
 assert(graphSession.graph.bindings[`${m1.id}/${t1.id}`].from[m1.id]==='private','new connection changed existing wire');
 graphCancelConnection();graphSession.edge=firstEdge;drawGraph();
 const binding=document.querySelector('[data-graph-binding="out"]');binding.value='public';binding.dispatchEvent(new Event('change',{bubbles:true}));
 assert(graphSession.graph.bindings[`${m1.id}/${t1.id}`].from[m1.id]==='public','wire inspector did not set IP');
 assert(graphSession.graph.bindings[`${m1.id}/${t2.id}`].from[m1.id]==='private','IP change leaked into another wire');
 const autoBinding=document.querySelector('[data-graph-binding="out"]');
 autoBinding.value='@auto';autoBinding.dispatchEvent(new Event('change',{bubbles:true}));
 assert(graphSession.graph.bindings[`${m1.id}/${t1.id}`].from[m1.id]==='@auto','automatic egress selection was lost');
 const autoPin=document.querySelector(`[data-graph-node="${m1.id}"] .graph-port.out[data-address="@auto"]`);
 assert(autoPin?.classList.contains('is-bound'),'automatic egress is not visible on its pin');
 const autoPoint=graphPortPoint(graphSession.graph,m1.id,'out','@auto',graphBoxes(graphSession.graph));
 const autoNode=graphSession.graph.vertices.find(v=>v.id===m1.id);
 assert(autoPoint[1]-autoNode.y===parseFloat(autoPin.style.top),'wire and automatic egress pin are misaligned');
 const restoreBinding=document.querySelector('[data-graph-binding="out"]');
 restoreBinding.value='public';restoreBinding.dispatchEvent(new Event('change',{bubbles:true}));
 graphSession.edge=null;drawGraph();
 const beforeInput=JSON.stringify(graphSession.graph.bindings);
 click(`[data-graph-node="${m1.id}"] .graph-port.in[data-address="private"]`);
 assert(JSON.stringify(graphSession.graph.bindings)===beforeInput,'receive pin rewrote existing wires');
 // Dropping on a multi-IP header waits for a choice; IP text is also a drop target.
 const connectedGraph=structuredClone(graphSession.graph);
 graphSession.graph.edges=graphSession.graph.edges.filter(([a,b])=>a!==e1.id||b!==m1.id);
 graphSession.selected=null;drawGraph();topologyBinding.fit();
 const dropOn=selector=>{
   const source=document.querySelector(`[data-graph-node="${e1.id}"] .graph-port.out[data-address="public"]`),r=source.getBoundingClientRect();
   source.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:11,clientX:r.x+5,clientY:r.y+5}));
   const rect=document.querySelector(selector).getBoundingClientRect(),x=rect.x+rect.width/2,y=rect.y+rect.height/2;
   document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:11,clientX:x,clientY:y}));
   document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:11,clientX:x,clientY:y}));
 };
 dropOn(`[data-graph-node="${m1.id}"] .graph-node-body`);await pause(10);
 assert(graphSession.connectTarget===m1.id&&graphSession.graph.edges.length===7,'ambiguous drop added an incomplete edge');
 click(`[data-graph-node="${m1.id}"] .graph-port.in[data-address="private"]`);
 assert(graphSession.graph.bindings[`${e1.id}/${m1.id}`].to[m1.id]==='private','pending IP choice was ignored');
 graphSession.graph.edges=graphSession.graph.edges.filter(([a,b])=>a!==e1.id||b!==m1.id);drawGraph();
 dropOn(`[data-graph-node="${m1.id}"] .graph-ip-row code`);await pause(10);
 assert(graphSession.graph.edges.length===8,'drop on IP text failed');
 graphSession.graph=connectedGraph;graphSession.selected=null;graphSession.edge=null;drawGraph();
 const editorLines=graphConnectionRoutes(graphSession.graph,graphBoxes(graphSession.graph));
 const ends=editorLines.filter(r=>r.b===m1.id).map(r=>r.in.point.join(','));
 assert(new Set(ends).size===ends.length,'editor arrowheads overlap at receiving IP');
 graphFocusEditor(document.querySelector(`[data-graph-node="${m1.id}"]`));
 assert(document.querySelector('.graph-edit-focus .graph-nearby[data-graph-from]'),'editor focus did not highlight connected lines');
 graphSession.edge=firstEdge;drawGraph();
 assert(graphConnectionRoutes(graphSession.graph,graphBoxes(graphSession.graph))===editorLines,'selection recalculated geometry');
 graphSession.edge=null;drawGraph();
 // Choose a receive IP through the actual card button, without an address dialog.
 click(`[data-graph-node="${e1.id}"] [data-action="graph-listen"][data-address="private"]`);
 assert(graphSession.graph.vertices.find(v=>v.id===e1.id).address==='private','receive IP click ignored');
 assert(document.querySelector('#modal').hidden,'IP picking opened a modal');
 click(`[data-graph-node="${e1.id}"] .graph-node-body`);
 const targetPicker=document.querySelector('[data-graph-port-target]');targetPicker.value=t2.id;targetPicker.dispatchEvent(new Event('change',{bubbles:true}));
 const portInput=document.querySelector('[data-graph-entry-port]');portInput.value='10080';portInput.dispatchEvent(new Event('input',{bubbles:true}));portInput.dispatchEvent(new Event('change',{bubbles:true}));
 assert(graphSession.graph.ports[`${e1.id}/${t2.id}`]===10080,'entry port was not saved for selected target');
 targetPicker.value=t1.id;targetPicker.dispatchEvent(new Event('change',{bubbles:true}));
 assert(document.querySelector('[data-graph-entry-port]').value==='','entry port leaked into another target');
 targetPicker.value=t2.id;targetPicker.dispatchEvent(new Event('change',{bubbles:true}));
 assert(document.querySelector('[data-graph-entry-port]').value==='10080','entry port was lost on target change');
 click(`[data-graph-node="${e2.id}"] .graph-node-body`);
 assert(document.querySelector('[data-graph-entry-port]').value==='','entry port leaked into another entry');
 assert(document.querySelector('[data-graph-resource]').value===String(e2.node_id),'resource selector retained the previous entry');
 click(`[data-graph-node="${e1.id}"] .graph-node-body`);
 assert(document.querySelector('[data-graph-entry-port]').value==='10080','entry port was lost on entry change');
 await ACTIONS['graph-publish']();await wait(()=>!graphBusy&&!graphSession);
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 assert(poolView.link.configurations.length===4,'combination generation failed');
 assert(poolView.link.configurations.find(c=>c.entry===e1.id&&c.target===t2.id).port===10080,'custom entry port was not applied');
 assert(poolView.link.active_path.includes(m1.id),'initial route missing');
 assert(document.querySelectorAll('[data-graph-line]').length===8,'node connections missing from multi topology');
 // A pending preview locks editing, and a superseded response cannot replace a newer session.
 const originalAPI=api;let releasePreview;
 api=async(...args)=>{if(args[1]==='topology-preview')await new Promise(resolve=>releasePreview=resolve);return originalAPI(...args);};
 const choosing=ACTIONS['graph-select']({dataset:{id:e1.id}});
 assert(graphBusy&&document.querySelector('[data-action=link-edit]').disabled,'preview left edit enabled');
 ACTIONS['link-edit']();assert(!graphSession,'edit action bypassed the pending preview');
 releasePreview();await choosing;api=originalAPI;
 assert(graphSession?.kind==='choose','preview did not finish');
 // Real focus followed by Escape used to remove the viewport twice during morph.
 click(`[data-graph-node="${e2.id}"] .graph-node-body`);await wait(()=>!graphBusy&&graphSession?.prefix[0]===e2.id);
 document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 assert(!graphSession,'Escape did not leave route selection');
 api=async(...args)=>{if(args[1]==='topology-preview')await new Promise(resolve=>releasePreview=resolve);return originalAPI(...args);};
 const stale=ACTIONS['graph-select']({dataset:{id:e1.id}});
 startGraphEditor(poolView.link);const newer=graphSession;
 releasePreview();await stale;api=originalAPI;
 assert(graphSession===newer&&newer.kind==='edit','late preview replaced a newer editor');
 await ACTIONS['graph-exit']();
 let rejectPreview;
 api=(...args)=>args[1]==='topology-preview'?new Promise((_,reject)=>rejectPreview=reject):originalAPI(...args);
 const failedPreview=ACTIONS['graph-select']({dataset:{id:e1.id}});
 startGraphEditor(poolView.link);const newest=graphSession;
 rejectPreview(new ApiError(503,{error:'old request'}));await failedPreview;api=originalAPI;
 assert(graphSession===newest&&!newest.error,'late preview error polluted a newer editor');
 await ACTIONS['graph-exit']();
 const ports=poolView.link.configurations.map(c=>c.port).join(',');
 click(`[data-graph-node="${m2.id}"] .graph-node-body`);await wait(()=>!graphBusy&&!graphSession);
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 assert(poolView.link.active_path.includes(m2.id),'click did not switch shared route');
 assert(poolView.link.state==='active'&&!poolView.link.pending_changes,'confirmed switch shows unapplied changes');
 assert(poolView.link.configurations.map(c=>c.port).join(',')===ports,'switch changed endpoint ports');
 assert(document.querySelector('#modal').hidden,'switch opened a confirmation dialog');
 // A new independent branch may serve only one entry, with both its targets.
 click('[data-action=link-edit]');
 graphAddResource('node',nodes[1].id);const extra=graphSession.graph.vertices.at(-1);
 extra.x=30000;extra.y=30000;drawGraph();
 await ACTIONS['graph-publish']();
 assert(graphSession.kind==='edit'&&graphSession.error.includes(nodes[1].name),'disconnected node lacks a readable error');
 assert(!graphSession.error.includes(extra.id),'error exposes an internal vertex ID');
 const bad=document.querySelector(`[data-graph-node="${extra.id}"]`),br=bad.getBoundingClientRect(),vr=topologyBinding.viewport.getBoundingClientRect();
 assert(bad.classList.contains('graph-invalid')&&br.left<vr.right&&br.right>vr.left&&br.top<vr.bottom&&br.bottom>vr.top,'invalid node is not revealed');
 graphChooseAddress(extra.id,'in','public');graphChooseAddress(extra.id,'out','private');
 graphConnect(e1.id,'out','public');graphConnect(extra.id,'in','public');
 assert(!graphSession.connect&&graphSession.graph.edges[graphSession.edge].join('/')===`${e1.id}/${extra.id}`,'completed connection did not show its own settings');
 // A previously selected, unrelated edge must not swallow the next pin click.
 graphSession.edge=graphSession.graph.edges.findIndex(([a,b])=>a===e1.id&&b===m1.id);drawGraph();
 const nextPin=document.querySelector(`[data-graph-node="${extra.id}"] .graph-port.out[data-address="private"]`),nr=nextPin.getBoundingClientRect();
 nextPin.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:11,clientX:nr.x+5,clientY:nr.y+5}));
 document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:11}));await pause(10);
 assert(graphSession.connect===extra.id,'next connection was discarded');
 click(`[data-graph-node="${t1.id}"] .graph-port.in`);
 assert(graphSession.graph.edges.some(([a,b])=>a===extra.id&&b===t1.id),'click-to-connect failed');
 graphConnect(extra.id,'out','private');graphConnect(t2.id,'in');
 await ACTIONS['graph-publish']();await wait(()=>!graphBusy&&!graphSession);
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 assert(poolView.link.graph.vertices.some(v=>v.id===extra.id),'complete independent branch was discarded');
 assert(poolView.link.configurations.length===4&&poolView.link.state==='active'&&!poolView.link.pending_changes,'complete independent branch cannot be saved');
 // Only the entry that can reach this branch switches; all its targets move together.
 const originals=poolView.link.configurations.map(c=>({id:c.id,entry:c.entry,target:c.target,path:c.active_path.join('/')}));
 const layout=JSON.stringify(poolView.link.active_graph.vertices.map(v=>[v.id,v.x,v.y]));
 assert(new Set([...document.querySelectorAll('.graph-entry-chip')].map(el=>el.style.getPropertyValue('--route-color'))).size===2,'entries need distinct colors');
 click(`[data-graph-node="${extra.id}"] .graph-node-body`);await wait(()=>!graphBusy&&!graphSession);
 assert(document.querySelector('.graph-route-line.graph-next'),'pending route is not shown: '+JSON.stringify({configs:poolView.link.configurations.map(c=>({path:c.active_path,pending:c.pending_path})),job:poolView.link.job,toast:document.querySelector('#toast')?.textContent}));
 assert(poolView.link.configurations.every(c=>c.active_path.join('/')===originals.find(x=>x.id===c.id).path),'path switched before acknowledgement');
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 for(const c of poolView.link.configurations){
   if(c.entry===e1.id)assert(c.active_path.includes(extra.id),'entry targets did not switch together');
   else assert(c.active_path.join('/')===originals.find(x=>x.id===c.id).path,'unreachable entry changed');
 }
 assert(JSON.stringify(poolView.link.active_graph.vertices.map(v=>[v.id,v.x,v.y]))===layout,'switch rearranged nodes');
 // Targets are fixed suffixes. Choosing the final forwarding node completes the entry.
 click(`[data-graph-node="${e1.id}"] .graph-node-body`);await wait(()=>!graphBusy&&graphSession?.kind==='choose');
 assert(graphSession.options.includes(extra.id)&&graphSession.options.includes(m1.id),'next-hop choices incomplete');
 assert(!document.querySelector(`[data-graph-node="${t1.id}"] [data-action="graph-select"]`),'target is still a route choice');
 click(`[data-graph-node="${t1.id}"] .graph-node-body`);
 assert(graphSession.prefix.join(',')===e1.id,'target click changed route selection');
 click(`[data-graph-node="${e2.id}"] .graph-node-body`);await wait(()=>!graphBusy&&graphSession?.prefix[0]===e2.id);
 click(`[data-graph-node="${e1.id}"] .graph-node-body`);await wait(()=>!graphBusy&&graphSession?.prefix[0]===e1.id);
 click(`[data-graph-node="${m1.id}"] .graph-node-body`);await wait(()=>!graphBusy&&!graphSession);
 assert(poolView.link.configurations.filter(c=>c.entry===e1.id).every(c=>c.active_path.includes(extra.id)),'final-hop selection bypassed node acknowledgement');
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 assert(poolView.link.configurations.find(c=>c.entry===e1.id&&c.target===t1.id).active_path.includes(m1.id),'selected pair not switched');
 assert(poolView.link.configurations.find(c=>c.entry===e1.id&&c.target===t2.id).active_path.includes(m1.id),'other target did not share the selected middle path');
 // Automatic completion respects the selected entry; Escape is inert.
 click(`[data-graph-node="${e2.id}"] .graph-node-body`);await wait(()=>!graphBusy&&graphSession?.kind==='choose');
 assert(!graphSession.options.includes(extra.id),'unreachable branch was offered');
 click('[data-action=graph-autocomplete]');await wait(()=>!graphBusy&&!graphSession);
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 assert(poolView.link.configurations.filter(c=>c.entry===e2.id).every(c=>c.active_path.includes(m2.id)),'autocomplete did not preserve the selected entry path');
 const rev=poolView.link.revision;
 click(`[data-graph-node="${e1.id}"] .graph-node-body`);await wait(()=>!graphBusy&&graphSession?.kind==='choose');
 document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 assert(!graphSession&&poolView.link.revision===rev,'Escape changed runtime');
 // Address inventory controls the rendered port rows even without a graph edit.
 const model=graphDisplay(poolView.link.active_graph),middle=model.vertices.find(v=>v.id===m1.id);
 const inventory=poolView.nodes.find(n=>n.id===middle.node_id).addresses;
 graphRouteCache=null;graphRuntimeLines(model,poolView.link,graphBoxes(model));
 const beforeGeometry=JSON.stringify(graphRouteCache.list);
 inventory.reverse();graphRuntimeLines(model,poolView.link,graphBoxes(model));
 const updatedGeometry=JSON.stringify(graphRouteCache.list);
 assert(updatedGeometry!==beforeGeometry,'live IP rows did not invalidate route geometry');
 graphRouteCache=null;graphRuntimeLines(model,poolView.link,graphBoxes(model));
 assert(JSON.stringify(graphRouteCache.list)===updatedGeometry,'cached and fresh route geometry disagree');
 inventory.reverse();graphRouteCache=null;
 const mid={...middle,id:'mid',group:null,x:400,y:400},source=model.vertices.find(v=>v.id===e1.id);
 const dest={...model.vertices.find(v=>v.id===t1.id),id:'dest',x:800,y:400};
 const entries=Array.from({length:8},(_,i)=>({...source,id:'lane'+i,group:null,x:0,y:160*i}));
 const lanes={version:2,vertices:[...entries,mid,dest],groups:[],edges:[...entries.map(e=>[e.id,'mid']),['mid','dest']],bindings:{'mid/dest':{from:{mid:'public'},to:{}}}};
 for(const e of entries)lanes.bindings[e.id+'/mid']={from:{[e.id]:'public'},to:{mid:'public'}};
 const laneLink={selection_mode:'independent',configurations:entries.map((e,i)=>({id:i,entry:e.id,target:'dest',state:'active',active_path:[e.id,'mid','dest']}))};
 const laneMarkup=graphRuntimeLines(lanes,laneLink,graphBoxes(lanes));
 const laneSVG=new DOMParser().parseFromString(`<svg xmlns="http://www.w3.org/2000/svg">${laneMarkup}</svg>`,'image/svg+xml');
 // Different incoming edges must reserve distinct arrow space at a shared IP.
 for(const target of ['mid','dest']){
   const tips=[...laneSVG.querySelectorAll(`.graph-route-line[data-graph-to="${target}"]`)].map(line=>{
     const marker=laneSVG.getElementById(line.getAttribute('marker-end').slice(5,-1));
     const point=line.getPointAtLength(line.getTotalLength());
     assert(marker.getAttribute('markerUnits')==='userSpaceOnUse','arrow size still depends on line width');
     return {point,height:Number(marker.getAttribute('markerHeight')),stroke:Number(line.style.getPropertyValue('--route-stroke'))};
   });
   assert(tips.length===entries.length,'missing entry routes at shared receiving port');
   for(let i=0;i<tips.length;i++)for(let j=0;j<i;j++){
     const gap=Math.abs(tips[i].point.y-tips[j].point.y);
     assert(gap>(tips[i].height+tips[j].height)/2+.2,'incoming arrowheads overlap');
     assert(gap>Math.max(tips[i].height,tips[j].height)/2+Math.max(tips[i].stroke,tips[j].stroke)/2+.2,'arrowhead overlaps parallel line');
   }
 }
 for(const span of graphRouteCache.list.filter(s=>s.a==='mid')){
   const line=document.createElementNS('http://www.w3.org/2000/svg','path');line.setAttribute('d',span.d);
   const start=line.getPointAtLength(0),end=line.getPointAtLength(line.getTotalLength());
   assert(start.y>=mid.y+62&&start.y<=mid.y+86&&end.y>=dest.y+62&&end.y<=dest.y+86,'lane anchor left its IP row');
 }
 graphRouteCache=null;
 // A first hop reaching only one target must not split an entry's traffic.
 click('[data-action=link-edit]');
 graphSession.graph.edges=graphSession.graph.edges.filter(([a,b])=>a!==extra.id||b!==t2.id);
 graphTouch();await ACTIONS['graph-publish']();await wait(()=>!graphBusy&&!graphSession);
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 const beforeRejected=JSON.stringify(poolView.link.configurations);
 click(`[data-graph-node="${extra.id}"] .graph-node-body`);await wait(()=>!graphBusy&&!graphSession);
 assert(JSON.stringify(poolView.link.configurations)===beforeRejected,'invalid first hop changed configurations');
 assert(document.querySelector('#toast').textContent.includes('统一首跳'),'missing first-hop rejection reason');
 click(`[data-graph-node="${e1.id}"] .graph-node-body`);await wait(()=>!graphBusy&&graphSession?.kind==='choose');
 assert(!graphSession.options.includes(extra.id),'unreachable first hop was offered');
 document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 // The same branch is valid downstream of a common first hop.
 click('[data-action=link-edit]');
 graphSession.graph.edges=graphSession.graph.edges.filter(([a,b])=>a!==e2.id||b!==m2.id);
 graphConnect(m2.id,'out','private');graphConnect(extra.id,'in','public');
 graphTouch();await ACTIONS['graph-publish']();await wait(()=>!graphBusy&&!graphSession);
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 click(`[data-graph-node="${m2.id}"] .graph-node-body`);await wait(()=>!graphBusy&&!graphSession);
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 const partialBefore=structuredClone(poolView.link.configurations);
 click(`[data-graph-node="${extra.id}"] .graph-node-body`);await wait(()=>!graphBusy&&!graphSession);
 assert(poolView.link.configurations.filter(c=>c.switching).length===1,'partial branch switched unreachable targets');
 await fetch('/panel/test-ack',{method:'POST'});await refresh();
 for(const c of poolView.link.configurations){
   const old=partialBefore.find(p=>p.id===c.id);
   assert(c.port===old.port&&c.name===old.name,'partial switch changed configuration identity');
   if(c.entry===e1.id&&c.target===t1.id)assert(c.active_path.includes(extra.id),'reachable target did not switch');
   else assert(c.active_path.join('/')===old.active_path.join('/'),'unreachable target changed its route');
 }
 graphFocusWires({type:'mouseover',target:document.querySelector(`[data-graph-node="${extra.id}"] .graph-node-body`)});
 assert(document.querySelector(`.graph-route-line.graph-nearby[data-route-entry="${e1.id}"][data-graph-to="${t1.id}"]`),'hover did not highlight the reachable target');
 assert(!document.querySelector(`.graph-route-line.graph-nearby[data-route-entry="${e1.id}"][data-graph-to="${t2.id}"]`),'hover highlighted another target path');
 click(`[data-graph-node="${e1.id}"] .graph-node-body`);await wait(()=>!graphBusy&&graphSession?.kind==='choose');
 assert(!graphSession.options.includes(extra.id),'partial target branch was offered as a first hop');
 click(`[data-graph-node="${m2.id}"] .graph-node-body`);await wait(()=>!graphBusy&&graphSession?.prefix.at(-1)===m2.id);
 assert(graphSession.options.includes(extra.id),'downstream branch is missing from next-hop choices');
 click(`[data-graph-node="${extra.id}"] .graph-node-body`);await wait(()=>!graphBusy&&!graphSession);
 assert(document.querySelector('#modal').hidden,'partial target branch required an extra confirmation');
 // Renaming a configuration changes only its display/export name.
 const rootId=poolView.link.id,revision=poolView.link.revision;
 location.hash='#/configs';
 await wait(()=>document.querySelector('[data-action=config-rename]'));
 const config=list.data.chains.find(c=>c.link_id===rootId),code=config.configuration_code;
 assert(/^C\d{4,}$/.test(code),'configuration ID is missing');
 click(`[data-config-id="${config.id}"] [data-action=config-rename]`);
 const name='香港 # "专线" <1>';
 const input=document.querySelector('#modal input[name=name]');
 assert(input.value===config.remark,'rename form uses a different name');
 input.value=name;input.dispatchEvent(new Event('input',{bubbles:true}));
 click('#modal button.primary');
 await wait(()=>document.querySelector('#modal').hidden&&list.data.chains.some(c=>c.id===config.id&&c.remark===name));
 const row=document.querySelector(`[data-config-id="${config.id}"]`);
 assert(row.querySelector('.title').textContent===name,'renamed text was not safely rendered');
 assert(row.querySelector('.config-id').textContent===code,'rename changed configuration ID');
 const link=(await api('GET',`links/${rootId}`)).link;
 assert(link.revision===revision,'rename revised the topology');
 assert(link.configurations.find(c=>c.id===config.configuration_id).name===name,'topology has a stale configuration name');
 const subs=await api('GET','subscriptions');
 assert(subs.configs.find(c=>c.id===config.configuration_id).name===name,'subscription selector has a stale configuration name');
 await fetch('/panel/test-result',{method:'POST',body:'PASS'});
}catch(e){await fetch('/panel/test-result',{method:'POST',body:'FAIL '+e.stack});}})();
'''
        html=(Path(settings.WEB_DIR)/'index.html').read_text().replace('</body>','<script>'+exercise+'</script></body>')
        done=threading.Event();results=[]
        class Handler(http_harness.Handler):
            panel_path='/panel';sync_path='/sync'
            def run_request(self):
                if self.path=='/panel/':
                    data=html.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                    self.send_header('Set-Cookie',f'ictl_session={token}; Path=/panel/; HttpOnly; SameSite=Strict')
                    self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
                if self.path=='/panel/test-result':
                    results.append(self.rfile.read(int(self.headers.get('Content-Length','0'))).decode())
                    self.send_response(204);self.send_header('Content-Length','0');self.end_headers();done.set();return
                if self.path=='/panel/test-ack':
                    c=database.db()
                    for _ in range(8):
                        with c:
                            for n in c.execute('SELECT * FROM nodes').fetchall():
                                packet=agent_v3.packet(c,n,status=dict(version=settings.COMPONENTS['active_agent'],state='ok'))
                                if packet.get('action')=='apply':agent_v3.acknowledge(c,database.get_node(c,n['id']),dict(op=packet['op'],generation=packet['generation'],hash=packet['hash'],ok=True,version=settings.COMPONENTS['active_agent']))
                            before=node_sync.mutation_snapshot(c);pools.tick(c);node_sync.finish_mutation(c,before)
                    c.close();self.send_response(204);self.send_header('Content-Length','0');self.end_headers();return
                super().run_request()
            do_GET=do_POST=do_PATCH=do_DELETE=run_request
        root=Path(self.temp.name)
        with http_harness.LimitedHTTPServer(('127.0.0.1',0),Handler) as server,(root/'chrome.log').open('w') as log:
            threading.Thread(target=server.serve_forever,daemon=True).start()
            process=subprocess.Popen([shutil.which('google-chrome') or shutil.which('chromium'),'--headless','--no-sandbox','--disable-gpu','--disable-background-networking','--no-first-run','--no-default-browser-check','--disable-dev-shm-usage','--window-size=1200,900',f'--user-data-dir={root}/chrome',f'http://127.0.0.1:{server.server_port}/panel/#/routes/{lid}'],stdout=log,stderr=log)
            try:
                self.assertTrue(done.wait(30),'Browser timeout: '+(root/'chrome.log').read_text()[-2000:])
                self.assertEqual(results[0],'PASS',results[0])
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                server.shutdown()


if __name__=='__main__':unittest.main()
