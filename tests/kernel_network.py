"""Run ONLY via: sudo unshare -nm env ICTL_KERNEL_TEST=1 python3 ... ACTIVE_BINARY.

The parent network and mount namespaces are never modified. No persistent netns.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types

assert os.environ.get('ICTL_KERNEL_TEST') == '1' and os.geteuid() == 0
assert os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net'), 'refusing host network'
assert os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt'), 'refusing host mounts'


def run(*args, **kw):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kw).stdout


run('mount','--make-rprivate','/')
run('mount','-t','tmpfs','tmpfs','/run')
run('ip','link','set','lo','up')
Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
for name, prefix in [('client','192.0.2'),('target','198.51.100')]:
    run('ip','netns','add',name)
    run('ip','link','add',name,'type','veth','peer','name',name+'-peer')
    run('ip','link','set',name+'-peer','netns',name)
    run('ip','address','add',prefix+'.1/24','dev',name)
    run('ip','link','set',name,'up')
    run('ip','-n',name,'address','add',prefix+'.2/24','dev',name+'-peer')
    run('ip','-n',name,'link','set',name+'-peer','up')
    run('ip','-n',name,'link','set','lo','up')

server = r'''
import socket,threading,time
def tcp():
 s=socket.socket();s.bind(('0.0.0.0',30443));s.listen()
 while True:
  c,_=s.accept()
  with c:
   while True:
    b=c.recv(65536)
    if not b:break
    c.sendall(b)
threading.Thread(target=tcp,daemon=True).start()
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',30443))
while True:
 b,a=s.recvfrom(65535);s.sendto(b,a)
'''
echo=subprocess.Popen(['ip','netns','exec','target',sys.executable,'-c',server])
try:
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        conf=root/'identity';conf.write_text('URL=https://example.invalid/sync\nKEY='+'ab'*32+'\n')
        binary=str(Path(sys.argv[1]).resolve())
        def agent(*args,**kw):return run(binary,'--state-root',str(root),'--conf',str(conf),*args,**kw)
        def apply(port=30123):
            line=f'1 both {port} 198.51.100.2 30443'
            (root/'snapshot').write_text('v='+hashlib.sha256(line.encode()).hexdigest()[:12]+'\n'+line+'\n')
            agent('--apply-file',str(root/'snapshot'),'--generation','1')
        def traffic():
            agent('--checkpoint')
            return {r['protocol']:r for r in json.loads(agent('--status'))['traffic']}
        def client(protocol):
            run('ip','netns','exec','client',sys.executable,'-c',f'''
import socket
s=socket.socket(socket.AF_INET,socket.SOCK_{'DGRAM' if protocol=='udp' else 'STREAM'});s.settimeout(3)
s.connect(('192.0.2.1',30123))
for _ in range(10):
 s.sendall(b'x'*257)
 b=b''
 while len(b)<257:b+=s.recv(257-len(b))
 assert b==b'x'*257
s.close()
''')
        # Foreign rules survive applying, updating and clearing the owned table.
        run('nft','add','table','ip','unrelated')
        run('nft','add','chain','ip','unrelated','pre','{ type nat hook prerouting priority -110; }')
        run('nft','add','rule','ip','unrelated','pre','udp','dport','30124','dnat','to','198.51.100.2:30443')
        apply()
        time.sleep(.15)
        client('tcp');client('udp')
        first=traffic()
        for direction in ('up','down'):
            assert int(first['udp'][direction]) == (20+8+257)*10, first
            assert int(first['tcp'][direction]) >= 2570, first
        apply();client('udp')
        second=traffic()
        assert second['udp']['epoch']==first['udp']['epoch']
        assert int(second['udp']['up'])==int(first['udp']['up'])*2
        assert int(second['udp']['down'])==int(first['udp']['down'])*2
        old_hash=json.loads(agent('--status'))['config']['hash']
        try:apply(30124)
        except subprocess.CalledProcessError as exc:assert '端口冲突' in exc.stderr
        else:raise AssertionError('native port conflict accepted')
        assert json.loads(agent('--status'))['config']['hash']==old_hash
        # Resetting only one direction must neither subtract traffic nor double the other.
        counters=json.loads(run('nft','-j','list','table','ip','ictl_active'))['nftables']
        counter=next(v['counter']['name'] for v in counters if v.get('counter',{}).get('name','').startswith('r1_udp_up_'))
        run('nft','reset','counter','ip','ictl_active',counter)
        client('udp');agent('--checkpoint')
        rows=json.loads(agent('--status'))['traffic']
        udp=[r for r in rows if r['protocol']=='udp']
        assert len(udp)==2 and sum(int(r['up']) for r in udp)==(20+8+257)*30, udp
        assert sum(int(r['down']) for r in udp)==(20+8+257)*30, udp
        assert any('重置' in r['quality'] for r in udp), udp
        apply(30125)
        assert 30123 in json.loads(agent('--held-ports')), 'old UDP flows lost reservation'
        agent('--clear')
        assert '30124' in run('nft','list','table','ip','unrelated')
        # Feed the passive/wake checker actual iptables-nft output produced by
        # the standalone backend, including its ct status dnat ACCEPT rules.
        repo=Path(__file__).resolve().parents[1]
        standalone=types.ModuleType('standalone')
        source=(repo/'ipt.sh').read_text().split("<<'IPT_PYTHON'\n",1)[1].split('\nIPT_PYTHON',1)[0]
        exec(compile(source,'ipt.sh:backend','exec'),standalone.__dict__)
        row=dict(id='a'*32,incoming='10001',host='198.51.100.2',ip='198.51.100.2',target='443',proto='udp',comment='coexist')
        for rule in standalone.make_rules([row]):
            run('iptables','-w','-t',rule['table'],'-A',rule['chain'],*rule['spec'])
        listing=run('nft','-n','list','ruleset')
        assert 'ct status dnat' in listing, listing
        check='''source "$1"
LIB="$2"; CONF_DIR="$2"; IPT_CONFIG="$2/standalone.conf"
ERRS=(); ICTL_WAKE_CHECK=1
check_port_conflicts "$3" || { printf '%s\\n' "${ERRS[@]}" >&2; exit 1; }
'''
        def wake_check(port):
            return run('bash','-c',check,'test',str(repo/'agent/passive.sh'),str(root),
                       f'v=check\n1 udp {port} 127.0.0.1 1\n',env={**os.environ,'ICTL_LIBRARY':'1'})
        before=run('nft','-j','list','ruleset')
        for port in (443,61024,62000):wake_check(port)
        try:wake_check(10001)
        except subprocess.CalledProcessError as exc:assert '端口冲突' in exc.stderr, exc.stderr
        else:raise AssertionError('standalone DNAT port accepted as a wake port')
        assert run('nft','-j','list','ruleset')==before, 'port checks changed firewall rules'
        print('PASS: TCP/UDP forwarding, exact counters, update continuity, native conflicts, scoped cleanup and passive wake checks with real ipt.sh rules')
finally:
    echo.terminate();echo.wait(timeout=5)
