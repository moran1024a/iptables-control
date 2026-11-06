import hashlib
import json
import database
import errors
import validation


def reserve_removed(conn, node_id, port):
    n = database.get_node(conn, node_id)
    if n["first_seen"]:
        conn.execute("INSERT OR IGNORE INTO retired_ports VALUES (?, ?)", (node_id, port))


def apply_config_hops(conn, chain_id, pairs):
    old = conn.execute("SELECT * FROM hops WHERE chain_id = ? ORDER BY seq", (chain_id,)).fetchall()
    wanted = set(pairs)
    for h in old:
        if (h["node_id"], h["port"]) not in wanted:
            reserve_removed(conn, h["node_id"], h["port"])
            conn.execute("DELETE FROM hops WHERE id = ?", (h["id"],))
    existing = {(h["node_id"], h["port"]): h["id"] for h in old}
    for seq, pair in enumerate(pairs):
        if pair in existing:
            conn.execute("UPDATE hops SET seq = ? WHERE id = ?", (seq, existing[pair]))
        else:
            hid = int(database.setting(conn, "next_hop_id"))
            database.set_setting(conn, "next_hop_id", str(hid + 1))
            conn.execute(
                "INSERT INTO hops(id, chain_id, seq, node_id, port) VALUES (?, ?, ?, ?, ?)",
                (hid, chain_id, seq, *pair),
            )


def route_hops(conn, route, chain_id=None, ports=None):
    ids = json.loads(route["node_ids"])
    if ports is not None and (not isinstance(ports, list) or len(ports) != len(ids)):
        raise errors.UserError("端口数量必须与链路跳数一致", "ports")
    old = list(conn.execute("SELECT * FROM hops WHERE chain_id = ? ORDER BY seq", (chain_id,)))
    hops = []
    for i, nid in enumerate(ids):
        match = next((h for h in old if h["node_id"] == nid), None)
        if match:
            old.remove(match)
        port = ports[i] if ports is not None else match["port"] if match else None
        hops.append({"node_id": nid, "port": port})
    return parse_hops(conn, hops, chain_id)


def parse_hops(conn, hops, chain_id=None):
    """把 [{node_id, port}] 解析为 [(node_id, port)]，检查端口冲突；端口留空时随机分配。
    返回 (跳列表, 自动分配说明列表)。"""
    if not isinstance(hops, list) or not hops:
        raise errors.UserError("至少需要一跳", "hops")
    result = []
    for i, h in enumerate(hops, 1):
        nid = h.get("node_id") if isinstance(h, dict) else None
        node = (
            conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()
            if isinstance(nid, int)
            else None
        )
        if not node:
            raise errors.UserError(f"第 {i} 跳：请选择节点", "hops")
        if node["kind"] != "forward":
            raise errors.UserError(f"第 {i} 跳：只能使用转发节点", "hops")
        if node["deleting"]:
            raise errors.UserError(f"第 {i} 跳：节点 {node['name']} 正在删除，不能使用", "hops")
        port = None
        if str(h.get("port") or "").strip():
            try:
                port = validation.check_port(h.get("port"), "hops")
            except errors.UserError:
                raise errors.UserError(f"第 {i} 跳：端口必须是 1-65535 的整数，或留空自动分配", "hops")
            if not validation.in_ports(port, validation.parse_ports(node["ports"])):
                raise errors.UserError(
                    f"第 {i} 跳：端口 {port} 不在节点 {node['name']} 允许的端口 {node['ports']} 内", "hops"
                )
        result.append([node, port])
    seen = set()
    for i, (node, port) in enumerate(result, 1):
        if port is None:
            continue
        if node["wake_enabled"] and port == node["wake_port"]:
            raise errors.UserError(f"第 {i} 跳：端口 {port} 已预留为远程唤醒端口", "hops")
        if (
            node["desired_mode"] == "active"
            and port == node["quality_port"]
            and (
                not conn.execute(
                    "SELECT 1 FROM hops WHERE node_id=? AND port=? AND chain_id=?",
                    (node["id"], port, chain_id),
                ).fetchone()
            )
        ):
            raise errors.UserError(
                f"第 {i} 跳：端口 {port} 已预留为质量探测端口，请修改节点探测端口或转发端口", "hops"
            )
        if (node["id"], port) in seen:
            raise errors.UserError(f"第 {i} 跳：重复使用了节点 {node['name']} 的端口 {port}", "hops")
        seen.add((node["id"], port))
        if port in reserved(conn, node["id"]):
            raise errors.UserError(f"第 {i} 跳：端口 {port} 已被链路占用或等待清理", "ports")
        row = conn.execute(
            "SELECT c.remark || CASE c.deleting WHEN 1 THEN '（删除中）' ELSE '' END AS remark\n               FROM hops h JOIN chains c ON c.id = h.chain_id\n               WHERE h.node_id = ? AND h.port = ? AND h.chain_id IS NOT ?",
            (node["id"], port, chain_id),
        ).fetchone()
        if conn.execute(
            "SELECT 1 FROM retired_ports WHERE node_id = ? AND port = ?", (node["id"], port)
        ).fetchone():
            raise errors.UserError(f"第 {i} 跳：端口 {port} 尚在等待旧规则删除确认", "ports")
        if row:
            raise errors.UserError(
                f"第 {i} 跳：节点 {node['name']} 的端口 {port} 已被转发「{row['remark']}」使用", "hops"
            )
    assigned = []
    for i, item in enumerate(result, 1):
        node, port = item
        if port is not None:
            continue
        used = {
            r[0]
            for r in conn.execute(
                "SELECT port FROM hops WHERE node_id = ? AND chain_id IS NOT ?", (node["id"], chain_id)
            )
        }
        used |= {
            r[0] for r in conn.execute("SELECT port FROM retired_ports WHERE node_id = ?", (node["id"],))
        }
        used |= reserved(conn, node["id"])
        used |= {p for n, p in result if n["id"] == node["id"] and p is not None}
        if node["desired_mode"] == "active":
            used.add(node["quality_port"])
        if node["wake_enabled"]:
            used.add(node["wake_port"])
        item[1] = validation.pick_port(validation.parse_ports(node["ports"]), used)
        if item[1] is None:
            raise errors.UserError(
                f"第 {i} 跳：节点 {node['name']} 允许的端口 {node['ports']} 已全部被占用", "hops"
            )
        assigned.append(f"{node['name']}:{item[1]}")
    return ([(node["id"], port) for node, port in result], assigned)


def legacy_node_rule_rows(conn, node):
    """节点应有的规则：非最后一跳指向下一跳节点，最后一跳指向链的目标。"""
    if node["deleting"]:
        return []
    ready = (
        "AND c.ready = 1" if "ready" in [r["name"] for r in conn.execute("PRAGMA table_info(chains)")] else ""
    )
    rows = conn.execute(
        f"\n        SELECT h.id, h.port, c.id AS chain_id, c.remark AS chain_remark, c.target_host, c.target_port,\n               n2.addr AS next_addr, h2.port AS next_port\n        FROM hops h\n        JOIN chains c ON c.id = h.chain_id\n        LEFT JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1\n        LEFT JOIN nodes n2 ON n2.id = h2.node_id\n        WHERE h.node_id = ? AND c.deleting = 0 {ready}\n        ORDER BY h.id\n        ",
        (node["id"],),
    )
    out = []
    for r in rows:
        if r["next_port"] is None:
            host, port = (r["target_host"], r["target_port"])
        else:
            host, port = (r["next_addr"], r["next_port"])
        out.append(
            {
                "id": r["id"],
                "port": r["port"],
                "host": host,
                "tport": port,
                "chain_id": r["chain_id"],
                "chain_remark": r["chain_remark"],
            }
        )
    return out


def legacy_node_rules(conn, node):
    return [f"{r['id']} both {r['port']} {r['host']} {r['tport']}" for r in legacy_node_rule_rows(conn, node)]


def node_rule_rows(conn, node):
    """节点应有的规则：非最后一跳指向下一跳节点，最后一跳指向链的目标。"""
    if node["deleting"]:
        return []
    rows = conn.execute(
        f"\n        SELECT h.id, h.port, c.id AS chain_id, c.remark AS chain_remark, t.host AS target_host, t.port AS target_port,\n               n2.addr AS next_addr, h2.port AS next_port\n        FROM hops h\n        JOIN chains c ON c.id = h.chain_id\n        JOIN targets t ON t.id = c.target_id\n        LEFT JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1\n        LEFT JOIN nodes n2 ON n2.id = h2.node_id\n        WHERE h.node_id = ? AND c.deleting = 0\n        ORDER BY h.id\n        ",
        (node["id"],),
    )
    has_links = conn.execute("SELECT 1 FROM sqlite_master WHERE name='links'").fetchone()
    managed = (
        {r[0] for r in conn.execute("SELECT chain_id FROM links WHERE managed=1")} if has_links else set()
    )
    out = []
    for r in rows:
        if r["chain_id"] in managed:
            continue
        if r["next_port"] is None:
            host, port = (r["target_host"], r["target_port"])
        else:
            host, port = (r["next_addr"], r["next_port"])
        out.append(
            {
                "id": r["id"],
                "port": r["port"],
                "host": host,
                "tport": port,
                "chain_id": r["chain_id"],
                "chain_remark": r["chain_remark"],
            }
        )
    if has_links:
        out.extend(rule_rows(conn, node["id"]))
    return sorted(out, key=lambda r: r["id"])


def node_rules(conn, node):
    """下发给节点的规则行：规则id 协议 入站端口 目标地址 目标端口。"""
    return [f"{r['id']} both {r['port']} {r['host']} {r['tport']}" for r in node_rule_rows(conn, node)]


def rules_version(lines):
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def reserved(c, nid):
    result = {
        r[0]
        for r in c.execute(
            "SELECT port FROM link_rules WHERE node_id=? UNION SELECT port FROM link_retired WHERE node_id=?",
            (nid, nid),
        )
    }
    for row in c.execute("SELECT job FROM links WHERE job IS NOT NULL"):
        result.update((r["port"] for r in json.loads(row["job"])["new"] if r["node_id"] == nid))
    return result


def rule_rows(c, nid):
    return [
        dict(r)
        for r in c.execute(
            "SELECT r.id,r.port,r.host,r.tport,l.chain_id,c.remark AS chain_remark\n        FROM link_rules r JOIN links l ON l.id=r.link_id JOIN chains c ON c.id=l.chain_id WHERE r.node_id=?",
            (nid,),
        )
    ]
