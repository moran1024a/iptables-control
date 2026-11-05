"""Passive wake-up authorization, durable deadlines and configuration receipts."""
import secrets
import time
import threading
import types
import unittest
from test_server import Fixture, s
from test_sync import fields
import wake
from test_agent import AgentHarness


class WakeTests(Fixture, unittest.TestCase):
    def report(self, nid, **kw):
        data = dict(t=str(int(time.time())), state='ok', v='', agent='0.4.0-rc.3', au='0',
                    proto='2', source='manual', phase='begin', op=secrets.token_hex(16),
                    mode='passive', generation='0', agent_kind='passive')
        data.update(kw)
        with self.db:
            return fields(s.sync_report(self.db, s.get_node(self.db, nid), data, '192.0.2.2'))

    def ack(self, nid, operation, **kw):
        data = {k: operation[k] for k in ('op', 'generation', 'mode', 'v')}
        data.update(phase='ack', **kw)
        return self.report(nid, **data)['result']

    def setup_passive(self):
        nid = self.node()
        route = self.route([nid])
        config = self.config(route, [10001])
        self.call('api_node_edit', {'desired_mode': 'passive'}, nid)
        self.assertEqual(self.ack(nid, self.report(nid)), 'confirmed')
        with self.db:
            self.db.execute("UPDATE nodes SET installed_agent='passive' WHERE id=?", (nid,))
        return nid, config

    def enable(self, nid):
        self.call('api_node_edit', {'wake_enabled': True, 'wake_port': 39001}, nid)
        return self.db.execute('SELECT * FROM wake_jobs WHERE node_id=?', (nid,)).fetchone()

    def begin_wake(self, nid):
        token = self.db.execute('SELECT token FROM wake_jobs WHERE node_id=?', (nid,)).fetchone()[0]
        return self.report(nid, source='wake', wake_id=token)

    def test_default_off_and_explicit_port_reservation(self):
        nid, config = self.setup_passive()
        node = s.get_node(self.db, nid)
        self.assertEqual((node['wake_enabled'], node['wake_port']), (0, 0))
        with self.assertRaises(s.UserError):
            self.call('api_node_edit', {'wake_enabled': True, 'wake_port': 10001}, nid)
        self.enable(nid)
        self.call('api_node_edit', {'ports': '10000-40000'}, nid)
        with self.assertRaises(s.UserError):
            self.call('api_config_save', {'ports': [39001]}, config)
        self.call('api_node_edit', {'wake_enabled': False}, nid)
        self.assertIsNone(wake.summary(self.db, s.get_node(self.db, nid)))

    def test_three_attempts_timeout_survives_restart_new_change_rearms(self):
        nid, config = self.setup_passive()
        job = self.enable(nid)
        monitor = wake.Monitor(types.SimpleNamespace(s=s))
        start = job['started']
        with self.db:
            self.assertEqual(len(monitor.due(self.db, start)), 1)
            self.assertEqual(monitor.due(self.db, start+9), [])
            self.assertEqual(len(monitor.due(self.db, start+10)), 1)
            self.assertEqual(len(monitor.due(self.db, start+20)), 1)
            self.assertEqual(monitor.due(self.db, start+30), [])
        node = s.get_node(self.db, nid)
        self.assertEqual(wake.summary(self.db, node)['state'], 'timeout')
        self.assertEqual(s.node_status(self.db, node, start+30), 'error')
        restarted = wake.Monitor(types.SimpleNamespace(s=s))
        self.assertEqual(restarted.due(self.db, start+100), [])
        self.call('api_node_edit', {'name': 'renamed'}, nid)
        self.assertEqual(wake.summary(self.db, node)['state'], 'timeout')
        self.call('api_config_save', {'route_id': None}, config)
        self.assertEqual(wake.summary(self.db, node)['state'], 'waiting')

    def test_only_authorized_wake_can_fetch_no_mode_confirmation_or_upgrade(self):
        nid, _ = self.setup_passive()
        self.enable(nid)
        self.assertEqual(self.report(nid, source='wake', wake_id='0'*32)['action'], 'pause')
        operation = self.begin_wake(nid)
        self.assertEqual(operation['action'], 'apply')
        self.assertNotIn('update', operation)
        with self.db:
            self.db.execute('UPDATE nodes SET manual_required=1 WHERE id=?', (nid,))
        self.assertEqual(self.begin_wake(nid)['action'], 'pause')
        self.assertTrue(s.get_node(self.db, nid)['manual_required'])

    def test_late_receipt_recovers_timeout_and_marks_activity_once(self):
        nid, _ = self.setup_passive()
        job = self.enable(nid)
        operation = self.begin_wake(nid)
        with self.db:
            wake.Monitor(types.SimpleNamespace(s=s)).due(self.db, job['started']+30)
        before = self.db.execute('SELECT SUM(manual_count) FROM windows WHERE node_id=?', (nid,)).fetchone()[0]
        self.assertEqual(self.ack(nid, operation, source='wake'), 'confirmed')
        self.assertEqual(self.ack(nid, operation, source='wake'), 'confirmed')
        node = s.get_node(self.db, nid)
        self.assertEqual(s.node_status(self.db, node, int(time.time())), 'synced')
        self.assertEqual(wake.summary(self.db, node)['state'], 'confirmed')
        after = self.db.execute('SELECT SUM(manual_count) FROM windows WHERE node_id=?', (nid,)).fetchone()[0]
        self.assertEqual(after, before+1)

    def test_old_snapshot_does_not_confirm_new_config_and_failure_is_visible(self):
        nid, config = self.setup_passive()
        self.enable(nid)
        old = self.begin_wake(nid)
        self.call('api_config_save', {'route_id': None}, config)
        self.assertEqual(self.ack(nid, old, source='wake'), 'changed')
        self.assertEqual(wake.summary(self.db, s.get_node(self.db, nid))['state'], 'waiting')
        current = self.begin_wake(nid)
        self.assertEqual(self.ack(nid, current, source='wake', state='error', err='apply failed'), 'failed')
        self.assertEqual(wake.summary(self.db, s.get_node(self.db, nid))['error'], 'apply failed')
        self.assertEqual(s.node_status(self.db, s.get_node(self.db, nid), int(time.time())), 'error')
        self.assertEqual(self.ack(nid, self.report(nid)), 'confirmed')
        self.assertEqual(wake.summary(self.db, s.get_node(self.db, nid))['state'], 'confirmed')

    def test_packet_is_authenticated_and_contains_no_node_secret(self):
        key = 'ab'*32
        a = wake.packet(key, 'cd'*16, 1700000000)
        b = wake.packet(key, 'cd'*16, 1700000000)
        self.assertNotEqual(a, b)
        self.assertEqual(a.split(b'|')[:3], [b'IWK1', b'1700000000', b'cd'*16])
        self.assertNotIn(key.encode(), a)
        self.assertLess(len(a), 192)


class PassiveWakeHTTPTests(AgentHarness, Fixture, unittest.TestCase):
    source_file = 'agent/passive.sh'

    def setUp(self):
        Fixture.setUp(self)
        self.setup_agent()
        s.Handler.panel_path = s.setting(self.db, 'panel_path')
        s.Handler.sync_path = s.setting(self.db, 'sync_path')
        self.server = s.LimitedHTTPServer(('127.0.0.1', 0), s.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        Fixture.tearDown(self)

    def test_bash_config_only_sync_over_real_authenticated_http(self):
        nid = self.node()
        self.config(self.route([nid]), [10001])
        self.call('api_node_edit', {'desired_mode': 'passive'}, nid)
        key = s.get_node(self.db, nid)['key']
        (self.conf/'agent.conf').write_text(
            f'URL=http://127.0.0.1:{self.server.server_port}{s.Handler.sync_path}\nKEY={key}\nAUTO_UPDATE=1\n')
        self.assertShell('with_lock cycle manual; [ "$CONFIRM" = confirmed ]')
        with self.db:
            self.db.execute("UPDATE nodes SET installed_agent='passive' WHERE id=?", (nid,))
        self.call('api_node_edit', {'wake_enabled': True, 'wake_port': 39001}, nid)
        token = self.db.execute('SELECT token FROM wake_jobs WHERE node_id=?', (nid,)).fetchone()[0]
        (self.conf/'wake.conf').write_text('ENABLED=1\nPORT=39001\nALLOW=127.0.0.1\n')
        (self.lib/'agent-kind').write_text('passive\n')
        self.assertShell(f'''need_root() {{ :; }}
auto_update() {{ echo unexpected-upgrade >&2; return 99; }}
main --wake-sync {token}
[ "$CONFIRM" = confirmed ] && [ "$MODE" = passive ]
''')
        node = s.get_node(self.db, nid)
        self.assertEqual(wake.summary(self.db, node)['state'], 'confirmed')
        self.assertFalse((self.lib/'wake-ack').exists())
        self.assertIn('10001', (self.lib/'config').read_text())


class WakeLocalSettingsTests(unittest.TestCase):
    def test_failed_start_restores_previous_config_and_service(self):
        import os
        import subprocess
        import tempfile
        from pathlib import Path
        from test_server import ROOT
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('pkg','conf','lib','bin'):(root/name).mkdir()
            (root/'lib/agent-kind').write_text('passive\n')
            previous='ENABLED=1\nPORT=61023\nALLOW=192.0.2.1\n'
            (root/'conf/wake.conf').write_text(previous); (root/'unit').write_text('old service')
            (root/'pkg/passive.sh').write_text('CONFIG="$CASE_ROOT/missing"; check_port_conflicts() { return 0; }')
            (root/'pkg/wake').write_text('''#!/bin/bash
if [ "$1" = --make-config ]; then printf 'ENABLED=1\\nPORT=%s\\nALLOW=%s\\n' "$2" "$3"; fi
'''); (root/'pkg/wake').chmod(0o755)
            (root/'bin/systemctl').write_text('''#!/bin/bash
echo "$*" >> "$CASE_ROOT/services"
if [ "$1" = enable ] && [ "${2:-}" = --now ]; then exit 1; fi
'''); (root/'bin/systemctl').chmod(0o755)
            source=(ROOT/'agent/wake.sh').read_text()
            for path,target in (('/usr/local/lib/ictl','pkg'),('/etc/ictl','conf'),('/var/lib/ictl','lib'),('/etc/systemd/system/ictl-wake.service','unit'),('/run/ictl.lock','lock')):
                source=source.replace(path,str(root/target))
            script=root/'wake.sh'; script.write_text(source)
            code='ICTL_LIBRARY=1 source "$CASE_ROOT/wake.sh"; policy_apply() { :; }; policy_remove() { :; }; configure 61024 192.0.2.1'
            result=subprocess.run(['bash','-c',code],env={**os.environ,'CASE_ROOT':directory,'PATH':str(root/'bin')+':'+os.environ['PATH']},capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual((root/'conf/wake.conf').read_text(),previous)
            self.assertEqual((root/'unit').read_text(),'old service')
            self.assertIn('start ictl-wake',(root/'services').read_text())
            self.assertFalse(list((root/'conf').glob('wake-backup.*')))
