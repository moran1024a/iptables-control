"""Docker upgrade failures restore the paired image/container and database, using mocks only."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from test_server import ROOT

DOCKER = r'''#!/usr/bin/env python3
import sys,os,pathlib,json,sqlite3
root=pathlib.Path(os.environ['MOCK_ROOT']);args=sys.argv[1:]
state=root/'containers';data=json.loads(state.read_text())
with (root/'docker.log').open('a') as f:f.write(' '.join(args[:4])+'\n')
def save():state.write_text(json.dumps(data))
if args[0] in ('info','pull'):sys.exit(0)
if args[:2]==['container','inspect']:
 name=args[-1]
 if name not in data:sys.exit(1)
 if '-f' not in args:print('{}');sys.exit(0)
 fmt=args[args.index('-f')+1]
 if 'Config.Image' in fmt:print(data[name]['image'])
 elif 'PortBindings' in fmt:print('127.0.0.1|8730')
 elif 'Mounts' in fmt:print('volume|ictl-data|unused|true')
 elif 'Config.Env' in fmt:print('ICTL_DB=/data/ictl.db')
 sys.exit(0)
if args[0]=='run':
 if '--rm' in args:
  if '-c' in args:
   code=args[args.index('-c')+1].replace('/data/',str(root/'data')+'/')
   if '.restore' in code and os.environ.get('FAIL_RESTORE'):sys.exit(1)
   sys.argv=['python']+[a.replace('/data/',str(root/'data')+'/') for a in args[args.index('-c')+2:]]
   exec(code)
  sys.exit(0)
 if os.environ.get('FAIL_RUN'):sys.exit(1)
 name=args[args.index('--name')+1];data[name]={'image':args[-1],'running':True};save()
 conn=sqlite3.connect(root/'data/ictl.db');conn.execute('PRAGMA user_version=300');conn.execute("UPDATE example SET value='new'");conn.commit();conn.close();sys.exit(0)
if args[0]=='rename':data[args[2]]=data.pop(args[1]);save();sys.exit(0)
if args[0] in ('stop','start'):
 data[args[1]]['running']=args[0]=='start';save();sys.exit(0)
if args[0]=='rm':data.pop(args[-1],None);save();sys.exit(0)
if args[0]=='logs':
 print('server startup diagnostic');sys.exit(0)
if args[0]=='exec':
 if args[-1]=='health' and os.environ.get('FAIL_HEALTH'):
  print('错误：数据库尚未就绪',file=sys.stderr);sys.exit(1)
 print('ready');sys.exit(0)
sys.exit(1)
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'bin').mkdir();(self.root/'data').mkdir()
        self.env={**os.environ,'PATH':str(self.root/'bin')+':'+os.environ['PATH'],'MOCK_ROOT':str(self.root)}
        for name,text in [('docker',DOCKER),('sleep','#!/bin/sh\nexit 0\n')]:
            p=self.root/'bin'/name;p.write_text(text);p.chmod(0o755)
        (self.root/'containers').write_text(json.dumps({'ictl-server':{'image':'ghcr.io/moran1024a/ictl-server:0.2.1','running':True}}))
        c=sqlite3.connect(self.root/'data/ictl.db');c.executescript("PRAGMA user_version=202; CREATE TABLE example(value TEXT);INSERT INTO example VALUES ('old');");c.close()

    def tearDown(self):self.tmp.cleanup()

    def run_install(self,**env):
        return subprocess.run(['bash',str(ROOT/'deploy/install-server.sh')],env={**self.env,**env},capture_output=True,text=True,timeout=15)

    def assert_old(self):
        containers=json.loads((self.root/'containers').read_text())
        self.assertEqual(list(containers),['ictl-server']);self.assertTrue(containers['ictl-server']['running'])
        self.assertTrue(containers['ictl-server']['image'].endswith('0.2.1'))
        c=sqlite3.connect(self.root/'data/ictl.db')
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],202)
        self.assertEqual(c.execute('SELECT value FROM example').fetchone()[0],'old');c.close()

    def test_failed_new_container_recovers_old(self):
        r=self.run_install(FAIL_RUN='1');self.assertNotEqual(r.returncode,0);self.assert_old()
        self.assertTrue(list((self.root/'data').glob('*.bak')))

    def test_health_failure_restores_old_database_after_migration(self):
        r=self.run_install(FAIL_HEALTH='1');self.assertNotEqual(r.returncode,0);self.assert_old()
        self.assertNotIn('volume rm',(self.root/'docker.log').read_text())
        self.assertIn('错误：数据库尚未就绪',r.stderr)
        self.assertIn('server startup diagnostic',r.stderr)
        commands=(self.root/'docker.log').read_text()
        self.assertLess(commands.index('logs --tail 50'),commands.index('rm -f'))

    def test_failed_restore_keeps_old_stopped(self):
        r=self.run_install(FAIL_HEALTH='1',FAIL_RESTORE='1');self.assertNotEqual(r.returncode,0)
        containers=json.loads((self.root/'containers').read_text());self.assertEqual(len(containers),1)
        self.assertFalse(next(iter(containers.values()))['running']);self.assertIn('数据库恢复失败',r.stderr)

    def test_success_removes_only_old_container_and_keeps_backup(self):
        r=self.run_install();self.assertEqual(r.returncode,0,r.stderr)
        containers=json.loads((self.root/'containers').read_text());self.assertEqual(list(containers),['ictl-server'])
        self.assertTrue(containers['ictl-server']['image'].endswith('0.4.5'))
        backup=next((self.root/'data').glob('*.bak'));c=sqlite3.connect(backup)
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],202);c.close()

    def test_missing_port_value_rejected(self):
        r=subprocess.run(['bash',str(ROOT/'deploy/install-server.sh'),'--port'],env=self.env,capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0);self.assertIn('缺少参数',r.stderr)


class AgentInstallerTests(unittest.TestCase):
    def test_failed_preparation_restores_identity_and_entry_scripts(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('bin','etc','pkg','lib','run','assets'):(root/name).mkdir()
            key='ab'*32
            original={root/'etc/agent.conf':f'URL=https://old.invalid/sync\nKEY={key}\nAUTO_UPDATE=0\n',
                      root/'pkg/manage.sh':'old manager',root/'pkg/control.sh':'old control'}
            for path,text in original.items():path.write_text(text)
            for name in ('manage.sh','control.sh'):
                (root/'assets'/name).write_text('#!/bin/bash\nexit 42\n')
            (root/'assets/SHA256SUMS').write_text(''.join(hashlib.sha256((root/'assets'/name).read_bytes()).hexdigest()+'  '+name+'\n' for name in ('manage.sh','control.sh')))
            for name in ('systemctl','nft','ss','conntrack'):
                (root/'bin'/name).write_text('#!/bin/sh\nexit 0\n'); (root/'bin'/name).chmod(0o755)
            (root/'bin/id').write_text('#!/bin/sh\necho 0\n'); (root/'bin/id').chmod(0o755)
            (root/'bin/curl').write_text('''#!/bin/bash
for arg in "$@"; do
  if [[ "$arg" == https://* ]]; then file="${arg##*/}"; fi
  if [ "${prev:-}" = -o ]; then output="$arg"; fi
  prev="$arg"
done
cp "$CASE_ROOT/assets/$file" "$output"
'''); (root/'bin/curl').chmod(0o755)
            source=(ROOT/'deploy/install-agent.sh').read_text()
            for path,target in (('/etc/ictl','etc'),('/usr/local/lib/ictl','pkg'),('/var/lib/ictl','lib'),('/run/ictl-manager.lock','run/manager.lock')):
                source=source.replace(path,str(root/target))
            script=root/'install'; script.write_text(source)
            result=subprocess.run(['bash',str(script),'--url','https://new.invalid/sync','--key',key],
                                  env={**os.environ,'CASE_ROOT':directory,'PATH':str(root/'bin')+':'+os.environ['PATH']},capture_output=True,text=True)
            self.assertEqual(result.returncode,42,result.stderr)
            for path,text in original.items():self.assertEqual(path.read_text(),text)
            self.assertFalse(list((root/'lib').glob('install-backup.*')))
