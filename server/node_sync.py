import re
import time
import agent_files
import database
import errors
import forwarding
import lifecycle
import node_state
import settings
import wake


def mutation_snapshot(conn):
    out = {}
    for n in conn.execute("SELECT * FROM nodes").fetchall():
        out[n["id"]] = (
            dict(n),
            forwarding.node_rule_rows(conn, n),
            {r[0] for r in conn.execute("SELECT port FROM retired_ports WHERE node_id=?", (n["id"],))},
        )
    return out


def track_mutation(method):
    """Keep generation changes and all affected configuration waits in the same transaction."""

    def wrapped(self, conn, *args, **kwargs):
        with database.atomic(conn):
            before = mutation_snapshot(conn)
            result = method(self, conn, *args, **kwargs)
            finish_mutation(conn, before)
            return result

    return wrapped


def finish_mutation(conn, before):
    notified = False
    for nid, (n, rows, retired) in mutation_snapshot(conn).items():
        old, old_rows, old_retired = before.get(nid, ({}, [], set()))
        key = lambda r: (r["id"], r["port"], r["host"], r["tport"])
        changed = (
            [key(r) for r in rows] != [key(r) for r in old_rows]
            or retired != old_retired
            or any((n[k] != old.get(k, n[k]) for k in ("key", "desired_mode", "deleting")))
        )
        if changed:
            conn.execute("UPDATE nodes SET desired_generation=desired_generation+1 WHERE id=?", (nid,))
            notified = True
        if changed or any((n[k] != old.get(k, n[k]) for k in ("wake_enabled", "wake_port"))):
            wake.queue(conn, database.get_node(conn, nid))
        for cid in {r["chain_id"] for r in old_rows + rows}:
            if [key(r) for r in old_rows if r["chain_id"] == cid] != [
                key(r) for r in rows if r["chain_id"] == cid
            ]:
                if conn.execute("SELECT 1 FROM chains WHERE id=?", (cid,)).fetchone():
                    lifecycle.mark_pending(conn, cid, [nid])
    return notified


def clear_pending(conn, node):
    conn.execute("DELETE FROM retired_ports WHERE node_id=?", (node["id"],))
    conn.execute("DELETE FROM chain_pending WHERE node_id=?", (node["id"],))
    lifecycle.finish_deleted_chains(conn)


def sync_report(conn, node, fields, source_ip, now=None, *, observe=True, observe_only=False):
    """Return authenticated plaintext. Network IO is handled outside the DB transaction."""
    now = int(time.time()) if now is None else now
    state = fields.get("state")
    if state not in ("init", "ok", "error") or not re.fullmatch("[0-9]{1,12}", fields.get("t", "")):
        raise errors.UserError("无效上报")
    skew = int(fields["t"]) - now
    err, agent = (fields.get("err", "")[:500], fields.get("agent", "")[:32])
    health = "skew" if abs(skew) > settings.MAX_SKEW else "error" if state == "error" else "ok"
    if health == "skew":
        conn.execute("UPDATE nodes SET skew=?,health=? WHERE id=?", (skew, health, node["id"]))
        return f"time={now}\n"
    proto = fields.get("proto") == "2"
    source, phase = (fields.get("source"), fields.get("phase"))
    if fields.get("agent_kind") == "passive" and node["desired_mode"] == "active":
        return "proto=2\naction=switch\nmode=active\n"
    if (
        proto
        and phase in ("poll", "begin")
        and (node["state"] == "error")
        and node_state.port_error(node["err"])
    ):
        health, err = ("error", node["err"])
    if proto and (source not in ("manual", "auto", "wake") or phase not in ("begin", "poll", "ack", "asset")):
        raise errors.UserError("无效同步阶段")
    if source == "wake" and phase != "ack" and (not wake.authorized(conn, node, fields)):
        return "proto=2\naction=pause\nmode=passive\n"
    au = {"0": 0, "1": 1}.get(fields.get("au"))
    if fields.get("get") == "agent":
        if (
            agent_files.AGENT
            and au
            and (
                not node["manual_required"]
                and node["desired_mode"] == "active"
                or (proto and source == "manual")
            )
        ):
            return agent_files.AGENT[2]
        raise errors.UserError("没有可用更新")
    if observe:
        node_state.record_report(conn, node, now, health, err, skew)
        conn.execute(
            "UPDATE nodes SET first_seen=COALESCE(first_seen,?),last_seen=?,src_ip=?,\n        agent=?,skew=?,health=?,auto_update=?,protocol_version=MAX(protocol_version,?) WHERE id=?",
            (now, now, source_ip, agent, skew, health, au, 2 if proto else 1, node["id"]),
        )
        if proto and fields.get("mode") in ("active", "passive"):
            conn.execute("UPDATE nodes SET reported_mode=? WHERE id=?", (fields["mode"], node["id"]))
    if observe_only:
        return "proto=2\n"
    lines = forwarding.node_rules(conn, node)
    want = forwarding.rules_version(lines)
    update = (
        f"update={agent_files.AGENT[0]} {agent_files.AGENT[1]}\n"
        if source != "wake" and agent_files.agent_update(agent, au) == "pending"
        else ""
    )
    if not proto:
        if node["protocol_version"] >= 2 or node["manual_required"] or node["desired_mode"] != "active":
            return "same\n" + update
        v = fields.get("v", "")
        conn.execute("UPDATE nodes SET reported_v=?,state=?,err=? WHERE id=?", (v, state, err, node["id"]))
        if v == want and state == "ok":
            conn.execute("UPDATE nodes SET applied_generation=desired_generation WHERE id=?", (node["id"],))
            clear_pending(conn, node)
            if node["deleting"]:
                lifecycle.delete_node(conn, node["id"])
                return "same\n"
        return (
            "same\n" if v == want and state != "init" else "\n".join([f"v={want}"] + lines) + "\n"
        ) + update
    if source == "auto" and (node["desired_mode"] == "passive" or node["manual_required"]):
        return "proto=2\naction=pause\nmode=passive\n"
    opid = fields.get("op", "")
    if not re.fullmatch("[a-f0-9]{32}", opid):
        raise errors.UserError("无效操作编号")
    op = conn.execute("SELECT * FROM sync_ops WHERE node_id=?", (node["id"],)).fetchone()
    if phase == "ack":
        if (
            not op
            or op["op"] != opid
            or op["source"] != source
            or (op["expires"] < now)
            or (fields.get("generation") != str(op["generation"]))
            or (fields.get("mode") != op["mode"])
            or (fields.get("v") != op["hash"])
        ):
            return "proto=2\nresult=expired\n"
        if op["result"]:
            return "proto=2\nresult=" + op["result"] + "\n"
        current = (
            op["generation"] == node["desired_generation"]
            and op["mode"] == node["desired_mode"]
            and (op["hash"] == want)
        )
        result = "failed" if state != "ok" else "confirmed" if current else "changed"
        conn.execute("UPDATE sync_ops SET result=? WHERE node_id=?", (result, node["id"]))
        if result != "changed" or not node_state.port_error(node["err"]):
            conn.execute("UPDATE nodes SET state=?,err=? WHERE id=?", (state, err, node["id"]))
        if state == "ok":
            conn.execute(
                "UPDATE nodes SET reported_v=?,reported_mode=?,applied_generation=?,last_success=? WHERE id=?",
                (op["hash"], op["mode"], op["generation"], now, node["id"]),
            )
        lifecycle.add_event(
            conn,
            node["id"],
            {"manual": "手动", "auto": "自动", "wake": "远程唤醒"}[source]
            + "同步："
            + {"confirmed": "确认成功", "changed": "已应用旧快照，仍有新变更", "failed": "失败：" + err}[
                result
            ],
            now,
        )
        wake.receipt(conn, node, op["generation"], result, err)
        if source == "wake" and result == "confirmed":
            conn.execute(
                "INSERT INTO windows(node_id,win,level,manual_count,last_activity) VALUES (?,?,2,1,?)\n                ON CONFLICT(node_id,win) DO UPDATE SET manual_count=manual_count+1,last_activity=excluded.last_activity",
                (node["id"], now // settings.WINDOW, now),
            )
        if result == "confirmed":
            if source == "manual":
                conn.execute("UPDATE nodes SET manual_required=0 WHERE id=?", (node["id"],))
            clear_pending(conn, node)
            if node["deleting"]:
                conn.execute(
                    "INSERT OR REPLACE INTO deleted_receipts VALUES (?,?,?,?,?,?)",
                    (node["key"], opid, op["generation"], op["mode"], op["hash"], now + 600),
                )
                lifecycle.delete_node(conn, node["id"])
                return "proto=2\nresult=deleted\n"
        return f"proto=2\nresult={result}\n"
    if (
        phase not in ("begin", "poll")
        or (source in ("manual", "wake") and phase != "begin")
        or (source == "auto" and phase != "poll")
    ):
        raise errors.UserError("无效同步阶段")
    if (
        source in ("auto", "wake")
        and op
        and (op["source"] == "manual")
        and (not op["result"])
        and (op["expires"] >= now)
    ):
        return "proto=2\naction=busy\n"
    if (
        source == "auto"
        and op
        and (op["result"] == "failed")
        and node_state.port_error(node["err"])
        and (op["generation"] == node["desired_generation"])
        and (op["mode"] == node["desired_mode"])
        and (op["hash"] == want)
    ):
        return f"proto=2\naction=blocked\ngeneration={op['generation']}\nerr={node['err']}\n" + update
    if op and op["op"] == opid:
        if op["expires"] < now or op["source"] != source:
            return "proto=2\nresult=expired\n"
    else:
        if source == "auto" and (
            node["applied_generation"] == node["desired_generation"]
            and fields.get("generation") == str(node["desired_generation"])
            and (fields.get("v") == want)
            and (state == "ok")
            and (not node_state.port_error(node["err"]))
        ):
            conn.execute("UPDATE nodes SET state='ok',err='' WHERE id=?", (node["id"],))
            return "proto=2\naction=same\n" + update
        op = {
            "op": opid,
            "source": source,
            "generation": node["desired_generation"],
            "mode": node["desired_mode"],
            "hash": want,
            "rules": "\n".join(lines),
            "expires": now + 600,
        }
        conn.execute(
            "INSERT OR REPLACE INTO sync_ops(node_id,op,source,generation,mode,hash,rules,expires)\n            VALUES (?,?,?,?,?,?,?,?)",
            (node["id"], opid, source, op["generation"], op["mode"], want, op["rules"], op["expires"]),
        )
        if source == "manual":
            conn.execute(
                "INSERT INTO windows(node_id,win,level,manual_count,last_activity) VALUES (?,?,2,1,?)\n                ON CONFLICT(node_id,win) DO UPDATE SET manual_count=manual_count+1,last_activity=excluded.last_activity",
                (node["id"], now // settings.WINDOW, now),
            )
            conn.execute(
                "UPDATE nodes SET last_manual=?,state=CASE WHEN state='error' THEN state ELSE 'init' END WHERE id=?",
                (now, node["id"]),
            )
            lifecycle.add_event(conn, node["id"], "手动同步开始", now)
    return (
        f"proto=2\naction=apply\nop={opid}\ngeneration={op['generation']}\nmode={op['mode']}\nv={op['hash']}\n{op['rules']}\n"
        + update
    )
