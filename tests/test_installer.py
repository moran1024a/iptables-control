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
if args[0]=='exec':
 if args[-1]=='health' and os.environ.get('FAIL_HEALTH'):sys.exit(1)
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

    def test_failed_restore_keeps_old_stopped(self):
        r=self.run_install(FAIL_HEALTH='1',FAIL_RESTORE='1');self.assertNotEqual(r.returncode,0)
        containers=json.loads((self.root/'containers').read_text());self.assertEqual(len(containers),1)
        self.assertFalse(next(iter(containers.values()))['running']);self.assertIn('数据库恢复失败',r.stderr)

    def test_success_removes_only_old_container_and_keeps_backup(self):
        r=self.run_install();self.assertEqual(r.returncode,0,r.stderr)
        containers=json.loads((self.root/'containers').read_text());self.assertEqual(list(containers),['ictl-server'])
        self.assertTrue(containers['ictl-server']['image'].endswith('0.3.1'))
        backup=next((self.root/'data').glob('*.bak'));c=sqlite3.connect(backup)
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],202);c.close()

    def test_missing_port_value_rejected(self):
        r=subprocess.run(['bash',str(ROOT/'deploy/install-server.sh'),'--port'],env=self.env,capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0);self.assertIn('缺少参数',r.stderr)
