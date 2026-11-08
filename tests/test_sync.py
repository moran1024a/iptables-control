"""Operation-bound receipts and manual/passive semantics."""

import secrets
import time
import unittest
from test_server import Fixture, fields
import database, errors, node_state, node_sync, forwarding


class SyncTests(Fixture, unittest.TestCase):
    def report(self, nid, **kw):
        data = dict(
            t=str(int(time.time())),
            proto="2",
            source="manual",
            phase="begin",
            op=secrets.token_hex(16),
            state="ok",
            agent="0.6.0-rc.1",
            mode="active",
            au="1",
        )
        data.update(kw)
        with self.db:
            return fields(
                node_sync.sync_report(
                    self.db, database.get_node(self.db, nid), data, "198.51.100.1"
                )
            )

    def finish(self, nid, op, **kw):
        payload = {k: op[k] for k in ("op", "generation", "mode", "v")}
        payload.update(phase="ack", **kw)
        return self.report(nid, **payload)

    def test_stale_wrong_and_repeat_receipts(self):
        n = self.node()
        op = self.report(n)
        self.assertEqual(self.finish(n, op, generation="999")["result"], "expired")
        self.call("api_node_edit", dict(desired_mode="passive"), n)
        self.assertEqual(self.finish(n, op)["result"], "changed")
        self.assertTrue(database.get_node(self.db, n)["manual_required"])
        new = self.report(n, mode="passive")
        self.assertEqual(self.finish(n, new)["result"], "confirmed")
        before = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(self.finish(n, new)["result"], "confirmed")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)

    def test_manual_confirmation_and_white_bar(self):
        n = self.node()
        self.ready(n)
        self.call("api_node_edit", dict(desired_mode="passive"), n)
        self.assertEqual(self.report(n, source="auto", phase="poll")["action"], "pause")
        op = self.report(n, mode="passive")
        self.assertEqual(self.finish(n, op)["result"], "confirmed")
        node = database.get_node(self.db, n)
        now = int(time.time())
        self.assertEqual(node_state.node_status(self.db, node, now + 86400), "synced")
        self.assertEqual(node_state.node_bar(self.db, node, now)["levels"][-1], "ok")
        self.assertEqual(set(node_state.node_bar(self.db, node, now + 3601)["levels"]), {"white"})

    def test_failed_auto_receipt_latches_conflict_until_retry(self):
        n = self.node()
        self.ready(n)
        with self.db:
            self.db.execute(
                "UPDATE nodes SET desired_generation=desired_generation+1 WHERE id=?", (n,)
            )
        op = self.report(n, source="auto", phase="poll")
        self.assertEqual(
            self.finish(n, op, source="auto", state="error", err="端口冲突：TCP 10001")["result"],
            "failed",
        )
        self.assertEqual(self.report(n, source="auto", phase="poll")["action"], "blocked")
        self.assertEqual(self.finish(n, self.report(n))["result"], "confirmed")

    def test_deleted_receipt_survives_node_deletion(self):
        n = self.node()
        self.ready(n)
        key = database.get_node(self.db, n)["key"]
        self.call("api_node_del", {}, n)
        self.assertEqual(self.finish(n, self.report(n))["result"], "deleted")
        self.assertFalse(self.db.execute("SELECT 1 FROM nodes").fetchone())
        self.assertEqual(self.db.execute("SELECT key FROM deleted_receipts").fetchone()[0], key)

    def test_old_protocol_rejected_without_runtime_compatibility(self):
        with self.assertRaises(errors.UserError):
            self.report(self.node(), proto="1")
