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
        self.assertEqual(g['edges'],[['entry','pool'],['pool','target'],['pool','c'],['c','target']])
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
        self.assertEqual(set(decision['options']),{'target','c'})
        self.assertFalse(decision['complete'])

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
        for reference in ([],{},7):
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

    def test_layout_centres_single_nodes_on_pool_and_keeps_parallel_lanes(self):
        g=copy.deepcopy(self.graph)
        g['vertices'] += [dict(id='d',kind='node',node_id=self.b,x=300,y=500),
                          dict(id='e',kind='node',node_id=self.c,x=600,y=500)]
        g['edges']=[['entry','pool'],['pool','c'],['c','target'],['entry','d'],['d','e'],['e','target']]
        original=copy.deepcopy(g)
        layout=topology.arrange(topology.validate(self.db,g))
        self.assertEqual(g,original)
        vertices={v['id']:v for v in layout['vertices']};pool=layout['groups'][0]
        centre=pool['y']+pool['height']/2
        for ident in ('entry','c','target'):
            self.assertEqual(vertices[ident]['y']+30,centre,ident)
        self.assertEqual(vertices['d']['y'],vertices['e']['y'])
        self.assertEqual(vertices['d']['x']+90,pool['x']+pool['width']/2)
        self.assertGreaterEqual(vertices['d']['y'],pool['y']+pool['height']+48)
        self.assertEqual(topology.arrange(layout),layout)
        for path in (['entry','a','c','target'],['entry','d','e','target']):
            self.assertEqual(topology.check_path(layout,path),path)

    def test_free_layout_and_independent_pool_geometry_survive_draft(self):
        self.graph['groups'][0].update(x=-850,y=-420,width=520,height=380)
        self.graph['vertices'][1].update(x=-820,y=-370)
        self.graph['vertices'][2].update(x=-810,y=-280)
        lid=self.save()
        draft=self.get(lid)['draft']['graph']
        self.assertEqual(draft['groups'],self.graph['groups'])
        self.assertEqual(draft['vertices'][1]['x'],-820)
        self.save(['entry','a','target'],lid,graph=draft);self.finish(lid)
        graph=self.get(lid)['active_graph'];container=graph['groups'][0]
        for v in graph['vertices']:
            if v.get('group')==container['id']:
                self.assertGreaterEqual(v['x'],container['x'])
                self.assertGreaterEqual(v['y'],container['y']+32)
                self.assertLessEqual(v['x']+180,container['x']+container['width'])
                self.assertLessEqual(v['y']+60,container['y']+container['height'])

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
        with self.assertRaises(errors.UserError):self.call('api_node_edit',dict(desired_mode='passive'),self.b)
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
        token='topology-browser-test'
        with self.db:
            database.set_setting(self.db,'must_change','0')
            self.db.execute('INSERT INTO sessions VALUES (?,?)',(hashlib.sha256(token.encode()).hexdigest(),int(time.time())+86400*7))
        exercise=r'''
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const wait=async fn=>{for(let i=0;i<200;i++){if(fn())return;await pause(30);}throw Error('Timeout: '+fn);};
const assert=(x,m)=>{if(!x)throw Error(m);};
window.addEventListener('error',e=>fetch('/panel/test-result',{method:'POST',body:'FAIL '+e.message}));
(async()=>{try {
  await wait(()=>document.querySelector('[data-graph-node="b"]'));
  assert(!document.querySelector('[data-nav=pools]'),'obsolete pool navigation');
  assert(!document.querySelector('.graph-port'),'runtime exposes editing');
  const wireClear=(source,target,barriers)=>{
    const boxes=new Map([source,target,...barriers].map(b=>[b.id,b]));
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');
    path.setAttribute('d',graphCurve(source,target,boxes));
    const length=path.getTotalLength();assert(Number.isFinite(length)&&length>0,'invalid wire geometry');
    for(let t=0;t<=length;t+=2){const point=path.getPointAtLength(t);for(const b of barriers){
      assert(!(point.x>b.x-4&&point.x<b.x+b.width+4&&point.y>b.y-4&&point.y<b.y+b.height+4),'wire crosses component '+b.id+': '+path.getAttribute('d'));
    }}
    return path.getAttribute('d');
  };
  const wireA={id:'wire-a',x:0,y:100,width:180,height:60},wireB={id:'wire-b',x:700,y:100,width:180,height:60};
  assert(!wireClear(wireA,wireB,[]).includes('C'),'aligned endpoints should have a straight wire');
  wireClear(wireA,wireB,[{id:'block',x:320,y:75,width:220,height:120}]);
  wireClear(wireA,wireB,[{id:'block',x:320,y:75,width:220,height:120},{id:'upper',x:280,y:-20,width:260,height:70}]);
  wireClear(wireB,wireA,[{id:'block',x:320,y:75,width:220,height:120}]);
  const hoverNode=document.querySelector('[data-graph-node="b"]');hoverNode.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
  assert(document.querySelector('.graph-focus')&&document.querySelector('[data-graph-from="b"].graph-nearby'),'hover does not highlight connected wires');
  hoverNode.dispatchEvent(new MouseEvent('mouseout',{bubbles:true}));assert(!document.querySelector('.graph-focus'),'wire focus was not cleared');
  document.querySelector('[data-graph-node="b"] button').click();
  await wait(()=>document.querySelector('[data-confirm="1"]'));
  assert(graphSession.prefix.join(',')==='entry,b,target','pool switch failed to preserve downstream path');
  assert(!(await api('GET','links/1')).link.job,'selection applied before confirmation');
  document.querySelector('[data-confirm="0"]').click();await wait(()=>!graphSession&&!graphBusy);
  document.querySelector('[data-action=link-edit]').click();
  assert(graphSession.kind==='edit'&&document.querySelector('.graph-port'),'editor missing');
  assert(!document.querySelector('[data-graph-node="a"] .graph-port'),'member still exposes individual ports');
  assert(document.querySelector('[data-graph-group="pool"] .graph-port'),'pool ports missing');
  assert(document.querySelectorAll('[data-graph-line]').length===4,'pool edges were not collapsed');
  ACTIONS['canvas-fullscreen']();fullRender=true;drawGraph();
  assert(getComputedStyle(document.querySelector('.topology-panel')).transform==='none','page entrance animation moves the interactive canvas');
  assert(topologyBinding.view.fullscreen&&document.body.classList.contains('canvas-fullscreen'),'fullscreen state lost on redraw');
  assert(document.querySelector('.graph-fullscreen-actions [data-action="graph-publish"]').getClientRects().length,'fullscreen cannot apply edits');
  ACTIONS['canvas-fullscreen']();
  const originalName=graphSession.name,nameInput=document.querySelector('[data-graph-field="name"]');
  nameInput.value='renamed';nameInput.dispatchEvent(new Event('input',{bubbles:true}));nameInput.dispatchEvent(new Event('change',{bubbles:true}));
  ACTIONS['graph-undo']();assert(graphSession.name===originalName&&document.querySelector('[data-graph-field="name"]').value===originalName,'undo left stale field value');
  graphSession.selected='c';ACTIONS['graph-remove']();assert(!graphSession.graph.vertices.some(v=>v.id==='c'),'delete failed');
  ACTIONS['graph-undo']();assert(graphSession.graph.vertices.some(v=>v.id==='c'),'undo lost deleted node');
  ACTIONS['graph-redo']();assert(!graphSession.graph.vertices.some(v=>v.id==='c'),'redo failed');
  ACTIONS['graph-undo']();
  const originalEdges=JSON.stringify(graphSession.graph.edges);
  graphSession.graph.groups.push({id:'other',name:'other',locked:null,x:900,y:500,width:260,height:180});graphTouch();
  graphMembership(graphSession.graph.vertices.find(v=>v.id==='a'),'other');graphTouch();
  assert(JSON.stringify(graphSession.graph.edges)===originalEdges,'cross-pool move imported another pool wiring');
  ACTIONS['graph-undo']();assert(graphSession.graph.vertices.find(v=>v.id==='a').group==='pool','undo did not restore membership');
  ACTIONS['graph-undo']();assert(!graphSession.graph.groups.some(p=>p.id==='other'),'undo did not remove temporary pool');
  ACTIONS['graph-group']({dataset:{id:'pool'}});
  const poolBefore=structuredClone(graphSession.graph.groups[0]),membersBefore=JSON.stringify(graphSession.graph.vertices.filter(v=>v.group==='pool'));
  const grip=document.querySelector('[data-graph-group="pool"] .graph-resize'),gripRect=grip.getBoundingClientRect(),gripScale=topologyBinding.view.scale;
  grip.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:9,clientX:gripRect.x+12,clientY:gripRect.y+12}));
  document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:9,clientX:gripRect.x+12+60*gripScale,clientY:gripRect.y+12+40*gripScale}));
  document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:9}));await pause(20);
  assert(graphSession.graph.groups[0].width===poolBefore.width+60,'pool resize did not adjust width');
  assert(JSON.stringify(graphSession.graph.vertices.filter(v=>v.group==='pool'))===membersBefore,'pool resize moved members');
  ACTIONS['graph-undo']();assert(graphSession.graph.groups[0].width===poolBefore.width,'pool resize undo failed');
  ACTIONS['graph-select']({dataset:{id:'c'}});
  const previousResource=graphSession.graph.vertices.find(v=>v.id==='c').node_id,referenceEdges=JSON.stringify(graphSession.graph.edges);
  const resource=document.querySelector('[data-graph-resource]');resource.value=poolView.nodes[0].id;resource.dispatchEvent(new Event('change',{bubbles:true}));
  assert(graphSession.graph.vertices.find(v=>v.id==='c').node_id===poolView.nodes[0].id,'resource replacement failed');
  assert(JSON.stringify(graphSession.graph.edges)===referenceEdges,'resource replacement dropped wires');
  ACTIONS['graph-undo']();assert(graphSession.graph.vertices.find(v=>v.id==='c').node_id===previousResource,'resource undo failed');
  const savedGraph=structuredClone(graphSession.graph);
  const startDrag=(selector,dx,dy,corner=false)=>{
    const el=document.querySelector(selector),r=el.getBoundingClientRect(),scale=topologyBinding.view.scale;
    const x=corner?r.right-8:r.x+30,y=corner?r.bottom-8:r.y+20;
    const hit=document.elementFromPoint(x,y);
    if(corner)assert(hit?.closest('[data-graph-drag="group"]')===el,'pool background cannot be selected');
    el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:7,clientX:x,clientY:y}));
    document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:7,clientX:x+dx*scale,clientY:y+dy*scale}));
  };
  const endDrag=async(cancel=false)=>{
    document.dispatchEvent(new PointerEvent(cancel?'pointercancel':'pointerup',{bubbles:true,pointerId:7}));await pause(20);
  };
  ACTIONS['graph-add-open']({dataset:{kind:'pool'}});document.querySelector('#graph-pool-name').value='拖拽池';ACTIONS['graph-add-group']();
  const container=graphSession.graph.groups.at(-1),poolId=container.id;
  assert(graphSession.selected===poolId,'new pool is not selectable');
  ACTIONS['graph-add-open']({dataset:{kind:'node'}});
  assert(document.querySelector('#graph-add-resource').options.length===poolView.nodes.length+1,'resource list changed with selected pool');
  document.querySelector('#graph-add-resource').value=poolView.nodes[0].id;ACTIONS['graph-add-node']();
  const added=graphSession.graph.vertices.at(-1),nodeSelector=`[data-graph-node="${added.id}"] .graph-node-body`;
  assert(!added.group,'adding a node implicitly joined selected pool');
  startDrag(nodeSelector,container.x+30-added.x,container.y+50-added.y);
  assert(document.querySelector(`[data-graph-group="${poolId}"]`).classList.contains('graph-drop-target'),'drop target not highlighted');
  await endDrag();assert(added.group===poolId,'dragging into pool failed');
  const box=JSON.stringify(graphBounds(graphSession.graph,container));
  startDrag(nodeSelector,-500-added.x,-300-added.y);
  assert(JSON.stringify(graphBounds(graphSession.graph,container))===box,'pool followed member during drag out');
  await endDrag();assert(!added.group&&added.x===-500&&added.y===-300,'drag out still clamps coordinates or retains membership');
  assert(JSON.stringify(graphBounds(graphSession.graph,container))===box,'empty pool lost independent geometry');
  startDrag(nodeSelector,container.x+30-added.x,container.y+50-added.y);await endDrag();
  topologyBinding.fit();
  const beforeMove={x:container.x,y:container.y,nx:added.x,ny:added.y};
  startDrag(`[data-graph-group="${poolId}"] .graph-pool-body`,-container.x-100,-container.y-80,true);
  const dx=container.x-beforeMove.x,dy=container.y-beforeMove.y;
  assert(added.x===beforeMove.nx+dx&&added.y===beforeMove.ny+dy,'members did not follow pool');
  const beforeRelease=document.querySelector(nodeSelector).getBoundingClientRect();
  const viewportBefore=topologyBinding.viewport.getBoundingClientRect(),scrollBefore=window.scrollY;
  await endDrag();
  const afterRelease=document.querySelector(nodeSelector).getBoundingClientRect();
  assert(Math.abs(beforeRelease.x-afterRelease.x)<2&&Math.abs(beforeRelease.y-afterRelease.y)<2,'negative origin made canvas jump: '+JSON.stringify({before:beforeRelease,after:afterRelease,viewportBefore,viewportAfter:topologyBinding.viewport.getBoundingClientRect(),scrollBefore,scrollAfter:window.scrollY}));
  assert(added.group===poolId&&container.x===-100&&container.y===-80,'pool move clamped or changed membership');
  const cancelled={x:added.x,y:added.y};startDrag(nodeSelector,700,400);await endDrag(true);
  assert(added.x===cancelled.x&&added.y===cancelled.y&&added.group===poolId,'cancelled drag committed a change');
  graphSession.graph=savedGraph;graphSession.selected=null;graphSession.dirty=false;drawGraph();topologyBinding.fit();
  const card=document.querySelector('[data-graph-node="a"] .graph-node-body');
  const r=card.getBoundingClientRect(),before=graphSession.graph.vertices.find(v=>v.id==='a').x;
  card.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:1,clientX:r.x+30,clientY:r.y+20}));
  document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:1,clientX:r.x+55,clientY:r.y+30}));
  document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:1}));await pause(20);
  assert(graphSession.graph.vertices.find(v=>v.id==='a').x>before,'drag did not move node');
  graphSession.edge=graphSession.graph.edges.findIndex(e=>e.join(',')==='pool,c');ACTIONS['graph-remove-edge']();
  await ACTIONS['graph-check']();assert(graphSession.error,'disconnected topology passed validation');
  const output=document.querySelector('[data-graph-group="pool"] .graph-port.out'),start=output.getBoundingClientRect();
  output.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:8,clientX:start.x+16,clientY:start.y+16}));
  const input=document.querySelector('[data-graph-node="c"] .graph-port.in').getBoundingClientRect();
  document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:8,clientX:input.x+16,clientY:input.y+16}));
  assert(document.querySelector('.graph-wire')?.getAttribute('d'),'wire preview missing');
  document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:8,clientX:input.x+16,clientY:input.y+16}));await pause(20);
  assert(graphSession.graph.edges.some(e=>e.join(',')==='pool,c'),'drag-to-connect failed');
  ACTIONS['graph-undo']();assert(!graphSession.graph.edges.some(e=>e.join(',')==='pool,c'),'wire undo failed');
  ACTIONS['graph-redo']();
  ACTIONS['graph-port']({dataset:{id:'c',side:'out'}});ACTIONS['graph-port']({dataset:{id:'pool',side:'in'}});
  assert(graphSession.error.includes('循环')&&!graphSession.graph.edges.some(e=>e.join(',')==='c,pool'),'cycle not rejected immediately');
  graphSession.connect=null;drawGraph();
  await ACTIONS['graph-check']();assert(!graphSession.error&&graphSession.notice,'valid topology rejected');
  const members=graphSession.graph.vertices.filter(v=>v.group==='pool');
  assert(members[0].x===members[1].x,'validated pool was not aligned');
  assert(graphSession.graph.vertices.find(v=>v.id==='c').x>members[0].x,'layout is not left to right');
  await ACTIONS['graph-save-draft']();await wait(()=>!graphSession&&document.querySelector('[data-action=link-edit]'));
  let server=(await api('GET','links/1')).link;
  assert(server.active_path.join(',')==='entry,a,target'&&server.draft,'draft changed running path');
  document.querySelector('[data-action=link-edit]').click();
  graphSession.graph.groups[0].locked='b';graphSession.dirty=true;
  const publish=ACTIONS['graph-publish']();await wait(()=>document.querySelector('[data-confirm="1"]'));
  assert(graphSession.prefix.join(',')==='entry,b,target','publish did not select valid complete path');
  assert(graphChanged(),'unconfirmed editor was not protected from navigation');
  document.querySelector('[data-confirm="1"]').click();await publish;await wait(()=>!graphSession);
  server=(await api('GET','links/1')).link;assert(server.job&&server.active_path.join(',')==='entry,a,target','pending switch shown as already active');
  await fetch('/panel/test-ack',{method:'POST'});await refresh();
  server=(await api('GET','links/1')).link;assert(server.active_path.join(',')==='entry,b,target','confirmed path mismatch');
  assert(document.querySelectorAll('.graph-live').length===2,'active cables mismatch');
  await ACTIONS['graph-select']({dataset:{id:'b'}});
  assert(graphSession.options.includes('c')&&!graphSession.complete,'current node did not offer alternate downstream route');
  const select=ACTIONS['graph-select']({dataset:{id:'c'}});await wait(()=>document.querySelector('[data-confirm="1"]'));
  assert(graphSession.prefix.join(',')==='entry,b,c,target','canvas selection ignored chosen continuation');
  document.querySelector('[data-confirm="1"]').click();await select;
  await fetch('/panel/test-ack',{method:'POST'});await refresh();
  server=(await api('GET','links/1')).link;assert(server.active_path.join(',')==='entry,b,c,target','direct selection did not apply');
  ACTIONS['link-edit']();graphSession.name='local unsaved';graphTouch();graphSession.revision=0;
  await ACTIONS['graph-save-draft']();assert(graphSession.conflict,'stale revision was not recognized');
  ACTIONS['canvas-fullscreen']();assert(document.querySelector('.topology-panel .graph-error')?.getClientRects().length,'fullscreen hid save error');
  const cancelledReload=ACTIONS['graph-reload']();await wait(()=>document.querySelector('[data-confirm="0"]'));
  document.querySelector('[data-confirm="0"]').click();await cancelledReload;
  assert(graphSession.name==='local unsaved','cancelled reload lost local edits');
  const reload=ACTIONS['graph-reload']();await wait(()=>document.querySelector('[data-confirm="1"]'));
  document.querySelector('[data-confirm="1"]').click();await reload;
  assert(!graphSession.dirty&&graphSession.revision>0,'reload did not restore current server state');
  graphSession.name='keep after failed apply';graphSession.revision=0;graphSession.graph.vertices[0].x=-620;graphTouch();drawGraph();
  const staleApply=ACTIONS['graph-publish']();await wait(()=>document.querySelector('[data-confirm="1"]'));
  document.querySelector('[data-confirm="1"]').click();await staleApply;
  assert(graphSession.kind==='edit'&&graphSession.conflict,'failed apply did not return to editor');
  assert(graphSession.name==='keep after failed apply'&&graphSession.graph.vertices[0].x===-620,'failed apply lost draft or layout');
  const discardReload=ACTIONS['graph-reload']();await wait(()=>document.querySelector('[data-confirm="1"]'));
  document.querySelector('[data-confirm="1"]').click();await discardReload;
  ACTIONS['canvas-fullscreen']();await ACTIONS['graph-exit']();
  ACTIONS['link-new']();await wait(()=>graphSession?.kind==='edit'&&!graphSession.id);
  graphSession.name='new';graphSession.graph=structuredClone(server.active_graph);graphSession.graph.vertices[0].x=-777;graphTouch();drawGraph();
  const portInput=document.querySelector('[data-graph-field="port"]');portInput.value='0';portInput.dispatchEvent(new Event('input',{bubbles:true}));
  await ACTIONS['graph-publish']();assert(graphSession.kind==='edit'&&graphSession.errorField==='port','invalid port reached confirmation');
  assert(!document.querySelector('[data-confirm="1"]'),'invalid port opened confirm dialog');
  document.querySelector('[data-graph-field="port"]').value='';document.querySelector('[data-graph-field="port"]').dispatchEvent(new Event('input',{bubbles:true}));
  graphSession.name=' ';await ACTIONS['graph-save-draft']();assert(graphSession.errorField==='name','empty name was accepted');
  graphSession.name='new';graphTouch();drawGraph();
  const create=ACTIONS['graph-publish']();await wait(()=>document.querySelector('[data-confirm="1"]'));
  document.querySelector('[data-confirm="0"]').click();await create;
  assert(graphSession.kind==='edit'&&!graphSession.id,'cancel did not preserve new editor');
  assert(graphSession.graph.vertices[0].x===-777,'cancelled apply changed free-form layout');
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
                                packet=agent_v3.packet(c,n,status=dict(version='0.4.7',state='ok'))
                                if packet.get('action')=='apply':agent_v3.acknowledge(c,database.get_node(c,n['id']),dict(op=packet['op'],generation=packet['generation'],hash=packet['hash'],ok=True,version='0.4.7'))
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
                self.assertEqual(results[0],'PASS')
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                server.shutdown()


if __name__=='__main__':unittest.main()
