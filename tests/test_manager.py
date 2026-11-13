"""Manager transactions with relocated paths and fake service/kernel tools."""
import os
import fcntl
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from test_server import ROOT


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        for name in ('pkg','lib','bin','etc','run','root'):(self.root/name).mkdir()
        paths={'/usr/local/lib/ictl':'pkg','/usr/local/bin/ictl':'bin/ictl','/var/lib/ictl':'lib',
               '/etc/ictl':'etc','/etc/systemd/system/ictl-agent.service':'unit','/etc/sysctl.d/99-ictl.conf':'sysctl',
               '/root/ictl.sh':'root/ictl.sh','/ictl.sh':'entry.sh','/run/ictl':'run/ictl'}
        source=re.sub('|'.join(re.escape(p) for p in sorted(paths,key=len,reverse=True)),lambda m:str(self.root/paths[m[0]]),(ROOT/'agent/manage.sh').read_text())
        (self.root/'manager').write_text(source)
        target=self.root/'target'
        target.write_text('''#!/bin/bash
echo "$*" >> "$CASE_ROOT/backend.log"
case "$1" in
 --version) echo 0.4.0-rc.1 ;;
 --held-ports) echo '[]' ;;
 --generation-only) echo 1 ;;
 --flush-traffic) [ ! -e "$CASE_ROOT/fail-flush" ] ;;
 --apply-file)
  [ ! -e "$CASE_ROOT/fail-apply" ] || { echo '端口冲突：TCP 10001' >&2; exit 1; }
  cp "$2" "$CASE_ROOT/lib/config" ;;
esac
''');target.chmod(0o755)
        for name in ('systemctl','nft','ss','conntrack','iptables','iptables-save','iptables-restore'):
            p=self.root/'bin'/name;p.write_text('#!/bin/bash\necho "$*" >> "$CASE_ROOT/tools.log"\n');p.chmod(0o755)
        (self.root/'mode').write_text('passive')
        self.env={**os.environ,'CASE_ROOT':str(self.root),'PATH':str(self.root/'bin')+':'+os.environ['PATH']}
        self.setup='''ICTL_LIBRARY=1 source "$CASE_ROOT/manager"
URL=https://example.invalid/sync
ARCH=amd64
rpc() {
 case "$1" in
  info) REPLY="$(printf 'proto=2\\nmode=%s\\n' "$(cat "$CASE_ROOT/mode")")" ;;
  prepare)
   [ ! -e "$CASE_ROOT/fail-prepare" ] || return 1
   REPLY=$'proto=2\\nop=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\ngeneration=1\\nv=e3b0c44298fc\\nartifact=target\\nversion=0.4.0-rc.1\\nconfig_b64=dj1lM2IwYzQ0Mjk4ZmMK' ;;
  commit)
   echo commit >> "$CASE_ROOT/backend.log"
   [ ! -e "$CASE_ROOT/fail-commit" ] || return 1
   REPLY=$'proto=2\\nresult=confirmed\\n' ;;
  failed) echo "$2" > "$CASE_ROOT/reported-error" ;;
  abort) echo abort >> "$CASE_ROOT/backend.log" ;;
 esac
}
fetch_component() { if [ "$1" = manage.sh ]; then cp "$CASE_ROOT/manager" "$3"; else cp "$CASE_ROOT/target" "$3"; fi; }
'''

    def tearDown(self):self.temp.cleanup()

    def run_manager(self,code='perform_sync'):
        return subprocess.run(['bash','-c',self.setup+'\n'+code],env=self.env,text=True,capture_output=True,timeout=10)

    def existing(self):
        (self.root/'lib/agent-kind').write_text('active')
        (self.root/'lib/config').write_text('old config')
        (self.root/'unit').write_text('old unit')
        p=self.root/'pkg/active';p.write_bytes((self.root/'target').read_bytes());p.chmod(0o755)
        (self.root/'lib/active').mkdir();(self.root/'lib/active/enabled').write_text('confirmed')

    def test_proxy_menu_reconnects_only_after_success_and_denies_passive(self):
        self.existing()
        client = self.root/'pkg/active'
        client.write_text('''#!/bin/bash
echo "$*" >> "$CASE_ROOT/proxy.log"
case "$2" in
 set) cat > "$CASE_ROOT/proxy-input"; [ ! -f "$CASE_ROOT/fail-proxy" ] ;;
esac
''')
        for choice, action in (('1\nss://private-key@proxy:443\n', 'set'), ('2\n', 'enable'), ('3\n', 'disable')):
            result = subprocess.run(['bash','-c',self.setup+'\nproxy_settings'],env=self.env,
                                    input=choice,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--proxy-action '+action,(self.root/'proxy.log').read_text())
        self.assertNotIn('private-key',(self.root/'proxy.log').read_text())
        self.assertEqual((self.root/'proxy-input').read_text(),'ss://private-key@proxy:443')
        log = (self.root/'tools.log').read_text()
        self.assertEqual(log.count('kill --kill-who=main --signal=HUP ictl-agent'),3)
        self.assertNotIn('restart', log)
        (self.root/'fail-proxy').touch()
        result = subprocess.run(['bash','-c',self.setup+'\nproxy_settings'],env=self.env,
                                input='1\nss://invalid\n',text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(log,(self.root/'tools.log').read_text())
        (self.root/'lib/agent-kind').write_text('passive')
        result = self.run_manager('proxy_settings')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('只有主动 Agent',result.stderr)

    def test_proxy_transport_never_falls_back_and_survives_mode_handover(self):
        self.existing()
        (self.root/'etc/proxy.json').write_text('{"enabled":true}')
        client = self.root/'pkg/active'
        client.write_text('#!/bin/bash\necho proxy >> "$CASE_ROOT/requests"\nexit 7\n')
        curl = self.root/'bin/curl'
        curl.write_text('#!/bin/bash\necho direct >> "$CASE_ROOT/requests"\n'); curl.chmod(0o755)
        (self.root/'request').write_text('encrypted request')
        code = 'control_post "$CASE_ROOT/request" "$CASE_ROOT/response" 5'
        result = self.run_manager(code)
        self.assertEqual(result.returncode,7,result.stderr)
        self.assertEqual((self.root/'requests').read_text(),'proxy\n')
        client.rename(self.root/'pkg/control-client')
        (self.root/'lib/agent-kind').write_text('passive')
        result = self.run_manager('FROM=active; '+code)
        self.assertEqual(result.returncode,7,result.stderr)
        self.assertEqual((self.root/'requests').read_text(),'proxy\nproxy\n')
        result = self.run_manager(code)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'requests').read_text(),'proxy\nproxy\ndirect\n')

    def test_new_manager_ignores_old_control_transport(self):
        # An old updater replaces manage.sh but leaves its old control.sh behind.
        result = self.run_manager('''
source "''' + str(ROOT/'agent/control.sh') + '''"
send_request() { echo old-direct >> "$CASE_ROOT/requests"; }
control_post() { echo explicit-transport >> "$CASE_ROOT/requests"; printf 503; }
KEY=''' + 'ab'*32 + '''
derive_keys
printf 'info' | control_request "$CASE_ROOT/response"
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'requests').read_text(),'explicit-transport\n')

    def test_switch_to_passive_keeps_final_request_client_and_disables_proxy(self):
        self.existing()
        (self.root/'etc/proxy.json').write_text('{"enabled":true}')
        result = self.run_manager()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--proxy-action disable',(self.root/'backend.log').read_text())
        self.assertTrue((self.root/'pkg/control-client').exists())
        self.assertFalse((self.root/'pkg/active').exists())

    def test_proxy_bootstrap_uploads_old_agent_statistics_without_direct_connection(self):
        self.existing()
        (self.root/'mode').write_text('active')
        (self.root/'etc/proxy.json').write_text('{"enabled":true}')
        old = self.root/'pkg/active'
        old.write_text(old.read_text().replace('--flush-traffic) [ ! -e "$CASE_ROOT/fail-flush" ]',
                                               '--flush-traffic) echo old-direct >&2; exit 88'))
        client = self.root/'pkg/control-client'
        client.write_text('#!/bin/bash\necho "bootstrap $*" >> "$CASE_ROOT/backend.log"\n')
        client.chmod(0o755)
        self.env.update(ICTL_CONTROL_CLIENT=str(client), ICTL_ACTIVE_PROXY='1')
        result = self.run_manager()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('old-direct',result.stderr)
        self.assertEqual((self.root/'backend.log').read_text().count('bootstrap --flush-traffic'),2)

    def test_061_statistics_workaround_is_local_and_pending_receipt_can_complete(self):
        self.existing()
        (self.root/'mode').write_text('active')
        (self.root/'fail-commit').touch()
        self.assertNotEqual(self.run_manager().returncode,0)
        old = self.root/'pkg/active'
        old.write_text(old.read_text().replace('echo 0.4.0-rc.1', 'echo 0.6.1').replace(
            '--flush-traffic) [ ! -e "$CASE_ROOT/fail-flush" ]',
            '--flush-traffic) [ "${GODEBUG:-}" = http2debug=0,http2client=0 ] || exit 88'))
        # Retry the statistics upload as well as the receipt, as on the failed node.
        (self.root/'lib/switch-work/traffic-receipt').unlink()
        (self.root/'fail-commit').unlink()
        self.env['GODEBUG']='http2debug=0'
        result=self.run_manager('perform_sync && [ "$GODEBUG" = http2debug=0 ]')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((self.root/'lib/switch-work/transaction').exists())
        self.assertTrue((self.root/'lib/active/enabled').exists())
        self.assertEqual((self.root/'backend.log').read_text().count('--apply-file'),1)

    def test_new_binary_upload_does_not_receive_legacy_workaround(self):
        self.existing()
        client=self.root/'pkg/active'
        client.write_text('''#!/bin/bash
case "$1" in
 --version) echo 0.6.2 ;;
 --flush-traffic) [ "${GODEBUG:-}" = http2debug=0 ] ;;
esac
''')
        self.env['GODEBUG']='http2debug=0'
        result=self.run_manager('flush_traffic')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_idle_menu_and_status_do_not_lock_sync_but_actions_do(self):
        (self.root/'pkg/control.sh').write_text('derive_keys() { :; }\n')
        (self.root/'etc/agent.conf').write_text('URL=https://test.invalid/sync\nKEY='+'ab'*32+'\nAUTO_UPDATE=1\n')
        (self.root/'bin/id').write_text('#!/bin/sh\necho 0\n')
        (self.root/'bin/id').chmod(0o755)
        process = subprocess.Popen(['bash',str(self.root/'manager')], env=self.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            while True:
                line = process.stdout.readline()
                self.assertTrue(line, 'menu exited before displaying choices')
                if line.startswith('0)'):
                    break
            with (self.root/'run/ictl-manager.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                status = subprocess.run(['bash',str(self.root/'manager'),'--status'],env=self.env,text=True,capture_output=True,timeout=5)
                self.assertEqual(status.returncode,0,status.stderr)
                _, error = process.communicate('1\n',timeout=5)
                self.assertNotEqual(process.returncode,0)
                self.assertIn('另一项安装或同步正在执行',error)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_unchanged_passive_sync_skips_install_and_service_restart(self):
        import hashlib
        helper = self.root/'pkg/passive.sh'
        helper.write_text('#!/bin/bash\necho "$*" >> "$CASE_ROOT/backend.log"\n')
        manager = self.root/'bin/ictl'
        manager.write_bytes((self.root/'manager').read_bytes())
        (self.root/'lib/agent-kind').write_text('passive')
        (self.root/'lib/mode').write_text('passive')
        (self.root/'unit').write_text(f'ExecStart={helper} --daemon\n')
        (self.root/'lib/installation-id').write_text('stable-id')
        helper_hash = hashlib.sha256(helper.read_bytes()).hexdigest()
        manager_hash = hashlib.sha256(manager.read_bytes()).hexdigest()
        code = f"""sha256_hex() {{ sha256sum | cut -d' ' -f1; }}
rpc() {{
 [ "$1" = info ] || return 99
 REPLY=$'proto=2\\nmode=passive\\ninstalled=passive\\ninstallation_id=stable-id\\npassive_sha256={helper_hash}\\nmanager_sha256={manager_hash}\\n'
}}
perform_sync
"""
        result = self.run_manager(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root/'backend.log').read_text().strip(), '--sync')
        self.assertFalse((self.root/'tools.log').exists())
        self.assertFalse((self.root/'lib/switch-work/transaction').exists())

    def test_install_and_both_directions(self):
        for kind in ('active','passive','active'):
            (self.root/'mode').write_text(kind)
            r=self.run_manager();self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            self.assertEqual((self.root/'lib/agent-kind').read_text().strip(),kind)
            self.assertFalse((self.root/'lib/switch-work/transaction').exists())
            self.assertTrue((self.root/'pkg'/('active' if kind=='active' else 'passive.sh')).exists())
            self.assertFalse((self.root/'pkg'/('passive.sh' if kind=='active' else 'active')).exists())

    def test_failed_prepare_leaves_old_service(self):
        self.existing();(self.root/'fail-prepare').touch()
        self.assertNotEqual(self.run_manager().returncode,0)
        self.assertFalse((self.root/'tools.log').exists())
        self.assertEqual((self.root/'lib/config').read_text(),'old config')

    def test_early_update_failure_restarts_service_and_allows_retry(self):
        self.existing(); (self.root/'mode').write_text('active')
        (self.root/'fail-flush').touch()
        self.assertNotEqual(self.run_manager('UPDATE_ONLY=1 perform_sync').returncode, 0)
        self.assertIn('start ictl-agent', (self.root/'tools.log').read_text())
        self.assertIn('abort', (self.root/'backend.log').read_text())
        self.assertFalse((self.root/'lib/switch-work/transaction').exists())
        (self.root/'fail-flush').unlink()
        result = self.run_manager('UPDATE_ONLY=1 perform_sync')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_download_failure_releases_server_operation(self):
        self.existing(); (self.root/'mode').write_text('active')
        result = self.run_manager('fetch_component() { return 1; }; perform_sync')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('abort', (self.root/'backend.log').read_text())
        self.assertFalse((self.root/'tools.log').exists())

    def test_credentials_validated_before_save(self):
        old = 'URL=https://old.invalid/sync\nKEY=' + 'ab'*32 + '\nAUTO_UPDATE=1\n'
        conf = self.root/'etc/agent.conf'; conf.write_text(old)
        code = "derive_keys() { :; }; rpc() { return 1; }; credentials"
        result = subprocess.run(['bash', '-c', self.setup + '\n' + code], env=self.env,
                                input='https://new.invalid/sync\n'+'cd'*32+'\n', text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0); self.assertEqual(conf.read_text(), old)
        code = "derive_keys() { :; }; perform_sync() { echo sync >> \"$CASE_ROOT/backend.log\"; }; credentials"
        result = subprocess.run(['bash', '-c', self.setup + '\n' + code], env=self.env,
                                input='https://new.invalid/sync\n'+'cd'*32+'\n', text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('KEY='+'cd'*32, conf.read_text()); self.assertIn('sync', (self.root/'backend.log').read_text())

    def test_lost_receipt_does_not_enable_active_before_retry(self):
        (self.root/'mode').write_text('active');(self.root/'fail-commit').touch()
        self.assertNotEqual(self.run_manager().returncode,0)
        self.assertIn('PHASE=installed',(self.root/'lib/switch-work/transaction').read_text())
        self.assertFalse((self.root/'lib/active/enabled').exists())
        (self.root/'fail-commit').unlink()
        r=self.run_manager();self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual((self.root/'backend.log').read_text().count('--apply-file'),1)
        self.assertIn('--restore',(self.root/'backend.log').read_text())
        self.assertEqual((self.root/'backend.log').read_text().count('--flush-traffic'),1)
        self.assertLess((self.root/'backend.log').read_text().index('--flush-traffic'),
                        (self.root/'backend.log').read_text().index('commit'))
        self.assertTrue((self.root/'lib/active/enabled').exists())

    def scheduler(self):
        p=self.root/'bin/systemd-run';p.write_text('#!/bin/bash\necho "systemd-run $*" >> "$CASE_ROOT/tools.log"\n');p.chmod(0o755)

    def test_auto_update_retries_a_lost_receipt_until_confirmed(self):
        self.existing();self.scheduler();(self.root/'mode').write_text('active');(self.root/'fail-commit').touch()
        journal=self.root/'lib/switch-work/transaction'
        self.assertNotEqual(self.run_manager('auto_update').returncode,0)
        self.assertIn('PHASE=installed',journal.read_text());self.assertIn('FROM=active',journal.read_text())
        self.assertIn('--on-active=300',(self.root/'tools.log').read_text())
        self.assertIn('--auto-update',(self.root/'tools.log').read_text())
        # The agent is stopped, so the scheduled run is the only retry: it must not be skipped.
        self.assertNotEqual(self.run_manager('auto_update').returncode,0)
        self.assertEqual((self.root/'backend.log').read_text().splitlines().count('commit'),2)
        self.assertEqual((self.root/'tools.log').read_text().count('--on-active=300'),2)
        (self.root/'fail-commit').unlink()
        r=self.run_manager('auto_update');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse(journal.exists());self.assertTrue((self.root/'lib/active/enabled').exists())
        self.assertIn('start ictl-agent',(self.root/'tools.log').read_text())
        self.assertEqual((self.root/'backend.log').read_text().count('--apply-file'),1)

    def test_auto_update_never_confirms_a_pending_mode_switch(self):
        self.scheduler();(self.root/'mode').write_text('active');(self.root/'fail-commit').touch()
        self.assertNotEqual(self.run_manager().returncode,0)
        (self.root/'fail-commit').unlink()
        previous=(self.root/'backend.log').read_text()
        self.assertEqual(self.run_manager('auto_update').returncode,0)
        self.assertEqual((self.root/'backend.log').read_text(),previous)
        self.assertIn('PHASE=installed',(self.root/'lib/switch-work/transaction').read_text())

    def test_failed_switch_to_passive_before_handover_is_reported(self):
        self.existing();(self.root/'fail-flush').touch()
        r=self.run_manager();self.assertNotEqual(r.returncode,0)
        self.assertIn('切换未完成',r.stderr)
        self.assertFalse((self.root/'lib/switch-work/transaction').exists())

    def test_final_traffic_failure_prevents_commit(self):
        (self.root/'mode').write_text('active');(self.root/'fail-flush').touch()
        self.assertNotEqual(self.run_manager().returncode,0)
        self.assertNotIn('commit',(self.root/'backend.log').read_text())
        self.assertIn('PHASE=installed',(self.root/'lib/switch-work/transaction').read_text())
        (self.root/'fail-flush').unlink()
        self.assertEqual(self.run_manager().returncode,0)

    def test_update_cannot_confirm_pending_switch(self):
        (self.root/'mode').write_text('active');(self.root/'fail-commit').touch()
        self.assertNotEqual(self.run_manager().returncode,0)
        previous=(self.root/'backend.log').read_text()
        r=self.run_manager('UPDATE_ONLY=1 perform_sync')
        self.assertNotEqual(r.returncode,0);self.assertIn('--sync',r.stderr)
        self.assertEqual((self.root/'backend.log').read_text(),previous)

    def test_failed_handover_restores_old_kind_and_reports_conflict(self):
        self.existing();(self.root/'fail-apply').touch()
        r=self.run_manager();self.assertNotEqual(r.returncode,0)
        self.assertEqual((self.root/'lib/agent-kind').read_text(),'active')
        self.assertEqual((self.root/'lib/config').read_text(),'old config')
        self.assertTrue((self.root/'lib/paused').exists())
        self.assertIn('--restore',(self.root/'backend.log').read_text())
        self.assertIn('端口冲突',(self.root/'reported-error').read_text())

    def test_update_cannot_change_type(self):
        self.existing()
        r=self.run_manager('UPDATE_ONLY=1 perform_sync')
        self.assertNotEqual(r.returncode,0);self.assertIn('更新不能转换',r.stderr)
        self.assertFalse((self.root/'tools.log').exists())

    def test_failed_active_retry_repairs_before_replacing_journal_and_backup(self):
        (self.root/'mode').write_text('active')
        work=self.root/'lib/switch-work';work.mkdir()
        journal='OP='+'a'*32+'\nTARGET=active\nGEN=7\nHASH=e3b0c44298fc\nNEW_VERSION=0.4.0\nPHASE=failed\nFROM=none\n'
        (work/'transaction').write_text(journal)
        (work/'backup.tar').write_bytes(b'old backup')
        target=self.root/'target'
        target.write_text(target.read_text().replace(' --version)', ''' --recover-empty-table)
  grep -qx 'PHASE=failed' "$CASE_ROOT/lib/switch-work/transaction" || exit 2
  [ "$(cat "$CASE_ROOT/lib/switch-work/backup.tar")" = 'old backup' ] || exit 3
  [ ! -e "$CASE_ROOT/fail-recover" ] || exit 4 ;;
 --version)'''))
        (self.root/'fail-recover').touch()
        r=self.run_manager();self.assertNotEqual(r.returncode,0)
        self.assertEqual((work/'transaction').read_text(),journal)
        self.assertEqual((work/'backup.tar').read_bytes(),b'old backup')
        self.assertFalse((self.root/'tools.log').exists())
        (self.root/'fail-recover').unlink()
        r=self.run_manager();self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse((work/'transaction').exists())
        self.assertTrue((self.root/'lib/active/enabled').exists())

    def test_backup_includes_installation_identity(self):
        import tarfile
        self.existing()
        identity=self.root/'lib/installation-id';identity.write_text('a'*32)
        (self.root/'mode').write_text('active')
        r=self.run_manager();self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        with tarfile.open(self.root/'lib/rollback/previous.tar') as archive:
            saved=archive.extractfile(str(identity).lstrip('/')).read().decode()
        self.assertEqual(saved,'a'*32)
