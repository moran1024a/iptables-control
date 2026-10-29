"""Real Bash Agent and standalone Python backend sharing fake kernel commands and locks."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from test_agent import AgentHarness
from test_server import ROOT


def standalone_module():
    source=(ROOT/'ipt.sh').read_text().split("<<'IPT_PYTHON'\n",1)[1].split('\nIPT_PYTHON',1)[0]
    module=types.ModuleType('standalone')
    exec(compile(source,'ipt.sh:backend','exec'),module.__dict__)
    return module


class CoexistTests(AgentHarness,unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.setup_agent();self.ipt=standalone_module()
        self.environment=patch.dict(os.environ,self.env);self.environment.start()
        paths={'CONF':self.root/'managed','BACKUPS':self.root/'backups','PERSIST':self.root/'ipt-rules.v4',
               'LEGACY_PERSIST':self.root/'system-rules.v4','SYSCTL':self.root/'sysctl',
               'IP_FORWARD':self.root/'ip_forward','LOCK':self.root/'ipt.lock','AGENT_LOCK':self.root/'lock',
               'AGENT_CONF':self.lib/'config','LOG':self.root/'ipt.log','UNIT_DIR':self.root/'units'}
        for name,value in paths.items():setattr(self.ipt,name,value)
        self.ipt.IP_FORWARD.write_text('1\n')
        self.ipt.scheduler=lambda *args:None
        self.ipt.scheduler_state=lambda:{}
        self.ipt.restore_scheduler=lambda state:None

    def tearDown(self):self.environment.stop();self.temp.cleanup()

    def dispatch(self,*args):
        with self.ipt.locked():return self.ipt.dispatch(list(args),str(ROOT/'ipt.sh'))

    def add(self,port=10001,proto='both',host='198.51.100.2'):
        return self.dispatch('add',str(port),host,'443',proto,'standalone')

    def row(self):return self.ipt.load_config(self.ipt.snapshot())[0]
    def kernel(self):return json.loads((self.root/'kernel.json').read_text())

    def agent_apply(self,port=10001):
        return self.assertShell(f'printf "v=abc\\n1 both {port} 192.0.2.1 443\\n" > "$CONFIG"; local_apply; [ "$R_STATE" = ok ]')

    def test_standalone_without_agent_and_protocol_specific_ports(self):
        (self.root/'lock').touch()  # A lock is not an installed Agent.
        self.add(proto='tcp');self.add(proto='udp')
        self.assertEqual(len(self.ipt.load_config(self.ipt.snapshot())),2)
        self.assertFalse((self.lib/'config').exists())
        with self.assertRaisesRegex(self.ipt.Failure,'配置冲突'):self.add(proto='tcp')

    def test_agent_without_standalone_and_both_managers_on_distinct_ports(self):
        self.agent_apply();self.add(10002)
        self.assertShell('with_lock local_apply; [ "$R_STATE" = ok ]')
        kernel=self.kernel()
        self.assertEqual(len(kernel['nat']['ICTL-PRE']),2)
        self.assertEqual(len([r for r in kernel['nat']['PREROUTING'] if 'iptfm:' in r]),2)
        self.assertEqual(kernel['nat']['OTHER'],['-j ACCEPT'])

    def test_standalone_conflict_with_stopped_agent_saved_configuration(self):
        (self.lib/'config').write_text('v=abc\n1 both 10001 192.0.2.1 443\n')
        with self.assertRaisesRegex(self.ipt.Failure,'Agent 保存的配置'):self.add()
        self.assertFalse(self.ipt.CONF.exists());self.assertFalse((self.root/'kernel.json').exists())

    def test_agent_conflict_with_standalone_saved_configuration(self):
        self.ipt.CONF.write_text('# ipt-forward-manager v3\n'+'a'*32+' 10001 example.com 443 udp single 192.0.2.5\n')
        self.assertShell("SOURCE=manual; ! with_lock apply_snapshot $'v=abc\\n1 both 10001 192.0.2.1 443' 2 active; [[ \"$R_ERR\" == '端口冲突：UDP 10001'* ]]")
        self.assertFalse((self.lib/'config').exists());self.assertFalse((self.root/'kernel.json').exists())

    def test_damaged_peer_config_fails_closed_but_local_delete_is_allowed(self):
        self.add(10002);row=self.row()
        (self.lib/'config').write_text('broken\n')
        with self.assertRaisesRegex(self.ipt.Failure,'无法校验 Agent'):self.add(10003)
        self.dispatch('delete',row['id'],self.ipt.fingerprint(row))
        self.assertEqual(self.ipt.load_config(self.ipt.snapshot()),[])
        self.ipt.CONF.write_text('broken\n')
        self.assertShell("SOURCE=manual; ! with_lock apply_snapshot $'v=abc\\n1 both 10001 192.0.2.1 443' 2 active; [[ \"$R_ERR\" == '端口检查失败：'* ]]")
        self.assertShell("SOURCE=manual; with_lock apply_snapshot 'v=e3b0c44298fc' 3 active")

    def test_listener_conflicts_and_ipv6_only_listener_is_ignored(self):
        for proto,output in [('tcp','LISTEN 0 128 0.0.0.0:10001 0.0.0.0:*\n'),('udp','UNCONN 0 0 [::]:10001 [::]:*\n')]:
            with self.subTest(proto=proto):
                file=self.root/(proto+'.sockets');file.write_text(output)
                with self.assertRaisesRegex(self.ipt.Failure,'端口冲突'):self.add()
                self.assertShell("SOURCE=manual; ! with_lock apply_snapshot $'v=abc\\n1 both 10001 192.0.2.1 443' 2 active; [[ \"$R_ERR\" == '端口冲突：'* ]]")
                file.unlink()
        (self.root/'tcp.sockets').write_text('LISTEN 0 128 [2001:db8::1]:10001 [::]:*\n')
        self.agent_apply()

    def test_orphan_nested_nat_and_multiport_rules_are_detected(self):
        kernel={'nat':{'PREROUTING':['-j EXTERNAL'],'POSTROUTING':[],'EXTERNAL':['-p tcp -m multiport --dports 10000:10005,12345 -j DNAT --to-destination 192.0.2.9:80']},'filter':{'FORWARD':[]}}
        (self.root/'kernel.json').write_text(json.dumps(kernel))
        with self.assertRaisesRegex(self.ipt.Failure,'端口冲突'):self.add()
        self.assertShell("SOURCE=manual; ! with_lock apply_snapshot $'v=abc\\n1 both 10001 192.0.2.1 443' 2 active; [[ \"$R_ERR\" == '端口冲突：'* ]]")
        self.assertEqual(kernel,self.kernel())
        self.agent_apply(10006)

    def test_ss_failure_does_not_apply_any_agent_rule(self):
        (self.root/'fail-ss').touch()
        self.assertShell("SOURCE=manual; ! with_lock apply_snapshot $'v=abc\\n1 both 10001 192.0.2.1 443' 2 active; [[ \"$R_ERR\" == '端口检查失败：'* ]]")
        self.assertFalse((self.root/'kernel.json').exists())
        with self.assertRaises(self.ipt.Failure):self.add()

    def test_passive_boot_conflict_is_local_only_and_preserves_failure(self):
        self.agent_apply();(self.lib/'mode').write_text('passive\n')
        self.ipt.CONF.write_text('10001 example.com 443 tcp single\n')
        before=self.kernel()
        self.assertShell('cycle() { exit 88; }; daemon; [[ "$R_ERR" == "端口冲突："* ]]')
        self.assertEqual(self.kernel(),before)
        self.ipt.CONF.unlink()
        self.assertShell('daemon; load_state; [[ "$R_ERR" == "端口冲突："* ]] && [ "$R_STATE" = error ]')

    def test_passive_cached_restore_failure_rolls_back_only_agent_rules(self):
        self.agent_apply();self.add(10002);(self.lib/'mode').write_text('passive\n')
        kernel=self.kernel();kernel['nat']['ICTL-PRE']=[]
        (self.root/'kernel.json').write_text(json.dumps(kernel));(self.root/'fail-filter').touch()
        self.assertShell('! with_lock local_apply_saved; [ "$R_STATE" = error ]')
        self.assertEqual(kernel,self.kernel())

    def test_restore_changes_only_standalone_rules_and_rejects_conflict(self):
        self.agent_apply();self.add(10002)
        backup=self.ipt.backup(self.ipt.snapshot())
        self.assertNotIn('ICTL-',(backup/'rules.v4').read_text())
        self.assertNotIn('OTHER',(backup/'rules.v4').read_text())
        row=self.row();self.dispatch('update',row['id'],self.ipt.fingerprint(row),'10003','198.51.100.3','8443','both','changed')
        before=self.kernel()['nat']['ICTL-PRE']
        self.dispatch('restore',backup.name)
        self.assertEqual(self.row()['incoming'],'10002');self.assertEqual(self.kernel()['nat']['ICTL-PRE'],before)
        self.assertNotIn('ICTL-',self.ipt.PERSIST.read_text())
        # An old full-firewall backup is parsed, but its foreign rules are never restored.
        full=self.ipt.snapshot()['text'];(backup/'rules.v4').write_text(full.replace('192.0.2.1','203.0.113.1'))
        self.dispatch('restore',backup.name)
        self.assertEqual(self.kernel()['nat']['ICTL-PRE'],before)
        (self.lib/'config').write_text('v=abc\n1 both 10002 192.0.2.1 443\n')
        kernel=self.kernel()
        with self.assertRaisesRegex(self.ipt.Failure,'端口冲突'):self.dispatch('restore',backup.name)
        self.assertEqual(kernel,self.kernel())

    def test_failed_ddns_change_rolls_back_owned_rules_and_preserves_agent(self):
        self.agent_apply();self.add(10002)
        row=self.row();row['host']='example.com';self.ipt.CONF.write_text(self.ipt.serialize([row]))
        before=self.kernel();config=self.ipt.CONF.read_bytes()
        original=self.ipt.command
        def command(args,*a,**kw):
            if args[:1]==['iptables'] and '-A' in args and 'PREROUTING' in args and '192.0.2.50:443' in args:
                raise self.ipt.Failure('simulated apply failure')
            return original(args,*a,**kw)
        def resolve(rows):
            for r in rows:r['ip']='192.0.2.50'
        with patch.object(self.ipt,'command',side_effect=command),patch.object(self.ipt,'resolve',side_effect=resolve):
            with self.assertRaisesRegex(self.ipt.Failure,'已撤销'):self.dispatch('refresh')
        self.assertEqual(self.kernel(),before);self.assertEqual(self.ipt.CONF.read_bytes(),config)

    def test_legacy_global_persistence_is_migrated_by_each_owner(self):
        self.agent_apply();self.add(10002)
        full=self.ipt.snapshot()['text'];self.ipt.LEGACY_PERSIST.write_text(full)
        row=self.row();self.dispatch('update',row['id'],self.ipt.fingerprint(row),'10002','198.51.100.3','443','both','changed')
        text=self.ipt.LEGACY_PERSIST.read_text()
        self.assertNotIn('iptfm:',text);self.assertIn('ICTL-PRE',text);self.assertIn('OTHER',text)
        before=self.kernel()
        self.assertShell('with_lock clean_legacy_persistence')
        text=self.ipt.LEGACY_PERSIST.read_text()
        self.assertNotIn('ICTL-',text);self.assertIn('OTHER',text)
        self.assertEqual(before,self.kernel());self.assertTrue((self.lib/'pre-coexist-rules.v4.bak').exists())

    def test_shared_lock_makes_second_writer_check_first_committed_config(self):
        code='source "$1"\ncommit() { touch "$MOCK_ROOT/locked"; sleep 0.3; printf "v=abc\\n1 both 10001 192.0.2.1 443\\n" > "$CONFIG"; }; with_lock commit'
        proc=subprocess.Popen(['bash','-c',code,'test',str(self.script)],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not (self.root/'locked').exists():
                self.assertLess(time.monotonic(),deadline);time.sleep(0.01)
            with self.assertRaisesRegex(self.ipt.Failure,'Agent 保存的配置'):self.add()
            self.assertFalse(self.ipt.CONF.exists())
        finally:
            out,err=proc.communicate(timeout=5)
        self.assertEqual(proc.returncode,0,out+err)

    def test_agent_uninstall_keeps_standalone_and_removes_its_legacy_boot_entries(self):
        self.agent_apply();self.add(10002)
        self.ipt.LEGACY_PERSIST.write_text(self.ipt.snapshot()['text'])
        config=self.ipt.CONF.read_bytes()
        self.assertShell('with_lock uninstall_files')
        kernel=self.kernel()
        self.assertNotIn('ICTL-PRE',kernel['nat']);self.assertIn('OTHER',kernel['nat'])
        self.assertEqual(len([r for r in kernel['nat']['PREROUTING'] if 'iptfm:' in r]),2)
        self.assertEqual(config,self.ipt.CONF.read_bytes())
        self.assertNotIn('ICTL-',self.ipt.LEGACY_PERSIST.read_text())
        self.assertIn('iptfm:',self.ipt.LEGACY_PERSIST.read_text())

    def test_persistence_cleanup_ignores_chain_names_inside_foreign_comments(self):
        text='*nat\n:PREROUTING ACCEPT [0:0]\n:ICTL-PRE - [0:0]\n-A PREROUTING -j ICTL-PRE\n-A PREROUTING -m comment --comment "keep -j ICTL-PRE" -j ACCEPT\nCOMMIT\n'
        self.ipt.LEGACY_PERSIST.write_text(text)
        self.assertShell('with_lock clean_legacy_persistence')
        saved=self.ipt.LEGACY_PERSIST.read_text()
        self.assertIn('--comment "keep -j ICTL-PRE" -j ACCEPT',saved)
        self.assertNotIn(':ICTL-PRE',saved);self.assertNotIn('-A PREROUTING -j ICTL-PRE',saved)
        inode=self.ipt.LEGACY_PERSIST.stat().st_ino
        self.assertShell('with_lock clean_legacy_persistence')
        self.assertEqual(self.ipt.LEGACY_PERSIST.stat().st_ino,inode)
