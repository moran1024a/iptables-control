"""Weighted, per-configuration recovery. Only execution.py changes forwarding rules."""

import copy
import json
import logging
import time

import configurations
import database
import errors
import execution
import node_addresses
import topology
import target_addresses
import wake
import agent_v3

OFFLINE = 60
FAULT = 30
RECOVERY = 30
MAX_ATTEMPTS = 3


def offline_timeout(c):
    try:
        value = int(database.setting(c, "failover_offline_seconds", OFFLINE))
    except (TypeError, ValueError):
        return OFFLINE
    return value if 30 <= value <= 3600 else OFFLINE


def select_path(c, graph, current, nodes, bad, healthy, now, attempts=()):
    """Read-only selector shared by recovery and hypothetical node failures."""
    by = {v["id"]: v for v in current["graph"]["vertices"]}
    graph_by = {v["id"]: v for v in graph["vertices"]}
    old_nodes = {v["node_id"] for v in by.values() if v.get("node_id")}
    entry, target = current["graph_path"][0], current["graph_path"][-1]
    if entry not in graph_by or target not in graph_by:
        return None
    node_ok, edge_ok, networks = {}, {}, {}

    def allowed(v):
        nid = v.get("node_id")
        if not nid:
            return True
        if nid not in node_ok:
            n = nodes.get(nid)
            node_ok[nid] = False
            if n and nid not in bad and n["state"] == "ok":
                try:
                    execution.ready(c, nid, now)
                    node_ok[nid] = (nid in old_nodes and bool(wake.eligible(n))
                                    if n["desired_mode"] == "passive" else nid in healthy)
                except errors.UserError:
                    pass
        return node_ok[nid]

    def edge_allowed(a, b):
        key = a + "/" + b
        if key not in edge_ok:
            v = graph_by[a]
            edge_ok[key] = True
            if v.get("node_id"):
                selected = graph["bindings"][key]["from"][a]
                if selected != "@auto":
                    nid = v["node_id"]
                    if nid not in networks:
                        networks[nid] = node_addresses.network(c, nid)
                    snapshot = networks[nid]
                    address = copy.deepcopy(graph["address_book"][a][selected])
                    node_addresses.resolve(address, snapshot["addresses"])
                    edge_ok[key] = not node_addresses.egress_problem(address, snapshot)
        return edge_ok[key]

    failures = [i for i, vid in enumerate(current["graph_path"])
                if by[vid]["kind"] == "node" and by[vid]["node_id"] in bad]
    before = failures[0] if failures else len(current["graph_path"]) - 1
    for length in range(before, 0, -1):
        prefix = current["graph_path"][:length]
        if by[prefix[-1]]["kind"] == "direct":
            continue
        # Stable ties also make repeated previews agree with actual recovery.
        chosen = topology.find_path(graph, entry=entry, target=target, prefix=prefix,
                                    allowed=allowed, edge_allowed=edge_allowed,
                                    forbidden=list(attempts) + [current["graph_path"]])
        if chosen:
            return chosen
    return None


class Engine:
    def __init__(self, now=None):
        self.started = int(time.time()) if now is None else now
        self.observed = {}

    def health(self, nodes, now, offline=OFFLINE, *, observe=True):
        # Publish immutable observations. HTTP previews can read a snapshot
        # without changing stability timers or racing the monitoring thread.
        previous, observed = self.observed, {}
        bad, healthy = {}, set()
        for nid, n in nodes.items():
            if n["desired_mode"] != "active" or n["installed_agent"] != "active" or n["manual_required"]:
                continue
            seen = n["last_seen"]
            recent = seen is not None and 0 <= now-seen < 30
            runtime = n["runtime_state"] if 0 <= now-n["runtime_at"] < 30 else "unknown"
            caps = agent_v3.node_capabilities(n)
            checked = caps is not None and (runtime == "ok" or "forwarding_health" not in caps)
            state = "error" if recent and runtime == "error" else "ok" if recent and n["state"] == "ok" and checked else "unknown"
            old = previous.get(nid)
            # A changed boot or a gap in reports restarts the stability interval.
            if not old or old["state"] != state or old["boot"] != n["boot_id"] or (seen or 0)-(old["seen"] or 0) >= 30:
                old = dict(state=state, since=now, boot=n["boot_id"], seen=seen)
            observed[nid] = dict(old, seen=seen)
            # Reports missed while the server itself was down do not count as node downtime.
            if seen is not None and now-max(seen, self.started) >= offline:
                bad[nid] = "持续失联"
            elif state == "error" and now-old["since"] >= FAULT:
                bad[nid] = n["runtime_error"] or "运行中的转发规则异常"
            if state == "ok" and now-old["since"] >= RECOVERY:
                healthy.add(nid)
        if observe:
            self.observed = observed
        return bad, healthy

    def tick(self, c, now=None):
        now = int(time.time()) if now is None else now
        nodes = {n["id"]: dict(n) for n in c.execute("SELECT * FROM nodes")}
        bad, healthy = self.health(nodes, now, offline_timeout(c))
        if now-self.started < RECOVERY:
            return
        for parent in c.execute("SELECT * FROM topologies WHERE failover_enabled=1 AND deleted=0").fetchall():
            for config in configurations.members(c, parent["id"]):
                try:
                    with database.atomic(c):
                        self.recover(c, dict(parent), config, nodes, bad, healthy, now)
                except errors.UserError as exc:
                    status = json.loads(config["failover"])
                    status.update(phase="unavailable", message=str(exc), next_try=now+10)
                    self.record(c, config, status)
                except Exception:
                    logging.exception("failover configuration %s", config["id"])

    def record(self, c, config, status):
        old = json.loads(config["failover"])
        if status.get("message") and status.get("message") != old.get("message"):
            execution.event(c, config["id"], "自动灾备：" + status["message"])
        c.execute("UPDATE configurations SET failover=? WHERE id=?", (execution.dump(status), config["id"]))

    def preview(self, c, ident, vertex, revision=None, now=None):
        now = int(time.time()) if now is None else now
        parent = configurations.get_topology(c, ident)
        if revision is not None and revision != parent["revision"]:
            raise errors.UserError("拓扑已变化，请刷新后预览", status=409)
        raw = json.loads(parent["graph"])
        configs = configurations.members(c, ident)
        v = next((v for v in raw["vertices"] if v["id"] == vertex), None)
        # A confirmed route may still contain a vertex removed by a pending edit.
        if v is None:
            v = next((v for r in configs if r["applied"]
                      for v in json.loads(r["applied"])["plan"]["graph"]["vertices"]
                      if v["id"] == vertex), None)
        if not v or v["kind"] != "node":
            raise errors.UserError("仅中间节点支持灾备预览")
        nodes = {n["id"]: dict(n) for n in c.execute("SELECT * FROM nodes")}
        node = nodes.get(v["node_id"])
        result = dict(vertex=vertex, node_id=v["node_id"], revision=parent["revision"],
                      at=now, offline_seconds=offline_timeout(c), paths=[], message="", graph=raw)
        if parent["deleted"]:
            result["message"] = "链路正在删除"
        elif not node or node["desired_mode"] != "active":
            result["message"] = "被动节点不参与失联自动判定"
        elif node["installed_agent"] != "active" or node["manual_required"]:
            result["message"] = "节点尚未完成主动 Agent 同步"
        elif not parent["failover_enabled"]:
            result["message"] = "自动灾备未开启"
        elif now - self.started < RECOVERY:
            result["message"] = "服务端正在观察节点健康状态"
        if result["message"]:
            return result
        graph = topology.validate(c, raw)
        result["graph"] = graph
        bad, healthy = self.health(nodes, now, result["offline_seconds"], observe=False)
        bad[node["id"]] = "假设失联"
        healthy.discard(node["id"])
        busy = {r[0] for r in c.execute("SELECT configuration_id FROM jobs")}
        for config in configs:
            if config["deleted"] or not config["applied"]:
                continue
            applied = json.loads(config["applied"])
            current = applied["plan"]
            if not any(v["kind"] == "node" and v["node_id"] == node["id"]
                       for v in current["graph"]["vertices"]):
                continue
            row = dict(id=config["id"], name=config["name"], entry=config["entry"],
                       target=config["target"], active_path=current["graph_path"],
                       path=[], message="", state="blocked")
            result["paths"].append(row)
            status = json.loads(config["failover"])
            if status.get("phase") == "paused":
                row["message"] = "自动灾备已暂停，等待手动处理"
            elif config["id"] in busy:
                row["message"] = "配置正在切换，等待当前任务完成"
            elif status.get("phase") in ("pending", "retry"):
                row["message"] = "配置正在灾备处理"
            elif now < status.get("next_try", 0):
                row["message"] = "等待下一次灾备重试"
            elif not config["desired"] or config["state"] != "active" or execution.operational(json.loads(config["desired"])) not in (
                    execution.operational(current), execution.operational(applied.get("intent", current))):
                row["message"] = "配置有待生效变更，暂不自动切换"
            if row["message"]:
                continue
            attempts = status.get("attempts", []) if status.get("phase") == "unavailable" else []
            if len(attempts) >= MAX_ATTEMPTS:
                row["message"] = "候选尝试次数已达上限，等待手动处理"
                continue
            try:
                chosen = select_path(c, graph, current, nodes, bad, healthy, now, attempts)
                if not chosen:
                    row.update(state="unavailable", message="无可用备用路径")
                    continue
                plan = topology.compile_plan(graph, chosen, current["port"])
                common = 0
                for a, b in zip(current["graph_path"], chosen):
                    if a != b:
                        break
                    common += 1
                switch_at = next(i for i in reversed(chosen[:common])
                                 if next(v for v in graph["vertices"] if v["id"] == i)["kind"] != "direct")
                row.update(state="available", path=chosen, switch_at=switch_at,
                           weight=topology.path_weight(graph, chosen),
                           target_address=target_addresses.describe(c, plan, target_addresses.resolve(c, plan)))
            except errors.UserError as exc:
                row.update(state="unavailable", message=str(exc))
        if not result["paths"]:
            result["message"] = "当前没有使用此节点的配置"
        return result

    def recover(self, c, parent, config, nodes, bad, healthy, now):
        if config["deleted"] or not config["applied"] or not config["desired"]:
            return
        status = json.loads(config["failover"])
        if status.get("phase") == "paused":
            return
        if c.execute("SELECT 1 FROM jobs WHERE configuration_id=?", (config["id"],)).fetchone():
            return
        if status.get("phase") == "pending":
            if config["state"] == "active":
                self.record(c, config, dict(phase="done", message="备用路径已确认生效", at=now))
                return
            if config["state"] != "error":
                planned = json.loads(config["desired"])
                if not any(v["kind"] == "node" and v["node_id"] in bad for v in planned["graph"]["vertices"]):
                    return
                # No job exists, so preparation has not sent any configuration.
                # A candidate that failed while waiting for shared nodes is safe to replace.
            status.update(phase="retry", message="候选下发失败，寻找其他路径")
        if now < status.get("next_try", 0):
            return
        applied = json.loads(config["applied"])
        current, desired = applied["plan"], json.loads(config["desired"])
        automatic = bool(status.get("attempts")) and status.get("phase") in ("retry", "unavailable")
        if not automatic and (config["state"] != "active" or execution.operational(desired) not in (
                execution.operational(current), execution.operational(applied.get("intent", current)))):
            return  # A human has a newer design; never replace it with recovery work.
        by = {v["id"]: v for v in current["graph"]["vertices"]}
        failures = [i for i, vid in enumerate(current["graph_path"])
                    if by[vid]["kind"] == "node" and by[vid]["node_id"] in bad]
        if not failures and not automatic:
            if status.get("phase") == "unavailable":
                self.record(c, config, {})
            return
        if automatic and all(v["node_id"] in healthy for v in by.values() if v["kind"] == "node"):
            # An explicitly failed attempt is already rolled back by execution.
            # Keep the recovered original route instead of starting another switch.
            c.execute("UPDATE configurations SET desired=?,state='pending',error='',revision=revision+1 WHERE id=?",
                      (execution.dump(applied.get("intent", current)), config["id"]))
            c.execute("UPDATE topologies SET revision=revision+1 WHERE id=?", (parent["id"],))
            self.record(c, config, dict(phase="done", message="原路径已恢复，取消备用切换", at=now))
            return
        attempts = status.get("attempts", []) if status.get("phase") in ("retry", "unavailable") else []
        if len(attempts) >= MAX_ATTEMPTS:
            self.record(c, config, dict(phase="paused", attempts=attempts,
                        message="连续 3 条候选下发失败，等待手动处理", at=now))
            return
        graph = topology.validate(c, json.loads(parent["graph"]))
        chosen = select_path(c, graph, current, nodes, bad, healthy, now, attempts)
        if not chosen:
            self.record(c, config, dict(phase="unavailable", attempts=attempts,
                        message="没有可同步的备用路径，等待节点恢复或手动处理", next_try=now+10))
            return
        plan = topology.compile_plan(graph, chosen, current["port"])
        message = "正在切换备用路径（权重 " + str(topology.path_weight(graph, chosen)) + "）"
        self.record(c, config, dict(phase="pending", attempts=attempts+[chosen], message=message, at=now))
        c.execute("UPDATE configurations SET desired=?,state='pending',error='',revision=revision+1 WHERE id=?",
                  (execution.dump(plan), config["id"]))
        c.execute("UPDATE topologies SET revision=revision+1 WHERE id=?", (parent["id"],))
