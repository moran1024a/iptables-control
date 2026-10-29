"""Run the real Bash Agent against fake kernel/systemd commands and local encrypted HTTP."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import test_server
from test_server import ROOT, s

FAKE_IPTABLES = r'''#!/usr/bin/env python3
import sys,os,json,pathlib
root=pathlib.Path(os.environ['MOCK_ROOT']);p=root/'kernel.json'
x=json.loads(p.read_text()) if p.exists() else {'nat':{'PREROUTING':[],'POSTROUTING':[],'OTHER':['-j ACCEPT']},'filter':{'FORWARD':[],'OTHER':['-j ACCEPT']}}
args=sys.argv[1:];name=pathlib.Path(sys.argv[0]).name
with (root/'commands').open('a') as log:log.write(name+' '+' '.join(args)+'\n')
def save():p.write_text(json.dumps(x))
if name=='iptables-save':
 t=args[args.index('-t')+1]
 print('*'+t)
 for chain,rules in x[t].items():print(':'+chain+' - [0:0]')
 for chain,rules in x[t].items():
  for rule in rules:print('-A '+chain+' '+rule)
 print('COMMIT');sys.exit(0)
if name=='iptables-restore':
 current=None
 for line in sys.stdin.read().splitlines():
  if line.startswith('*'):current=line[1:]
  elif line.startswith(':'):x[current][line.split()[0][1:]]=[]
  elif line.startswith('-A '):
   _,chain,rule=line.split(' ',2);x[current][chain].append(rule)
  elif line=='COMMIT':
   marker=root/'fail-filter'
   if current=='filter' and marker.exists():marker.unlink();sys.exit(1)
   save()
 sys.exit(0)
args=[a for a in args if a!='-w'];t='filter'
if '-t' in args:i=args.index('-t');t=args[i+1];del args[i:i+2]
op,chain=args[:2] if len(args)>1 else (args[0],None)
if op=='-S':
 for c,rules in x[t].items():
  print('-N '+c)
  for rule in rules:print('-A '+c+' '+rule)
 sys.exit(0)
rest=args[2:]
if op=='-I' and rest and rest[0].isdigit():rest=rest[1:]
rule=' '.join(rest)
if op=='-C':sys.exit(0 if rule in x[t].get(chain,[]) else 1)
if op=='-A':x[t].setdefault(chain,[]).append(rule)
elif op=='-I':x[t].setdefault(chain,[]).insert(0,rule)
elif op=='-D':
 if rule not in x[t].get(chain,[]):sys.exit(1)
 x[t][chain].remove(rule)
elif op=='-F':x[t][chain]=[]
elif op=='-X':x[t].pop(chain,None)
else:sys.exit(1)
save()
'''


class AgentHarness:
    def setup_agent(self):
        self.root=Path(self.temp.name);self.bin=self.root/'bin';self.bin.mkdir()
        fake=self.bin/'iptables';fake.write_text(FAKE_IPTABLES);fake.chmod(0o755)
        for name in ('iptables-save','iptables-restore'):(self.bin/name).symlink_to(fake)
        systemctl=self.bin/'systemctl';systemctl.write_text('''#!/bin/bash
printf 'systemctl %s\n' "$*" >> "$MOCK_ROOT/commands"
[ ! -f "$MOCK_ROOT/fail-service" ]
''');systemctl.chmod(0o755)
        self.env={**os.environ,'PATH':str(self.bin)+':'+os.environ['PATH'],'MOCK_ROOT':str(self.root)}
        source=(ROOT/'agent/ictl.sh').read_text().rsplit('case "${1:-}" in',1)[0]
        source=source.replace('/run/ictl.',str(self.root)+'/request.')
        self.lib=self.root/'lib';self.lib.mkdir()
        self.conf=self.root/'conf';self.conf.mkdir()
        source+='\n'+f'''LIB={shlex.quote(str(self.lib))}
CONF_DIR={shlex.quote(str(self.conf))}
CONF="$CONF_DIR/agent.conf"
CONFIG="$LIB/config"; APPLIED="$LIB/applied"; RESOLVED="$LIB/resolved"; STATE="$LIB/state"
KERNEL="$LIB/kernel"; MODE_FILE="$LIB/mode"; ACK_FILE="$LIB/ack"
LOCK={shlex.quote(str(self.root/'lock'))}
SERVICE={shlex.quote(str(self.root/'service'))}
BIN={shlex.quote(str(self.root/'ictl'))}
ensure_ip_forward() {{ :; }}
'''
        self.script=self.root/'agent-lib.sh';self.script.write_text(source)

    def shell(self,code):
        result=subprocess.run(['bash','-c','source "$1"\n'+code,'test',str(self.script)],env=self.env,capture_output=True,text=True,timeout=40)
        return result

    def assertShell(self,code):
        r=self.shell(code);self.assertEqual(r.returncode,0,r.stdout+r.stderr);return r


class AgentTests(AgentHarness,unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.setup_agent()
    def tearDown(self):self.temp.cleanup()

    def setup_rules(self):
        self.assertShell('printf "v=abc\\n1 both 10001 192.0.2.1 443\\n" > "$CONFIG"\nlocal_apply\n[ "$R_STATE" = ok ]')

    def test_repairs_flushed_chains_even_when_jumps_and_cache_match(self):
        self.setup_rules();p=self.root/'kernel.json';x=json.loads(p.read_text());x['nat']['ICTL-PRE']=[];p.write_text(json.dumps(x))
        self.assertShell('local_apply; [ "$R_STATE" = ok ]')
        x=json.loads(p.read_text());self.assertEqual(len(x['nat']['ICTL-PRE']),2)
        self.assertEqual(x['nat']['OTHER'],['-j ACCEPT']);self.assertEqual(x['filter']['OTHER'],['-j ACCEPT'])

    def test_partial_table_failure_restores_only_owned_rules_and_local_config(self):
        self.setup_rules();before=json.loads((self.root/'kernel.json').read_text());old=(self.lib/'config').read_text()
        (self.root/'fail-filter').touch()
        self.assertShell("SOURCE=manual; load_state; load_mode; ! apply_snapshot $'v=def\\n1 both 10001 192.0.2.2 8443' 9 passive\n[ \"$R_STATE\" = error ]")
        self.assertEqual(json.loads((self.root/'kernel.json').read_text()),before)
        self.assertEqual((self.lib/'config').read_text(),old)
        self.assertFalse((self.lib/'mode').exists())

    def test_passive_boot_does_not_resolve_or_contact_server(self):
        self.setup_rules();(self.lib/'mode').write_text('passive\n')
        self.assertShell('getent() { exit 77; }; cycle() { exit 78; }; auto_update() { exit 79; }; daemon')
        self.assertEqual((self.lib/'mode').read_text(),'passive\n')

    def test_migrates_legacy_restart_policy_before_passive_exit(self):
        (self.root/'service').write_text('[Service]\nRestart=always\n');(self.lib/'mode').write_text('passive\n')
        self.assertShell('daemon')
        self.assertIn('Restart=on-failure',(self.root/'service').read_text())
        self.assertIn('daemon-reload',(self.root/'commands').read_text())

    def test_mode_survives_configuration_save(self):
        (self.lib/'mode').write_text('passive\n')
        self.assertShell('URL=https://example.com/s; KEY=abcd; save_conf; load_mode; [ "$MODE" = passive ]')

    def test_failed_service_reload_is_retried_after_file_migration(self):
        (self.root/'service').write_text('[Service]\nRestart=always\n')
        (self.root/'fail-service').touch()
        self.assertShell('! ensure_service_policy')
        (self.root/'fail-service').unlink()
        self.assertShell('ensure_service_policy')
        self.assertEqual((self.root/'commands').read_text().count('daemon-reload'),2)

    def test_active_boot_can_fetch_replacement_when_cached_dns_fails(self):
        self.assertShell('local_apply_saved() { return 1; }; cycle() { MODE=passive; save_mode; }; daemon')
        self.assertEqual((self.lib/'mode').read_text(),'passive\n')


class AgentHTTPTests(AgentHarness,test_server.HTTPTests):
    test_forward_sync_keeps_protocol_and_ack_gate=None
    test_auth_body_limits_and_removed_endpoints=None
    test_deleted_node_receipt_can_be_retried_without_config_access=None
    test_idle_partial_headers_timeout_without_db_lock=None
    test_connection_cap_rejects_and_releases_slots=None

    def setUp(self):super().setUp();self.setup_agent()

    def enroll(self):
        n=self.node();r=self.route([n]);c=self.config(r,[10001]);key=s.get_node(self.db,n)['key']
        (self.conf/'agent.conf').write_text(f'URL=http://127.0.0.1:{self.port}{s.Handler.sync_path}\nKEY={key}\nAUTO_UPDATE=0\n')
        self.assertShell('with_lock cycle manual; rc=$?; print_net_result; exit "$rc"')
        return n,r,c

    def test_real_bash_encryption_two_request_sync_and_modes(self):
        n,r,c=self.enroll();self.assertFalse(self.db.execute('SELECT 1 FROM chain_pending').fetchone())
        self.call('api_node_edit',{'desired_mode':'passive'},n)
        self.assertShell('with_lock cycle auto; [ "$MODE" = passive ] && [ "$CONFIRM" = waiting ]')
        self.assertTrue(s.get_node(self.db,n)['manual_required'])
        self.assertShell('with_lock cycle manual; [ "$CONFIRM" = confirmed ]')
        self.assertFalse(s.get_node(self.db,n)['manual_required'])
        self.assertEqual((self.lib/'mode').read_text(),'passive\n')
        self.call('api_node_edit',{'desired_mode':'active'},n)
        (self.root/'service').write_text('[Service]\nRestart=always\n')
        self.assertShell('sync_now')
        self.assertEqual((self.lib/'mode').read_text(),'active\n')
        self.assertFalse(s.get_node(self.db,n)['manual_required'])

    def test_lost_ack_response_retry_and_empty_cleanup(self):
        n,r,c=self.enroll();self.call('api_chain_del',{},c)
        self.assertShell('''eval "$(declare -f send_request | sed '1s/send_request/real_send_request/')"
send_request() {
  local text code
  text="$(cat)"
  code="$(printf '%s\\n' "$text" | real_send_request "$@")"
  if [[ "$text" == *phase=ack* ]] && [ ! -f "$LIB/dropped" ]; then touch "$LIB/dropped"; echo 000; else echo "$code"; fi
}
with_lock cycle manual
[ "$CONFIRM" = confirmed ] && [ ! -f "$ACK_FILE" ]
''')
        self.assertFalse(self.db.execute('SELECT 1 FROM chains').fetchone())
        x=json.loads((self.root/'kernel.json').read_text());self.assertEqual(x['nat']['ICTL-PRE'],[])

    def test_service_failure_cannot_confirm_mode_change(self):
        n,r,c=self.enroll();self.call('api_node_edit',{'desired_mode':'passive'},n)
        self.assertShell('with_lock cycle manual')
        self.call('api_node_edit',{'desired_mode':'active'},n)
        (self.root/'service').write_text('[Service]\nRestart=on-failure\n');(self.root/'fail-service').touch()
        self.assertShell('! with_lock cycle manual; [ "$R_STATE" = error ]')
        self.assertTrue(s.get_node(self.db,n)['manual_required'])
        self.assertEqual((self.lib/'mode').read_text(),'passive\n')

    def test_mode_change_during_auto_apply_stops_even_when_ack_gets_pause(self):
        n,r,c=self.enroll();self.call('api_target_save',{'port':8443},s.get_chain(self.db,c)['target_id'])
        original=s.sync_report
        def intercept(conn,node,fields,source_ip,now=None):
            if fields.get('source')=='auto' and fields.get('phase')=='ack':
                conn.execute("UPDATE nodes SET desired_mode='passive',manual_required=1,desired_generation=desired_generation+1 WHERE id=?",(n,))
                node=s.get_node(conn,n)
            return original(conn,node,fields,source_ip,now)
        with patch.object(s,'sync_report',side_effect=intercept):
            self.assertShell('! with_lock cycle auto; [ "$MODE" = passive ] && [ "$CONFIRM" = waiting ] && [ ! -f "$ACK_FILE" ]')
        self.assertTrue(s.get_node(self.db,n)['manual_required'])
        self.assertTrue(self.db.execute('SELECT 1 FROM chain_pending').fetchone())

    def test_pending_ack_pause_stops_before_another_auto_poll(self):
        n,r,c=self.enroll();self.call('api_target_save',{'port':8443},s.get_chain(self.db,c)['target_id'])
        self.assertShell('''eval "$(declare -f send_request | sed '1s/send_request/real_send_request/')"
send_request() {
  local text
  text="$(cat)"
  if [[ "$text" == *phase=ack* ]]; then echo 000; else printf '%s\\n' "$text" | real_send_request "$@"; fi
}
! with_lock cycle auto
[ -f "$ACK_FILE" ]
''')
        self.call('api_node_edit',{'desired_mode':'passive'},n)
        original=s.sync_report;phases=[]
        def intercept(conn,node,fields,source_ip,now=None):
            phases.append(fields['phase'])
            return original(conn,node,fields,source_ip,now)
        with patch.object(s,'sync_report',side_effect=intercept):
            self.assertShell('with_lock cycle auto; [ "$MODE" = passive ] && [ "$CONFIRM" = waiting ] && [ ! -f "$ACK_FILE" ]')
        self.assertEqual(phases,['ack'])
        self.assertTrue(s.get_node(self.db,n)['manual_required'])
        self.assertTrue(self.db.execute('SELECT 1 FROM chain_pending').fetchone())
        self.assertShell('with_lock cycle manual; [ "$CONFIRM" = confirmed ]')
        self.assertFalse(s.get_node(self.db,n)['manual_required'])
