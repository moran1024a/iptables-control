"""Run with sudo unshare -n: real whitelist ICMP behavior, no host firewall changes."""
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.geteuid() != 0 or os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
        raise SystemExit('Run in an isolated network namespace: sudo unshare -n python3 tests/kernel_wake.py /path/to/ictl-wake')
    binary = Path(sys.argv[1]).resolve()
    subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
    with tempfile.TemporaryDirectory(prefix='ictl-wake-kernel-') as temp:
        work = Path(temp)
        (work/'wake').symlink_to(binary)
        (work/'wake.conf').write_text('ENABLED=1\nPORT=39001\nALLOW=127.0.0.2,::1\n')
        setup = f'ICTL_LIBRARY=1 source {shlex.quote(str(ROOT / "agent/wake.sh"))}\n'
        setup += f'PKG={shlex.quote(temp)}\nCONF="$PKG/wake.conf"\n'

        def policy(action):
            subprocess.run(['bash', '-c', setup + action], check=True)

        def probe(family, source, dest, port, expected):
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.bind((source, 0))
                sock.settimeout(.3)
                sock.connect((dest, port))
                sock.send(b'probe')
                try:
                    sock.recv(256)
                    result = 'reply'
                except ConnectionRefusedError:
                    result = 'unreachable'
                except TimeoutError:
                    result = 'silent'
            assert result == expected, (source, port, result, expected)

        policy('policy_apply')
        # No receiver exists: the kernel must still stay silent for allowlisted peers.
        probe(socket.AF_INET, '127.0.0.2', '127.0.0.1', 39001, 'silent')
        probe(socket.AF_INET, '127.0.0.3', '127.0.0.1', 39001, 'unreachable')
        probe(socket.AF_INET, '127.0.0.2', '127.0.0.1', 39002, 'unreachable')
        probe(socket.AF_INET6, '::1', '::1', 39001, 'silent')
        policy('policy_apply')  # Repeated starts must not duplicate jump rules.
        rules = subprocess.check_output(['iptables-save'], text=True)
        assert rules.count('-A INPUT -j ICTL-WAKE-IN') == 1
        assert rules.count('-A OUTPUT -j ICTL-WAKE-OUT') == 1
        policy('policy_remove')
        probe(socket.AF_INET, '127.0.0.2', '127.0.0.1', 39001, 'unreachable')
        assert 'ICTL-WAKE' not in subprocess.check_output(['iptables-save'], text=True)
        assert 'ICTL-WAKE' not in subprocess.check_output(['ip6tables-save'], text=True)
    print('Wake-up firewall behavior, unrelated ports and cleanup: OK')


if __name__ == '__main__':
    main()
