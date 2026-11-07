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
import transport


class V3Tests(Fixture, unittest.TestCase):
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
        self.assertEqual(database.get_node(self.db,nid)['installed_agent'],'legacy')
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
        self.assertEqual(n['installed_agent'],'legacy')
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

    def test_release_comparison_respects_rc_and_stable(self):
        self.assertTrue(agent_v3.newer('0.4.0','0.4.0-rc.1'))
        self.assertTrue(agent_v3.newer('0.4.0-rc.10','0.4.0-rc.2'))
        self.assertFalse(agent_v3.newer('0.4.0-rc.1','0.4.0'))


class TransportTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_updates_are_coalesced_and_stale_receipts_do_not_replace_inflight(self):
        nid = self.node()
        cid = self.config(self.route([nid]), [10001])
        with self.db:
            self.db.execute("UPDATE nodes SET installed_agent='active' WHERE id=?", (nid,))
        ws, session, first = await self.connect(nid)
        self.call('api_config_save', dict(ports=[10002]), cid)
        self.call('api_config_save', dict(ports=[10003]), cid)
        with self.db:
            self.db.execute('UPDATE nodes SET last_seen=1 WHERE id=?', (nid,))
        self.app['hub'].notify()
        await asyncio.sleep(.05)
        self.assertEqual(self.db.execute('SELECT op FROM sync_ops WHERE node_id=?', (nid,)).fetchone()[0], first['op'])
        self.assertEqual(database.get_node(self.db, nid)['last_seen'], 1)

        async def receipt(config):
            await ws.send_json(dict(type='apply_result', session=session, op=config['op'],
                                   generation=config['generation'], hash=config['hash'], ok=True, held_ports=[]))
            return await ws.receive_json(timeout=3)

        self.assertEqual((await receipt(first))['result'], 'changed')
        latest = await ws.receive_json(timeout=3)
        self.assertEqual(latest['generation'], database.get_node(self.db, nid)['desired_generation'])
        self.assertEqual(latest['rules'][0]['port'], 10003)
        self.assertEqual((await receipt(first))['result'], 'expired')
        self.assertEqual(self.db.execute('SELECT op FROM sync_ops WHERE node_id=?', (nid,)).fetchone()[0], latest['op'])
        self.assertEqual((await receipt(latest))['result'], 'confirmed')
        self.assertEqual((await ws.receive_json(timeout=3))['type'], 'ready')
        n = database.get_node(self.db, nid)
        self.assertEqual(n['applied_generation'], n['desired_generation'])
        await ws.close()

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
        rid = self.route([a, b])
        path = http_harness.Handler.panel_path + '/api/quality'
        payload = dict(scope='routes', id=rid)
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
        cid = self.config(rid, [10001, 10002])
        lid = self.db.execute('SELECT id FROM links WHERE chain_id=?', (cid,)).fetchone()[0]
        response = await self.client.post(path, json=dict(scope='links', id=lid, configuration=999), headers=headers)
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

    async def test_encrypted_legacy_sync_still_works(self):
        nid=self.node()
        node=database.get_node(self.db,nid)
        ek,mk=security.derive(node['key'])
        body=security.aes(ek,('t='+str(int(time.time()))+'\nstate=init\nagent=0.2.1\n').encode())[8:]
        raw=body+security.mac(mk,body)
        response=await self.client.post(http_harness.Handler.sync_path,data=base64.b64encode(raw),headers={'Content-Type':'text/plain'})
        self.assertEqual(response.status,200)
        self.assertGreater(len(await response.read()),32)
