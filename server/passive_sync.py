import base64
import hmac
import time
import agent_v3
import database
import errors
import node_sync
import security
import settings


def handle(self):
    try:
        length = self.body_length(settings.MAX_BODY)
        raw = base64.b64decode(self.rfile.read(length).strip(), validate=True)
        if len(raw) < 40:
            raise ValueError()
    except (errors.UserError, ValueError):
        return self.not_found()
    msg, req_tag = (raw[:-32], raw[-32:])
    conn = database.db()
    out = None
    try:
        identities = [dict(n) for n in conn.execute("SELECT * FROM nodes")]
        identities += [
            dict(r)
            for r in conn.execute("SELECT * FROM deleted_receipts WHERE expires>=?", (int(time.time()),))
        ]
        for candidate in identities:
            ek, mk = security.derive(candidate["key"])
            if hmac.compare_digest(req_tag, security.mac(mk, msg)):
                break
        else:
            return self.not_found()
        plaintext = security.aes(ek, b"Salted__" + msg, decrypt=True).decode("utf-8")
        fields = {}
        for line in plaintext.splitlines():
            k, sep, v = line.partition("=")
            if sep:
                if k in fields:
                    raise errors.UserError("重复字段")
                fields[k] = v.strip()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            node = conn.execute("SELECT * FROM nodes WHERE key=?", (candidate["key"],)).fetchone()
            if node:
                if fields.get("manage"):
                    out = agent_v3.manage(conn, node, fields)
                else:
                    out = node_sync.sync_report(conn, node, fields, self.source_ip())
            else:
                receipt = conn.execute(
                    "SELECT * FROM deleted_receipts WHERE key=? AND expires>=?",
                    (candidate["key"], int(time.time())),
                ).fetchone()
                if (
                    receipt
                    and fields.get("manage") == "commit"
                    and (fields.get("op") == receipt["op"])
                    and (fields.get("target") == receipt["mode"])
                    and fields.get("t", "").isdigit()
                    and (abs(int(fields["t"]) - int(time.time())) <= settings.MAX_SKEW)
                ):
                    out = "proto=2\nresult=deleted\n"
                if (
                    receipt
                    and fields.get("proto") == "2"
                    and (fields.get("phase") == "ack")
                    and (fields.get("state") == "ok")
                    and (fields.get("op") == receipt["op"])
                    and (fields.get("v") == receipt["hash"])
                    and (fields.get("generation") == str(receipt["generation"]))
                    and (fields.get("mode") == receipt["mode"])
                    and fields.get("t", "").isdigit()
                    and (abs(int(fields["t"]) - int(time.time())) <= settings.MAX_SKEW)
                ):
                    out = "proto=2\nresult=deleted\n"
    except (errors.UserError, ValueError, UnicodeError):
        pass
    finally:
        conn.close()
    if out is None:
        return self.not_found()
    data = out if isinstance(out, bytes) else out.encode()
    self.reply(200, security.seal_bytes(candidate["key"], req_tag, data))
