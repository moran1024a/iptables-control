import tempfile
import unittest
from test_agent import AgentHarness


class PassiveTests(AgentHarness,unittest.TestCase):
    source_file='agent/passive.sh'

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.setup_agent()

    def tearDown(self):self.temp.cleanup()

    def test_dns_is_resolved_once_before_rule_lock(self):
        getent = self.bin/'getent'
        getent.write_text('#!/bin/sh\necho lookup >> "$MOCK_ROOT/dns-count"\necho "192.0.2.1 STREAM example.test"\n')
        getent.chmod(0o755)
        self.assertShell("""printf 'v=abc\\n1 both 10001 example.test 443\\n2 tcp 10002 example.test 443\\n' > "$CONFIG"
warm_resolutions "$(cat "$CONFIG")" || exit 1
with_lock local_apply
[ "$R_STATE" = ok ]
""")
        self.assertEqual((self.root/'dns-count').read_text().splitlines(), ['lookup'])

    def test_expired_preparation_never_changes_rules(self):
        self.assertShell("""SYNC_DEADLINE=0
! with_lock apply_snapshot $'v=abc\\n1 both 10001 192.0.2.1 443' 1 passive || exit 1
[ "$R_STATE" = error ]
""")
        self.assertFalse((self.root/'kernel.json').exists())
        self.assertFalse((self.lib/'config').exists())

    def test_wake_retries_receipt_with_ack_timeout_before_new_work(self):
        (self.conf/'agent.conf').write_text('URL=https://example.invalid/sync\nKEY='+'ab'*32+'\n')
        (self.lib/'ack').write_text('proto=2\nsource=wake\nphase=ack\nop='+'ab'*16+'\n')
        self.assertShell('''
derive_keys() { :; }
exchange() {
  [ "$SOURCE/$PHASE" = wake/ack ] || return 1
  RESPONSE=$'proto=2\\nresult=deleted\\n'
}
cycle wake || exit 1
[ "$CONFIRM" = deleted ] && [ ! -f "$ACK_FILE" ]
''')

    def test_new_receive_and_source_route_format(self):
        self.assertShell('''
route_fields 'v2,198.18.0.7,-,-,-' || exit 1
[ "$R_LISTEN" = 198.18.0.7 ] && [ -z "$R_IN$R_OUT" ] || exit 1
route_fields 'v2,cloud.example,10.0.0.2,ens3,-' || exit 1
[ -z "$R_IN" ] && [ "$R_OUT,$R_ODEV,$R_OGW" = '10.0.0.2,ens3,-' ] || exit 1
! route_fields 'v2,cloud.example,-,ens3,-' || exit 1
! route_fields 'v2,cloud.example,10.0.0.2,-,-' || exit 1
! route_fields 'v2,cloud.example,-,-,-,extra' || exit 1
printf '%s\\n' 'v=abc' '1 both 10001 192.0.2.1 443 v2,198.18.0.7,-,-,-' '2 tcp 10002 192.0.2.1 443 v2,cloud.example,10.0.0.2,ens3,-' > "$CONFIG"
valid_config "$(cat "$CONFIG")" || exit 1
[ "$(current_route_profiles)" = '10.0.0.2,ens3,-' ]
''')

    def test_policy_priority_can_be_shared_only_by_exact_owned_marks(self):
        self.assertShell('''
mkdir -p "$LIB/routing"
for key in '10.0.0.95,eth0,-' '10.0.0.108,eth0,-'; do
  route_numbers "$key"
  [ "$ROUTE_PRIORITY" = 24860 ] || exit 1
  printf '%s\\n' "$key" > "$LIB/routing/$ROUTE_TABLE"
  printf -v mark '0x%x' "$ROUTE_MARK"
  rule="$ROUTE_PRIORITY: from all fwmark $mark lookup $ROUTE_TABLE proto 242"
  route_policy_owned "$rule" || exit 1
  ! route_policy_owned "$ROUTE_PRIORITY: from all fwmark $mark/0xffffff00 lookup $ROUTE_TABLE proto 242" || exit 1
  ! route_policy_owned "${rule/proto 242/proto 4}" || exit 1
done
''')

    def test_route_noop_requires_exact_owned_route(self):
        self.assertShell('''
line='default via 10.0.0.1 dev eth0 proto 242 src 10.0.0.2 metric 10'
route_present "$line" default 10 eth0 10.0.0.2 10.0.0.1 '' || exit 1
for changed in "${line/10.0.0.1/10.0.0.9}" "${line/10.0.0.2/10.0.0.3}" "${line/eth0/eth1}" "${line/proto 242/proto 4}" "$line onlink" "$line mtu 1400"; do
  ! route_present "$changed" default 10 eth0 10.0.0.2 10.0.0.1 '' || exit 1
done
route_present '10.0.0.0/24 dev eth0 proto 242 scope link src 10.0.0.2' 10.0.0.0/24 0 eth0 10.0.0.2 '' '' || exit 1
route_present 'unreachable default proto 242 metric 32767' default 32767 '' '' '' unreachable || exit 1
route_present '7 default proto 242 metric 32767' default 32767 '' '' '' unreachable
''')

    def test_route_preparation_reuses_reads_but_rechecks_next_sync(self):
        self.assertShell('''
mkdir -p "$LIB/routing"
: > "$MOCK_ROOT/policies"
for source in 10.0.0.2 10.0.0.3; do
  key="$source,test0,-"; route_numbers "$key"
  printf '%s\\n' "$key" > "$LIB/routing/$ROUTE_TABLE"
  printf -v mark '0x%x' "$ROUTE_MARK"
  printf '%s: from all fwmark %s lookup %s proto 242\\n' "$ROUTE_PRIORITY" "$mark" "$ROUTE_TABLE" >> "$MOCK_ROOT/policies"
  printf '10.0.0.0/24 dev test0 proto 242 scope link src %s\\ndefault via 10.0.0.1 dev test0 proto 242 src %s metric 10\\nunreachable default proto 242 metric 32767\\n' "$source" "$source" > "$MOCK_ROOT/table-$ROUTE_TABLE"
done
printf 'v=abc\\n1 tcp 10001 192.0.2.1 443 v2,198.18.0.1,10.0.0.2,test0,-\\n2 tcp 10002 192.0.2.2 443 v2,198.18.0.1,10.0.0.3,test0,-\\n' > "$CONFIG"
cat() {
  case "$1" in /proc/sys/net/ipv4/conf/*/rp_filter) echo 0 ;; *) command cat "$@" ;; esac
}
ip() {
  printf '%s\\n' "$*" >> "$MOCK_ROOT/ip-calls"
  case "$*" in
    '-o -4 address show dev test0') printf '1: test0 inet 10.0.0.2/24 scope global test0\\n1: test0 inet 10.0.0.3/24 scope global test0\\n' ;;
    '-N -4 -o rule show') cat "$MOCK_ROOT/policies" ;;
    '-N -4 route show table '*) cat "$MOCK_ROOT/table-${@: -1}" ;;
    '-N -4 route show dev test0 scope link') echo '10.0.0.0/24 dev test0 proto kernel scope link' ;;
    '-N -4 route show default dev test0') echo 'default via 10.0.0.1 dev test0' ;;
    '-4 route get '*) echo '192.0.2.1 via 10.0.0.1 dev test0' ;;
    '-4 route replace '*) [ -f "$MOCK_ROOT/allow-repair" ] ;;
    *) echo "unexpected ip $*" >&2; return 1 ;;
  esac
}
prepare_routes || exit 1
[ "$(grep -c -- '-N -4 -o rule show' "$MOCK_ROOT/ip-calls")" = 1 ] || exit 1
[ "$(grep -c -- '-o -4 address show dev test0' "$MOCK_ROOT/ip-calls")" = 1 ] || exit 1
[ "$(grep -c -- '-N -4 route show dev test0 scope link' "$MOCK_ROOT/ip-calls")" = 1 ] || exit 1
[ "$(grep -c -- '-N -4 route show default dev test0' "$MOCK_ROOT/ip-calls")" = 1 ] || exit 1
! grep -q 'replace' "$MOCK_ROOT/ip-calls" || exit 1
# A subsequent sync must observe and repair a changed route, despite identical config.
route_numbers '10.0.0.2,test0,-'
sed -i 's/via 10.0.0.1 /via 10.0.0.9 /' "$MOCK_ROOT/table-$ROUTE_TABLE"
touch "$MOCK_ROOT/allow-repair"
prepare_routes || exit 1
[ "$(grep -c -- '-N -4 -o rule show' "$MOCK_ROOT/ip-calls")" = 2 ] || exit 1
[ "$(grep -c -- '-4 route replace default via 10.0.0.1 dev test0 src 10.0.0.2' "$MOCK_ROOT/ip-calls")" = 1 ] || exit 1
# Foreign routes must still prevent application.
route_numbers '10.0.0.2,test0,-'
sed -i 's/proto 242/proto 4/g' "$MOCK_ROOT/table-$ROUTE_TABLE"
! prepare_routes
''')

    def test_clear_removes_only_owned_tcp_and_udp_connections(self):
        self.assertShell('printf "v=abc\\n1 both 10001 192.0.2.1 443\\n" > "$CONFIG"\nlocal_apply\n[ "$R_STATE" = ok ]')
        self.assertShell('remove_rules')
        calls = [line for line in (self.root/'commands').read_text().splitlines() if line.startswith('conntrack ')]
        self.assertEqual(len(calls), 2)
        for proto, call in zip(('tcp', 'udp'), calls):
            self.assertIn(f'-p {proto} --orig-port-dst 10001 --reply-src 192.0.2.1 --reply-port-src 443 --dst-nat', call)
        self.assertFalse((self.lib/'clear-owned.rules').exists())

    def test_clear_failure_keeps_destinations_for_retry_after_chains_are_gone(self):
        self.assertShell('printf "v=abc\\n1 both 10001 192.0.2.1 443\\n" > "$CONFIG"\nlocal_apply\n[ "$R_STATE" = ok ]')
        self.assertShell('conntrack() { echo failed >&2; return 1; }; ! remove_rules')
        self.assertIn('--to-destination 192.0.2.1:443', (self.lib/'clear-owned.rules').read_text())
        self.assertShell('remove_rules')
        self.assertFalse((self.lib/'clear-owned.rules').exists())
        calls = (self.root/'commands').read_text()
        self.assertIn('conntrack -D -f ipv4 -p udp --orig-port-dst 10001', calls)

    def test_restore_does_not_forget_unfinished_cleanup(self):
        self.assertShell('''printf 'v=abc\n1 both 10001 192.0.2.1 443\n' > "$CONFIG"
local_apply
snapshot="$(owned_snapshot)"
conntrack() { return 1; }
! remove_rules || exit 1
! restore_owned "$snapshot" $'1\n1\n1\n1' || exit 1
[ -f "$LIB/clear-owned.rules" ] || exit 1
unset -f conntrack
restore_owned "$snapshot" $'1\n1\n1\n1' && jumps_ok
[ ! -f "$LIB/clear-owned.rules" ]
''')

    def test_restore_reconnects_parent_chains_after_handover_cleanup(self):
        self.assertShell('''printf 'v=abc\n1 both 10001 192.0.2.1 443\n' > "$CONFIG"
local_apply
[ "$R_STATE" = ok ] || exit 1
snapshot="$(owned_snapshot)"
remove_rules
restore_owned "$snapshot" $'1\n1\n1\n1' && jumps_ok
''')

    def test_ipv6_wake_listener_conflict(self):
        (self.root/'udp.sockets').write_text('UNCONN 0 0 [::1]:61024 [::]:*\n')
        self.assertShell("ICTL_WAKE_CHECK=1; check_port_conflicts $'v=check\\n1 udp 61024 127.0.0.1 1'")
        self.assertShell("ICTL_WAKE_CHECK=1; ICTL_WAKE_IPV6=1; ! check_port_conflicts $'v=check\\n1 udp 61024 127.0.0.1 1'")

    def test_connected_udp_client_socket_is_not_a_port_conflict(self):
        (self.root/'udp.sockets').write_text('ESTAB 0 0 203.0.113.5:41234 198.51.100.9:443\nUNCONN 0 0 0.0.0.0:41235 0.0.0.0:*\n')
        self.assertShell("check_port_conflicts $'v=check\\n1 udp 41234 192.0.2.1 443'")
        self.assertShell("! check_port_conflicts $'v=check\\n1 udp 41235 192.0.2.1 443'")

    def test_dns_outage_keeps_forwarding_on_the_cached_address(self):
        getent = self.bin/'getent'
        getent.write_text('#!/bin/sh\nexit 2\n'); getent.chmod(0o755)
        r = self.assertShell("""printf 'v=abc\\n1 tcp 30001 target.example 443\\n' > "$CONFIG"
printf 'target.example 192.0.2.1\\n' > "$RESOLVED"
with_lock local_apply
[ "$R_STATE" = ok ] || { echo "$R_STATE $R_ERR"; exit 1; }
owned_snapshot | grep -q -- '--to-destination 192.0.2.1:443'
""")
        self.assertIn('沿用缓存地址', r.stderr)

    def test_snat_source_change_flushes_connections_in_saved_rule_order(self):
        self.assertShell("""ERRS=()
prev=$'-A ICTL-PRE -p tcp -m addrtype --dst-type LOCAL -m tcp --dport 30001 -j DNAT --to-destination 192.0.2.1:443\\n-A ICTL-POST -d 192.0.2.1/32 -p tcp -m tcp --dport 443 -m conntrack --ctstate DNAT --ctorigdstport 30001 -j MASQUERADE'
clear_changed_connections "$prev" "$prev" || exit 1
! grep -q conntrack "$MOCK_ROOT/commands" 2>/dev/null || exit 1
clear_changed_connections "$prev" "${prev/-j MASQUERADE/-j SNAT --to-source 10.0.0.2}" || exit 1
grep -q 'conntrack -D .*--orig-port-dst 30001' "$MOCK_ROOT/commands"
""")

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
