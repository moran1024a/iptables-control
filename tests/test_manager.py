"""Manager transactions with relocated paths and fake service/kernel tools."""
import os
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
 --apply-file) [ ! -e "$CASE_ROOT/fail-apply" ] || { echo '端口冲突：TCP 10001' >&2; exit 1; } ;;
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

    def test_install_and_both_directions(self):
        for kind in ('active','passive','active'):
            (self.root/'mode').write_text(kind)
            (self.root/'lib/config').write_text('snapshot')
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
