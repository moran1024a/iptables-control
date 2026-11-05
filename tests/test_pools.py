"""Pool behavior through real rule compilation and authenticated-sync receipts."""
import json
from pathlib import Path
import time
import unittest
from test_server import Fixture, s


class PoolTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.old_agent = s.AGENT
        s.load_agent()
        self.entry, self.a, self.b, self.c = [self.node(n, ports='10000-10100') for n in ('entry','a','b','c')]
        self.target_id = self.target()
        for i, nid in enumerate((self.entry, self.a, self.b, self.c), 1):
            with self.db:
                self.db.execute("UPDATE nodes SET addr=?,installed_agent='active',agent='0.4.0-rc.2',protocol_version=2,first_seen=1,last_seen=?,state='ok' WHERE id=?",
                                (f'192.0.2.{i}', int(time.time()), nid))
            self.ack(nid)
        self.pool = self.call('api_pool_save', dict(name='pool', members=[self.a,self.b]))['id']

    def tearDown(self):
        s.AGENT = self.old_agent
        super().tearDown()

    def ack(self, nid, ok=True):
        with self.db:
            n = s.get_node(self.db, nid)
            p = s.agent_v3.packet(s._module(), self.db, n, status=dict(version='0.4.0-rc.2',state='ok'))
            if p.get('action') == 'apply':
                return s.agent_v3.acknowledge(s._module(), self.db, s.get_node(self.db,nid),
                    dict(op=p['op'],generation=p['generation'],hash=p['hash'],ok=ok,error='' if ok else '端口冲突：测试',version='0.4.0-rc.2'))

    def tick(self, now=None):
        with self.db:
            before=s.mutation_snapshot(self.db)
            s.pools.tick(s._module(), self.db, now=now)
            s.finish_mutation(self.db,before)

    def link(self):
        return self.call('api_link_save',dict(name='link',plan=dict(entry=self.entry,target=self.target_id,
            port=10001,stages=[dict(id='pool-a',kind='pool',pool_id=self.pool)])))['id']

    def get(self, lid):
        return self.h.api_link_get(self.db,{},lid)['link']

    def finish(self, lid):
        for _ in range(6):
            for nid in (self.entry,self.a,self.b,self.c):self.ack(nid)
            self.tick()
            if self.get(lid)['job'] is None:return
        self.fail('switch did not finish')

    def test_cancel_prepare_releases_link_and_restores_default(self):
        lid=self.link(); self.finish(lid)
        before=self.get(lid)
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=before['revision']),lid)
        self.tick(int(time.time())+31)
        waiting=self.get(lid); self.assertEqual(waiting['state'],'waiting')
        self.call('api_link_action',dict(action='cancel',revision=waiting['revision']),lid)
        after=self.get(lid)
        self.assertIsNone(after['job']); self.assertEqual(after['plan'],before['plan'])
        self.assertEqual(after['selected'],before['selected'])
        self.assertFalse(s.node_rules(self.db,s.get_node(self.db,self.b)))
        self.assertTrue(after['cleanup'])
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.a,revision=after['revision']),lid)

    def test_cancel_commit_keeps_downstream_until_entry_restored(self):
        lid=self.link(); self.finish(lid)
        before=self.get(lid)
        old_rules=s.node_rules(self.db,s.get_node(self.db,self.entry))
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=before['revision']),lid)
        self.ack(self.b); self.tick()
        switching=self.get(lid); self.assertEqual(switching['job']['phase'],'commit')
        self.call('api_link_action',dict(action='cancel',revision=switching['revision']),lid)
        self.assertEqual(self.get(lid)['job']['phase'],'rollback')
        self.assertEqual(s.node_rules(self.db,s.get_node(self.db,self.entry)),old_rules)
        self.assertTrue(s.node_rules(self.db,s.get_node(self.db,self.b)))
        self.tick(); self.assertIsNotNone(self.get(lid)['job'])
        self.ack(self.entry); self.tick()
        self.assertIsNone(self.get(lid)['job'])
        self.assertFalse(s.node_rules(self.db,s.get_node(self.db,self.b)))
        self.assertEqual(self.get(lid)['plan'],before['plan'])

    def test_cancel_new_link_returns_to_editable_draft(self):
        lid=self.link(); self.ack(self.a); self.tick()
        self.call('api_link_action',dict(action='cancel',revision=self.get(lid)['revision']),lid)
        self.ack(self.entry); self.tick()
        link=self.get(lid)
        self.assertEqual(link['state'],'draft'); self.assertIsNone(link['chain_id'])
        self.assertIsNone(link['job'])

    def test_receipt_summary_distinguishes_manual_timeout_and_failure(self):
        with self.db:
            self.db.execute("UPDATE nodes SET desired_mode='passive',installed_agent='passive',agent='0.4.0-rc.4' WHERE id=?", (self.entry,))
        lid = self.link()
        self.assertEqual(self.get(lid)['sync']['state'], 'preparing')
        self.ack(self.a); self.tick()
        before = self.get(lid)
        self.assertEqual(before['state'], 'switching')
        self.assertEqual(before['sync']['state'], 'manual')
        self.assertEqual([n['id'] for n in before['sync']['nodes']], [self.entry])
        self.assertFalse(before['sync']['retry'])
        with self.db:
            self.db.execute('UPDATE nodes SET wake_enabled=1,wake_port=33446 WHERE id=?', (self.entry,))
            s.wake.queue(s._module(), self.db, s.get_node(self.db,self.entry))
        self.assertEqual(self.get(lid)['sync']['state'], 'switching')
        with self.db:
            self.db.execute("UPDATE wake_jobs SET state='timeout' WHERE node_id=?", (self.entry,))
        self.assertEqual(self.get(lid)['sync']['state'], 'timeout')
        self.assertTrue(self.get(lid)['sync']['retry'])
        with self.db:
            self.db.execute("UPDATE nodes SET state='error',err='端口冲突：10001' WHERE id=?", (self.entry,))
        self.assertEqual(self.get(lid)['sync']['state'], 'failed')
        # Reading the presentation must not change the switch or claimed active path.
        self.assertEqual(self.get(lid)['state'], before['state'])
        self.assertEqual(self.get(lid)['selected'], before['selected'])
        config = self.h.api_configs(self.db,{})['chains'][0]
        self.assertEqual(config['link_sync']['state'], 'failed')

    def test_prepare_commit_and_stable_entry(self):
        lid=self.link()
        self.assertIsNone(self.get(lid)['connection'])
        self.assertEqual(s.node_rules(self.db,s.get_node(self.db,self.entry)),[])
        self.assertEqual(len(s.node_rules(self.db,s.get_node(self.db,self.a))),1)
        self.tick();self.assertEqual(self.get(lid)['state'],'preparing')
        self.ack(self.a);self.tick();self.assertEqual(self.get(lid)['state'],'switching')
        self.assertEqual(self.get(lid)['hops'],[])
        self.ack(self.entry);self.tick()
        l=self.get(lid);self.assertEqual(l['selected']['pool-a'],self.a)
        self.assertEqual(l['connection']['port'],10001)
        self.assertEqual([h['node_id'] for h in l['hops']],[self.entry,self.a])
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=l['revision']),lid)
        self.assertEqual(self.get(lid)['selected']['pool-a'],self.a)
        self.finish(lid)
        l=self.get(lid);self.assertEqual(l['selected']['pool-a'],self.b)
        self.assertEqual(l['connection']['port'],10001)

    def test_membership_inheritance_is_not_priority_inheritance(self):
        lid=self.link();self.finish(lid)
        l=self.get(lid);l['plan']['stages'][0]['order']=[self.b,self.a]
        self.call('api_link_save',dict(name=l['name'],plan=l['plan'],revision=l['revision']),lid);self.finish(lid)
        self.call('api_pool_save',dict(name='pool',members=[self.c,self.a,self.b]),self.pool)
        self.assertEqual(self.get(lid)['plan']['stages'][0]['order'],[self.b,self.a,self.c])
        self.assertEqual(self.get(lid)['selected']['pool-a'],self.a)
        self.assertEqual(self.get(lid)['plan']['stages'][0]['default'],self.a)
        with self.assertRaises(s.UserError):self.call('api_pool_delete',{},self.pool)
        with self.assertRaises(s.UserError):self.call('api_pool_save',dict(members=[self.b,self.c]),self.pool)
        l=self.get(lid)
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=l['revision']),lid)
        self.finish(lid)
        # Confirm old-rule cleanup, then an unused member can be removed.
        self.ack(self.a);self.tick()
        self.call('api_pool_save',dict(members=[self.b,self.c]),self.pool)
        self.assertEqual(self.get(lid)['plan']['stages'][0]['order'],[self.b,self.c])

    def test_quick_switch_changes_only_default_and_waits_for_receipt(self):
        self.call('api_pool_save',dict(members=[self.a,self.b,self.c]),self.pool)
        lid=self.link();self.finish(lid)
        other=self.call('api_link_save',dict(name='other',apply=False,plan=dict(entry=self.entry,
            target=self.target_id,stages=[dict(id='pool-a',kind='pool',pool_id=self.pool)])))['id']
        l=self.get(lid)
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.c,revision=l['revision']),lid)
        l=self.get(lid)
        self.assertEqual(l['plan']['stages'][0]['order'],[self.a,self.b,self.c])
        self.assertEqual(l['plan']['stages'][0]['default'],self.c)
        self.assertEqual(l['selected']['pool-a'],self.a)
        self.assertEqual(l['job']['selected']['pool-a'],self.c)
        self.assertEqual(l['applied_target_id'],self.target_id)
        self.assertEqual(self.get(other)['plan']['stages'][0]['order'],[self.a,self.b,self.c])
        self.assertEqual(json.loads(self.db.execute('SELECT members FROM pools WHERE id=?',(self.pool,)).fetchone()[0]),[self.a,self.b,self.c])
        self.finish(lid)
        self.assertEqual(self.get(lid)['selected']['pool-a'],self.c)

    def test_quick_switch_rejections_do_not_change_priority(self):
        lid=self.link();self.finish(lid)
        l=self.get(lid);l['plan']['stages'][0]['locked']=self.a
        self.call('api_link_save',dict(plan=l['plan'],revision=l['revision']),lid)
        l=self.get(lid)
        with self.assertRaises(s.UserError):
            self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=l['revision']),lid)
        self.assertEqual(self.get(lid)['plan'],l['plan'])
        l['plan']['stages'][0]['locked']=None
        self.call('api_link_save',dict(plan=l['plan'],revision=l['revision']),lid)
        l=self.get(lid)
        with self.db:self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(int(time.time())-120,self.b))
        with self.assertRaises(s.UserError):
            self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=l['revision']),lid)
        self.assertEqual(self.get(lid)['plan'],l['plan'])
        self.assertIsNone(self.get(lid)['job'])
        self.ack(self.b)
        with self.assertRaises(s.UserError):
            self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=l['revision']-1),lid)
        self.assertEqual(self.get(lid)['plan'],l['plan'])

    def test_setting_current_default_does_not_reapply_and_direct_keeps_priority(self):
        lid=self.link();self.finish(lid)
        l=self.get(lid)
        # A recovered first-priority node need not be the currently selected one.
        l['plan']['stages'][0].update(order=[self.b,self.a,0],direct=True)
        with self.db:self.db.execute('UPDATE links SET plan=? WHERE id=?',(json.dumps(l['plan']),lid))
        before=s.node_rules(self.db,s.get_node(self.db,self.entry))
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.a,revision=l['revision']),lid)
        l=self.get(lid)
        self.assertIsNone(l['job'])
        self.assertEqual(l['plan']['stages'][0]['order'],[self.b,self.a,0])
        self.assertEqual(l['plan']['stages'][0]['default'],self.a)
        self.assertEqual(s.node_rules(self.db,s.get_node(self.db,self.entry)),before)
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=0,revision=l['revision']),lid)
        self.assertEqual(self.get(lid)['plan']['stages'][0]['order'],[self.b,self.a,0])
        self.assertEqual(self.get(lid)['plan']['stages'][0]['default'],0)
        self.finish(lid)
        self.assertEqual(self.get(lid)['selected']['pool-a'],0)

    def test_rejected_apply_retains_preference_and_last_confirmed_path(self):
        lid=self.link();self.finish(lid);l=self.get(lid)
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=l['revision']),lid)
        self.ack(self.b,False);self.tick()
        l=self.get(lid)
        self.assertEqual(l['plan']['stages'][0]['order'],[self.a,self.b])
        self.assertEqual(l['plan']['stages'][0]['default'],self.b)
        self.assertEqual(l['selected']['pool-a'],self.a)
        self.assertEqual(l['state'],'error')

    def test_failover_uses_priority_without_changing_default_or_failing_back(self):
        self.call('api_pool_save',dict(members=[self.a,self.b,self.c]),self.pool)
        lid=self.link();self.finish(lid);l=self.get(lid)
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.c,revision=l['revision']),lid)
        self.finish(lid);self.ack(self.a);self.tick()
        with self.db:self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(int(time.time())-61,self.c))
        self.tick()
        self.assertEqual(self.get(lid)['job']['selected']['pool-a'],self.a)
        self.finish(lid)
        l=self.get(lid)
        self.assertEqual(l['selected']['pool-a'],self.a)
        self.assertEqual(l['plan']['stages'][0]['default'],self.c)
        self.assertEqual(l['plan']['stages'][0]['order'],[self.a,self.b,self.c])
        # Recovery and a priority edit both keep the current path and default.
        self.ack(self.c);self.tick()
        l['plan']['stages'][0]['order']=[self.b,self.a,self.c]
        self.call('api_link_save',dict(plan=l['plan'],revision=l['revision']),lid)
        l=self.get(lid)
        self.assertIsNone(l['job'])
        self.assertEqual(l['selected']['pool-a'],self.a)
        self.assertEqual(l['plan']['stages'][0]['default'],self.c)

    def test_legacy_default_and_membership_changes_preserve_instance_order(self):
        lid=self.link();self.finish(lid);l=self.get(lid)
        del l['plan']['stages'][0]['default']
        with self.db:self.db.execute('UPDATE links SET plan=? WHERE id=?',(json.dumps(l['plan']),lid))
        self.assertEqual(self.get(lid)['plan']['stages'][0]['default'],self.a)
        self.call('api_pool_save',dict(members=[self.c,self.b,self.a]),self.pool)
        stage=self.get(lid)['plan']['stages'][0]
        self.assertEqual(stage['order'],[self.a,self.b,self.c])
        self.assertEqual(stage['default'],self.a)
        self.call('api_pool_save',dict(members=[self.c,self.a]),self.pool)
        self.assertEqual(self.get(lid)['plan']['stages'][0]['order'],[self.a,self.c])
        self.assertEqual(self.get(lid)['plan']['stages'][0]['default'],self.a)

    def test_timeout_is_unknown_and_late_receipt_finishes(self):
        lid=self.link();self.ack(self.a);self.tick()
        l=self.get(lid);self.tick(l['job']['at']+31)
        self.assertEqual(self.get(lid)['state'],'waiting')
        self.assertIsNotNone(self.get(lid)['job'])
        with self.assertRaises(s.UserError):self.call('api_pool_save',dict(members=[self.b]),self.pool)
        self.ack(self.entry);self.tick()
        self.assertEqual(self.get(lid)['state'],'active')

    def test_failover_needs_60_seconds_and_does_not_failback(self):
        lid=self.link();self.finish(lid);now=int(time.time())
        with self.db:self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(now-59,self.a))
        self.tick(now);self.assertIsNone(self.get(lid)['job'])
        with self.db:self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(now-61,self.a))
        self.tick(now);self.assertEqual(self.get(lid)['job']['selected']['pool-a'],self.b)
        self.finish(lid);self.ack(self.a);self.tick()
        self.assertEqual(self.get(lid)['selected']['pool-a'],self.b)

    def test_failover_skips_member_without_free_internal_port(self):
        self.call('api_pool_save',dict(members=[self.a,self.b,self.c]),self.pool)
        lid=self.link();self.finish(lid)
        with self.db:
            self.db.execute("UPDATE nodes SET ports='10000' WHERE id=?",(self.b,))
            self.db.execute('INSERT INTO retired_ports VALUES (?,10000)',(self.b,))
            self.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(int(time.time())-61,self.a))
        self.tick();self.assertIn(self.b,self.get(lid)['failures'])
        self.tick();self.assertEqual(self.get(lid)['job']['selected']['pool-a'],self.c)

    def test_lock_disable_direct_and_node_references(self):
        lid=self.link();self.finish(lid);l=self.get(lid)
        l['plan']['stages'][0]['locked']=self.a
        self.call('api_link_save',dict(name=l['name'],plan=l['plan'],revision=l['revision']),lid)
        with self.assertRaises(s.UserError):self.call('api_node_edit',dict(desired_mode='passive'),self.a)
        with self.assertRaises(s.UserError):self.call('api_node_del',{},self.b)
        l=self.get(lid)
        with self.assertRaises(s.UserError):self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=l['revision']),lid)
        l['plan']['stages'][0].update(locked=None,direct=True,order=[0,self.a,self.b],default=0)
        self.call('api_link_save',dict(name=l['name'],plan=l['plan'],revision=l['revision']),lid);self.finish(lid)
        self.assertEqual(self.get(lid)['selected']['pool-a'],0)
        self.assertEqual([h['node_id'] for h in self.get(lid)['hops']],[self.entry])

    def test_pending_entry_port_is_reserved_and_delete_waits_for_cleanup(self):
        lid=self.link()
        with self.assertRaises(s.UserError):self.link()
        self.finish(lid)
        self.call('api_link_delete',{},lid);self.tick()
        self.assertEqual(self.get(lid)['state'],'deleting')
        self.ack(self.entry);self.ack(self.a);self.tick()
        with self.assertRaises(s.UserError):self.get(lid)

    def test_preparation_failure_keeps_old_entry(self):
        lid=self.link();self.finish(lid)
        before=s.node_rules(self.db,s.get_node(self.db,self.entry))
        l=self.get(lid)
        self.call('api_link_action',dict(action='switch',stage='pool-a',member=self.b,revision=l['revision']),lid)
        self.ack(self.b,False);self.tick()
        self.assertEqual(self.get(lid)['state'],'error')
        self.assertEqual(s.node_rules(self.db,s.get_node(self.db,self.entry)),before)
        self.assertEqual(self.get(lid)['selected']['pool-a'],self.a)

    def test_graph_probes_candidates_and_bypass_without_pool_internal_edges(self):
        p2=self.call('api_pool_save',dict(name='second',members=[self.c]))['id']
        lid=self.call('api_link_save',dict(name='graph',apply=False,plan=dict(entry=self.entry,target=self.target_id,
            stages=[dict(id='one',kind='pool',pool_id=self.pool),dict(id='two',kind='pool',pool_id=p2,direct=True)])))['id']
        with self.db:self.db.execute("UPDATE links SET state='active' WHERE id=?",(lid,))
        paths,edges=s.pools.quality_paths(self.db)
        self.assertIn((self.entry,self.a),edges);self.assertIn((self.a,self.c),edges)
        self.assertNotIn((self.a,self.b),edges)
        self.assertEqual(set(paths[('links',lid)]),{self.entry,self.a,self.b,self.c})

    def test_migration_preserves_rules_and_creates_drafts(self):
        route=self.route([self.entry,self.a])
        cid=self.config(route,[10003,10004],target_id=self.target_id)
        self.route([self.b],name='unused')
        before={n:s.node_rules(self.db,s.get_node(self.db,n)) for n in (self.entry,self.a)}
        with self.db:
            for table in ('link_rules','link_retired','link_events','links','pools'):
                self.db.execute('DROP TABLE '+table)
            self.db.execute('PRAGMA user_version=402')
        self.db.close();self.db=s.init_db()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],403)
        self.assertEqual(before,{n:s.node_rules(self.db,s.get_node(self.db,n)) for n in before})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM links WHERE state='draft'").fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT chain_id FROM links WHERE chain_id IS NOT NULL').fetchone()[0],cid)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM routes').fetchone()[0],0)
        self.assertTrue(Path(s.DB_PATH+'.pre-0.4.0-rc.4.bak').is_file())
        self.db.close();self.db=s.init_db()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM links').fetchone()[0],2)

    def test_passive_wake_receipt_and_restart_resume(self):
        with self.db:
            self.db.execute("UPDATE nodes SET desired_mode='passive',installed_agent='passive',agent='0.4.0-rc.4',wake_enabled=1,wake_port=39000 WHERE id=?",(self.entry,))
        lid=self.link();self.ack(self.a);self.tick()
        l=self.get(lid);self.assertEqual(l['state'],'switching')
        job=self.db.execute('SELECT * FROM wake_jobs WHERE node_id=?',(self.entry,)).fetchone()
        self.assertEqual(job['state'],'waiting')
        self.tick(l['job']['at']+31)
        self.db.close();self.db=s.init_db()
        self.assertEqual(self.get(lid)['state'],'waiting')
        fields=dict(t=str(int(time.time())),proto='2',source='wake',phase='begin',op='a'*32,
                    agent_kind='passive',agent='0.4.0-rc.4',mode='passive',state='ok',v='',au='0',wake_id=job['token'])
        with self.db:
            p=s.agent_v3.fields(s.sync_report(self.db,s.get_node(self.db,self.entry),fields,'127.0.0.1'))
            self.assertEqual(p['action'],'apply')
            fields.update(phase='ack',op=p['op'],generation=p['generation'],v=p['v'])
            receipt=s.agent_v3.fields(s.sync_report(self.db,s.get_node(self.db,self.entry),fields,'127.0.0.1'))
            self.assertEqual(receipt['result'],'confirmed')
        self.tick();self.assertEqual(self.get(lid)['state'],'active')
