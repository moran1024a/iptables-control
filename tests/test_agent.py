import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import database
import http_harness
import node_state
import node_sync
"""Run the real Bash Agent against fake kernel/systemd commands and local encrypted HTTP."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import test_server
from test_server import ROOT

FAKE_IPTABLES = r'''#!/usr/bin/env python3
import sys,os,json,pathlib
root=pathlib.Path(os.environ['MOCK_ROOT']);p=root/'kernel.json'
x=json.loads(p.read_text()) if p.exists() else {'nat':{'PREROUTING':[],'POSTROUTING':[],'OTHER':['-j ACCEPT']},'filter':{'FORWARD':[],'OTHER':['-j ACCEPT']},'mangle':{'PREROUTING':[],'OTHER':['-j ACCEPT']}}
args=sys.argv[1:];name=pathlib.Path(sys.argv[0]).name
with (root/'commands').open('a') as log:log.write(name+' '+' '.join(args)+'\n')
def save():p.write_text(json.dumps(x))
if name=='iptables-save':
 tables=[args[args.index('-t')+1]] if '-t' in args else list(x)
 for t in tables:
  print('*'+t)
  for chain,rules in x[t].items():print(':'+chain+' - [0:0]')
  for chain,rules in x[t].items():
   for rule in rules:print('-A '+chain+' '+rule)
  print('COMMIT')
 sys.exit(0)
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
if '-w' in args:
 i=args.index('-w');del args[i]
 if i<len(args) and args[i].isdigit():del args[i]
t='filter'
if '-t' in args:i=args.index('-t');t=args[i+1];del args[i:i+2]
op,chain=args[:2] if len(args)>1 else (args[0],None)
if op=='-S':
 for c,rules in x[t].items():
  print('-N '+c)
  for rule in rules:print('-A '+c+' '+rule)
 sys.exit(0)
rest=args[2:];position=0
if op=='-I' and rest and rest[0].isdigit():position=int(rest[0])-1;rest=rest[1:]
rule=' '.join(rest)
if op=='-C':sys.exit(0 if rule in x[t].get(chain,[]) else 1)
if op=='-A':x[t].setdefault(chain,[]).append(rule)
elif op=='-I':x[t].setdefault(chain,[]).insert(position,rule)
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
        nft=self.bin/'nft';nft.write_text("#!/bin/sh\nif [ -f \"$MOCK_ROOT/native.json\" ]; then cat \"$MOCK_ROOT/native.json\"; else echo '{\"nftables\":[]}'; fi\n");nft.chmod(0o755)
        systemctl=self.bin/'systemctl';systemctl.write_text('''#!/bin/bash
printf 'systemctl %s\n' "$*" >> "$MOCK_ROOT/commands"
[ ! -f "$MOCK_ROOT/fail-service" ]
''');systemctl.chmod(0o755)
        ss=self.bin/'ss';ss.write_text('''#!/bin/bash
[ ! -f "$MOCK_ROOT/fail-ss" ] || exit 1
case "$*" in *t*) file="$MOCK_ROOT/tcp.sockets" ;; *) file="$MOCK_ROOT/udp.sockets" ;; esac
if [ -f "$file" ]; then cat "$file"; fi
''');ss.chmod(0o755)
        conntrack=self.bin/'conntrack';conntrack.write_text('#!/bin/sh\nprintf \'conntrack %s\\n\' \"$*\" >> \"$MOCK_ROOT/commands\"\n');conntrack.chmod(0o755)
        self.env={**os.environ,'PATH':str(self.bin)+':'+os.environ['PATH'],'MOCK_ROOT':str(self.root)}
        source=(ROOT/getattr(self,'source_file','agent/passive.sh')).read_text().rsplit('\nmain "$@"',1)[0]
        source=source.split('\nif [ "${ICTL_LIBRARY:-0}" != 1 ]; then main "$@"; fi')[0]
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
IPT_CONFIG={shlex.quote(str(self.root/'managed'))}
LEGACY_PERSIST={shlex.quote(str(self.root/'system-rules.v4'))}
SYSCTL={shlex.quote(str(self.root/'agent-sysctl'))}
LEGACY_SCRIPTS=({shlex.quote(str(self.root/'root-ictl.sh'))} {shlex.quote(str(self.root/'ictl.sh'))})
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
