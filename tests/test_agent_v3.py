import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import database
import errors
import forwarding
import hashlib
import http_harness
import security
import settings
"""New protocol receipts, cumulative counters and shared HTTP/WebSocket transport."""
import asyncio
import base64
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from test_server import Fixture
import agent_v3
import node_addresses
import transport


class V3Tests(Fixture, unittest.TestCase):
    def test_corrupt_capabilities_cannot_stop_global_scheduling(self):
        import execution, failover, network_quality
        a, b = self.node(), self.node()
        self.ready(a, b)
        self.save(self.graph([a, b]))
        now = int(time.time())
        engine = failover.Engine(now - 60)
        for value in ('["truncated', 'null', '"network_quality"', '{"network_quality":true}'):
            with self.subTest(value=value), self.db:
                self.db.execute('UPDATE nodes SET capabilities=? WHERE id=?', (value, a))
                engine.tick(self.db, now)
                execution.tick(self.db, now)
                nodes = {n['id']:dict(n) for n in self.db.execute('SELECT * FROM nodes')}
                self.assertNotIn(a, engine.health(nodes, now + 1)[1])
                self.assertEqual(network_quality.stopped(nodes, dict(source=a,target=b,protocol='udp-echo-v1'))[0], 'unsupported')

    def test_upgrade_prepare_preserves_version_and_auto_update_until_commit(self):
        nid=self.node();self.ready(nid)
        with self.db:
            self.db.execute("UPDATE nodes SET agent='0.5.3',auto_update=1 WHERE id=?",(nid,))
        component=Path(self.temp.name)/'asset';component.write_text('test')
        def manage(**kw):
            with self.db:
                return agent_v3.fields(agent_v3.manage(self.db,database.get_node(self.db,nid),{'t':str(int(time.time())),**kw}))
        with patch.object(agent_v3,'artifact',return_value=(component,'f'*64)):
            prepared=manage(manage='prepare',target='active',arch='amd64')
        n=database.get_node(self.db,nid)
        self.assertEqual((n['agent'],n['auto_update']),('0.5.3',1))
        import execution
        with self.assertRaisesRegex(errors.UserError,'等待 Agent 升级'):
            execution.ready(self.db,nid,int(time.time()))
        self.assertEqual(manage(manage='commit',op=prepared['op'],target='active',agent=settings.VERSION)['result'],'confirmed')
        n=database.get_node(self.db,nid)
        self.assertEqual((n['agent'],n['auto_update']),(settings.VERSION,1))

    def test_aborted_download_releases_manual_sync_without_confirming_mode(self):
        nid=self.node()
        component=Path(self.temp.name)/'asset'; component.write_text('test')
        def manage(**kw):
            with self.db:
                return agent_v3.fields(agent_v3.manage(self.db,database.get_node(self.db,nid),{'t':str(int(time.time())), **kw}))
        with patch.object(agent_v3,'artifact',return_value=(component,'f'*64)):
            prepared=manage(manage='prepare',target='active',arch='amd64')
        self.assertTrue(self.db.execute("SELECT 1 FROM sync_ops WHERE source='manual'").fetchone())
        self.assertEqual(manage(manage='abort',op=prepared['op'])['result'],'aborted')
        self.assertFalse(self.db.execute("SELECT 1 FROM sync_ops WHERE source='manual'").fetchone())
        self.assertEqual(database.get_node(self.db,nid)['installed_agent'],'unknown')
        self.assertFalse(database.get_node(self.db,nid)['manual_required'])
        with self.assertRaises(errors.UserError):
            manage(manage='commit',op=prepared['op'],target='active')

    def test_cumulative_uint64_deduplicates_reorders_and_adds_epochs(self):
        nid = self.node()
        row = dict(rule_id=1, protocol='tcp', epoch='a'*32, up=str(2**63+17), down='30', up_packets='5', down_packets='3')
        with self.db:
            for _ in range(2):
                agent_v3.accept_traffic(self.db, nid, [row])
            agent_v3.accept_traffic(self.db, nid, [{**row, 'up':'1'}])
            agent_v3.accept_traffic(self.db, nid, [{**row, 'epoch':'b'*32, 'up':'20'}])
        totals=agent_v3.telemetry(self.db,nid)['traffic'][0]
        self.assertEqual(totals['up'], str(2**63+37))
        self.assertEqual(totals['down'], '60')
        with self.assertRaises(errors.UserError):
            agent_v3.accept_traffic(self.db, nid, [{**row, 'up':str(2**64)}])

    def test_manual_handover_is_confirmed_and_stale_receipt_cannot_resume(self):
        nid=self.node()
        self.call('api_node_edit', {'desired_mode':'passive'}, nid)
        component=Path(self.temp.name)/'asset'; component.write_text('test')
        def manage(**kw):
            with self.db:
                return agent_v3.fields(agent_v3.manage(self.db,database.get_node(self.db,nid),{'t':str(int(time.time())), **kw}))
        with patch.object(agent_v3,'artifact',return_value=(component,'f'*64)):
            prepared=manage(manage='prepare',target='passive',arch='portable')
        n=database.get_node(self.db,nid)
        self.assertEqual(n['installed_agent'],'unknown')
        self.assertEqual(n['manual_required'],1)
        ack=manage(manage='commit',op=prepared['op'],target='passive',agent=settings.VERSION)
        self.assertEqual(ack['result'],'confirmed')
        self.assertEqual(database.get_node(self.db,nid)['installed_agent'],'passive')
        self.assertEqual(database.get_node(self.db,nid)['err'],'')
        self.call('api_node_edit',{'desired_mode':'active'},nid)
        self.assertEqual(manage(manage='commit',op=prepared['op'],target='passive')['result'],'changed')
        self.assertEqual(database.get_node(self.db,nid)['manual_required'],1)

    def test_port_snapshot_repairs_lost_release_but_cannot_clear_new_generation(self):
        nid=self.node()
        config_hash=forwarding.rules_version([])
        with self.db:
            self.db.execute("UPDATE nodes SET installed_agent='active', applied_generation=desired_generation,reported_v=? WHERE id=?", (config_hash,nid))
            self.db.execute('INSERT INTO retired_ports VALUES (?,10001)',(nid,))
            node=database.get_node(self.db,nid)
            report=dict(generation=node['desired_generation'],hash=config_hash,held_ports=[])
            agent_v3.refresh_held_ports(self.db,node,report)
            self.assertEqual(self.db.execute('SELECT count(*) FROM retired_ports').fetchone()[0],0)
            self.db.execute('UPDATE nodes SET desired_generation=desired_generation+1 WHERE id=?',(nid,))
            self.db.execute('INSERT INTO retired_ports VALUES (?,10002)',(nid,))
            agent_v3.refresh_held_ports(self.db,database.get_node(self.db,nid),report)
            self.assertEqual(self.db.execute('SELECT port FROM retired_ports').fetchone()[0],10002)

    def test_forwarding_health_requires_current_generation_and_actual_report(self):
        import node_state
        nid = self.node()
        self.ready(nid)
        node = database.get_node(self.db, nid)
        snapshot = dict(generation=node['applied_generation'], hash=node['reported_v'], state='ok')
        health = dict(snapshot, state='error', error='missing rules')
        with self.db:
            agent_v3.packet(self.db, node, status=dict(snapshot, forwarding=health), observe_only=True)
        current = database.get_node(self.db, nid)
        self.assertEqual(current['runtime_state'], 'error')
        self.assertEqual(current['state'], 'ok')  # Does not corrupt config confirmation.
        self.assertEqual(node_state.node_status(self.db, current, int(time.time())), 'error')
        for observe, generation in ((False, node['applied_generation']), (True, node['applied_generation']-1)):
            with self.db:
                agent_v3.packet(self.db, database.get_node(self.db, nid), status=dict(snapshot, forwarding=dict(health, state='ok', generation=generation)), observe=observe, observe_only=True)
            self.assertEqual(database.get_node(self.db, nid)['runtime_state'], 'error')
        with self.db:
            agent_v3.packet(self.db, database.get_node(self.db, nid), status=dict(snapshot, forwarding=dict(health, state='ok', error='')), observe_only=True)
        self.assertEqual(database.get_node(self.db, nid)['runtime_state'], 'ok')
        self.ack(nid)
        self.assertEqual(database.get_node(self.db, nid)['runtime_state'], 'unknown')

    def test_release_comparison_respects_rc_and_stable(self):
        self.assertTrue(agent_v3.newer('0.4.0','0.4.0-rc.1'))
        self.assertTrue(agent_v3.newer('0.4.0-rc.10','0.4.0-rc.2'))
        self.assertFalse(agent_v3.newer('0.4.0-rc.1','0.4.0'))


class TransportTests(Fixture, unittest.IsolatedAsyncioTestCase):

    async def test_invalid_hello_does_not_replace_connection_or_store_corrupt_data(self):
        from aiohttp import WSMsgType
        nid = self.node()
        self.ready(nid)
        valid, _, _ = await self.connect(nid)
        session = self.app['hub'].sessions[nid]
        for capabilities in (None, {}, 'network_quality', ['x'] * 300, ['x' * 4096]):
            ws = await self.client.ws_connect(http_harness.Handler.sync_path+'/v3/ws',
                headers={'Authorization':'Bearer '+database.get_node(self.db,nid)['key']})
            await ws.send_json(dict(type='hello', protocol=3, capabilities=capabilities))
            self.assertEqual((await ws.receive(timeout=3)).type, WSMsgType.CLOSE)
            self.assertEqual(ws.close_code, 1008)
            self.assertIs(self.app['hub'].sessions[nid], session)
            self.assertIsNotNone(agent_v3.node_capabilities(database.get_node(self.db,nid)))
            await ws.close()
        await valid.close()

    async def test_heartbeat_refreshes_liveness_without_acknowledging_rules(self):
        nid = self.node()
        self.ready(nid)
        ws, session, _ = await self.connect(nid)
        with self.db:
            self.db.execute('UPDATE nodes SET last_seen=1,desired_generation=desired_generation+1 WHERE id=?', (nid,))
        before = dict(database.get_node(self.db,nid))
        await ws.send_json(dict(type='heartbeat', session=session))
        for _ in range(100):
            after = database.get_node(self.db,nid)
            if after['last_seen'] > 1:
                break
            await asyncio.sleep(0.01)
        self.assertGreater(after['last_seen'], 1)
        for key in ('applied_generation', 'reported_v', 'runtime_at', 'state'):
            self.assertEqual(after[key], before[key])
        await ws.close()

    async def test_private_controls_and_artifacts_reject_missing_or_wrong_identity(self):
        nid = self.node()
        panel, sync = http_harness.Handler.panel_path, http_harness.Handler.sync_path
        for path in ('topologies', 'quality', f'nodes/{nid}/query'):
            response = await self.client.post(panel + '/api/' + path, json={})
            self.assertEqual(response.status, 401)
        for headers in ({}, {'Authorization': 'Bearer ' + 'ff' * 32}):
            for path in ('ws', 'artifacts/manage.sh'):
                response = await self.client.get(sync + '/v3/' + path, headers=headers)
                self.assertEqual(response.status, 404)
            response = await self.client.post(sync + '/v3/traffic', json=[], headers=headers)
            self.assertEqual(response.status, 404)
        # A correctly encrypted body with a forged MAC must not update node state.
        node = database.get_node(self.db, nid)
        ek, _ = security.derive(node['key'])
        body = security.aes(ek, f't={int(time.time())}\nstate=ok\nproto=2\n'.encode())[8:]
        response = await self.client.post(sync, data=base64.b64encode(body + bytes(32)))
        self.assertEqual(response.status, 404)
        self.assertIsNone(database.get_node(self.db, nid)['last_seen'])

    async def asyncSetUp(self):
        http_harness.Handler.sync_path=database.setting(self.db,'sync_path')
        http_harness.Handler.panel_path=database.setting(self.db,'panel_path')
        self.app=transport.create_app()
        self.client=TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def connect(self,nid):
        node=database.get_node(self.db,nid)
        ws=await self.client.ws_connect(http_harness.Handler.sync_path+'/v3/ws',headers={'Authorization':'Bearer '+node['key']})
        await ws.send_json(dict(type='hello',protocol=3,version=settings.VERSION,generation=0,hash='',installation_id='a'*32))
        welcome=await ws.receive_json(timeout=3)
        config=await ws.receive_json(timeout=3)
        return ws,welcome['session'],config

    async def test_configuration_receipt_push_pause_and_old_http(self):
        nid=self.node()
        with self.db:
            self.db.execute("UPDATE nodes SET installed_agent='active',backend='nftables' WHERE id=?",(nid,))
        ws,session,c=await self.connect(nid)
        self.assertEqual(c['type'],'config')
        await ws.send_json(dict(type='apply_result',session=session,op=c['op'],generation=c['generation'],hash=c['hash'],ok=True,held_ports=[],version=settings.VERSION))
        ack=await ws.receive_json(timeout=3)
        self.assertEqual(ack['result'],'confirmed')
        self.assertEqual((await ws.receive_json(timeout=3))['type'],'ready')
        # Slow telemetry queued before the receipt must not roll back status.
        await ws.send_json(dict(type='status', session=session, generation=0, hash='', state='ok'))
        await ws.send_json(dict(type='heartbeat', session=session))
        self.assertEqual((await ws.receive_json(timeout=3))['type'], 'ready')
        query=asyncio.create_task(self.app['hub'].query(nid,dict(kind='inventory')))
        request=await ws.receive_json(timeout=3)
        self.assertEqual(request['type'],'query')
        await ws.send_json(dict(type='query_result',session=session,id=request['id'],data=dict(
            network_complete=True,interfaces=[dict(name='ens3',addresses=['192.0.2.1/24'])])))
        self.assertEqual((await query).status,200)
        self.assertEqual(node_addresses.listing(self.db,database.get_node(self.db,nid))[0]['device'],'ens3')
        response=await self.client.get(http_harness.Handler.panel_path+'/api/me')
        self.assertEqual(response.status,401)
        self.call('api_node_edit',{'desired_mode':'passive'},nid)
        self.app['hub'].notify()
        self.assertEqual((await ws.receive_json(timeout=3))['type'],'quiesce')
        await ws.send_json(dict(type='quiesced',session=session))
        await ws.close()
        self.assertEqual(database.get_node(self.db,nid)['manual_required'],1)

    async def test_quality_api_authentication_scope_and_cookie_renewal(self):
        a, b = self.node(), self.node('B')
        rid = self.save(self.graph([a, b]))
        path = http_harness.Handler.panel_path + '/api/quality'
        payload = dict(scope='topologies', id=rid,source=a,target=b)
        response = await self.client.post(path, json=payload)
        self.assertEqual(response.status, 401)
        token = 'quality-session'
        with self.db:
            database.set_setting(self.db, 'must_change', '0')
            self.db.execute('INSERT INTO sessions VALUES (?,?)',
                            (hashlib.sha256(token.encode()).hexdigest(), int(time.time()) + 100))
        headers = {'Cookie': settings.SESSION_COOKIE + '=' + token}
        response = await self.client.post(path, json=payload, headers=headers)
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data['pair']['source'], a)
        self.assertEqual(len(data['series']), 2)
        self.assertEqual(data['series'][0]['state'], 'unsupported')
        self.assertIn('Set-Cookie', response.headers)
        response = await self.client.post(path, json={**payload, 'source': a, 'target': 999}, headers=headers)
        self.assertEqual(response.status, 400)
        response = await self.client.post(path, json=dict(scope='topologies', id=rid, configuration=999), headers=headers)
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())['field'], 'configuration')

    async def test_seventy_idle_sessions_do_not_exhaust_http_workers(self):
        nodes=[self.node(str(i)) for i in range(70)]
        with self.db:
            self.db.execute("UPDATE nodes SET installed_agent='active',backend='nftables'")
        connections=await asyncio.gather(*(self.connect(nid) for nid in nodes))
        response=await asyncio.wait_for(self.client.get(http_harness.Handler.panel_path+'/api/me'),3)
        self.assertEqual(response.status,401)
        self.assertEqual(len(self.app['hub'].sessions),70)
        await asyncio.gather(*(ws.close() for ws,_,_ in connections))

    async def test_encrypted_legacy_sync_is_rejected(self):
        nid=self.node()
        node=database.get_node(self.db,nid)
        ek,mk=security.derive(node['key'])
        body=security.aes(ek,('t='+str(int(time.time()))+'\nstate=init\nagent=0.2.1\n').encode())[8:]
        raw=body+security.mac(mk,body)
        response=await self.client.post(http_harness.Handler.sync_path,data=base64.b64encode(raw),headers={'Content-Type':'text/plain'})
        self.assertEqual(response.status,404)

    async def test_update_is_sent_before_broken_forwarding_and_preserves_observation(self):
        nid=self.node()
        with self.db:self.db.execute("UPDATE nodes SET installed_agent='active',state='error',err='端口冲突：TCP 10001',auto_update=1 WHERE id=?",(nid,))
        n=database.get_node(self.db,nid)
        ws=await self.client.ws_connect(http_harness.Handler.sync_path+'/v3/ws',headers={'Authorization':'Bearer '+n['key']})
        # The released 0.5.3 Agent does not include auto_update in its hello.
        await ws.send_json(dict(type='hello',protocol=3,version='0.5.3',generation=0,hash='',installation_id='a'*32))
        await ws.receive_json(timeout=3)
        message=await ws.receive_json(timeout=3)
        self.assertEqual((message['type'],message['version']),('update',settings.COMPONENTS['active_agent']))
        n=database.get_node(self.db,nid);self.assertEqual(n['agent'],'0.5.3');self.assertTrue(n['last_seen']);self.assertEqual(n['auto_update'],1)
        await ws.close()
