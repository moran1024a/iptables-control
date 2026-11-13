#!/usr/bin/env python3
"""Run the real server on a throw-away database with sample data and simulated agents.

For panel development and browser tests only. Nothing here touches a firewall or a network
beyond the listening socket:

    python3 web/dev/demo_server.py --port 8731 [--web DIR] [--fresh]

Prints one line `PANEL <url>` when ready. Login: admin / ictl-demo-pass
(`--fresh` keeps the first-login state: admin / admin_pass).
"""
import argparse
import asyncio
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

from aiohttp import web  # noqa: E402

import agent_v3  # noqa: E402
import database  # noqa: E402
import node_state  # noqa: E402
import security  # noqa: E402
import settings  # noqa: E402
import static_files  # noqa: E402
import transport  # noqa: E402
from test_server import Fixture  # noqa: E402

SS = "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ@203.0.113.9:8388#东京出口"
TROJAN = "trojan://secret@example.com:443?sni=example.com#外部 Trojan"


def telemetry(db, nid, cpu):
    now = int(time.time() * 1000)
    agent_v3.save_snapshot(db, nid, "inventory", {
        "sampled_at": now, "version": settings.VERSION, "os": 'NAME="Debian GNU/Linux"\nVERSION_ID="12"',
        "kernel": "6.1.0-28-amd64", "arch": "amd64", "cpus": 2, "boot_id": "b" * 32, "installation_id": "i" * 32,
        "backend": "nftables", "nft_version": "nftables v1.0.6",
        "interfaces": [{"name": "lo", "mtu": 65536, "addresses": ["127.0.0.1/8"], "flags": "up|loopback"},
                       {"name": "eth0", "mtu": 1500, "addresses": [f"192.0.2.{nid}/24"], "flags": "up|broadcast"}],
        "network_complete": True})
    agent_v3.save_snapshot(db, nid, "metrics", {
        "sampled_at": now, "memory": {"MemTotal": 2063597568, "MemAvailable": 1400000000 + nid * 1000000},
        "agent_rss": 12000000, "cpu_percent": cpu,
        "disk": {"total": "21474836480", "available": "15032385536"},
        "interfaces": {"eth0": {"rx_bytes": str(98_000_000_000 + nid), "tx_bytes": str(120_000_000_000 + nid),
                                "rx_bytes_per_second": 152000.0 * cpu, "tx_bytes_per_second": 187000.0 * cpu}},
        "conntrack_count": str(120 + nid * 7), "conntrack_max": "262144", "traffic_quality": ""})
    agent_v3.save_snapshot(db, nid, "ports", {
        "sampled_at": now, "errors": [], "total": 2, "truncated": False,
        "rows": [{"source": "socket", "protocol": "tcp", "address": "0.0.0.0", "port": 22},
                 {"source": "socket", "protocol": "udp", "address": "0.0.0.0", "port": 33445}]})


def seed(fx):
    db, call = fx.db, fx.call
    asia = call("api_group_add", dict(kind="nodes", name="亚洲"))["id"]
    call("api_group_add", dict(kind="nodes", name="欧美"))
    hk = call("api_tag_save", dict(name="HK", color="#ef4444"))["id"]
    jp = call("api_tag_save", dict(name="JP", color="#3b82f6"))["id"]
    vip = call("api_tag_save", dict(name="高优先级", color="#f59e0b"))["id"]

    def node(name, addr, **kw):
        return call("api_node_add", dict(name=name, addr=addr, ports="10000-10100", **kw))["id"]

    n_hk = node("香港入口", "192.0.2.1", tag_ids=[hk, vip], weight=10, group_id=asia, note="主入口，BGP 多线")
    n_jp = node("东京中转", "192.0.2.2", tag_ids=[jp], weight=5, group_id=asia)
    n_sg = node("新加坡中转", "192.0.2.3", group_id=asia,
                addresses=[dict(id="main", primary=True, host="192.0.2.3"), dict(id="alt", name="副线", host="192.0.2.33")])
    n_us = node("洛杉矶中转", "192.0.2.4", weight=-5)
    n_de = node("法兰克福（被动）", "de.example.com", desired_mode="passive", quality_port=0, wake_enabled=True, wake_port=10050)
    node("首尔（未接入）", "192.0.2.6", tag_ids=[vip, hk, jp])
    n_kr = node("首尔入口", "192.0.2.7", group_id=asia)
    online = [n_hk, n_jp, n_sg, n_kr]
    fx.ready(*online, n_us)
    fx.ready(n_de, mode="passive")
    now = int(time.time())
    with db:
        db.execute("UPDATE nodes SET last_seen=? WHERE id=?", (now - 900, n_us))
        db.execute("UPDATE nodes SET state='error',err='端口冲突：TCP 10001 已被本机服务占用' WHERE id=?", (n_kr,))
        db.execute("INSERT INTO windows(node_id,win,level,manual_count,last_activity) VALUES (?,?,2,1,?)", (n_de, now // 60, now))
        for nid in online:
            telemetry(db, nid, 3.5 + nid)

    web_target = call("api_target_save", dict(name="业务服务", host="198.51.100.1", port=443,
                                              addresses=[dict(id="bk", name="备用", host="198.51.100.2", port=8443)]))["id"]
    ss_target = call("api_target_save", dict(uri=SS))["id"]
    call("api_target_save", dict(name="数据库", host="db.example.net", port=5432))

    graph = dict(version=2, routing_version=2,
                 vertices=[dict(id="v1", kind="entry", node_id=n_hk), dict(id="v2", kind="node", node_id=n_jp),
                           dict(id="d1", kind="direct", weight=3), dict(id="v3", kind="node", node_id=n_sg),
                           dict(id="t1", kind="target", target_id=web_target), dict(id="t2", kind="target", target_id=ss_target)],
                 edges=[["v1", "v2"], ["v2", "t1"], ["v1", "d1"], ["d1", "v3"], ["v3", "t1"], ["v3", "t2"], ["v2", "t2"]],
                 bindings={"d1/v3": {"to": {"v3": "alt"}}})
    main = call("api_topology_save", dict(name="主线路", graph=graph, failover_enabled=True))["id"]
    backup = fx.graph([n_de, n_us], web_target)
    call("api_topology_save", dict(name="欧洲备用", graph=backup))
    for _ in range(12):
        fx.tick()
        for nid in online[:3]:
            n = database.get_node(db, nid)
            if n["desired_generation"] != n["applied_generation"]:
                fx.ack(nid)
    call("api_external_save", dict(uri=TROJAN))
    configs = [c["id"] for c in call("api_configs")["configs"] if c.get("source") == "topology"]
    call("api_subscription_save", dict(name="手机客户端", configuration_ids=configs[:2]))
    with db:
        database.set_setting(db, "admin_user", "admin")
    return online[:3], n_kr, main


class DummyWS:
    closed = False

    async def close(self, **kw):
        self.closed = True


def session_for(hub, nid):
    session = {"id": f"demo{nid}", "wake": asyncio.Event(), "ws": DummyWS(), "paused": False, "quality_capable": True}

    async def send(message):
        kind = message.get("type")
        if kind == "quality_setup":
            session["quality_listener"] = {"id": message["id"], "port": message["port"]}
        elif kind == "quality_probe":
            async def reply():
                await asyncio.sleep(0.05)
                lost = random.random() < 0.04
                await hub.quality.result(nid, session, {
                    "id": message["id"], "status": "timeout" if lost else "ok",
                    "rtt": None if lost else round(random.gauss(18 + nid * 6, 2.5), 2), "resolved_ip": message["host"]})
            asyncio.ensure_future(reply())
        elif kind == "query":
            pending = hub.queries.get((nid, message["id"]))
            query = message["query"]
            data = {"sampled_at": int(time.time() * 1000), "kind": query["kind"], "note": "演示数据"}
            if pending:
                pending[1].set_result({"type": "query_result", "id": message["id"], "data": data, "session": session["id"]})

    session["send"] = send
    return session


async def simulate(fx, hub, online, faulty):
    """Keep sample nodes alive and confirm whatever the panel asks them to apply."""
    db = fx.db
    while True:
        now = int(time.time())
        try:
            with db:
                for nid in online + [faulty]:
                    try:
                        n = database.get_node(db, nid)
                    except Exception:
                        continue
                    node_state.record_report(db, n, now, "ok" if nid != faulty else "error", n["err"], 0)
                    db.execute("UPDATE nodes SET last_seen=?,first_seen=COALESCE(first_seen,?) WHERE id=?", (now, now, nid))
                    telemetry(db, nid, round(random.uniform(2, 18), 1))
            for nid in online:
                try:
                    n = database.get_node(db, nid)
                    if n["desired_generation"] != n["applied_generation"] and not n["deleting"]:
                        fx.ack(nid)
                except Exception:
                    continue
        except sqlite3.OperationalError:
            pass
        await asyncio.sleep(2)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--web", help="directory with the built panel (default: server/web)")
    parser.add_argument("--fresh", action="store_true", help="keep the first-login state and seed nothing")
    args = parser.parse_args()
    if args.web:
        settings.WEB_DIR = str(Path(args.web).resolve())
    # Pick up every rebuild without restarting.
    static_files.files = lambda: static_files.load(settings.WEB_DIR)
    fx = Fixture()
    fx.setUp()
    online, faulty = [], None
    if not args.fresh:
        online, faulty, _ = seed(fx)
        with fx.db:
            database.set_setting(fx.db, "admin_hash", security.hash_password("ictl-demo-pass"))
            database.set_setting(fx.db, "must_change", "0")
    app = transport.create_app()
    hub = app["hub"]

    async def no_wake(item):
        return None

    hub.passive_wake.send = no_wake  # never emit real UDP datagrams
    for nid in online:
        hub.sessions[nid] = session_for(hub, nid)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()
    print(f"PANEL http://{args.host}:{args.port}{database.setting(fx.db, 'panel_path')}/", flush=True)
    if online:
        await simulate(fx, hub, online, faulty)
    else:
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
