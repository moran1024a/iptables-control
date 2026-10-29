"""Operation-bound acknowledgments, passive lifecycle and legacy compatibility."""
import secrets
import time
import unittest
from unittest.mock import patch
from test_server import Fixture, s


def fields(text):
    return dict(line.split('=',1) for line in text.splitlines() if '=' in line)


class SyncTests(Fixture, unittest.TestCase):
    def report(self,nid,**kw):
        data=dict(t=str(int(time.time())),state='ok',v='',agent='0.2.0',au='0',proto='2',source='manual',phase='begin',op=secrets.token_hex(16),mode='active',generation='0')
        data.update(kw)
        with self.db:
            return s.sync_report(self.db,s.get_node(self.db,nid),data,'127.0.0.1')

    def begin(self,nid,**kw):return fields(self.report(nid,**kw))

    def ack(self,nid,op,**kw):
        payload={k:op[k] for k in ('op','generation','mode','v')};payload.update(phase='ack');payload.update(kw)
        return fields(self.report(nid,**payload)).get('result')

    def ready(self):
        n=self.node();r=self.route([n]);c=self.config(r,[10001]);op=self.begin(n)
        self.assertEqual(self.ack(n,op),'confirmed');return n,r,c

    def passive(self):
        n,r,c=self.ready();self.call('api_node_edit',{'desired_mode':'passive'},n)
        op=self.begin(n);self.assertEqual(self.ack(n,op),'confirmed');return n,r,c

    def waiting(self,n):
        self.assertEqual(s.node_status(self.db,s.get_node(self.db,n),int(time.time())+864000),'syncing')

    def test_begin_old_ok_cannot_clear_pending_or_reserved_ports(self):
        n,r,c=self.ready();self.call('api_config_save',{'route_id':None},c)
        want=s.rules_version(s.node_rules(self.db,s.get_node(self.db,n)))
        op=self.begin(n,v=want,state='ok')
        self.assertTrue(self.db.execute('SELECT 1 FROM retired_ports').fetchone())
        self.assertTrue(self.db.execute('SELECT 1 FROM chain_pending').fetchone())
        self.assertEqual(self.ack(n,op),'confirmed')
        self.assertFalse(self.db.execute('SELECT 1 FROM retired_ports').fetchone())

    def test_auto_pause_does_not_apply_or_confirm_passive(self):
        n,r,c=self.ready();self.call('api_node_edit',{'desired_mode':'passive'},n)
        out=self.begin(n,source='auto',phase='poll');self.assertEqual(out['action'],'pause');self.assertNotIn('v',out)
        self.waiting(n);self.assertTrue(s.get_node(self.db,n)['manual_required'])
        op=self.begin(n,mode='passive');self.waiting(n);self.assertEqual(self.ack(n,op),'confirmed')
        self.assertEqual(s.node_status(self.db,s.get_node(self.db,n),int(time.time())+864000),'synced')

    def test_passive_to_active_and_back_requires_manual_current_generation(self):
        n,r,c=self.passive();self.call('api_node_edit',{'desired_mode':'active'},n)
        old=self.begin(n,mode='passive');self.call('api_node_edit',{'desired_mode':'passive'},n)
        self.call('api_node_edit',{'desired_mode':'active'},n)
        self.assertEqual(self.ack(n,old),'changed');self.waiting(n)
        self.assertEqual(self.begin(n,source='auto',phase='poll')['action'],'pause')
        op=self.begin(n);self.assertEqual(self.ack(n,op),'confirmed')
        self.assertFalse(s.get_node(self.db,n)['manual_required'])

    def test_passive_failures_wait_forever_and_record_green(self):
        n,r,c=self.passive();t=s.get_chain(self.db,c)['target_id'];self.call('api_target_save',{'port':8443},t)
        op=self.begin(n,mode='passive');self.assertEqual(self.ack(n,op,state='error',err='apply failed'),'failed');self.waiting(n)
        self.assertTrue(self.db.execute('SELECT 1 FROM chain_pending').fetchone())
        bar=s.node_bar(self.db,s.get_node(self.db,n),int(time.time()))
        self.assertEqual(bar['levels'][-1],'ok');self.assertTrue(all(x in ('ok','white') for x in bar['levels']))
        future=s.node_bar(self.db,s.get_node(self.db,n),int(time.time())+3601)
        self.assertEqual(set(future['levels']),{'white'})

    def test_bad_expired_and_replaced_ack_never_clear(self):
        n,r,c=self.ready();self.call('api_config_save',{'route_id':None},c);old=self.begin(n)
        for kw in ({'generation':'999'},{'v':'bad'},{'mode':'passive'},{'source':'auto'}):
            self.assertEqual(self.ack(n,old,**kw),'expired')
            self.assertTrue(self.db.execute('SELECT 1 FROM retired_ports').fetchone())
        with self.db:self.db.execute('UPDATE sync_ops SET expires=0 WHERE node_id=?',(n,))
        self.assertEqual(self.ack(n,old),'expired')
        new=self.begin(n);self.assertEqual(self.ack(n,old),'expired');self.assertEqual(self.ack(n,new),'confirmed')

    def test_concurrent_edit_and_aba_advance_generation(self):
        n,r,c=self.ready();t=s.get_chain(self.db,c)['target_id'];old=self.begin(n)
        self.call('api_target_save',{'port':80},t);self.call('api_target_save',{'port':443},t)
        self.assertEqual(old['v'],s.rules_version(s.node_rules(self.db,s.get_node(self.db,n))))
        self.assertEqual(self.ack(n,old),'changed');self.assertTrue(self.db.execute('SELECT 1 FROM chain_pending').fetchone())
        self.assertEqual(self.ack(n,self.begin(n)),'confirmed')

    def test_ack_receipt_survives_restart_and_is_idempotent(self):
        n,r,c=self.ready();op=self.begin(n);self.db.close();self.db=s.init_db()
        self.assertEqual(self.ack(n,op),'confirmed')
        events=self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        self.db.close();self.db=s.init_db();self.assertEqual(self.ack(n,op),'confirmed')
        self.assertEqual(events,self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0])

    def test_manual_activity_count_deduplicates_and_excludes_auto(self):
        n=self.node();token=secrets.token_hex(16)
        self.begin(n,source='auto',phase='poll')
        self.assertEqual(self.db.execute('SELECT COALESCE(SUM(manual_count),0) FROM windows').fetchone()[0],0)
        self.begin(n,op=token);self.begin(n,op=token)
        self.assertEqual(self.db.execute('SELECT SUM(manual_count) FROM windows').fetchone()[0],1)
        self.assertEqual(self.begin(n,source='auto',phase='poll')['action'],'busy')
        self.assertEqual(self.db.execute('SELECT op FROM sync_ops').fetchone()[0],token)

    def test_legacy_can_upgrade_but_not_switch_or_override_proto2(self):
        n=self.node();want=s.rules_version([])
        self.report(n,proto='1',v=want,agent='0.1.8')
        with self.assertRaises(s.UserError):self.call('api_node_edit',{'desired_mode':'passive'},n)
        self.begin(n);self.call('api_node_edit',{'desired_mode':'passive'},n)
        self.report(n,proto='1',v=want,agent='0.1.8');self.waiting(n)
        self.assertEqual(s.get_node(self.db,n)['protocol_version'],2)

    def test_delete_passive_requires_ack_and_preserves_final_receipt(self):
        n,r,c=self.passive();self.call('api_chain_del',{},c)
        self.assertTrue(self.db.execute('SELECT 1 FROM chains').fetchone())
        op=self.begin(n,mode='passive');self.assertEqual(self.ack(n,op),'confirmed')
        self.assertFalse(self.db.execute('SELECT 1 FROM chains').fetchone())
        self.call('api_route_del',{},r);key=s.get_node(self.db,n)['key'];self.call('api_node_del',{},n)
        op=self.begin(n,mode='passive');self.assertEqual(self.ack(n,op),'deleted')
        self.assertFalse(self.db.execute('SELECT 1 FROM nodes').fetchone())
        self.assertEqual(self.db.execute('SELECT key FROM deleted_receipts').fetchone()[0],key)

    def test_shared_target_only_changes_last_hop_generation(self):
        a,b=self.node(),self.node('B');r=self.route([a,b]);t=self.target();self.config(r,[10001,10002],target_id=t)
        before={n:s.get_node(self.db,n)['desired_generation'] for n in (a,b)}
        self.db.execute('DELETE FROM chain_pending');self.db.commit()
        self.call('api_target_save',{'host':'changed.example.com'},t)
        self.assertEqual(s.get_node(self.db,a)['desired_generation'],before[a])
        self.assertGreater(s.get_node(self.db,b)['desired_generation'],before[b])
        self.assertEqual([r[0] for r in self.db.execute('SELECT node_id FROM chain_pending')],[b])

    def test_route_fanout_failure_rolls_back_generations(self):
        a,b=self.node(),self.node('B',ports='10000');r=self.route([a]);self.config(r);self.config(r)
        old=s.mutation_snapshot(self.db)
        with self.assertRaises(s.UserError):self.call('api_route_save',{'node_ids':[a,b]},r)
        self.assertEqual(old,s.mutation_snapshot(self.db))

    def test_force_delete_port_held_until_matching_ack(self):
        n,r,c=self.ready();self.h.query={'force':['1']};self.call('api_chain_del',{},c)
        self.assertTrue(self.db.execute('SELECT 1 FROM retired_ports').fetchone())
        with self.assertRaises(s.UserError):self.config(r,[10001])
        self.assertEqual(self.ack(n,self.begin(n)),'confirmed');self.config(r,[10001])

    def test_metadata_and_credentials_do_not_request_forward_sync(self):
        n,r,c=self.ready();g=s.get_node(self.db,n)['desired_generation']
        self.call('api_node_edit',{'note':'note','name':'renamed'},n)
        self.call('api_config_save',{'remark':'renamed'},c);self.call('api_route_save',{'name':'new'},r)
        self.assertEqual(s.get_node(self.db,n)['desired_generation'],g)
        self.assertFalse(self.db.execute('SELECT 1 FROM chain_pending').fetchone())

    def test_legacy_agent_download_does_not_acknowledge_configuration(self):
        n=self.node();r=self.route([n]);self.config(r)
        with patch.object(s,'AGENT',None):
            s.load_agent()
            data=self.report(n,proto='1',agent='0.1.8',au='1',get='agent',v=s.rules_version(s.node_rules(self.db,s.get_node(self.db,n))))
            self.assertEqual(data,s.AGENT[2]);self.assertIn(b'VERSION="0.2.1"',data)
            self.assertTrue(self.db.execute('SELECT 1 FROM chain_pending').fetchone())
            self.assertEqual(s.get_node(self.db,n)['protocol_version'],1)

    def test_port_conflict_blocks_same_generation_without_clearing_on_heartbeat(self):
        n,r,c=self.ready();self.call('api_config_save',{'ports':[10002]},c)
        op=self.begin(n,source='auto',phase='poll')
        error='端口冲突：TCP 10002 已被单机脚本保存的配置占用'
        self.assertEqual(self.ack(n,op,source='auto',state='error',err=error),'failed')
        count=self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        for _ in range(3):
            response=self.begin(n,source='auto',phase='poll',state='ok',v=op['v'],generation=op['generation'])
            self.assertEqual(response['action'],'blocked');self.assertEqual(response['err'],error)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],count)
        self.assertEqual(s.node_status(self.db,s.get_node(self.db,n),int(time.time())),'error')
        self.assertEqual(self.call('api_configs')['chains'][0]['status'],'error')
        self.assertTrue(self.db.execute('SELECT 1 FROM chain_pending').fetchone())
        self.db.execute('UPDATE sync_ops SET expires=0');self.db.commit();s.cleanup(self.db)
        self.assertEqual(self.begin(n,source='auto',phase='poll')['action'],'blocked')
        self.call('api_config_save',{'ports':[10003]},c)
        new=self.begin(n,source='auto',phase='poll');self.assertEqual(new['action'],'apply')
        self.assertEqual(self.ack(n,new,source='auto'),'confirmed')
        self.assertEqual(s.node_status(self.db,s.get_node(self.db,n),int(time.time())),'synced')

    def test_passive_port_conflict_survives_begin_old_success_and_restart(self):
        n,r,c=self.passive();self.call('api_config_save',{'ports':[10002]},c)
        op=self.begin(n);self.assertEqual(self.ack(n,op,state='error',err='端口冲突：UDP 10002 已被本机服务占用'),'failed')
        self.db.close();self.db=s.init_db()
        self.assertEqual(s.node_status(self.db,s.get_node(self.db,n),int(time.time())+864000),'error')
        old=self.begin(n)
        self.assertEqual(s.node_status(self.db,s.get_node(self.db,n),int(time.time())),'error')
        self.call('api_config_save',{'ports':[10003]},c)
        self.assertEqual(self.ack(n,old),'changed')
        self.assertEqual(s.node_status(self.db,s.get_node(self.db,n),int(time.time())),'error')
        new=self.begin(n);self.assertEqual(self.ack(n,new),'confirmed')
        self.assertEqual(s.node_status(self.db,s.get_node(self.db,n),int(time.time())+864000),'synced')
