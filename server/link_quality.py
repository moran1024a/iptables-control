import hashlib
import json
import agent_v3
import execution
import configurations
import topology
import node_addresses
import node_tags
import target_addresses
import probe_store

PROTOCOLS = ("tcp-connect-v1", "udp-echo-v1", "icmp-echo-v1")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


def sections(c, topo, nodes, configuration=None):
    result = []
    for config in configurations.members(c, topo["id"]):
        if config["deleted"] or configuration is not None and config["id"] != configuration:
            continue
        for section in config_sections(c, config, nodes):
            section["id"] = f"config:{config['id']}:" + section["id"]
            for spec in section["probes"]:
                spec.update(topology_id=topo["id"], section=section["id"])
            result.append(section)
    return result


def config_sections(c, config, nodes):
    applied = json.loads(config["applied"]) if config["applied"] else None
    plan = applied["plan"] if applied else json.loads(config["desired"])
    target = (
        execution.applied_target(c, config)
        if applied
        else target_addresses.resolve(c, plan)
    )
    hops = [dict(r, seq=i) for i, r in enumerate(applied["rules"])] if applied else []
    by_node = {h["node_id"]: h for h in hops}
    live_rules = {h["id"]: h for h in hops}
    wire, path = topology.wire_graph(plan["graph"], plan["graph_path"])
    vertices = {v["id"]: v for v in wire["vertices"]}
    stages = [(vid, vertices[vid]["node_id"]) for vid in path[1:-1]]
    actual = [n for _, n in stages]
    expected = [plan["entry"]] + actual
    ready = bool(applied and target and [h["node_id"] for h in hops] == expected)
    name = lambda nid: "direct" if nid == 0 else nodes[nid]["name"] if nid in nodes else "未选择"
    target_name = target["name"] if target else "目标"
    descriptors = [("full", "全段", actual[0] if actual else None, None, actual)]
    for i, (sid, nid) in enumerate(stages):
        peer = stages[i + 1][1] if i + 1 < len(stages) else None
        descriptors.append(
            ("stage:" + sid, "子段", nid, peer, [nid, peer] if i + 1 < len(stages) else [nid])
        )
    result = []
    for sid, kind, source, peer, path in descriptors:
        full = sid == "full"
        terminal = full or sid == ("stage:" + stages[-1][0] if stages else None)
        label = " → ".join([name(n) for n in path] + ([target_name] if terminal else []))
        if full and (not actual):
            label = "无中间节点 → " + target_name
        labels = [dict(name=name(n), node_id=n) for n in path]
        if full and not actual:
            labels.append(dict(name="无中间节点"))
        if terminal:
            labels.append(dict(name=target_name, target_id=plan["target"]))
        probes = []
        for protocol in PROTOCOLS:
            spec = dict(
                topology_id=config["topology_id"],
                section=sid,
                protocol=protocol,
                source=source,
                target=peer,
                host="",
                port=0,
                rule_id=0,
                route=path,
                path_label=label,
                reason=None,
            )
            reason = None
            if config["deleted"] or not ready:
                reason = ("waiting", "等待链路生效")
            elif not source or (not terminal and (not peer)):
                reason = ("unknown", "direct" if 0 in path else "无可探测的中间节点")
            elif terminal:
                h = by_node[source]
                next_hop = next((x for x in hops if x["seq"] == h["seq"] + 1), None)
                spec.update(
                    host=(
                        live_rules[h["id"]]["host"]
                        if h["id"] in live_rules
                        else nodes[next_hop["node_id"]]["addr"] if next_hop else target["host"]
                    ),
                    port=(
                        live_rules[h["id"]]["tport"]
                        if h["id"] in live_rules
                        else next_hop["port"] if next_hop else target["port"]
                    ),
                    rule_id=h["id"],
                    target=None,
                )
                if protocol == "udp-echo-v1":
                    reason = ("unknown", "目标未提供 UDP 回显")
                elif protocol == "icmp-echo-v1" and next_hop:
                    reason = ("unknown", "ICMP 无法沿端口转发路径探测")
            elif protocol == "tcp-connect-v1":
                reason = ("unknown", "节点间使用 UDP / ICMP 探测")
            else:
                spec.update(
                    host=nodes[peer]["addr"],
                    port=nodes[peer]["quality_port"] if protocol == "udp-echo-v1" else 0,
                )
            explicit = bool(plan.get("routing"))
            if explicit and source in by_node:
                route = by_node[source].get("route", "")
                source_route = node_addresses.source_route(route)
                if source_route:
                    spec["source_route"] = source_route
                if peer and not terminal:
                    index = expected.index(peer)
                    spec["host"] = plan["hop_addresses"][index]
            identity = dict(
                source=source,
                target=peer,
                host=spec["host"],
                port=spec["port"],
                rule_id=spec["rule_id"],
                protocol=protocol,
                path=[
                    (n, nodes[n]["addr"], by_node.get(n, {}).get("port"))
                    for n in path
                    if n in nodes
                ],
                endpoint=(
                    (target["id"], target["host"], target["port"]) if terminal and target else None
                ),
            )
            if peer and source and not terminal:
                shared = probe_store.pair_identity(nodes, source, peer, protocol)
            elif terminal and source and source in nodes and spec["host"]:
                shared = dict(
                    kind="path",
                    protocol=protocol,
                    source=(source, nodes[source]["addr"]),
                    path=[
                        (n, nodes[n]["addr"], by_node[n]["port"]) for n in path[1:] if n in by_node
                    ],
                    endpoint=(target["host"], target["port"]),
                )
            else:
                shared = None
            if explicit and shared:
                shared["source"] = (source, spec.get("source_route", ""))
                if shared["kind"] == "pair":
                    shared["target"] = (peer, spec["host"])
                else:
                    shared["path"] = [
                        (
                            n,
                            plan["hop_addresses"][expected.index(n)],
                            plan["routing"][expected.index(n)],
                        )
                        for n in path[1:]
                        if n in expected
                    ]
                shared["source_route"] = spec.get("source_route", "")
            spec.update(path_key=fingerprint(shared or identity), identity=shared, reason=reason)
            probes.append(spec)
        result.append(dict(id=sid, kind=kind, label=label, labels=labels, probes=probes))
    return result


def reason(monitor, spec, nodes):
    if spec["reason"]:
        return spec["reason"]
    source = nodes.get(spec["source"])
    if (
        not source
        or source["deleting"]
        or source["desired_mode"] != "active"
        or (source["installed_agent"] != "active")
        or source["manual_required"]
    ):
        return ("unknown", "发起端不是已确认的主动节点")
    if not source["quality_port"]:
        return ("disabled", "节点已关闭质量探测")
    caps = agent_v3.node_capabilities(source) or ()
    cap = {
        "tcp-connect-v1": "network_quality_tcp",
        "icmp-echo-v1": "network_quality_icmp",
        "udp-echo-v1": "network_quality",
    }[spec["protocol"]]
    if cap not in caps:
        return ("unsupported", "请升级发起端 Agent")
    session = monitor.hub.sessions.get(source["id"])
    if not session or session.get("paused") or (not session.get("quality_capable")):
        return ("offline", "主动 Agent 未连接")
    if spec["target"]:
        pair = dict(
            source=spec["source"],
            target=spec["target"],
            protocol=spec["protocol"],
            port=spec["port"],
        )
        return monitor.unavailable(nodes, pair)
    for nid in spec["route"]:
        n = nodes.get(nid)
        if n and (n["manual_required"] or n["applied_generation"] != n["desired_generation"]):
            return ("waiting", "等待路径配置确认")
    return None


async def view(monitor, body, now):
    import network_quality as q

    def read(c):
        link = configurations.get_topology(c, body["id"])
        nodes = {r["id"]: dict(r) for r in c.execute("SELECT * FROM nodes")}
        members = {m["id"]: m for m in configurations.members(c, link["id"]) if not m["deleted"]}
        choices = [
            dict(id=m["id"], name=m["name"])
            for m in members.values()
        ]
        selected = body.get("configuration", choices[0]["id"] if choices else None)
        if selected is not None and (
            type(selected) is not int or selected not in {x["id"] for x in choices}
        ):
            import errors

            raise errors.UserError("请选择此链路中的配置", "configuration")
        result, tags = [], {}
        endpoint = None
        if selected is not None:
            config = members[selected]
            plan = execution.runtime_plan(config) or json.loads(config["desired"])
            target = execution.applied_target(c, config) if config["applied"] else target_addresses.resolve(c, plan)
            endpoint = target_addresses.describe(c, plan, target)
        for section in sections(c, link, nodes, configuration=selected):
            for item in section["labels"]:
                if item.get("node_id") in nodes:
                    nid = item["node_id"]
                    if nid not in tags:
                        tags[nid] = node_tags.listing(c, nid)
                    item["tags"] = tags[nid]
                if "target_id" in item:
                    item["address"] = endpoint
            series = []
            for spec in section.pop("probes"):
                rows = probe_store.link_rows(
                    c, spec, now - max(body.get("range", 900), 300) - 300, now,
                    bucket=q.HISTORY_STEPS[body.get("range", 900)],
                )
                why = reason(monitor, spec, nodes)
                history = q.summarize(rows, body.get("range", 900), now)
                history["summary"] = q.aggregate(
                    [
                        r
                        for r in rows
                        if now - 300 <= r["slot"] <= now and r["path_key"] == spec["path_key"]
                    ]
                )
                series.append(
                    dict(
                        protocol=spec["protocol"],
                        state=why[0] if why else "collecting",
                        note=why[1] if why else "",
                        path_key=spec["path_key"],
                        **history,
                    )
                )
            result.append(dict(section, series=series))
        return dict(
            name=link["name"],
            sections=result,
            now=now,
            interval=q.INTERVAL,
            configurations=choices,
            configuration=selected,
        )

    return await monitor.hub.database(read, readonly=True)
