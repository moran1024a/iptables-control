import json
import re
import time
import agent_files
import agent_v3
import database
import errors
import forwarding
import lifecycle
import network_quality
import node_state
import node_sync
import pools
import security
import settings
import subscriptions
import target_config
import validation
import wake


class Resources:
    """Resource use cases; no HTTP transport or authentication dependency."""

    def __init__(self, query=None):
        self.query = query or {}

    def node_brief(self, conn, n, now):
        return {
            "id": n["id"],
            "kind": n["kind"],
            "name": n["name"],
            "note": n["note"],
            "addr": n["addr"],
            "ports": n["ports"],
            "group_id": n["group_id"],
            "status": node_state.node_status(conn, n, now),
            "last_seen": n["last_seen"],
            "desired_mode": n["desired_mode"],
            "reported_mode": n["reported_mode"],
            "manual_required": bool(n["manual_required"]),
            "protocol_version": n["protocol_version"],
            "desired_generation": n["desired_generation"],
            "applied_generation": n["applied_generation"],
            "err": n["err"],
            "installed_agent": n["installed_agent"],
            "backend": n["backend"],
            "connection_state": n["connection_state"],
            "quality_port": n["quality_port"],
            "wake_enabled": bool(n["wake_enabled"]),
            "wake_port": n["wake_port"],
            "wake": wake.summary(conn, n),
        }

    def api_nodes(self, conn, body):
        now = int(time.time())
        kind = self.query.get("kind", ["forward"])[0]
        table = database.node_table(kind)
        nodes = [
            {**self.node_brief(conn, n, now), "bar": node_state.node_bar(conn, n, now)}
            for n in conn.execute(database.ordered(table)).fetchall()
        ]
        return {"now": now, "kind": kind, "groups": database.get_groups(conn, table), "nodes": nodes}

    @node_sync.track_mutation
    def api_node_add(self, conn, body):
        name = validation.check_remark(body.get("name"), "name", "名称")
        note = validation.check_note(body.get("note"))
        addr = validation.check_host(body.get("addr"), "addr")
        ports = validation.format_ports(validation.parse_ports(body.get("ports", "10000-65535")))
        if "heartbeat_interval" in body or "tunnel_heartbeat" in body:
            raise errors.UserError("新版只支持主动／被动同步模式")
        kind = body.get("kind", "forward")
        table = database.node_table(kind)
        group_id = database.check_group(conn, table, body.get("group_id"))
        node_id = int(database.setting(conn, "next_node_id"))
        database.set_setting(conn, "next_node_id", str(node_id + 1))
        cur = conn.execute(
            f"INSERT INTO {table} (id, name, note, addr, key, ports, group_id, kind, sort)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM {table}))",
            (node_id, name, note, addr, security.new_key(), ports, group_id, kind),
        )
        lifecycle.add_event(conn, cur.lastrowid, f"创建节点，入站地址 {addr}")
        mode = body.get("desired_mode", "active")
        if mode not in ("active", "passive"):
            raise errors.UserError("模式必须为主动或被动", "desired_mode")
        if mode == "passive":
            conn.execute(
                "UPDATE nodes SET desired_mode='passive',manual_required=1 WHERE id=?", (cur.lastrowid,)
            )
        quality_port = (
            validation.check_port(body.get("quality_port", network_quality.PORT), "quality_port")
            if body.get("quality_port", network_quality.PORT) != 0
            else 0
        )
        conn.execute("UPDATE nodes SET quality_port=? WHERE id=?", (quality_port, cur.lastrowid))
        self.set_wake(conn, body, database.get_node(conn, cur.lastrowid), mode)
        return {"id": cur.lastrowid}

    def set_wake(self, conn, body, node, mode):
        enabled = body.get("wake_enabled", bool(node["wake_enabled"]))
        if type(enabled) is not bool:
            raise errors.UserError("远程唤醒开关无效", "wake_enabled")
        port = body.get("wake_port", node["wake_port"])
        port = validation.check_port(port, "wake_port") if port != 0 else 0
        if mode != "passive":
            enabled = False
        if enabled:
            if not port:
                raise errors.UserError("启用远程唤醒需要填写端口", "wake_port")
            if port in forwarding.reserved(conn, node["id"]):
                raise errors.UserError("唤醒端口已被链路占用或等待清理", "wake_port")
            if conn.execute(
                "SELECT 1 FROM hops WHERE node_id=? AND port=? UNION ALL SELECT 1 FROM retired_ports WHERE node_id=? AND port=?",
                (node["id"], port, node["id"], port),
            ).fetchone():
                raise errors.UserError("唤醒端口已被转发配置占用或正在等待释放", "wake_port")
        conn.execute(
            "UPDATE nodes SET wake_enabled=?,wake_port=? WHERE id=?", (int(enabled), port, node["id"])
        )

    def api_node_get(self, conn, body, node_id):
        now = int(time.time())
        n = database.get_node(conn, node_id)
        chain_sort = {r["id"]: i for i, r in enumerate(conn.execute(database.ordered("chains")))}
        rules = sorted(forwarding.node_rule_rows(conn, n), key=lambda r: (chain_sort[r["chain_id"]], r["id"]))
        chains = conn.execute(
            database.ordered("chains", "WHERE t.id IN (SELECT chain_id FROM hops WHERE node_id = ?)"),
            (node_id,),
        ).fetchall()
        group = conn.execute("SELECT name FROM item_groups WHERE id = ?", (n["group_id"],)).fetchone()
        events = conn.execute(
            "SELECT ts, text FROM events WHERE node_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (node_id, settings.EVENTS_SHOWN),
        ).fetchall()
        return {
            "now": now,
            "sync_path": database.setting(conn, "sync_path"),
            "public_url": database.setting(conn, "public_url", ""),
            "agent_url": settings.AGENT_URL,
            "version": settings.VERSION,
            "forward_version": settings.COMPONENTS.get(
                n["installed_agent"] + "_agent", settings.FORWARD_VERSION
            ),
            "installer_url": f"https://github.com/{settings.REPO}/releases/download/v{settings.VERSION}/install-agent.sh",
            "telemetry": agent_v3.telemetry(conn, node_id),
            "node": {
                **self.node_brief(conn, n, now),
                "bar": node_state.node_bar(conn, n, now),
                "key": n["key"],
                "first_seen": n["first_seen"],
                "src_ip": n["src_ip"],
                "skew": n["skew"],
                "agent": n["agent"],
                "reported_v": n["reported_v"],
                "want_v": forwarding.rules_version(forwarding.node_rules(conn, n)),
                "state": n["state"],
                "err": n["err"],
                "group": group["name"],
                "agent_update": agent_files.agent_update(n["agent"], n["auto_update"])
                if n["installed_agent"] == "legacy"
                else "pending"
                if agent_v3.newer(settings.COMPONENTS[n["installed_agent"] + "_agent"], n["agent"])
                else "same",
            },
            "rules": rules,
            "chains": [{"id": c["id"], "remark": c["remark"]} for c in chains],
            "events": [dict(e) for e in events],
        }

    @node_sync.track_mutation
    def api_node_edit(self, conn, body, node_id):
        n = database.get_node(conn, node_id)
        table = database.node_table(n["kind"])
        if "heartbeat_interval" in body or "tunnel_heartbeat" in body:
            raise errors.UserError("新版只支持主动／被动同步模式")
        if "kind" in body and body["kind"] != n["kind"]:
            raise errors.UserError("节点类型不能修改", "kind")
        name = validation.check_remark(body["name"], "name", "名称") if "name" in body else n["name"]
        note = validation.check_note(body["note"]) if "note" in body else n["note"]
        addr = validation.check_host(body["addr"], "addr") if "addr" in body else n["addr"]
        ranges = (
            validation.parse_ports(body["ports"]) if "ports" in body else validation.parse_ports(n["ports"])
        )
        group_id = (
            database.check_group(conn, table, body["group_id"]) if "group_id" in body else n["group_id"]
        )
        outside = [
            r
            for r in conn.execute(
                "SELECT c.remark, h.port FROM hops h JOIN chains c ON c.id = h.chain_id\n               WHERE h.node_id = ? ORDER BY h.port",
                (node_id,),
            )
            if not validation.in_ports(r["port"], ranges)
        ]
        for r in conn.execute(
            "SELECT '等待移除' AS remark, port FROM retired_ports WHERE node_id = ?", (node_id,)
        ):
            if not validation.in_ports(r["port"], ranges):
                outside.append(r)
        if outside:
            used = "、".join((f"「{r['remark']}」{r['port']}" for r in outside))
            raise errors.UserError(
                f"以下转发使用的端口不在新的允许范围内：{used}。请先修改这些转发。", "ports"
            )
        mode = body.get("desired_mode", n["desired_mode"])
        extra_ports = [p for p in forwarding.reserved(conn, node_id) if not validation.in_ports(p, ranges)]
        if extra_ports:
            raise errors.UserError(
                "链路使用或待清理的端口不在新范围内：" + ",".join(map(str, extra_ports)), "ports"
            )
        if mode not in ("active", "passive"):
            raise errors.UserError("模式必须为主动或被动", "desired_mode")
        if mode != n["desired_mode"] and mode == "passive":
            refs = [
                p["name"] for p in conn.execute("SELECT * FROM pools") if node_id in json.loads(p["members"])
            ]
            if refs:
                raise errors.UserError("请先将节点从池中移除：" + "、".join(refs), "desired_mode")
        if mode != n["desired_mode"] and n["first_seen"] and (n["protocol_version"] < 2):
            raise errors.UserError("请先升级节点 Agent 并完成一次同步，再切换模式", "desired_mode")
        quality_port = body.get("quality_port", n["quality_port"])
        quality_port = validation.check_port(quality_port, "quality_port") if quality_port != 0 else 0
        if (
            quality_port
            and quality_port != n["quality_port"]
            and (quality_port in forwarding.reserved(conn, node_id))
        ):
            raise errors.UserError("探测端口已被链路占用或等待清理", "quality_port")
        conn.execute("UPDATE nodes SET quality_port=? WHERE id=?", (quality_port, node_id))
        self.set_wake(conn, body, n, mode)
        key = None
        if mode != n["desired_mode"]:
            conn.execute("UPDATE nodes SET desired_mode=?,manual_required=1 WHERE id=?", (mode, node_id))
            lifecycle.add_event(conn, node_id, "同步模式已修改，等待本机手动同步确认")
        conn.execute(
            f"UPDATE {table} SET name = ?, note = ?, addr = ?, ports = ? WHERE id = ?",
            (name, note, addr, validation.format_ports(ranges), node_id),
        )
        if group_id != n["group_id"]:
            database.move_group(conn, table, node_id, group_id)
        if addr != n["addr"]:
            key = security.new_key()
            conn.execute(f"UPDATE {table} SET key = ? WHERE id = ?", (key, node_id))
            lifecycle.add_event(conn, node_id, f"入站地址 {n['addr']} → {addr}，已重新生成密钥")
            for r in conn.execute(
                "SELECT h.chain_id, h.node_id FROM hops h\n                       JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1\n                       WHERE h2.node_id = ?",
                (node_id,),
            ).fetchall():
                lifecycle.mark_pending(conn, r["chain_id"], [r["node_id"]])
            pools.refresh_links(conn, node=node_id)
        return {"key": key, "upstream": node_state.upstream_nodes(conn, node_id) if key else []}

    @node_sync.track_mutation
    def api_node_rekey(self, conn, body, node_id):
        table = database.node_table(database.get_node(conn, node_id)["kind"])
        key = security.new_key()
        conn.execute(f"UPDATE {table} SET key = ? WHERE id = ?", (key, node_id))
        lifecycle.add_event(conn, node_id, "重置密钥")
        return {"key": key}

    @node_sync.track_mutation
    def api_node_del(self, conn, body, node_id):
        n = database.get_node(conn, node_id)
        table = database.node_table(n["kind"])
        pool_refs = pools.node_references(conn, node_id)
        if pool_refs:
            raise errors.UserError("节点仍被引用：" + "、".join(pool_refs), status=409)
        used = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT c.remark FROM hops h JOIN chains c ON c.id = h.chain_id\n               WHERE h.node_id = ? ORDER BY c.id",
                (node_id,),
            )
        ]
        for r in conn.execute("SELECT name, node_ids FROM routes"):
            if node_id in json.loads(r["node_ids"]):
                used.append("链路：" + r["name"])
        if used:
            raise errors.UserError(f"节点仍被转发使用：{'、'.join(used)}。请先删除或修改这些转发。")
        force = self.query.get("force") == ["1"]
        if force or not n["first_seen"]:
            lifecycle.delete_node(conn, node_id)
            return {"deleted": True}
        if not n["deleting"]:
            conn.execute(f"UPDATE {table} SET deleting = 1 WHERE id = ?", (node_id,))
            lifecycle.add_event(conn, node_id, "开始删除，等待节点清空规则")
        return {"deleted": False}

    def api_chains(self, conn, body):
        now = int(time.time())
        nodes = {n["id"]: n for n in conn.execute("SELECT * FROM nodes").fetchall()}
        status = {nid: node_state.node_status(conn, n, now) for nid, n in nodes.items()}
        node_info = lambda nid: {
            "node_id": nid,
            "name": nodes[nid]["name"],
            "status": status[nid],
            "desired_mode": nodes[nid]["desired_mode"],
            "err": nodes[nid]["err"],
            "wake_enabled": bool(nodes[nid]["wake_enabled"]),
            "wake": wake.summary(conn, nodes[nid]),
        }
        chains = []
        for c in conn.execute(database.ordered("chains")).fetchall():
            hops = [
                {**node_info(h["node_id"]), "addr": nodes[h["node_id"]]["addr"], "port": h["port"]}
                for h in conn.execute("SELECT * FROM hops WHERE chain_id = ? ORDER BY seq", (c["id"],))
            ]
            pending = [
                node_info(r["node_id"])
                for r in conn.execute(
                    "SELECT node_id FROM chain_pending WHERE chain_id = ? ORDER BY node_id", (c["id"],)
                )
            ]
            target = database.get_target(conn, c["target_id"])
            chains.append(
                {
                    "id": c["id"],
                    "remark": c["remark"],
                    "target_host": target["host"],
                    "target_port": target["port"],
                    "deleting": bool(c["deleting"]),
                    "group_id": c["group_id"],
                    "hops": hops,
                    "pending": pending,
                }
            )
        node_list = [
            {"id": n["id"], "name": n["name"], "group_id": n["group_id"]}
            for n in conn.execute(database.ordered("nodes")).fetchall()
        ]
        return {
            "chains": chains,
            "groups": database.get_groups(conn, "chains"),
            "nodes": node_list,
            "node_groups": database.get_groups(conn, "nodes"),
        }

    @node_sync.track_mutation
    def api_chain_del(self, conn, body, chain_id):
        chain = database.get_chain(conn, chain_id)
        link = conn.execute("SELECT id FROM links WHERE chain_id=? AND managed=1", (chain_id,)).fetchone()
        if link:
            return pools.delete_link(conn, link["id"])
        force = self.query.get("force") == ["1"]
        if force:
            for h in conn.execute(
                "SELECT node_id, port FROM hops WHERE chain_id = ?", (chain_id,)
            ).fetchall():
                forwarding.reserve_removed(conn, h["node_id"], h["port"])
            lifecycle.delete_chain(conn, chain_id)
            return {"deleted": True}
        conn.execute(
            "DELETE FROM chain_pending WHERE chain_id = ? AND node_id NOT IN\n                        (SELECT id FROM nodes WHERE first_seen IS NOT NULL)",
            (chain_id,),
        )
        if not chain["deleting"]:
            conn.execute("UPDATE chains SET deleting = 1 WHERE id = ?", (chain_id,))
            lifecycle.mark_pending(
                conn,
                chain_id,
                [
                    r[0]
                    for r in conn.execute(
                        "SELECT h.node_id FROM hops h JOIN nodes n ON n.id = h.node_id\n                   WHERE h.chain_id = ? AND n.first_seen IS NOT NULL",
                        (chain_id,),
                    )
                ],
            )
        lifecycle.finish_deleted_chains(conn)
        deleted = not conn.execute("SELECT 1 FROM chains WHERE id = ?", (chain_id,)).fetchone()
        return {"deleted": deleted}

    def api_subscriptions(self, conn, body):
        return subscriptions.listing(conn)

    def api_subscription_save(self, conn, body, ident=None):
        return subscriptions.save(conn, body, ident)

    def api_subscription_delete(self, conn, body, ident):
        pools.get(conn, "subscriptions", ident)
        conn.execute("DELETE FROM subscriptions WHERE id=?", (ident,))
        return {"deleted": True}

    def api_order(self, conn, body, kind):
        """保存顺序：groups 为分组顺序，ids 为全部对象按显示顺序排列（只能在组内调整，不改变所属分组）。"""
        groups, ids = (body.get("groups"), body.get("ids"))
        existing_groups = {r[0] for r in conn.execute("SELECT id FROM item_groups WHERE kind = ?", (kind,))}
        existing = {r[0] for r in conn.execute(f"SELECT id FROM {kind}")}
        if (
            not isinstance(groups, list)
            or not isinstance(ids, list)
            or any((type(x) is not int for x in groups + ids))
            or (len(groups) != len(existing_groups))
            or (set(groups) != existing_groups)
            or (len(ids) != len(existing))
            or (set(ids) != existing)
        ):
            raise errors.UserError("列表已变化或排序范围不匹配，请刷新后重试")
        for i, gid in enumerate(groups):
            conn.execute("UPDATE item_groups SET sort = ? WHERE id = ?", (i + 1, gid))
        for i, item_id in enumerate(ids):
            conn.execute(f"UPDATE {kind} SET sort = ? WHERE id = ?", (i + 1, item_id))
        return {}

    def get_group(self, conn, group_id):
        g = conn.execute("SELECT * FROM item_groups WHERE id = ?", (group_id,)).fetchone()
        if not g:
            raise errors.UserError("分组不存在", status=404)
        return g

    def api_group_add(self, conn, body):
        kind = body.get("kind")
        if kind not in settings.KINDS:
            raise errors.UserError("请求格式错误")
        name = validation.check_remark(body.get("name"), "name", "分组名称")
        cur = conn.execute(
            "INSERT INTO item_groups (kind, name, sort)\n                   VALUES (?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM item_groups WHERE kind = ?))",
            (kind, name, kind),
        )
        return {"id": cur.lastrowid}

    def api_group_edit(self, conn, body, group_id):
        self.get_group(conn, group_id)
        name = validation.check_remark(body.get("name"), "name", "分组名称")
        conn.execute("UPDATE item_groups SET name = ? WHERE id = ?", (name, group_id))
        return {}

    def api_group_del(self, conn, body, group_id):
        """删除分组，组内对象按原顺序移到默认分组末尾。"""
        g = self.get_group(conn, group_id)
        if g["builtin"]:
            raise errors.UserError("默认分组不能删除")
        kind = g["kind"]
        base = conn.execute(f"SELECT COALESCE(MAX(sort), 0) FROM {kind}").fetchone()[0]
        conn.execute(
            f"UPDATE {kind} SET group_id = ?, sort = sort + ? WHERE group_id = ?",
            (database.default_group(conn, kind), base, group_id),
        )
        conn.execute("DELETE FROM item_groups WHERE id = ?", (group_id,))
        return {}

    def api_pool_list(self, conn, body):
        return pools.listing(conn)

    def api_link_get(self, conn, body, ident):
        result = pools.listing(conn)
        result["link"] = pools.detail(conn, pools.get(conn, "links", ident))
        return result

    @node_sync.track_mutation
    def api_pool_save(self, conn, body, ident=None):
        return pools.pool_save(conn, body, ident)

    @node_sync.track_mutation
    def api_pool_delete(self, conn, body, ident):
        return pools.pool_delete(conn, ident)

    @node_sync.track_mutation
    def api_link_save(self, conn, body, ident=None):
        return pools.link_save(conn, body, ident)

    @node_sync.track_mutation
    def api_link_delete(self, conn, body, ident):
        return pools.delete_link(conn, ident)

    @node_sync.track_mutation
    def api_link_action(self, conn, body, ident):
        return pools.action(conn, body, ident)

    def api_routes(self, conn, body):
        result = []
        for r in conn.execute(database.ordered("routes")):
            ids = json.loads(r["node_ids"])
            refs = [
                dict(c)
                for c in conn.execute(
                    "SELECT id, remark FROM chains WHERE route_id = ? ORDER BY id", (r["id"],)
                )
            ]
            result.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "group_id": r["group_id"],
                    "node_ids": ids,
                    "configs": refs,
                    "nodes": [{"id": nid, "name": database.get_node(conn, nid)["name"]} for nid in ids],
                }
            )
        return {"routes": result, "groups": database.get_groups(conn, "routes")}

    @node_sync.track_mutation
    def api_route_save(self, conn, body, route_id=None):
        old = database.get_route(conn, route_id) if route_id is not None else None
        name = validation.check_remark(body.get("name", old["name"] if old else None), "name", "名称")
        ids = body.get("node_ids", json.loads(old["node_ids"]) if old else None)
        if not isinstance(ids, list) or not 1 <= len(ids) <= 32 or any((type(n) is not int for n in ids)):
            raise errors.UserError("链路需要 1-32 个有序的转发节点", "node_ids")
        for nid in ids:
            n = database.get_node(conn, nid)
            if n["kind"] != "forward" or n["deleting"]:
                raise errors.UserError("链路只能使用未删除的转发节点", "node_ids")
        group_id = database.check_group(
            conn, "routes", body.get("group_id", old["group_id"] if old else None)
        )
        refs = conn.execute(
            "SELECT * FROM chains WHERE route_id = ? AND deleting = 0 ORDER BY id", (route_id,)
        ).fetchall()
        if old:
            conn.execute(
                "UPDATE routes SET name = ?, node_ids = ? WHERE id = ?", (name, json.dumps(ids), route_id)
            )
            if group_id != old["group_id"]:
                database.move_group(conn, "routes", route_id, group_id)
        else:
            route_id = conn.execute(
                "INSERT INTO routes(name, node_ids, group_id, sort) VALUES (?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM routes))",
                (name, json.dumps(ids), group_id),
            ).lastrowid
        if old and ids != json.loads(old["node_ids"]):
            for c in refs:
                before = [
                    r[0] for r in conn.execute("SELECT node_id FROM hops WHERE chain_id = ?", (c["id"],))
                ]
                pairs, _ = forwarding.route_hops(conn, database.get_route(conn, route_id), c["id"])
                forwarding.apply_config_hops(conn, c["id"], pairs)
            pools.sync_legacy(conn)
        return {"id": route_id, "affected": len(refs)}

    @node_sync.track_mutation
    def api_route_del(self, conn, body, route_id):
        database.get_route(conn, route_id)
        if conn.execute("SELECT 1 FROM chains WHERE route_id = ?", (route_id,)).fetchone():
            raise errors.UserError("链路仍被配置引用，请先删除或修改引用它的配置")
        conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        return {"deleted": True}

    def api_configs(self, conn, body):
        data = self.api_chains(conn, body)
        for c in data["chains"]:
            row = database.get_chain(conn, c["id"])
            t = database.get_target(conn, row["target_id"])
            link = conn.execute("SELECT * FROM links WHERE chain_id=?", (c["id"],)).fetchone()
            if link and link["applied"]:
                t = pools.applied_target(conn, link)
            c.update(
                route_id=row["route_id"],
                route_name=database.get_route(conn, row["route_id"])["name"] if row["route_id"] else None,
                target_id=t["id"],
                target_name=t["name"],
                connection=None,
            )
            if not c["deleting"]:
                first = c["hops"][0] if c["hops"] else {"addr": t["host"], "port": t["port"]}
                name = database.config_name(
                    conn, link["name"] if link else c["remark"], link["group_id"] if link else c["group_id"]
                )
                c["connection"] = target_config.connection(t, first["addr"], first["port"], name)
            c["status"] = (
                "deleting"
                if c["deleting"]
                else "error"
                if any((n["status"] == "error" for n in c["hops"] + c["pending"]))
                else "applying"
                if c["pending"]
                else "saved"
                if not c["hops"]
                else "applied"
            )
            if link:
                link = dict(link)
                link["state"], link["error"] = pools.display_state(conn, link)
                c.update(
                    link_id=link["id"],
                    link_state=link["state"],
                    link_error=link["error"],
                    group_id=link["group_id"],
                    link_sync=pools.sync_status(conn, link),
                )
                if link["state"] in ("preparing", "switching", "pending", "waiting", "error"):
                    c["status"] = "error" if link["state"] in ("waiting", "error") else "applying"
                if not c["hops"] and link["state"] in ("preparing", "switching", "waiting"):
                    c["connection"] = None
        order = {r["id"]: i for i, r in enumerate(conn.execute(database.ordered("links")))}
        data["chains"].sort(key=lambda c: order.get(c.get("link_id"), len(order)))
        data["groups"] = database.get_groups(conn, "links")
        return data

    @node_sync.track_mutation
    def api_config_save(self, conn, body, chain_id=None):
        old = database.get_chain(conn, chain_id) if chain_id is not None else None
        if old and conn.execute("SELECT 1 FROM links WHERE chain_id=? AND managed=1", (chain_id,)).fetchone():
            raise errors.UserError("请在链路详情中编辑此配置", status=409)
        if old and old["deleting"]:
            raise errors.UserError("配置正在删除，不能修改")
        if any((k in body for k in ("target_host", "target_port", "tunnel_id", "target_type"))):
            raise errors.UserError("配置只能引用目标对象", "target_id")
        remark = validation.check_remark(body.get("remark", old["remark"] if old else None))
        route_id = body.get("route_id", old["route_id"] if old else None)
        route = database.get_route(conn, route_id) if route_id is not None else None
        target = database.get_target(conn, body.get("target_id", old["target_id"] if old else None))
        if route is None and body.get("ports") not in (None, []):
            raise errors.UserError("直连目标不分配转发端口", "ports")
        pairs, assigned = (
            forwarding.route_hops(conn, route, chain_id, body.get("ports")) if route else ([], [])
        )
        group_id = database.check_group(
            conn, "chains", body.get("group_id", old["group_id"] if old else None)
        )
        if old:
            conn.execute(
                "UPDATE chains SET remark=?,route_id=?,target_id=? WHERE id=?",
                (remark, route_id, target["id"], chain_id),
            )
            if group_id != old["group_id"]:
                database.move_group(conn, "chains", chain_id, group_id)
        else:
            chain_id = conn.execute(
                "INSERT INTO chains(remark,route_id,target_id,group_id,sort)\n                VALUES (?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM chains))",
                (remark, route_id, target["id"], group_id),
            ).lastrowid
        forwarding.apply_config_hops(conn, chain_id, pairs)
        pools.sync_legacy(conn)
        return {"id": chain_id, "assigned": assigned}

    def api_targets(self, conn, body):
        targets = []
        for t in conn.execute(database.ordered("targets")):
            refs = [
                dict(c)
                for c in conn.execute(
                    "SELECT id,remark FROM chains WHERE target_id=? ORDER BY id", (t["id"],)
                )
            ]
            targets.append(
                {
                    **dict(t),
                    "params": json.loads(t["params"]),
                    "configs": refs,
                    "uri": target_config.connection(t, t["host"], t["port"], t["name"])["uri"],
                }
            )
        return {"targets": targets, "groups": database.get_groups(conn, "targets")}

    @node_sync.track_mutation
    def api_target_save(self, conn, body, target_id=None):
        old = database.get_target(conn, target_id) if target_id is not None else None
        if "uri" in body:
            try:
                parsed = target_config.import_uri(body["uri"])
            except ValueError as e:
                raise errors.UserError(str(e), "uri") from None
            host = validation.check_host(parsed["host"], "uri")
            kind, port, params = (
                parsed["protocol"],
                parsed["port"],
                target_config.canonical(parsed["params"]),
            )
            if kind == "vless-reality":
                validation.check_host(parsed["params"]["server_name"], "uri")
            fallback = old["name"] if old else parsed["name"]
        elif old and old["type"] != "endpoint":
            if any((k in body for k in ("host", "port", "type", "params"))):
                raise errors.UserError("SS／VLESS 的连接参数请通过分享链接修改", "uri")
            host, port, kind, params, fallback = (
                old["host"],
                old["port"],
                old["type"],
                old["params"],
                old["name"],
            )
        else:
            if body.get("type", "endpoint") != "endpoint" or "params" in body:
                raise errors.UserError("SS／VLESS 请导入分享链接", "uri")
            host = validation.check_host(body.get("host", old["host"] if old else None), "host")
            port = validation.check_port(body.get("port", old["port"] if old else None), "port")
            kind, params, fallback = ("endpoint", "{}", old["name"] if old else host)
        name = validation.check_remark(body.get("name") or fallback, "name", "名称")
        group_id = database.check_group(
            conn, "targets", body.get("group_id", old["group_id"] if old else None)
        )
        if old:
            conn.execute(
                "UPDATE targets SET name=?,host=?,port=?,type=?,params=? WHERE id=?",
                (name, host, port, kind, params, target_id),
            )
            if group_id != old["group_id"]:
                database.move_group(conn, "targets", target_id, group_id)
            if host != old["host"] or port != old["port"]:
                pools.refresh_links(conn, target=target_id)
        else:
            target_id = conn.execute(
                "INSERT INTO targets(name,host,port,type,params,group_id,sort)\n                VALUES (?,?,?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM targets))",
                (name, host, port, kind, params, group_id),
            ).lastrowid
        return {"id": target_id}

    @node_sync.track_mutation
    def api_target_del(self, conn, body, target_id):
        database.get_target(conn, target_id)
        if any(
            (json.loads(l["plan"])["target"] == target_id for l in conn.execute("SELECT plan FROM links"))
        ):
            raise errors.UserError("目标仍被链路引用，请先解除引用")
        if conn.execute("SELECT 1 FROM chains WHERE target_id=?", (target_id,)).fetchone():
            raise errors.UserError("目标仍被配置引用，请先解除引用")
        conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
        return {"deleted": True}

    def api_settings(self, conn, body):
        return {
            "public_url": database.setting(conn, "public_url", ""),
            "sync_path": database.setting(conn, "sync_path"),
            "panel_path": database.setting(conn, "panel_path"),
            "username": database.setting(conn, "admin_user"),
            "version": settings.VERSION,
            "components": settings.COMPONENTS,
        }

    def api_settings_save(self, conn, body):
        url = str(body.get("public_url", database.setting(conn, "public_url", ""))).strip().rstrip("/")
        if url and (not re.fullmatch("https?://(?:[A-Za-z0-9.-]+|\\[[0-9a-fA-F:]+\\])(?::[0-9]{1,5})?", url)):
            raise errors.UserError("格式应为 http(s)://域名或IP[:端口]，不带路径", "public_url")
        database.set_setting(conn, "public_url", url)
        return {"public_url": url}
