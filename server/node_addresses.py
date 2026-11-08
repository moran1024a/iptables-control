"""Explicit node addresses. Saved addresses never reconfigure host interfaces."""

import ipaddress
import json
import re
import sqlite3
import time

import database
import errors
import settings
import validation


def listing(c, node):
    row = c.execute(
        "SELECT addresses FROM node_addresses WHERE node_id=?", (node["id"],)
    ).fetchone()
    return json.loads(row[0]) if row else []


ROUTING_FIELDS = ("id", "host", "local", "device", "gateway")
DEVICE = re.compile(r"[A-Za-z0-9_.:-]{1,15}")
AUTO = "@auto"


def routing(address):
    return {k: address.get(k, "") for k in ROUTING_FIELDS}


def bound_routing(graph, vid, address):
    """Receiving only binds the advertised host; source overrides are separate."""
    explicit = any(
        mapping.get("from", {}).get(vid) == address["id"]
        for mapping in graph.get("bindings", {}).values()
    )
    if not explicit:
        return {k: address[k] for k in ("id", "host")}
    return routing(address)


def write(c, nid, values):
    c.execute(
        "INSERT INTO node_addresses VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET addresses=excluded.addresses",
        (nid, json.dumps(values, ensure_ascii=False)),
    )


def primary_values(values, host):
    """Keep IDs and routing bindings intact when adopting the primary-address model."""
    values = [dict(a) for a in values]
    primary = next((a for a in values if a.get("host") == host), None)
    if primary is None:
        ident = "main"
        while any(a.get("id") == ident for a in values):
            ident += "_"
        primary = dict(id=ident, name="", host=host, local="", device="", gateway="")
        try:
            primary["local"] = ipv4(host)
        except errors.UserError:
            pass  # Preserve legacy DNS contact addresses.
        values.append(primary)
    for a in values:
        a["primary"] = a is primary
    return values


def network(c, nid):
    row = c.execute(
        "SELECT inventory,inventory_at FROM node_snapshots WHERE node_id=?", (nid,)
    ).fetchone()
    inv = json.loads(row["inventory"]) if row else {}
    candidates = []
    interfaces = inv.get("interfaces", [])
    complete = (
        "interfaces" in inv
        and isinstance(interfaces, list)
        and inv.get("network_complete", True) is True
    )
    if not isinstance(interfaces, list):
        interfaces = []
    for interface in interfaces:
        if not isinstance(interface, dict) or not DEVICE.fullmatch(
            str(interface.get("name", ""))
        ):
            continue
        addresses = interface.get("addresses", [])
        if not isinstance(addresses, list):
            continue
        for value in addresses:
            try:
                ip = ipv4(str(ipaddress.IPv4Interface(value).ip))
            except (ValueError, TypeError, errors.UserError):
                continue
            candidate = dict(local=ip, device=interface["name"])
            if candidate not in candidates:
                candidates.append(candidate)
    return dict(
        addresses=candidates,
        sampled_at=row["inventory_at"] if row and "interfaces" in inv else None,
        complete=complete,
    )


def resolve(address, candidates):
    """Only fill missing information; a later inventory must not rewire a live path."""
    if address["device"] or not address["local"]:
        return
    devices = {a["device"] for a in candidates if a["local"] == address["local"]}
    if len(devices) == 1:
        address["device"] = next(iter(devices))


def refresh_pending(c, nid):
    node = database.get_node(c, nid)
    values = listing(c, node)
    before = json.dumps(values)
    snapshot = network(c, nid)
    candidates = snapshot["addresses"] if snapshot["complete"] else []
    for a in values:
        resolve(a, candidates)
    if json.dumps(values) != before:
        write(c, nid, values)


def receive_network(c, nid, fields):
    """Small, bounded IPv4 inventory carried by the existing Bash sync envelope."""
    raw = fields.get("network_addresses")
    if raw is None:
        return
    if len(raw) > 4096 or len(raw.split(";")) > 96:
        raise errors.UserError("网络信息过大")
    interfaces = {}
    for item in raw.split(";"):
        if not item:
            continue
        device, sep, address = item.partition(",")
        if not sep or not DEVICE.fullmatch(device):
            raise errors.UserError("网络信息格式无效")
        try:
            value = ipaddress.IPv4Interface(address)
        except (ValueError, TypeError):
            raise errors.UserError("网络地址无效")
        interfaces.setdefault(device, []).append(str(value))
    row = c.execute("SELECT inventory FROM node_snapshots WHERE node_id=?", (nid,)).fetchone()
    inv = json.loads(row[0]) if row else {}
    inv.update(
        interfaces=[dict(name=k, addresses=v) for k, v in interfaces.items()],
        sampled_at=int(time.time() * 1000),
        network_complete=fields.get("network_complete") == "1",
    )
    c.execute("INSERT OR IGNORE INTO node_snapshots(node_id) VALUES (?)", (nid,))
    c.execute(
        "UPDATE node_snapshots SET inventory=?,inventory_at=? WHERE node_id=?",
        (json.dumps(inv), int(time.time()), nid),
    )
    refresh_pending(c, nid)


def problem(address, snapshot=None):
    if not address.get("local"):
        return "请选择对应的本机 IP"
    if (
        snapshot
        and snapshot["complete"]
        and not any(a["local"] == address["local"] for a in snapshot["addresses"])
    ):
        return f"最近采集未发现本机 IP {address['local']}；NAT 地址请选择对应的本机 IP"
    if not address.get("device"):
        return "网卡待识别，请同步 Agent 或选择本机 IP"
    if (
        snapshot
        and snapshot["complete"]
        and not any(
            a["local"] == address["local"] and a["device"] == address["device"]
            for a in snapshot["addresses"]
        )
    ):
        return f"最近采集未发现 {address['local']} / {address['device']}，请核对本机 IP 和网卡"
    return ""


def ready(address, snapshot=None):
    return not problem(address, snapshot)


def egress_problem(address, snapshot=None):
    """Only explicit source selection requires an address on this machine."""
    local = address.get("local") or address["host"]
    if (
        snapshot
        and snapshot["complete"]
        and not any(a["local"] == local for a in snapshot["addresses"])
    ):
        return f"指定出口 {local} 不在本机；请选择自动出口，或在高级设置中指定本机源 IP"
    if not address.get("local") or not address.get("device"):
        return "指定出口尚未识别，请同步 Agent；也可以选择自动出口"
    if (
        snapshot
        and snapshot["complete"]
        and not any(
            a["local"] == local and a["device"] == address["device"]
            for a in snapshot["addresses"]
        )
    ):
        return "指定出口的网卡已变化，请在节点高级设置中更新网卡"
    return ""


def ipv4(value, optional=False):
    if optional and not value:
        return ""
    try:
        ip = ipaddress.IPv4Address(value)
        if ip.is_unspecified or ip.is_multicast or ip.is_loopback:
            raise ValueError()
        return str(ip)
    except (ValueError, TypeError, ipaddress.AddressValueError):
        raise errors.UserError("请填写可用的 IPv4 地址", "addresses")


def save(c, node, values, *, addr=None):
    if not isinstance(values, list) or len(values) > 33:
        raise errors.UserError("每个节点最多配置 32 个副 IP", "addresses")
    values = [dict(v) if isinstance(v, dict) else v for v in values]
    if not all(isinstance(v, dict) for v in values):
        raise errors.UserError("地址格式无效", "addresses")
    explicit = any("primary" in a for a in values)
    if explicit:
        if (
            any(type(a.get("primary", False)) is not bool for a in values)
            or sum(a.get("primary", False) for a in values) != 1
        ):
            raise errors.UserError("请选择一个主 IP", "addresses")
        primary = next(a for a in values if a.get("primary"))
        if addr is not None and addr != validation.check_host(primary.get("host"), "addresses"):
            raise errors.UserError("主 IP 与节点地址不一致", "addresses")
    else:
        values = primary_values(values, addr or node["addr"])
    if len(values) > 33:
        raise errors.UserError("每个节点最多配置 32 个副 IP", "addresses")
    snapshot = network(c, node["id"])
    candidates = snapshot["addresses"] if snapshot["complete"] else []
    old = {a["id"]: a for a in listing(c, node)}
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise errors.UserError("地址格式无效", "addresses")
        ident = value.get("id", "")
        if (
            not isinstance(ident, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", ident)
            or ident in seen
        ):
            raise errors.UserError("地址标识无效或重复", "addresses")
        seen.add(ident)
        device = value.get("device", "")
        if not isinstance(device, str) or device and not DEVICE.fullmatch(device):
            raise errors.UserError("网卡名称无效", "addresses")
        primary = bool(value.get("primary"))
        existing_host = old.get(ident, {}).get("host")
        host = (
            validation.check_host(value.get("host"), "addresses")
            if primary or value.get("host") == existing_host
            else ipv4(value.get("host"))
        )
        if primary:
            try:
                ipaddress.IPv4Address(host)
            except ValueError:
                pass
            else:
                host = ipv4(host)
        local = value.get("local")
        if not local:
            try:
                local = ipv4(host)
            except errors.UserError:
                local = ""
        address = dict(
            id=ident,
            primary=primary,
            name=(
                validation.check_remark(value["name"], "addresses", "地址名称")
                if value.get("name")
                else ""
            ),
            host=host,
            local=ipv4(local, optional=True),
            device=device,
            gateway=ipv4(value.get("gateway"), optional=True),
        )
        resolve(address, candidates)
        result.append(address)
    # Labels are presentation only; changing them must not interrupt forwarding.
    new = {a["id"]: a for a in result}
    # Receiving addresses need not exist on a NIC; saved references remain stable.
    changed = {
        ident
        for ident, a in old.items()
        if ident not in new or routing(a) != routing(new[ident])
    }
    if changed:
        import topology

        # Deleting a referenced address would break the saved graph. Corrections
        # are allowed until an applied or in-flight snapshot actually uses it.
        for name, doc in topology.documents(c):
            graph = doc.get("graph", {})
            used = references(graph)
            if any(
                ident not in new
                for v in graph.get("vertices", [])
                if v.get("node_id") == node["id"]
                for ident in used.get(v["id"], set()) & changed
            ):
                raise errors.UserError(
                    f"地址仍被链路「{name}」引用，请先移除对应连线", "addresses"
                )
        for name, doc in topology.documents(c, include_pending=False):
            graph = doc.get("graph", {})
            used = references(graph)
            if any(
                ident not in new
                or bound_routing(graph, v["id"], old[ident])
                != bound_routing(graph, v["id"], new[ident])
                for v in graph.get("vertices", [])
                if v.get("node_id") == node["id"]
                for ident in used.get(v["id"], set()) & changed
            ):
                raise errors.UserError(
                    f"地址正被已应用或待下发的链路「{name}」引用。请新增修正后的 IP，在链路中替换并应用后再删除旧地址",
                    "addresses",
                )
    write(c, node["id"], result)
    host = next(a["host"] for a in result if a["primary"])
    c.execute("UPDATE nodes SET addr=? WHERE id=?", (host, node["id"]))
    if changed:
        update_pending(c, node["id"], new)
    return host


def update_pending(c, nid, addresses):
    """Carry a permitted correction into saved plans without rewriting receipts."""
    import execution

    def update(graph):
        dirty = False
        for v in graph["vertices"]:
            if v.get("node_id") == nid and graph["address_book"].get(v["id"]) != addresses:
                graph["address_book"][v["id"]] = addresses
                dirty = True
        return dirty

    for row in c.execute("SELECT id,graph FROM topologies").fetchall():
        graph = json.loads(row["graph"])
        if update(graph):
            c.execute(
                "UPDATE topologies SET graph=?,revision=revision+1 WHERE id=?",
                (execution.dump(graph), row["id"]),
            )
    for row in c.execute(
        "SELECT id,desired FROM configurations WHERE desired IS NOT NULL"
    ).fetchall():
        plan = json.loads(row["desired"])
        before = execution.operational(plan)
        if not update(plan["graph"]):
            continue
        c.execute(
            "UPDATE configurations SET desired=?,revision=revision+1 WHERE id=?",
            (execution.dump(plan), row["id"]),
        )
        if before != execution.operational(plan):
            c.execute(
                "UPDATE configurations SET state='pending',error='' WHERE id=?", (row["id"],)
            )


def get(c, node_id, ident):
    node = database.get_node(c, node_id)
    return next((a for a in listing(c, node) if a["id"] == ident), None)


def references(graph):
    """Addresses actually bound to vertices and edges, excluding the inventory."""
    used = {
        v["id"]: {v["address"]} if v.get("address") else set()
        for v in graph.get("vertices", [])
    }
    for mapping in graph.get("bindings", {}).values():
        for side in ("from", "to"):
            for vertex, address in mapping.get(side, {}).items():
                if vertex in used and address != AUTO:
                    used[vertex].add(address)
    return used


def edge_key(a, b):
    return a + "/" + b


def route_token(incoming, outgoing, version=2):
    source = (
        tuple(outgoing[k] or "-" for k in ("local", "device", "gateway"))
        if outgoing
        else ("-", "-", "-")
    )
    return ",".join(("v2", incoming["host"], *source))


def source_route(route):
    fields = route.split(",")
    return (
        ",".join(fields[2:])
        if len(fields) == 5 and fields[0] == "v2" and fields[2] != "-"
        else ""
    )


def rule_route(c, rule_id):
    row = c.execute("SELECT route FROM rules WHERE id=?", (rule_id,)).fetchone()
    return row[0] if row else ""
