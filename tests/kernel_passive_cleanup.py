"""Run with sudo unshare -nm python3 tests/kernel_passive_cleanup.py CONNTRACK.

All kernel changes stay in disposable network/mount namespaces; files use /tmp.
"""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

assert os.geteuid() == 0
assert os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net')
assert os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt')


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f'{args}: {result.stdout} {result.stderr}')
    return result.stdout


run('mount', '--make-rprivate', '/')
run('mount', '-t', 'tmpfs', 'tmpfs', '/run')
run('ip', 'link', 'set', 'lo', 'up')
Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
for name, prefix in [('client', '192.0.2'), ('target', '198.51.100')]:
    run('ip', 'netns', 'add', name)
    run('ip', 'link', 'add', name, 'type', 'veth', 'peer', 'name', name+'-peer')
    run('ip', 'link', 'set', name+'-peer', 'netns', name)
    run('ip', 'address', 'add', prefix+'.1/24', 'dev', name)
    run('ip', 'link', 'set', name, 'up')
    run('ip', '-n', name, 'address', 'add', prefix+'.2/24', 'dev', name+'-peer')
    run('ip', '-n', name, 'link', 'set', name+'-peer', 'up')
    run('ip', '-n', name, 'link', 'set', 'lo', 'up')

server = subprocess.Popen(['ip', 'netns', 'exec', 'target', sys.executable, '-u', '-c', '''
import socket,threading,time
def serve(port, answer):
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',port))
 while True:
  data,peer=s.recvfrom(1024);s.sendto(answer,peer)
for p,a in [(30443,b'A'),(30444,b'B')]:threading.Thread(target=serve,args=(p,a),daemon=True).start()
s=socket.socket();s.bind(('0.0.0.0',30443));s.listen()
print('ready',flush=True)
while True:
 c,_=s.accept();c.send(b'A');threading.Thread(target=lambda x:x.recv(1024),args=(c,),daemon=True).start()
'''], stdout=subprocess.PIPE, text=True)
clients = []


def client(port, tcp=False):
    code = f'''
import socket,sys
s=socket.socket(socket.AF_INET,socket.{'SOCK_STREAM' if tcp else 'SOCK_DGRAM'})
s.settimeout(3);s.connect(('192.0.2.1',{port}))
for line in sys.stdin:
 {'pass' if tcp else "s.send(b'probe')"}
 print(s.recv(1024).decode(),flush=True)
'''
    p = subprocess.Popen(['ip', 'netns', 'exec', 'client', sys.executable, '-u', '-c', code],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    clients.append(p)
    return p


def probe(p, expected):
    p.stdin.write('go\n'); p.stdin.flush()
    assert p.stdout.readline().strip() == expected


try:
    assert server.stdout.readline().strip() == 'ready'
    run('iptables', '-t', 'nat', '-A', 'PREROUTING', '-p', 'udp', '--dport', '30124', '-j', 'DNAT', '--to-destination', '198.51.100.2:30443')
    run('iptables', '-t', 'nat', '-A', 'POSTROUTING', '-p', 'udp', '-d', '198.51.100.2', '--dport', '30443', '-m', 'conntrack', '--ctorigdstport', '30124', '-j', 'MASQUERADE')
    peer = client(30124); probe(peer, 'A')
    with tempfile.TemporaryDirectory(prefix='ictl-cleanup-kernel-') as temp:
        root = Path(temp)
        binary = str(Path(sys.argv[1]).resolve())
        (root/'bin').mkdir(); (root/'bin/conntrack').symlink_to(binary)
        repo = Path(__file__).resolve().parents[1]
        setup = f'ICTL_LIBRARY=1 source {shlex.quote(str(repo/"agent/passive.sh"))}\nLIB={shlex.quote(temp)}\n'
        setup += f'export PATH={shlex.quote(str(root/"bin"))}:"$PATH"\n'
        setup += 'CONFIG="$LIB/config";APPLIED="$LIB/applied";KERNEL="$LIB/kernel";RESOLVED="$LIB/resolved"\n'
        setup += 'CONF_DIR="$LIB";SYSCTL="$LIB/sysctl";IPT_CONFIG="$LIB/ipt";LEGACY_PERSIST="$LIB/legacy"\n'

        def shell(code):
            return run('bash', '-c', setup+code)

        def apply(rid, port):
            (root/'config').write_text(f'v=test\n{rid} both 30123 198.51.100.2 {port}\n')
            shell('local_apply\n[ "$R_STATE" = ok ] || { echo "$R_ERR" >&2; exit 1; }')

        apply(1, 30443)
        udp = client(30123); probe(udp, 'A')
        tcp = client(30123, True); probe(tcp, 'A')
        shell('conntrack() { return 1; }; ! remove_rules')
        assert (root/'clear-owned.rules').exists()
        shell('remove_rules')
        assert not (root/'clear-owned.rules').exists()
        for protocol in ('tcp', 'udp'):
            assert not run(binary, '-L', '-p', protocol, '--orig-port-dst', '30123').strip()
        probe(peer, 'A')
        apply(2, 30444)
        probe(udp, 'B')
        probe(peer, 'A')
        shell('remove_rules')
        print('PASS: TCP/UDP cleanup, interrupted retry, same UDP socket switches target, unrelated forwarding survives')
finally:
    for p in clients+[server]:
        p.terminate()
    for p in clients+[server]:
        try: p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()
