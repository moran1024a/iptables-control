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
import wake

OFFLINE = 60
FAULT = 30
RECOVERY = 30
MAX_ATTEMPTS = 3


class Engine:
    def __init__(self, now=None):
        self.started = int(time.time()) if now is None else now
        self.observed = {}

    def health(self, nodes, now):
        self.observed = {k: v for k, v in self.observed.items() if k in nodes}
        bad, healthy = {}, set()
        for nid, n in nodes.items():
            if n["desired_mode"] != "active" or n["installed_agent"] != "active" or n["manual_required"]:
                self.observed.pop(nid, None)
                continue
            seen = n["last_seen"]
            recent = seen is not None and 0 <= now-seen < 30
            runtime = n["runtime_state"] if 0 <= now-n["runtime_at"] < 30 else "unknown"
            checked = runtime == "ok" or "forwarding_health" not in json.loads(n["capabilities"])
            state = "error" if recent and runtime == "error" else "ok" if recent and n["state"] == "ok" and checked else "unknown"
            old = self.observed.get(nid)
            # A changed boot or a gap in reports restarts the stability interval.
            if not old or old["state"] != state or old["boot"] != n["boot_id"] or (seen or 0)-(old["seen"] or 0) >= 30:
                old = dict(state=state, since=now, boot=n["boot_id"], seen=seen)
                self.observed[nid] = old
            old["seen"] = seen
            if seen is not None and now-seen >= OFFLINE:
                bad[nid] = "持续失联"
            elif state == "error" and now-old["since"] >= FAULT:
                bad[nid] = n["runtime_error"] or "运行中的转发规则异常"
            if state == "ok" and now-old["since"] >= RECOVERY:
                healthy.add(nid)
        return bad, healthy

    def tick(self, c, now=None):
        now = int(time.time()) if now is None else now
        nodes = {n["id"]: dict(n) for n in c.execute("SELECT * FROM nodes")}
        bad, healthy = self.health(nodes, now)
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
        graph_by = {v["id"]: v for v in graph["vertices"]}
        old_nodes = {v["node_id"] for v in by.values() if v.get("node_id")}
        entry = current["graph_path"][0]
        target = current["graph_path"][-1]
        if entry not in graph_by or target not in graph_by:
            return

        def allowed(v):
            if not v.get("node_id"):
                return True
            n = nodes[v["node_id"]]
            if n["state"] != "ok":
                return False
            try:
                execution.ready(c, n["id"], now)
            except errors.UserError:
                return False
            if n["desired_mode"] == "passive":
                return n["id"] in old_nodes and bool(wake.eligible(n))
            return n["id"] in healthy and n["id"] not in bad

        def edge_allowed(a, b):
            v = graph_by[a]
            if not v.get("node_id"):
                return True
            selected = graph["bindings"][a+"/"+b]["from"][a]
            if selected == "@auto":
                return True
            snapshot = node_addresses.network(c, v["node_id"])
            address = copy.deepcopy(graph["address_book"][a][selected])
            node_addresses.resolve(address, snapshot["addresses"])
            return not node_addresses.egress_problem(address, snapshot)

        chosen = None
        before = failures[0] if failures else len(current["graph_path"])-1
        for length in range(before, 0, -1):
            prefix = current["graph_path"][:length]
            if by[prefix[-1]]["kind"] == "direct":
                continue
            chosen = topology.find_path(graph, entry=entry, target=target, prefix=prefix,
                                        allowed=allowed, edge_allowed=edge_allowed,
                                        randomize=True, forbidden=attempts+[current["graph_path"]])
            if chosen:
                break
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
