import tempfile
import unittest
from test_agent import AgentHarness


class PassiveTests(AgentHarness,unittest.TestCase):
    source_file='agent/passive.sh'

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.setup_agent()

    def tearDown(self):self.temp.cleanup()

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
        self.assertShell('''if check_port_conflicts $'v=abc\\n1 both 10001 192.0.2.1 443'; then exit 1; fi
[[ "${ERRS[*]}" == *端口冲突* ]]
''')
