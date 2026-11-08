from pathlib import Path
import base64
import hashlib
import json
import re
import secrets
import time
import database
import errors
import forwarding
import node_sync
import node_addresses
import settings


def validate_capabilities(value):
    if (not isinstance(value, list) or len(value) > 32
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", item)
                   for item in value)):
        raise errors.UserError("无效的 Agent 能力列表")
    return list(dict.fromkeys(value))


def node_capabilities(node):
    """Invalid stored reports are unavailable, never a scheduler-wide failure."""
    try:
        return set(validate_capabilities(json.loads(node["capabilities"])))
    except (ValueError, TypeError, errors.UserError):
        return None


def fields(text):
    return dict((line.split("=", 1) for line in text.splitlines() if "=" in line))


def newer(candidate, current):

    def parts(value):
        m = re.fullmatch("(\\d+)\\.(\\d+)\\.(\\d+)(?:-rc\\.(\\d+))?", value or "")
        return (*map(int, m.groups()[:3]), int(m[4]) if m[4] else float("inf")) if m else None

    a, b = (parts(candidate), parts(current))
    return a is not None and (b is None or a > b)


def artifact(name):
    allowed = {
        "ictl-active-linux-amd64",
        "ictl-active-linux-arm64",
        "ictl-wake-linux-amd64",
        "ictl-wake-linux-arm64",
        "passive.sh",
        "manage.sh",
        "control.sh",
        "wake.sh",
    }
    if name not in allowed:
        raise errors.UserError("无效组件名称")
    root = Path(settings.BASE_DIR)
    candidates = [root / "assets" / name, root.parent / "dist" / name]
    if name in ("passive.sh", "manage.sh", "control.sh", "wake.sh"):
        candidates += [root / "agent" / name, root.parent / "agent" / name]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise errors.UserError("组件尚未构建：" + name, status=503)
    return (path, hashlib.sha256(path.read_bytes()).hexdigest())


def packet(conn, node, source="auto", op=None, status=None, *, observe=True, observe_only=False):
    status = status or {}
    data = {
        "t": str(int(time.time())),
        "proto": "2",
        "source": source,
        "phase": "begin" if source == "manual" else "poll",
        "op": op or secrets.token_hex(16),
        "agent": status.get("version", node["agent"] or ""),
        "au": "1" if status.get("auto_update", node["auto_update"]) else "0",
        "mode": "active",
        "state": status.get("state", "ok"),
        "err": str(status.get("error", ""))[:500],
        "generation": str(status.get("generation", node["applied_generation"])),
        "v": status.get("hash", node["reported_v"] or ""),
    }
    result = fields(
        node_sync.sync_report(
            conn,
            node,
            data,
            status.get("source_ip", ""),
            observe=observe,
            observe_only=observe_only,
        )
    )
    if observe and isinstance(status.get("forwarding"), dict):
        health = status["forwarding"]
        latest = database.get_node(conn, node["id"])
        if (health.get("state") in ("ok", "error", "unknown")
                and health.get("generation") == latest["applied_generation"]
                and health.get("hash") == latest["reported_v"]):
            conn.execute("UPDATE nodes SET runtime_state=?,runtime_error=?,runtime_at=? WHERE id=?",
                         (health["state"], str(health.get("error", ""))[:500], int(time.time()), node["id"]))
            if health["state"] == "error":
                conn.execute(
                    "INSERT INTO active_windows(node_id,win,level) VALUES (?,?,1) "
                    "ON CONFLICT(node_id,win) DO UPDATE SET level=1",
                    (node["id"], int(time.time()) // settings.ACTIVE_WINDOW),
                )
    if result.get("action") == "apply":
        result["rules"] = forwarding.node_rule_rows(conn, node)
        result["generation"] = int(result["generation"])
        result["hash"] = result.pop("v")
    return result


def acknowledge(conn, node, data, source="auto"):
    required = ("op", "generation", "hash")
    if not all((k in data for k in required)):
        raise errors.UserError("缺少配置确认字段")
    payload = {
        "t": str(int(time.time())),
        "proto": "2",
        "source": source,
        "phase": "ack",
        "op": str(data["op"]),
        "generation": str(data["generation"]),
        "v": str(data["hash"]),
        "mode": data.get("mode", "active"),
        "state": "ok" if data.get("ok") is True else "error",
        "err": str(data.get("error", ""))[:500],
        "au": "1" if node["auto_update"] else "0",
        "agent": data.get("version", settings.COMPONENTS["active_agent"]),
    }
    held = data.get("held_ports", [])
    if (
        not isinstance(held, list)
        or len(held) > 65535
        or any((type(p) is not int or not 1 <= p <= 65535 for p in held))
    ):
        raise errors.UserError("无效的保留端口")
    result = fields(node_sync.sync_report(conn, node, payload, ""))
    if result.get("result") == "confirmed":
        for port in held:
            conn.execute("INSERT OR IGNORE INTO retired_ports VALUES (?,?)", (node["id"], port))
    return result


def refresh_held_ports(conn, node, data):
    """A repeated complete snapshot repairs lost releases without clearing a newer wait."""
    held = data.get("held_ports")
    if not isinstance(held, list) or any((type(p) is not int or not 1 <= p <= 65535 for p in held)):
        raise errors.UserError("无效的保留端口")
    if (
        node["installed_agent"] != "active"
        or node["manual_required"]
        or node["desired_mode"] != "active"
        or (node["applied_generation"] != node["desired_generation"])
        or (data.get("generation") != node["applied_generation"])
        or (data.get("hash") != node["reported_v"])
    ):
        return
    conn.execute("DELETE FROM retired_ports WHERE node_id=?", (node["id"],))
    conn.executemany(
        "INSERT OR IGNORE INTO retired_ports VALUES (?,?)", ((node["id"], p) for p in held)
    )


def save_snapshot(conn, nid, kind, data):
    if kind not in ("inventory", "metrics", "ports") or not isinstance(data, dict):
        raise errors.UserError("无效的采集信息")
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 512 * 1024:
        raise errors.UserError("采集信息过大")
    now = int(time.time())
    conn.execute("INSERT OR IGNORE INTO node_snapshots(node_id) VALUES (?)", (nid,))
    conn.execute(
        f"UPDATE node_snapshots SET {kind}=?,{kind}_at=? WHERE node_id=?", (encoded, now, nid)
    )
    if kind == "inventory":
        node_addresses.refresh_pending(conn, nid)
    if kind == "metrics":
        values = {
            k: data[k] for k in ("cpu_percent", "agent_rss") if type(data.get(k)) in (int, float)
        }
        mem = data.get("memory", {})
        if mem.get("MemTotal") and type(mem.get("MemAvailable")) in (int, float):
            values["memory_used_percent"] = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])
        for table, size, keep in [("metric_windows", 60, 10080), ("metric_hours", 3600, 720)]:
            previous = conn.execute(
                f"SELECT data FROM {table} WHERE node_id=? AND window=?", (nid, now // size)
            ).fetchone()
            aggregate = (
                json.loads(previous[0])
                if previous
                else {"samples": 0, "first_at": now, "values": {}}
            )
            aggregate["samples"] += 1
            aggregate["last_at"] = now
            for key, value in values.items():
                stat = aggregate["values"].setdefault(
                    key, {"count": 0, "sum": 0, "min": value, "max": value}
                )
                stat["count"] += 1
                stat["sum"] += value
                stat["min"], stat["max"] = (min(stat["min"], value), max(stat["max"], value))
                stat["mean"] = stat["sum"] / stat["count"]
            conn.execute(
                f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)",
                (nid, now // size, json.dumps(aggregate)),
            )
            conn.execute(
                f"DELETE FROM {table} WHERE node_id=? AND window<?", (nid, now // size - keep)
            )


def accept_traffic(conn, nid, rows):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise errors.UserError("无效统计批次")
    keys = ("up", "down", "up_packets", "down_packets")
    acknowledgments = []
    for item in rows:
        if (
            not isinstance(item, dict)
            or type(item.get("rule_id")) is not int
            or item["rule_id"] < 1
            or (item.get("protocol") not in ("tcp", "udp"))
            or (not re.fullmatch("[a-f0-9]{32}", str(item.get("epoch", ""))))
        ):
            raise errors.UserError("无效计数器身份")
        values = []
        for key in keys:
            value = item.get(key)
            if (
                not isinstance(value, str)
                or not re.fullmatch("0|[1-9][0-9]{0,19}", value)
                or int(value) >= 2**64
            ):
                raise errors.UserError("无效累计值")
            values.append(int(value))
        ident = (nid, item["rule_id"], item["protocol"], item["epoch"])
        old = conn.execute(
            "SELECT * FROM traffic_epochs WHERE node_id=? AND rule_id=? AND protocol=? AND epoch=?",
            ident,
        ).fetchone()
        if old:
            values = [max(value, int(old[key])) for key, value in zip(keys, values)]
        quality = str(item.get("quality", "ok"))[:160]
        conn.execute(
            "INSERT OR REPLACE INTO traffic_epochs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*ident, *(str(v) for v in values), int(time.time()), quality),
        )
        acknowledgments.append(
            {
                "rule_id": item["rule_id"],
                "protocol": item["protocol"],
                "epoch": item["epoch"],
                **dict(zip(keys, map(str, values))),
            }
        )
    return acknowledgments


def telemetry(conn, nid):
    row = conn.execute("SELECT * FROM node_snapshots WHERE node_id=?", (nid,)).fetchone()
    out = dict(row) if row else {}
    for key in ("inventory", "metrics", "ports"):
        out[key] = json.loads(out.get(key) or "{}")
    totals = {}
    for row in conn.execute("SELECT * FROM traffic_epochs WHERE node_id=?", (nid,)):
        key = (row["rule_id"], row["protocol"])
        item = totals.setdefault(
            key,
            {
                "rule_id": key[0],
                "protocol": key[1],
                "up": 0,
                "down": 0,
                "up_packets": 0,
                "down_packets": 0,
                "updated": 0,
                "quality": "ok",
            },
        )
        for field in ("up", "down", "up_packets", "down_packets"):
            item[field] += int(row[field])
        item["updated"] = max(item["updated"], row["updated"])
        if row["quality"] != "ok":
            item["quality"] = row["quality"]
    for item in totals.values():
        for field in ("up", "down", "up_packets", "down_packets"):
            item[field] = str(item[field])
    out["traffic"] = list(totals.values())
    out["history"] = [
        {"time": r["window"] * 60, "data": json.loads(r["data"])}
        for r in conn.execute(
            "SELECT * FROM metric_windows WHERE node_id=? ORDER BY window DESC LIMIT 120", (nid,)
        )
    ][::-1]
    out["hourly_history"] = [
        {"time": r["window"] * 3600, "data": json.loads(r["data"])}
        for r in conn.execute(
            "SELECT * FROM metric_hours WHERE node_id=? ORDER BY window DESC LIMIT 720", (nid,)
        )
    ][::-1]
    return out


def manage(conn, node, data):
    """Authenticated v2 envelope: available to the small Bash installation manager."""
    now = int(time.time())
    if not str(data.get("t", "")).isdigit() or abs(now - int(data["t"])) > settings.MAX_SKEW:
        raise errors.UserError("节点时间不正确")
    action = data.get("manage")
    if action in ("prepare", "commit", "failed"):
        node_addresses.receive_network(conn, node["id"], data)
    if action == "asset":
        path, _ = artifact(data.get("name"))
        return path.read_bytes()
    if action == "info":
        return f"proto=2\nmode={node['desired_mode']}\ninstalled={node['installed_agent']}\ninstallation_id={node['installation_id'] or ''}\n"
    if action == "wake-assets":
        arch = data.get("arch")
        if arch not in ("amd64", "arm64"):
            raise errors.UserError("远程唤醒组件只支持 amd64 / arm64")
        name = f"ictl-wake-linux-{arch}"
        _, digest = artifact(name)
        _, script_digest = artifact("wake.sh")
        return f"proto=2\nwake_artifact={name}\nwake_sha256={digest}\nwake_script_sha256={script_digest}\n"
    if action == "prepare":
        target = data.get("target")
        if target not in ("active", "passive") or target != node["desired_mode"]:
            raise errors.UserError("目标类型与面板不一致")
        arch = data.get("arch")
        if target == "active" and arch not in ("amd64", "arm64"):
            raise errors.UserError("不支持的架构")
        name = f"ictl-active-linux-{arch}" if target == "active" else "passive.sh"
        _, digest = artifact(name)
        _, manager_digest = artifact("manage.sh")
        _, passive_digest = artifact("passive.sh")
        op = secrets.token_hex(16)
        result = packet(conn, node, "manual", op)
        config = "v=" + result["hash"] + "\n" + "\n".join(forwarding.node_rules(conn, node)) + "\n"
        conn.execute(
            "INSERT OR REPLACE INTO agent_switches VALUES (?,?,?,?,?,?,NULL,?)",
            (node["id"], op, target, result["generation"], result["hash"], config, now),
        )
        version = settings.COMPONENTS[target + "_agent"]
        return f"proto=2\nop={op}\nmode={target}\ngeneration={result['generation']}\nv={result['hash']}\nartifact={name}\nsha256={digest}\nmanager_sha256={manager_digest}\npassive_sha256={passive_digest}\nversion={version}\nconfig_b64={base64.b64encode(config.encode()).decode()}\n"
    record = conn.execute(
        "SELECT * FROM agent_switches WHERE node_id=? AND op=?", (node["id"], data.get("op", ""))
    ).fetchone()
    if not record:
        raise errors.UserError("找不到切换事务")
    if action == "abort":
        if record["result"] == "confirmed":
            raise errors.UserError("已确认的同步不能取消")
        conn.execute("DELETE FROM sync_ops WHERE node_id=? AND op=?", (node["id"], record["op"]))
        conn.execute(
            "UPDATE agent_switches SET result='aborted',updated=? WHERE node_id=?",
            (now, node["id"]),
        )
        if (
            node["state"] == "init"
            and node["applied_generation"] == node["desired_generation"]
            and (node["reported_v"] == forwarding.rules_version(forwarding.node_rules(conn, node)))
        ):
            conn.execute("UPDATE nodes SET state='ok' WHERE id=?", (node["id"],))
        return "proto=2\nresult=aborted\n"
    if record["result"] == "aborted":
        raise errors.UserError("同步已取消，请重新准备")
    imported = conn.execute(
        "SELECT 1 FROM migration_nodes WHERE node_id=?", (node["id"],)
    ).fetchone()
    if (
        imported
        and action in ("commit", "status")
        and record["generation"] < node["desired_generation"]
    ):
        return f"proto=2\nresult=changed\nmode={record['target']}\n"
    if action == "status":
        return f"proto=2\nresult={record['result'] or 'pending'}\nmode={record['target']}\n"
    if action not in ("commit", "failed") or data.get("target") != record["target"]:
        raise errors.UserError("无效切换确认")
    if record["result"] == "confirmed":
        current = (
            record["target"] == node["desired_mode"]
            and record["generation"] == node["desired_generation"]
        )
        return "proto=2\nresult=" + ("confirmed" if current else "changed") + "\n"
    conn.execute(
        "INSERT OR REPLACE INTO sync_ops(node_id,op,source,generation,mode,hash,rules,expires)\n        VALUES (?,?,'manual',?,?,?,?,?)",
        (
            node["id"],
            record["op"],
            record["generation"],
            record["target"],
            record["hash"],
            record["config"].partition("\n")[2].rstrip("\n"),
            now + 600,
        ),
    )
    result = acknowledge(
        conn,
        node,
        {
            "op": record["op"],
            "generation": record["generation"],
            "hash": record["hash"],
            "mode": record["target"],
            "ok": action == "commit",
            "error": (
                data.get("error", "本机交接失败，已尝试恢复原配置") if action == "failed" else ""
            ),
            "held_ports": json.loads(data.get("held_ports", "[]")),
            "version": data.get("agent", ""),
        },
        "manual",
    ).get("result")
    if result == "confirmed":
        conn.execute(
            "UPDATE nodes SET installed_agent=?,backend=?,connection_state='disconnected' WHERE id=?",
            (
                record["target"],
                "nftables" if record["target"] == "active" else "iptables",
                node["id"],
            ),
        )
        conn.execute(
            "UPDATE agent_switches SET result=?,updated=? WHERE node_id=?",
            (result, now, node["id"]),
        )
    return f"proto=2\nresult={result}\n"
