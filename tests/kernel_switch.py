"""Run via sudo unshare -nm python3 tests/kernel_switch.py ACTIVE_BINARY.

Exercises a persistent UDP client through both forwarding backends. All network
and mount changes are confined to disposable namespaces, all files to /tmp.
"""
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

assert os.geteuid() == 0
assert os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net')
assert os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt')


def run(*args, **kw):
    p = subprocess.run(args, text=True, capture_output=True, **kw)
    if p.returncode:
        raise RuntimeError(f'{args}: {p.stdout} {p.stderr}')
    return p.stdout


run('mount', '--make-rprivate', '/')
run('mount', '-t', 'tmpfs', 'tmpfs', '/run')
run('ip', 'link', 'set', 'lo', 'up')
Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
for name, prefix in [('client', '192.0.2'), ('target', '198.51.100')]:
    run('ip', 'netns', 'add', name)
    run('ip', 'link', 'add', name, 'type', 'veth', 'peer', 'name', name + '-peer')
    run('ip', 'link', 'set', name + '-peer', 'netns', name)
    run('ip', 'address', 'add', prefix + '.1/24', 'dev', name)
    run('ip', 'link', 'set', name, 'up')
    run('ip', '-n', name, 'address', 'add', prefix + '.2/24', 'dev', name + '-peer')
    run('ip', '-n', name, 'link', 'set', name + '-peer', 'up')
    run('ip', '-n', name, 'link', 'set', 'lo', 'up')

echo = subprocess.Popen(['ip', 'netns', 'exec', 'target', sys.executable, '-u', '-c', '''
import socket,threading,time
def serve(port, answer):
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',port))
 while True:
  data,peer=s.recvfrom(1024);s.sendto(answer,peer)
threading.Thread(target=serve,args=(30443,b'A'),daemon=True).start()
threading.Thread(target=serve,args=(30444,b'B'),daemon=True).start()
time.sleep(120)
'''])
clients = []


def client(port):
    p = subprocess.Popen(['ip', 'netns', 'exec', 'client', sys.executable, '-u', '-c', f'''
import socket,sys
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(3);s.connect(('192.0.2.1',{port}))
for line in sys.stdin:
 s.send(b'probe');print(s.recv(1024).decode(),flush=True)
'''], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    clients.append(p)
    return p


def probe(p, expected):
    p.stdin.write('go\n'); p.stdin.flush()
    assert p.stdout.readline().strip() == expected


try:
    time.sleep(.2)
    run('iptables', '-t', 'nat', '-A', 'PREROUTING', '-p', 'udp', '--dport', '30124', '-j', 'DNAT', '--to-destination', '198.51.100.2:30443')
    run('iptables', '-t', 'nat', '-A', 'POSTROUTING', '-p', 'udp', '-d', '198.51.100.2', '--dport', '30443', '-m', 'conntrack', '--ctorigdstport', '30124', '-j', 'MASQUERADE')
    other = client(30124); probe(other, 'A')
    with tempfile.TemporaryDirectory(prefix='ictl-switch-kernel-') as temp:
        root = Path(temp); conf = root/'identity'
        conf.write_text('URL=https://example.invalid/sync\nKEY=' + 'ab'*32 + '\n')
        binary = str(Path(sys.argv[1]).resolve())
        repo = Path(__file__).resolve().parents[1]
        for mode in ('active', 'passive'):
            state = root/mode; state.mkdir()
            def apply(rid, target):
                line = f'{rid} both 30123 198.51.100.2 {target}'
                snapshot = state/'config'
                snapshot.write_text('v=' + hashlib.sha256(line.encode()).hexdigest()[:12] + '\n' + line + '\n')
                if mode == 'active':
                    run(binary, '--state-root', str(state), '--conf', str(conf), '--apply-file', str(snapshot), '--generation', str(rid))
                else:
                    script = f'ICTL_LIBRARY=1 source {shlex.quote(str(repo/"agent/passive.sh"))}\n'
                    script += f'LIB={shlex.quote(str(state))}\n'
                    script += '''CONFIG="$LIB/config";APPLIED="$LIB/applied";KERNEL="$LIB/kernel";RESOLVED="$LIB/resolved"
CONF_DIR="$LIB";SYSCTL="$LIB/sysctl";IPT_CONFIG="$LIB/ipt";LEGACY_PERSIST="$LIB/legacy"
local_apply
[ "$R_STATE" = ok ] || { echo "$R_ERR" >&2; exit 1; }
'''
                    run('bash', '-c', script)
            apply(1, 30443)
            current = client(30123); probe(current, 'A')
            apply(2, 30444)
            probe(current, 'B')  # Same socket and source port: stale NAT cannot pass this.
            probe(other, 'A')
            current.terminate(); current.wait(timeout=5)
            if mode == 'active':
                run(binary, '--state-root', str(state), '--conf', str(conf), '--clear')
            assert '--dport 30124' in run('iptables-save')
    print('PASS: active/passive UDP cutover with unchanged client tuple; unrelated forwarding preserved')
finally:
    for p in clients + [echo]:
        if p.poll() is None: p.terminate()
        p.wait(timeout=5)
