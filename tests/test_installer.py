"""Exercise the installer with all managed paths relocated into a temporary directory."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.bin=self.root/'bin';self.bin.mkdir()
        self.managed=self.root/'managed';self.managed.mkdir()
        self.env={**os.environ,'PATH':str(self.bin)+':'+os.environ['PATH']}
        text=(ROOT/'tunnel-agent/install-tunnel.sh').read_text()
        text=text.replace('[[ $EUID == 0 ]]','true')
        for prefix in ('/etc/','/usr/local/','/var/lib/'):
            text=text.replace(prefix,str(self.managed)+prefix)
        self.installer=self.root/'installer.sh';self.installer.write_text(text)
        self.agent=self.root/'agent'
        self.agent.write_text('#!/bin/sh\nif [ "$1" = version ]; then echo 0.1.0; exit 0; fi\n'
            f'mkdir -p "{self.managed}/usr/local/lib/ictl-tunnel"\n'
            f'echo new-core > "{self.managed}/usr/local/lib/ictl-tunnel/sing-box"\n')
        self.agent.chmod(0o755)
        self.manifest=self.root/'components.json'
        self.manifest.write_text(json.dumps(dict(api=1,tunnel_agent='0.1.0',agent_assets={
            'amd64':dict(sha256=hashlib.sha256(self.agent.read_bytes()).hexdigest()),
            'arm64':dict(sha256=hashlib.sha256(self.agent.read_bytes()).hexdigest())})))
        self.script('curl',f'''#!/usr/bin/env python3
import sys,shutil
args=sys.argv[1:];target=args[args.index('-o')+1]
url=next(a for a in args if a.startswith('https://'))
shutil.copyfile({str(self.manifest)!r} if url.endswith('/components') else {str(self.agent)!r},target)
''')
        self.script('systemctl',f'''#!/usr/bin/env python3
import sys,pathlib
args=sys.argv[1:]
with open({str(self.root/'commands')!r},'a') as f:f.write(' '.join(args)+'\\n')
if args[0] in ('cat','is-active'):sys.exit(0)
if args[:2]==['restart','ictl-tunnel-agent'] and pathlib.Path({str(self.root/'fail')!r}).exists():sys.exit(1)
''')
        for path in ('etc/systemd/system','usr/local/bin','usr/local/lib/ictl-tunnel','etc/ictl-tunnel','var/lib/ictl-tunnel'):
            (self.managed/path).mkdir(parents=True,exist_ok=True)

    def script(self,name,text):
        path=self.bin/name;path.write_text(text);path.chmod(0o755)

    def tearDown(self):self.tmp.cleanup()

    def run_install(self):
        return subprocess.run(['bash',str(self.installer),'--url','https://example.com/agent','--token','ab'*32],
            env=self.env,capture_output=True,text=True,timeout=15)

    def test_installs_separate_services_and_private_credentials(self):
        r=self.run_install();self.assertEqual(r.returncode,0,r.stderr)
        cfg=self.managed/'etc/ictl-tunnel/agent.json'
        self.assertEqual(cfg.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(cfg.read_text())['url'],'https://example.com/agent')
        self.assertTrue((self.managed/'etc/systemd/system/ictl-tunnel-core.service').exists())
        self.assertTrue((self.managed/'usr/local/bin/ictl-tunnel-agent').exists())
        self.assertFalse((self.managed/'usr/local/bin/ictl').exists())

    def test_restart_failure_restores_old_installation(self):
        files=['etc/ictl-tunnel/agent.json','usr/local/bin/ictl-tunnel-agent','usr/local/lib/ictl-tunnel/sing-box',
               'etc/systemd/system/ictl-tunnel-agent.service','etc/systemd/system/ictl-tunnel-core.service','var/lib/ictl-tunnel/config.json']
        for path in files:(self.managed/path).write_text('old:'+path)
        (self.root/'fail').touch()
        r=self.run_install();self.assertNotEqual(r.returncode,0)
        for path in files:self.assertEqual((self.managed/path).read_text(),'old:'+path)
        self.assertIn('start ictl-tunnel-agent',(self.root/'commands').read_text())

    def test_bad_checksum_does_not_stop_existing_services(self):
        self.agent.write_text(self.agent.read_text()+'# corruption\n')
        r=self.run_install();self.assertNotEqual(r.returncode,0)
        log=self.root/'commands'
        self.assertFalse(log.exists() and 'stop' in log.read_text())

    def test_missing_argument_rejected(self):
        r=subprocess.run(['bash',str(self.installer),'--url'],env=self.env,capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0)
        self.assertIn('缺少参数',r.stderr)


if __name__=='__main__':unittest.main()
