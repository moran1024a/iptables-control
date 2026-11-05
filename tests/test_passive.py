import tempfile
import unittest
from test_agent import AgentHarness


class PassiveTests(AgentHarness,unittest.TestCase):
    source_file='agent/passive.sh'

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.setup_agent()

    def tearDown(self):self.temp.cleanup()

    def test_restore_reconnects_parent_chains_after_handover_cleanup(self):
        self.assertShell('''printf 'v=abc\n1 both 10001 192.0.2.1 443\n' > "$CONFIG"
local_apply
[ "$R_STATE" = ok ] || exit 1
snapshot="$(owned_snapshot)"
remove_rules
restore_owned "$snapshot" $'1\n1\n1' && jumps_ok
''')

    def test_ipv6_wake_listener_conflict(self):
        (self.root/'udp.sockets').write_text('UNCONN 0 0 [::1]:61024 [::]:*\n')
        self.assertShell("ICTL_WAKE_CHECK=1; check_port_conflicts $'v=check\\n1 udp 61024 127.0.0.1 1'")
        self.assertShell("ICTL_WAKE_CHECK=1; ICTL_WAKE_IPV6=1; ! check_port_conflicts $'v=check\\n1 udp 61024 127.0.0.1 1'")

    def test_reboot_restores_rules_without_dns_or_control_requests(self):
        self.assertShell('printf "v=abc\\n1 both 10001 192.0.2.1 443\\n" > "$CONFIG"\nlocal_apply\n[ "$R_STATE" = ok ]')
        r=self.assertShell('''curl() { echo forbidden >&2; return 1; }
getent() { echo forbidden >&2; return 1; }
daemon
''')
        self.assertNotIn('forbidden',r.stderr)

    def test_native_nft_port_conflict_is_detected(self):
        (self.root/'native.json').write_text('''table ip foreign {
 chain pre {
  type nat hook prerouting priority -100;
  tcp dport 10001 dnat to 192.0.2.2:443
 }
}
''')
        self.assertShell('''! check_port_conflicts $'v=abc\\n1 both 10001 192.0.2.1 443'
[[ "${ERRS[*]}" == *端口冲突* ]]
''')
        self.assertShell("check_port_conflicts $'v=abc\\n1 udp 10001 192.0.2.1 443'")
        self.assertShell("check_port_conflicts $'v=abc\\n1 both 10002 192.0.2.1 443'")

    def test_wake_coexists_with_standalone_conntrack_accept_rules(self):
        (self.root/'managed').write_text('# ipt-forward-manager v3\n' + 'a'*32 + ' 10001 example.com 443 udp single 192.0.2.2\n')
        (self.root/'native.json').write_text('''table ip filter {
 chain FORWARD {
  type filter hook forward priority filter; policy accept;
  ip daddr 192.0.2.2 udp dport 443 ct status dnat ct original proto-dst 10001 ct direction original counter accept comment "iptfm:test"
  ip saddr 192.0.2.2 udp sport 443 ct status dnat ct original proto-dst 10001 ct direction reply counter accept comment "iptfm:test"
 }
}
table ip nat {
 chain PREROUTING {
  type nat hook prerouting priority dstnat; policy accept;
  udp dport 10001 fib daddr type local counter dnat to 192.0.2.2:443 comment "iptfm:test"
 }
}
''')
        for port in (443, 61024, 62000):
            with self.subTest(port=port):
                self.assertShell(f"ICTL_WAKE_CHECK=1; check_port_conflicts $'v=check\\n1 udp {port} 127.0.0.1 1'")
        self.assertShell('''ICTL_WAKE_CHECK=1
! check_port_conflicts $'v=check\\n1 udp 10001 127.0.0.1 1'
[[ "${ERRS[*]}" == *单机脚本保存的配置* ]]
''')
        (self.root/'managed').unlink()
        self.assertShell('''ICTL_WAKE_CHECK=1
! check_port_conflicts $'v=check\\n1 udp 10001 127.0.0.1 1'
[[ "${ERRS[*]}" == *nat/PREROUTING* ]]
''')

    def test_native_ingress_paths_distinguish_actions_and_status_matches(self):
        (self.root/'native.json').write_text('''table inet filter {
 chain input {
  type filter hook input priority filter; policy accept;
  meta l4proto vmap { tcp : accept, udp : accept }
 }
 chain unrelated {
  udp dport 61024 redirect to :53
 }
 chain pre {
  type filter hook prerouting priority mangle; policy accept;
  ct status dnat accept
  udp dport 61025 jump udp_proxy
  tcp dport 61024 goto tcp_proxy
 }
 chain udp_proxy {
  jump loop
  tproxy to :53
 }
 chain loop {
  jump udp_proxy
 }
 chain tcp_proxy {
  redirect to :53
 }
}
''')
        self.assertShell("check_port_conflicts $'v=check\\n1 udp 61024 127.0.0.1 1'")
        for proto, port in (('udp', 61025), ('tcp', 61024)):
            with self.subTest(proto=proto, port=port):
                self.assertShell(f'''! check_port_conflicts $'v=check\\n1 {proto} {port} 127.0.0.1 1'
[[ "${{ERRS[*]}}" == *端口冲突* ]]
''')

    def test_wake_port_is_reserved_even_if_listener_has_stopped(self):
        (self.conf/'wake.conf').write_text('ENABLED=1\nPORT=10001\nALLOW=192.0.2.2\n')
        self.assertShell('''if check_port_conflicts $'v=abc\\n1 both 10001 192.0.2.1 443'; then exit 1; fi
[[ "${ERRS[*]}" == *远程唤醒* ]]
''')
        self.assertShell('''if check_port_conflicts $'v=abc\\n1 both 10001 192.0.2.1 443'; then exit 1; fi
[[ "${ERRS[*]}" == *端口冲突* ]]
''')
