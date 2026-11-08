import json
import re
import time
import agent_v3
import database
import errors
import forwarding
import lifecycle
import network_quality
import node_state
import node_addresses
import node_tags
import node_sync
import configurations
import execution
import failover
import topology
import security
import settings
import subscriptions
import target_config
import target_addresses
import validation
import wake


class Resources:
    """Resource use cases; no HTTP transport or authentication dependency."""

    def __init__(self, query=None, failover_engine=None):
        self.query = query or {}
        self.failover_engine = failover_engine

    def node_brief(self, conn, n, now):
        return {
            "id": n["id"],
            "kind": n["kind"],
            "name": n["name"],
            "note": n["note"],
            "weight": n["weight"],
            "tags": node_tags.listing(conn, n["id"]),
            "addr": n["addr"],
            "addresses": node_addresses.listing(conn, n),
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
            "err": n["err"] or node_state.runtime_error(n, now),
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
        return {
            "now": now,
            "kind": kind,
            "groups": database.get_groups(conn, table),
            "nodes": nodes,
            "tags": node_tags.catalog(conn),
        }

    @node_sync.track_mutation
    def api_node_add(self, conn, body):
        name = validation.check_remark(body.get("name"), "name", "名称")
        note = validation.check_note(body.get("note"))
        addr = validation.check_host(body.get("addr"), "addr")
        ports = validation.format_ports(validation.parse_ports(body.get("ports", "10000-65535")))
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
                "UPDATE nodes SET desired_mode='passive',manual_required=1 WHERE id=?",
                (cur.lastrowid,),
            )
        quality_port = (
            validation.check_port(body.get("quality_port", network_quality.PORT), "quality_port")
            if body.get("quality_port", network_quality.PORT) != 0
            else 0
        )
        conn.execute("UPDATE nodes SET quality_port=? WHERE id=?", (quality_port, cur.lastrowid))
        node_addresses.save(
            conn, database.get_node(conn, cur.lastrowid), body.get("addresses", []), addr=addr
        )
        conn.execute("UPDATE nodes SET weight=? WHERE id=?", (validation.check_weight(body.get("weight", 0)), node_id))
        node_tags.assign(conn, node_id, body.get("tag_ids", []))
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
                "SELECT 1 FROM rules WHERE node_id=? AND port=? UNION ALL SELECT 1 FROM retired_ports WHERE node_id=? AND port=?",
                (node["id"], port, node["id"], port),
            ).fetchone():
                raise errors.UserError("唤醒端口已被转发配置占用或正在等待释放", "wake_port")
        conn.execute(
            "UPDATE nodes SET wake_enabled=?,wake_port=? WHERE id=?",
            (int(enabled), port, node["id"]),
        )

    def api_node_get(self, conn, body, node_id):
        now = int(time.time())
        n = database.get_node(conn, node_id)
        rules = forwarding.node_rule_rows(conn, n)
        group = conn.execute(
            "SELECT name FROM item_groups WHERE id = ?", (n["group_id"],)
        ).fetchone()
        events = conn.execute(
            "SELECT ts, text FROM events WHERE node_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (node_id, settings.EVENTS_SHOWN),
        ).fetchall()
        return {
            "now": now,
            "sync_path": database.setting(conn, "sync_path"),
            "public_url": database.setting(conn, "public_url", ""),
            "version": settings.VERSION,
            "forward_version": settings.COMPONENTS.get(
                n["desired_mode"] + "_agent", settings.VERSION
            ),
            "installer_url": f"https://github.com/{settings.REPO}/releases/download/v{settings.VERSION}/install-agent.sh",
            "telemetry": agent_v3.telemetry(conn, node_id),
            "node": {
                **self.node_brief(conn, n, now),
                "network": node_addresses.network(conn, node_id),
                "bar": node_state.node_bar(conn, n, now),
                "key": n["key"],
                "first_seen": n["first_seen"],
                "src_ip": n["src_ip"],
                "skew": n["skew"],
                "agent": n["agent"],
                "reported_v": n["reported_v"],
                "want_v": forwarding.rules_version(forwarding.node_rules(conn, n)),
                "state": n["state"],
                "err": n["err"] or node_state.runtime_error(n, now),
                "group": group["name"],
                "agent_update": (
                    "pending"
                    if agent_v3.newer(
                        settings.COMPONENTS[n["desired_mode"] + "_agent"], n["agent"] or "0.0.0"
                    )
                    else "same"
                ),
            },
            "rules": rules,
            "configs": [
                dict(id=r["configuration_id"], name=r["configuration_name"]) for r in rules
            ],
            "events": [dict(e) for e in events],
        }

    @node_sync.track_mutation
    def api_node_edit(self, conn, body, node_id):
        n = database.get_node(conn, node_id)
        if "weight" in body:
            conn.execute("UPDATE nodes SET weight=? WHERE id=?", (validation.check_weight(body["weight"]), node_id))
        if "tag_ids" in body:
            node_tags.assign(conn, node_id, body["tag_ids"])
        table = database.node_table(n["kind"])
        if "kind" in body and body["kind"] != n["kind"]:
            raise errors.UserError("节点类型不能修改", "kind")
        name = (
            validation.check_remark(body["name"], "name", "名称") if "name" in body else n["name"]
        )
        note = validation.check_note(body["note"]) if "note" in body else n["note"]
        addr = validation.check_host(body["addr"], "addr") if "addr" in body else n["addr"]
        if "addresses" in body:
            addr = node_addresses.save(
                conn, n, body["addresses"], addr=addr if "addr" in body else None
            )
        elif addr != n["addr"]:
            addresses = node_addresses.listing(conn, n)
            if not addresses:
                addresses = node_addresses.primary_values([], addr)
            else:
                primary = next(a for a in addresses if a.get("primary"))
                primary.update(host=addr, local="", device="", gateway="")
            addr = node_addresses.save(conn, n, addresses, addr=addr)
        ranges = (
            validation.parse_ports(body["ports"])
            if "ports" in body
            else validation.parse_ports(n["ports"])
        )
        group_id = (
            database.check_group(conn, table, body["group_id"])
            if "group_id" in body
            else n["group_id"]
        )
        outside = [
            r
            for r in conn.execute(
                "SELECT c.name AS remark, r.port FROM rules r JOIN configurations c ON c.id=r.configuration_id WHERE r.node_id=? ORDER BY r.port",
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
        extra_ports = [
            p for p in forwarding.reserved(conn, node_id) if not validation.in_ports(p, ranges)
        ]
        if extra_ports:
            raise errors.UserError(
                "链路使用或待清理的端口不在新范围内：" + ",".join(map(str, extra_ports)), "ports"
            )
        if mode not in ("active", "passive"):
            raise errors.UserError("模式必须为主动或被动", "desired_mode")
        quality_port = body.get("quality_port", n["quality_port"])
        quality_port = (
            validation.check_port(quality_port, "quality_port") if quality_port != 0 else 0
        )
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
            conn.execute(
                "UPDATE nodes SET desired_mode=?,manual_required=1 WHERE id=?", (mode, node_id)
            )
            lifecycle.add_event(conn, node_id, "同步模式已修改，等待本机手动同步确认")
        conn.execute(
            f"UPDATE {table} SET name = ?, note = ?, addr = ?, ports = ? WHERE id = ?",
            (name, note, addr, validation.format_ports(ranges), node_id),
        )
        if group_id != n["group_id"]:
            database.move_group(conn, table, node_id, group_id)
        if addr != n["addr"]:
            lifecycle.add_event(conn, node_id, f"主地址 {n['addr']} → {addr}")
        return {
            "key": key,
            "upstream": node_state.upstream_nodes(conn, node_id) if addr != n["addr"] else [],
        }

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
        used = topology.references(conn, node_id)
        if used:
            raise errors.UserError("节点仍被引用：" + "、".join(used), status=409)
        if conn.execute(
            "SELECT 1 FROM rules WHERE node_id=? UNION SELECT 1 FROM retirements WHERE node_id=?",
            (node_id, node_id),
        ).fetchone():
            raise errors.UserError("节点仍有待清理的转发，请完成同步后删除", status=409)
        force = self.query.get("force") == ["1"]
        if force or not n["first_seen"]:
            lifecycle.delete_node(conn, node_id)
            return {"deleted": True}
        if not n["deleting"]:
            conn.execute(f"UPDATE {table} SET deleting = 1 WHERE id = ?", (node_id,))
            lifecycle.add_event(conn, node_id, "开始删除，等待节点清空规则")
        return {"deleted": False}

    def api_subscriptions(self, conn, body):
        return subscriptions.listing(conn)

    def api_subscription_save(self, conn, body, ident=None):
        return subscriptions.save(conn, body, ident)

    def api_subscription_delete(self, conn, body, ident):
        subscriptions.get(conn, ident)
        conn.execute("DELETE FROM subscriptions WHERE id=?", (ident,))
        return {"deleted": True}

    def api_order(self, conn, body, kind):
        """保存顺序：groups 为分组顺序，ids 为全部对象按显示顺序排列（只能在组内调整，不改变所属分组）。"""
        groups, ids = (body.get("groups"), body.get("ids"))
        existing_groups = {
            r[0] for r in conn.execute("SELECT id FROM item_groups WHERE kind = ?", (kind,))
        }
        query = f"SELECT id FROM {kind}"
        existing = {r[0] for r in conn.execute(query)}
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

    def api_targets(self, conn, body):
        targets = []
        for t in conn.execute(database.ordered("targets")):
            refs = [
                dict(id=r["id"], remark=r["name"])
                for r in conn.execute("SELECT id,name,desired,applied FROM configurations")
                if any(
                    json.loads(r[k]).get("plan", json.loads(r[k])).get("target") == t["id"]
                    for k in ("desired", "applied")
                    if r[k]
                )
            ]
            targets.append(
                {
                    **dict(t),
                    "params": json.loads(t["params"]),
                    "addresses": target_addresses.listing(t),
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
        else:
            target_id = conn.execute(
                "INSERT INTO targets(name,host,port,type,params,group_id,sort)\n                VALUES (?,?,?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM targets))",
                (name, host, port, kind, params, group_id),
            ).lastrowid
        target = database.get_target(conn, target_id)
        target_addresses.save(conn, target, body.get("addresses", json.loads(target["addresses"])))
        if old:
            target_addresses.refresh(conn, old, database.get_target(conn, target_id))
        return {"id": target_id}

    def api_tags(self, conn, body):
        return {"tags": node_tags.catalog(conn)}

    def api_tag_save(self, conn, body, ident=None):
        return node_tags.save(conn, body, ident)

    def api_tag_delete(self, conn, body, ident):
        conn.execute("DELETE FROM tags WHERE id=?", (ident,))
        return {"deleted": True}

    @node_sync.track_mutation
    def api_target_del(self, conn, body, target_id):
        database.get_target(conn, target_id)
        for name, doc in topology.documents(conn):
            if doc.get("target") == target_id or any(
                v.get("target_id") == target_id for v in doc.get("graph", {}).get("vertices", [])
            ):
                raise errors.UserError(f"目标仍被链路「{name}」引用，请先解除引用")
        conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
        return {"deleted": True}

    def api_settings(self, conn, body):
        return {
            "failover_offline_seconds": failover.offline_timeout(conn),
            "public_url": database.setting(conn, "public_url", ""),
            "sync_path": database.setting(conn, "sync_path"),
            "panel_path": database.setting(conn, "panel_path"),
            "username": database.setting(conn, "admin_user"),
            "version": settings.VERSION,
            "components": settings.COMPONENTS,
        }

    def api_settings_save(self, conn, body):
        timeout = body.get("failover_offline_seconds", failover.offline_timeout(conn))
        if type(timeout) is not int or not 30 <= timeout <= 3600:
            raise errors.UserError("失联等待时间应为 30～3600 秒的整数", "failover_offline_seconds")
        url = (
            str(body.get("public_url", database.setting(conn, "public_url", "")))
            .strip()
            .rstrip("/")
        )
        if url and (
            not re.fullmatch("https?://(?:[A-Za-z0-9.-]+|\\[[0-9a-fA-F:]+\\])(?::[0-9]{1,5})?", url)
        ):
            raise errors.UserError("格式应为 http(s)://域名或IP[:端口]，不带路径", "public_url")
        database.set_setting(conn, "public_url", url)
        database.set_setting(conn, "failover_offline_seconds", str(timeout))
        return {"public_url": url, "failover_offline_seconds": timeout}

    def api_failover_preview(self, conn, body, ident):
        vertices, revisions = self.query.get("vertex", []), self.query.get("revision", [])
        if (len(vertices) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", vertices[0])
                or len(revisions) != 1 or not re.fullmatch(r"[0-9]{1,15}", revisions[0])):
            raise errors.UserError("灾备预览参数无效")
        if self.failover_engine is None:
            raise errors.UserError("灾备监测尚未就绪", status=503)
        if not conn.in_transaction:
            conn.execute("BEGIN")
        return self.failover_engine.preview(conn, ident, vertices[0], int(revisions[0]))

    def api_topologies(self, conn, body):
        return configurations.listing(conn)

    def api_topology_get(self, conn, body, ident):
        return dict(configurations.resources(conn), link=configurations.detail(conn, ident))

    def api_topology_save(self, conn, body, ident=None):
        return configurations.save_topology(conn, body, ident)

    def api_topology_preview(self, conn, body):
        return configurations.preview(conn, body)

    def api_topology_delete(self, conn, body, ident):
        return configurations.delete(conn, ident)

    def api_topology_action(self, conn, body, ident):
        return configurations.action(conn, body, ident)

    def api_configs(self, conn, body):
        return configurations.configs(conn)

    def api_config_rename(self, conn, body, ident):
        config = execution.get(conn, ident)
        if config["deleted"]:
            raise errors.UserError("配置正在删除", status=409)
        name = validation.check_remark(body.get("name"), "name", "配置名称")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            raise errors.UserError("配置名称不能包含控制字符", "name")
        if body.get("previous_name") != config["name"]:
            raise errors.UserError("配置名称已变化，请重新打开", status=409)
        conn.execute("UPDATE configurations SET name=? WHERE id=?", (name, ident))
        return dict(id=ident, name=name)
