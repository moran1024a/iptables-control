"""sudo unshare -nm python3 tests/kernel_quality.py ACTIVE_BINARY GO_TEST_BINARY.

B can reach C only. C forwards to a target on another network with 80 ms
artificial delay. A successful measured handshake must traverse that suffix.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

assert os.geteuid() == 0
assert os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net')
assert os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt')


def run(*args):
    p = subprocess.run(args, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(f'{args}: {p.stdout} {p.stderr}')
    return p.stdout


run('mount', '--make-rprivate', '/')
run('mount', '-t', 'tmpfs', 'tmpfs', '/run')
run('ip', 'link', 'set', 'lo', 'up')
Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
for name, prefix in [('source', '192.0.2'), ('target', '198.51.100')]:
    run('ip', 'netns', 'add', name)
    run('ip', 'link', 'add', name, 'type', 'veth', 'peer', 'name', name+'-peer')
    run('ip', 'link', 'set', name+'-peer', 'netns', name)
    run('ip', 'address', 'add', prefix+'.1/24', 'dev', name)
    run('ip', 'link', 'set', name, 'up')
    run('ip', '-n', name, 'address', 'add', prefix+'.2/24', 'dev', name+'-peer')
    run('ip', '-n', name, 'link', 'set', name+'-peer', 'up')
    run('ip', '-n', name, 'link', 'set', 'lo', 'up')
run('tc', 'qdisc', 'add', 'dev', 'target', 'root', 'netem', 'delay', '80ms')
target = subprocess.Popen(['ip', 'netns', 'exec', 'target', sys.executable, '-u', '-c', '''
import socket
s=socket.socket();s.bind(('0.0.0.0',30443));s.listen();print('ready',flush=True)
while True:
 c,_=s.accept()
 with c:
  assert c.recv(32)==b'', 'probe sent application payload'
'''], stdout=subprocess.PIPE, text=True)
try:
    assert target.stdout.readline().strip() == 'ready'
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        identity = root/'identity'
        identity.write_text('URL=https://example.invalid/sync\nKEY='+'ab'*32+'\n')
        line = '1 both 30123 198.51.100.2 30443'
        snapshot = root/'snapshot'
        snapshot.write_text('v='+hashlib.sha256(line.encode()).hexdigest()[:12]+'\n'+line+'\n')
        run(str(Path(sys.argv[1]).resolve()), '--state-root', str(root), '--conf', str(identity),
            '--apply-file', str(snapshot), '--generation', '1')
        result = run('ip', 'netns', 'exec', 'source', 'env', 'ICTL_QUALITY_KERNEL=1',
                     str(Path(sys.argv[2]).resolve()), '-test.run', '^TestTCPQualityKernelPath$', '-test.v')
        assert 'PASS' in result, result
        assert target.poll() is None
        print('PASS: measured TCP handshake follows C to target and includes downstream delay')
finally:
    target.terminate()
    target.wait(timeout=5)
    target.stdout.close()
