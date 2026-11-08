"""IPv4 source/destination binding and handover in disposable network namespaces."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

assert os.geteuid() == 0
assert os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net')
assert os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt')


def run(*args):
    p = subprocess.run(args, text=True, capture_output=True, timeout=25)
    if p.returncode:
        print(subprocess.run(['ip','-j','-4','rule','show'],capture_output=True,text=True).stdout)
        raise RuntimeError(f'{args}: {p.stdout} {p.stderr}')
    return p.stdout


run('mount','--make-rprivate','/')
run('mount','-t','tmpfs','tmpfs','/run')
run('ip','link','set','lo','up')
Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
for device, prefix in [('client','192.0.2'), ('target','198.51.100')]:
    run('ip','netns','add',device)
    run('ip','link','add',device,'type','veth','peer','name',device+'-peer')
    run('ip','link','set',device+'-peer','netns',device)
    for suffix in (1,3): run('ip','address','add',f'{prefix}.{suffix}/24','dev',device)
    run('ip','link','set',device,'up')
    run('ip','-n',device,'address','add',f'{prefix}.2/24','dev',device+'-peer')
    run('ip','-n',device,'link','set',device+'-peer','up')
    run('ip','-n',device,'link','set','lo','up')
run('ip','-n','target','address','add','203.0.113.9/32','dev','lo')
# A functioning host route also permits reverse-path validation of public peers.
run('ip','route','add','default','via','198.51.100.2','dev','target')
# One address is routed to this machine without being configured on any NIC.
# Another is translated by an upstream namespace, as on a cloud public IP.
for ip in ('203.0.113.50', '198.18.0.7', '203.0.113.9'):
    run('ip','-n','client','route','add',ip+'/32','via','192.0.2.1')
run('ip','netns','exec','client','iptables','-t','nat','-A','OUTPUT',
    '-d','198.18.0.7','-j','DNAT','--to-destination','192.0.2.1')
# These profiles intentionally hash to the same RPDB priority. Exact marks
# must let them coexist, including during active/passive handover.
run('ip','link','add','eth0','type','dummy')
run('ip','link','set','eth0','up')
for suffix in (95,108): run('ip','address','add',f'10.0.0.{suffix}/24','dev','eth0')

server = subprocess.Popen(['ip','netns','exec','target',sys.executable,'-u','-c', '''
import socket,threading,time
def serve(port):
 s=socket.socket();s.bind(('0.0.0.0',port));s.listen()
 while True:
  c,a=s.accept()
  with c:c.sendall((str(port)+':'+a[0]).encode())
def udp():
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',30443))
 while True:
  b,a=s.recvfrom(100);s.sendto(a[0].encode(),a)
for port in (30443,30444):threading.Thread(target=serve,args=(port,),daemon=True).start()
udp()
'''])
try:
    time.sleep(.15)
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); conf=root/'identity'
        conf.write_text('URL=https://example.invalid/sync\nKEY='+'ab'*32+'\n')
        binary=str(Path(sys.argv[1]).resolve())
        def agent(*args): return run(binary,'--state-root',str(root),'--conf',str(conf),*args)
        lines=['1 both 30123 198.51.100.2 30443 192.0.2.3,198.51.100.3,client,target,-,-',
               '2 both 30124 198.51.100.2 30444 192.0.2.1,198.51.100.1,client,target,-,-',
               '3 both 30125 203.0.113.9 30443 192.0.2.3,198.51.100.3,client,target,-,198.51.100.2',
               '4 both 30126 198.51.100.2 30443 10.0.0.95,198.51.100.3,eth0,target,-,-',
               '5 both 30127 198.51.100.2 30443 10.0.0.108,198.51.100.3,eth0,target,-,-',
               '6 both 30128 198.51.100.2 30443 v2,192.0.2.3,-,-,-',
               '7 both 30129 198.51.100.2 30443 v2,203.0.113.50,-,-,-',
               '8 both 30130 198.51.100.2 30443 v2,198.18.0.7,-,-,-',
               '9 both 30131 198.51.100.2 30443 v2,198.18.0.7,198.51.100.3,target,-']
        content='\n'.join(lines); snapshot=root/'snapshot'
        snapshot.write_text('v='+hashlib.sha256(content.encode()).hexdigest()[:12]+'\n'+content+'\n')
        def check():
            run('ip','netns','exec','client',sys.executable,'-c', '''
import socket
for incoming,port,expected in [('192.0.2.3',30123,b'30443:198.51.100.3'),('192.0.2.1',30124,b'30444:198.51.100.1'),('192.0.2.3',30125,b'30443:198.51.100.3'),('192.0.2.3',30128,b'30443:198.51.100.1'),('203.0.113.50',30129,b'30443:198.51.100.1'),('198.18.0.7',30130,b'30443:198.51.100.1'),('198.18.0.7',30131,b'30443:198.51.100.3')]:
 s=socket.create_connection((incoming,port),timeout=2)
 assert s.recv(100)==expected
 s.close()
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(2)
for incoming,port,source in [('192.0.2.3',30123,b'198.51.100.3'),('203.0.113.50',30129,b'198.51.100.1'),('198.18.0.7',30130,b'198.51.100.1'),('198.18.0.7',30131,b'198.51.100.3')]:
 s.sendto(b'x',(incoming,port));assert s.recv(100)==source
for incoming,port in [('192.0.2.1',30123),('192.0.2.1',30128),('203.0.113.9',30129)]:
 try:socket.create_connection((incoming,port),timeout=.3)
 except OSError:pass
 else:raise AssertionError('accepted traffic on an unrelated destination')
''')
        agent('--apply-file',str(snapshot),'--generation','1');check()
        def reject_unavailable_source(command):
            saved = snapshot.read_text()
            bad = content.replace('v2,198.18.0.7,198.51.100.3,target,-', 'v2,198.18.0.7,198.51.100.99,target,-')
            snapshot.write_text('v='+hashlib.sha256(bad.encode()).hexdigest()[:12]+'\n'+bad+'\n')
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=25)
                assert result.returncode != 0 and '198.51.100.99' in result.stderr, result
                check()  # Rejection must leave the previous working rules in place.
            finally:
                snapshot.write_text(saved)
        reject_unavailable_source([binary,'--state-root',str(root),'--conf',str(conf),'--apply-file',str(snapshot),'--generation','2'])
        if len(sys.argv) > 2:
            result=run('env','ICTL_ROUTING_KERNEL=1',str(Path(sys.argv[2]).resolve()),
                       '-test.run','^TestDiagnosticRouteKernel$','-test.v')
            assert 'PASS' in result, result
        shared=[r for r in json.loads(run('ip','-j','-4','rule','show')) if r['priority']==24860]
        assert len(shared)==2 and len({r['fwmark'] for r in shared})==2,shared
        # Reapplying must accept our existing profile journals and routes.
        agent('--apply-file',str(snapshot),'--generation','1');check()
        def withdraw_routes(apply):
            run('ip','route','add','203.0.113.0/24','dev','client')
            run('ip','route','add','default','via','192.0.2.2','dev','client','metric','500')
            apply()
            run('ip','route','del','203.0.113.0/24','dev','client')
            run('ip','route','del','default','via','192.0.2.2','dev','client','metric','500')
            apply()
            for path in (root/'routing').glob('[0-9]*'):
                if ',client,' not in path.read_text(): continue
                routes=json.loads(run('ip','-N','-j','-4','route','show','table',path.name))
                assert not any(r.get('dst')=='203.0.113.0/24' or r.get('gateway')=='192.0.2.2' for r in routes), routes
                assert any(str(r.get('type')) in ('unreachable', '7') for r in routes), routes
            check()
        withdraw_routes(lambda: agent('--apply-file',str(snapshot),'--generation','1'))
        agent('--clear')
        assert not list((root/'routing').glob('[0-9]*'))
        passive=str(Path(__file__).resolve().parents[1]/'agent/passive.sh')
        script='''
export ICTL_LIBRARY=1
source "$1"
LIB="$2"; CONFIG="$LIB/snapshot"; APPLIED="$LIB/applied"; RESOLVED="$LIB/resolved"; KERNEL="$LIB/kernel"; STATE="$LIB/state"; SYSCTL="$LIB/sysctl"
CONF_DIR="$LIB/conf"; IPT_CONFIG="$LIB/standalone"; LEGACY_PERSIST="$LIB/legacy"
ensure_ip_forward() { :; }
local_apply
[ "$R_STATE" = ok ] || { echo "$R_ERR" >&2; exit 1; }
'''
        run('bash','-c',script,'test',passive,str(root));check()
        reject_unavailable_source(['bash','-c',script,'test',passive,str(root)])
        shared=[r for r in json.loads(run('ip','-j','-4','rule','show')) if r['priority']==24860]
        assert len(shared)==2 and len({r['fwmark'] for r in shared})==2,shared
        run('bash','-c',script,'test',passive,str(root));check()
        withdraw_routes(lambda: run('bash','-c',script,'test',passive,str(root)))
        run('bash','-c',script+'\nremove_rules','test',passive,str(root))
        assert not list((root/'routing').glob('[0-9]*'))
        rules=json.loads(run('ip','-j','-4','rule','show'))
        assert not any(str(r.get('protocol'))=='242' for r in rules), rules
        print('active/passive IPv4: local, routed, cloud NAT, automatic/explicit egress, TCP/UDP and cleanup: OK')
finally:
    server.terminate();server.wait(timeout=5)
