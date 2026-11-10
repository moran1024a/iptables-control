from collections import defaultdict
import json
import math
import time
import database
import errors
import link_quality
import probe_store
import agent_v3

INTERVAL = 10
RETENTION = 7 * 86400
HISTORY_STEPS = {900: 10, 3600: 60, 86400: 300, RETENTION: 1800}
PORT = 33445
PROTOCOLS = ("udp-echo-v1", "icmp-echo-v1")


def paths(conn):
    result = {}
    for r in conn.execute("SELECT id,graph FROM topologies WHERE deleted=0"):
        result["topologies", r["id"]] = list(
            dict.fromkeys(
                v["node_id"] for v in json.loads(r["graph"])["vertices"] if v.get("node_id")
            )
        )
    return result


def ensure_pair(conn, nodes, a, b, protocol="udp-echo-v1"):
    key = (
        a,
        b,
        nodes[a]["addr"],
        nodes[b]["addr"],
        nodes[b]["quality_port"] if protocol == "udp-echo-v1" else 0,
        protocol,
    )
    conn.execute(
        "INSERT OR IGNORE INTO quality_pairs(source,target,source_addr,target_addr,port,protocol) VALUES (?,?,?,?,?,?)",
        key,
    )
    return dict(
        conn.execute(
            "SELECT * FROM quality_pairs WHERE source=? AND target=? AND source_addr=? AND target_addr=? AND port=? AND protocol=?",
            key,
        ).fetchone()
    )


def stopped(nodes, pair):
    for nid in (pair["source"], pair["target"]):
        n = nodes.get(nid)
        if not n or n["deleting"]:
            return ("stopped", "节点已删除或正在删除")
        if (
            n["desired_mode"] != "active"
            or n["installed_agent"] == "passive"
            or n["manual_required"]
        ):
            return ("stopped", "仅已确认的主动节点参与探测")
        if n["installed_agent"] != "active" or "network_quality" not in (agent_v3.node_capabilities(n) or ()):
            return ("unsupported", "请升级两端主动 Agent")
        if not n["quality_port"]:
            return ("disabled", "节点已关闭质量探测")
    if pair["protocol"] == "icmp-echo-v1" and "network_quality_icmp" not in (agent_v3.node_capabilities(nodes[pair["source"]]) or ()):
        return ("unsupported", "请升级发起节点的主动 Agent 以支持 ICMP")
    return None


def continuous(a, b):
    if (
        a["status"] != "ok"
        or b["status"] != "ok"
        or a.get("resolved_ip", "") != b.get("resolved_ip", "")
        or a.get("path_key") != b.get("path_key")
    ):
        return False
    if a.get("epoch") or b.get("epoch"):
        return bool(a.get("epoch") == b.get("epoch") and b.get("previous_slot") == a["slot"])
    return 0 < b["slot"] - a["slot"] <= INTERVAL * 2


def aggregate(rows):
    good = [r["rtt"] for r in rows if r["status"] == "ok" and r["rtt"] is not None]
    lost = sum((r["status"] == "timeout" for r in rows))
    sent = len(good) + lost
    changes = [abs(b["rtt"] - a["rtt"]) for a, b in zip(rows, rows[1:]) if continuous(a, b)]
    ordered = sorted(good)
    return dict(
        samples=sent,
        lost=lost,
        loss=lost * 100 / sent if sent else None,
        mean=sum(good) / len(good) if good else None,
        peak=max(good) if good else None,
        p95=ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
        jitter=sum(changes) / len(changes) if changes else None,
    )


def history(conn, pair_id, seconds, now):
    pair = conn.execute("SELECT * FROM quality_pairs WHERE id=?", (pair_id,)).fetchone()
    rows = probe_store.pair_rows(conn, pair, now - max(seconds, 300) - 300) if pair else []
    return summarize(rows, seconds, now)


def summarize(rows, seconds, now):
    step = HISTORY_STEPS[seconds]
    rows = sorted((r for r in rows if r["slot"] <= now), key=lambda r: r["slot"])
    start = now - seconds
    buckets = defaultdict(list)
    for row in rows:
        if start <= row["slot"] <= now:
            buckets[row["slot"] // step * step].append(row)
    points, previous_sample = [], None
    for slot in range(start // step * step, now // step * step + 1, step):
        samples = buckets.get(slot, [])
        groups, group = [], []
        for row in samples:
            if group and not continuous(group[-1], row):
                groups.append(group)
                group = []
            group.append(row)
        groups.append(group)
        # Preserve ordinary failure boundaries without unbounded SVG growth.
        if len(groups) > 16:
            groups = [samples]
        for batch in groups:
            failures = [r for r in batch if r["status"] != "ok"]
            state = (failures or batch)[-1] if batch else None
            broken = any(not continuous(a, b) for a, b in zip(batch, batch[1:]))
            endpoints = {
                (r.get("path_key"), r.get("resolved_ip", "")) for r in batch if r["status"] == "ok"
            }
            points.append(
                dict(
                    time=batch[0]["slot"] if batch else max(start, slot),
                    end=min(batch[-1].get("last_slot", batch[-1]["slot"]) + INTERVAL * 2 if batch else slot + step, now + 1),
                    **aggregate(batch),
                    status=state["status"] if state else "missing",
                    error=state["error"] if state else "",
                    failures=sum(r.get("count", 1) for r in failures),
                    resolved_ip=next(
                        (r["resolved_ip"] for r in reversed(batch) if r["resolved_ip"]), ""
                    ),
                    first_slot=batch[0]["slot"] if batch else None,
                    last_slot=batch[-1].get("last_slot", batch[-1]["slot"]) if batch else None,
                    gap=bool(failures) or broken,
                    endpoint_change=len(endpoints) > 1,
                    connect=bool(
                        batch and previous_sample and continuous(previous_sample, batch[0])
                    ),
                    path_key=batch[-1].get("path_key") if batch else None,
                    path_label=batch[-1].get("path_label", "") if batch else "",
                    interval=(
                        (batch[-1]["slot"] - batch[-1]["previous_slot"])
                        if batch and batch[-1].get("previous_slot") is not None
                        else None
                    ),
                )
            )
            if batch:
                previous_sample = batch[-1]
    previous = None
    for point in points:
        if point["first_slot"] is not None:
            if previous is not None:
                previous["end"] = min(previous["end"], point["time"])
            previous = point
    recent = [r for r in rows if r["slot"] >= now - 300]
    latest = dict(rows[-1], slot=rows[-1].get("last_slot", rows[-1]["slot"])) if rows else None
    return dict(points=points, step=step, summary=aggregate(recent), latest=latest)


async def view(monitor, body):
    scope, ident = (body.get("scope"), body.get("id"))
    seconds = body.get("range", 900)
    if (
        scope != "topologies"
        or type(ident) is not int
        or type(seconds) is not int
        or (seconds not in HISTORY_STEPS)
    ):
        raise errors.UserError("无效的链路或时间范围")
    now = int(time.time())
    if scope == "topologies" and "source" not in body and ("target" not in body):
        return await link_quality.view(monitor, body, now)

    def read(conn):
        ids = paths(conn).get((scope, ident))
        if ids is None:
            raise errors.UserError("链路不存在", status=404)
        nodes = {r["id"]: dict(r) for r in conn.execute(database.ordered("nodes"))}
        ids = list(dict.fromkeys(ids))
        options = [
            dict(id=n["id"], name=n["name"], mode=n["desired_mode"])
            for n in nodes.values()
            if n["id"] in ids
        ]
        if len(ids) < 2:
            return dict(nodes=options, pair=None, now=now)
        a, b = (body.get("source", ids[0]), body.get("target", ids[1]))
        if type(a) is not int or type(b) is not int or a not in ids or (b not in ids) or (a == b):
            raise errors.UserError("请选择当前链路内的两个不同节点")
        series = []
        for protocol in PROTOCOLS:
            pair = ensure_pair(conn, nodes, a, b, protocol)
            why = monitor.unavailable(nodes, pair)
            series.append(
                dict(
                    protocol=protocol,
                    pair=pair,
                    state=why[0] if why else "collecting",
                    note=why[1] if why else "",
                    **history(conn, pair["id"], seconds, now),
                )
            )
        return dict(
            nodes=options,
            pair=series[0]["pair"],
            now=now,
            interval=INTERVAL,
            adjacent=(a, b) in set(zip(ids, ids[1:])),
            series=series,
        )

    result = await monitor.hub.database(read)
    if result["pair"]:
        p = result["pair"]
        monitor.extra[scope, ident, p["source"], p["target"]] = now + 60
    return result
