import base64
import json
import unittest
from test_server import Fixture
from test_imports import ss_uri, enc_uri
import configurations, database, subscriptions, target_config


class SubscriptionTests(Fixture, unittest.TestCase):
    def create(self, uri=None):
        n = self.node()
        self.ready(n)
        t = self.call("api_target_save", dict(uri=uri or ss_uri()))["id"]
        ident = self.save(self.graph([n], t))
        self.settle()
        c = configurations.members(self.db, ident)[0]
        s = self.call("api_subscription_save", dict(name="client", configuration_ids=[c["id"]]))[
            "id"
        ]
        return n, t, ident, c, s

    def test_both_formats_and_custom_name_use_confirmed_entry(self):
        n, t, ident, c, s = self.create()
        self.call("api_config_rename", dict(name="personal", previous_name=c["name"]), c["id"])
        row = subscriptions.get(self.db, s)
        raw = base64.b64decode(subscriptions.export(self.db, row["token"], "v2ray")[0]).decode()
        p = target_config.import_uri(raw.strip())
        self.assertEqual(
            (p["name"], p["host"], p["port"]),
            ("personal", "192.0.2.1", json.loads(c["applied"])["plan"]["port"]),
        )
        clash = subscriptions.export(self.db, row["token"], "clash")[0]
        self.assertIn("personal", clash)
        self.assertIn("192.0.2.1", clash)
        self.assertEqual(
            subscriptions.listing(self.db)["subscriptions"][0]["configuration_ids"], [c["id"]]
        )

    def test_pending_update_keeps_old_endpoint_credentials_and_reset_revokes(self):
        n, t, ident, c, s = self.create()
        token = subscriptions.get(self.db, s)["token"]
        old = subscriptions.export(self.db, token, "v2ray")
        self.call("api_target_save", dict(uri=ss_uri("other.example", 8443, b"x" * 16)), t)
        self.assertEqual(subscriptions.export(self.db, token, "v2ray"), old)
        self.settle()
        self.assertNotEqual(subscriptions.export(self.db, token, "v2ray"), old)
        self.call("api_subscription_save", dict(reset_token=True), s)
        self.assertIsNone(subscriptions.export(self.db, token, "v2ray"))

    def test_plain_target_and_unsupported_clash_do_not_break_exports(self):
        n, t, ident, c, s = self.create(enc_uri())
        row = subscriptions.listing(self.db)
        self.assertTrue(row["configs"][0]["clash_error"])
        self.assertFalse(row["configs"][0]["error"])
        token = subscriptions.get(self.db, s)["token"]
        self.assertIn("proxies: []", subscriptions.export(self.db, token, "clash")[0])
        self.assertTrue(
            base64.b64decode(subscriptions.export(self.db, token, "v2ray")[0]).startswith(
                b"vless://"
            )
        )

    def test_removed_configuration_is_omitted_before_cleanup(self):
        n, t, ident, c, s = self.create()
        token = subscriptions.get(self.db, s)["token"]
        self.call("api_topology_delete", {}, ident)
        self.assertEqual(base64.b64decode(subscriptions.export(self.db, token, "v2ray")[0]), b"")
