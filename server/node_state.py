import database
import forwarding
import lifecycle
import settings
import wake


def port_error(err):
    return (err or "").startswith(("端口冲突：", "端口检查失败："))


def node_status(conn, node, now):
    """new / offline / skew / syncing / synced / error / deleting"""
    if node["deleting"]:
        return "deleting"
    if node["state"] == "error" and port_error(node["err"]):
        return "error"
    if node["desired_mode"] == "passive" and node["wake_enabled"] and (not node["manual_required"]):
        job = wake.summary(conn, node)
        if job and job["generation"] == node["desired_generation"]:
            if job["state"] in ("timeout", "failed"):
                return "error"
            if job["state"] == "waiting":
                return "syncing"
    if node["manual_required"] or node["desired_mode"] == "passive":
        return (
            "syncing"
            if node["manual_required"]
            or node["applied_generation"] != node["desired_generation"]
            or node["state"] != "ok"
            else "synced"
        )
    if not node["first_seen"]:
        return "new"
    if node["last_seen"] is None or now - node["last_seen"] > (
        30 if node["installed_agent"] == "active" else settings.OFFLINE_AFTER
    ):
        return "offline"
    if node["skew"] is not None and abs(node["skew"]) > settings.MAX_SKEW:
        return "skew"
    if node["state"] == "error":
        return "error"
    if (
        node["applied_generation"] != node["desired_generation"]
        or node["reported_v"] != forwarding.rules_version(forwarding.node_rules(conn, node))
        or node["state"] == "init"
    ):
        return "syncing"
    return "synced" if node["state"] == "ok" else "error"


def node_bar(conn, node, now):
    """最近 BAR_WINDOWS 个时间窗口的状态：ok / fault / lost / none。"""
    if node["desired_mode"] == "passive":
        start = now // settings.WINDOW - settings.BAR_WINDOWS + 1
        activity = {
            r["win"]: dict(r)
            for r in conn.execute(
                "SELECT win,manual_count,last_activity FROM windows WHERE node_id=? AND win>=?",
                (node["id"], start),
            )
        }
        return {
            "start": start * settings.WINDOW,
            "window": settings.WINDOW,
            "levels": [
                "ok" if activity.get(w, {}).get("manual_count", 0) else "white"
                for w in range(start, now // settings.WINDOW + 1)
            ],
            "counts": [
                activity.get(w, {}).get("manual_count", 0) for w in range(start, now // settings.WINDOW + 1)
            ],
            "times": [
                activity.get(w, {}).get("last_activity") for w in range(start, now // settings.WINDOW + 1)
            ],
        }
    cur = now // settings.ACTIVE_WINDOW
    start = cur - settings.BAR_WINDOWS + 1
    levels = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT win, level FROM active_windows WHERE node_id = ? AND win >= ?", (node["id"], start)
        )
    }
    recording_since = int(database.setting(conn, "active_window_start", "0"))
    first = node["first_seen"]
    bar = []
    for w in range(start, cur + 1):
        if w in levels:
            bar.append("ok" if levels[w] == 2 else "fault")
        elif (
            not first
            or (w + 1) * settings.ACTIVE_WINDOW <= first
            or w == cur
            or (w * settings.ACTIVE_WINDOW < recording_since)
        ):
            bar.append("none")
        else:
            bar.append("lost")
    return {"start": start * settings.ACTIVE_WINDOW, "window": settings.ACTIVE_WINDOW, "levels": bar}


def duration(sec):
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    if sec < 86400:
        return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"
    return f"{sec // 86400} 天 {sec % 86400 // 3600} 小时"


def record_report(conn, node, now, health, err, skew):
    """记录一次上报：状态窗口 + 状态变化事件。"""
    if not node["first_seen"]:
        lifecycle.add_event(conn, node["id"], "首次连接", now)
    elif (
        node["desired_mode"] == "active"
        and node["last_seen"] is not None
        and (now - node["last_seen"] > settings.OFFLINE_AFTER)
    ):
        lifecycle.add_event(conn, node["id"], f"失联 {duration(now - node['last_seen'])} 后恢复", now)
    prev = node["health"] or "ok"
    if health != prev:
        text = f"{settings.HEALTH[prev]} → {settings.HEALTH[health]}"
        if health == "error" and err:
            text += f"：{err}"
        elif health == "skew":
            text += f"（{skew:+d} 秒）"
        lifecycle.add_event(conn, node["id"], text, now)
    conn.execute(
        "INSERT INTO active_windows(node_id,win,level) VALUES (?, ?, ?)\n           ON CONFLICT (node_id, win) DO UPDATE SET level = MIN(level, excluded.level)",
        (node["id"], now // settings.ACTIVE_WINDOW, 2 if health == "ok" else 1),
    )


def upstream_nodes(conn, node_id):
    """以该节点为下一跳的节点名称。"""
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT n.name FROM hops h\n           JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1\n           JOIN nodes n ON n.id = h.node_id\n           WHERE h2.node_id = ? ORDER BY n.id",
            (node_id,),
        )
    ]
