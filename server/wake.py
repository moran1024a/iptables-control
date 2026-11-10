import asyncio
import hashlib
import hmac
import logging
import secrets
import socket
import time
import lifecycle

TIMEOUT = 30
INTERVAL = 10
ATTEMPTS = 3


def eligible(node):
    return (
        node["wake_enabled"]
        and node["wake_port"]
        and (node["desired_mode"] == "passive")
        and (node["installed_agent"] == "passive")
        and (not node["manual_required"])
    )


def queue(conn, node, now=None):
    if not eligible(node):
        conn.execute("DELETE FROM wake_jobs WHERE node_id=?", (node["id"],))
        return
    now = int(time.time()) if now is None else now
    conn.execute(
        "INSERT OR REPLACE INTO wake_jobs(node_id,generation,token,started) VALUES (?,?,?,?)",
        (node["id"], node["desired_generation"], secrets.token_hex(16), now),
    )
    lifecycle.add_event(conn, node["id"], "已安排远程唤醒，30 秒内等待同步确认", now)


def summary(conn, node):
    job = conn.execute(
        "SELECT state,error,started,attempts,generation FROM wake_jobs WHERE node_id=?",
        (node["id"],),
    ).fetchone()
    return dict(job) if job else None


def authorized(conn, node, fields, now=None):
    if not eligible(node) or fields.get("agent_kind") != "passive":
        return False
    now = int(time.time()) if now is None else now
    job = conn.execute("SELECT token,state,started,generation FROM wake_jobs WHERE node_id=?", (node["id"],)).fetchone()
    return bool(job and job["state"] == "waiting" and now < job["started"] + TIMEOUT
                and job["generation"] == node["desired_generation"]
                and secrets.compare_digest(job["token"], fields.get("wake_id", "")))


def receipt(conn, node, generation, result, error=""):
    if result not in ("confirmed", "failed"):
        return
    conn.execute(
        "UPDATE wake_jobs SET state=?,error=? WHERE node_id=? AND generation=?",
        ("confirmed" if result == "confirmed" else "failed", error[:500], node["id"], generation),
    )


def packet(key, token, now=None):
    stamp = int(time.time()) if now is None else now
    body = f"IWK1|{stamp}|{token}|{secrets.token_hex(16)}".encode()
    derived = hmac.digest(bytes.fromhex(key), b"ictl-wake-v1", "sha256")
    return body + b"|" + hmac.new(derived, body, hashlib.sha256).hexdigest().encode()


class Monitor:
    def __init__(self, hub):
        self.hub = hub
        self.task = None
        self.event = asyncio.Event()

    def due(self, conn, now):
        outgoing = []
        for job in conn.execute("SELECT * FROM wake_jobs WHERE state='waiting'").fetchall():
            node = conn.execute("SELECT * FROM nodes WHERE id=?", (job["node_id"],)).fetchone()
            if (
                node is None
                or not eligible(node)
                or node["desired_generation"] != job["generation"]
            ):
                conn.execute("DELETE FROM wake_jobs WHERE node_id=?", (job["node_id"],))
                continue
            if now >= job["started"] + TIMEOUT:
                error = "同步超时，等待手动同步"
                conn.execute(
                    "UPDATE wake_jobs SET state='timeout',error=? WHERE node_id=?",
                    (error, node["id"]),
                )
                lifecycle.add_event(conn, node["id"], error, now)
            elif job["attempts"] < ATTEMPTS and now >= job["started"] + job["attempts"] * INTERVAL:
                conn.execute(
                    "UPDATE wake_jobs SET attempts=attempts+1 WHERE node_id=?", (node["id"],)
                )
                outgoing.append(
                    (
                        node["id"],
                        node["addr"],
                        node["wake_port"],
                        packet(node["key"], job["token"], now),
                        job["token"],
                    )
                )
        return outgoing

    async def send(self, item):
        nid, host, port, data, token = item
        try:
            loop = asyncio.get_running_loop()
            addresses = await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM), 3
            )
            sent, failure = (False, OSError("没有可用的节点地址"))
            for family, kind, proto, _, address in dict.fromkeys(addresses):
                try:
                    with socket.socket(family, kind, proto) as sock:
                        sock.setblocking(False)
                        await loop.sock_sendto(sock, data, address)
                        sent = True
                except OSError as exc:
                    failure = exc
            if not sent:
                raise failure
        except (OSError, asyncio.TimeoutError) as exc:
            message = "唤醒发送失败：" + str(exc)[:200]
            await self.hub.database(
                lambda c: c.execute(
                    "UPDATE wake_jobs SET error=? WHERE node_id=? AND token=? AND state='waiting'",
                    (message, nid, token),
                )
            )

    async def run(self):
        while True:
            self.event.clear()
            try:
                jobs = await self.hub.database(lambda c: self.due(c, int(time.time())))
                await asyncio.gather(*(self.send(item) for item in jobs))
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Passive wake-up scheduler failed")
            try:
                await asyncio.wait_for(self.event.wait(), 1)
            except asyncio.TimeoutError:
                pass
