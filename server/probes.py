"""One scheduler for pair and full-path probes, independent of their UI consumers."""

import asyncio
import logging
import math
import secrets
import time

import link_quality
import network_quality as q
import probe_store as store
import pools


class Monitor:
    def __init__(self, hub):
        self.hub = hub
        self.extra = {}
        self.pending = {}
        self.last = {}
        self.budget = {}
        self.task = None
        self.cleanup_at = 0
        self.streams = {}
        self.last_tick = None

    async def view(self, body):
        return await q.view(self, body)

    def unavailable(self, nodes, pair):
        reason = q.stopped(nodes, pair)
        if reason:
            return reason
        for nid in (pair["source"], pair["target"]):
            session = self.hub.sessions.get(nid)
            if not session or session.get("paused") or not session.get("quality_capable"):
                return ("offline", "主动 Agent 未连接")
        if pair["protocol"] == "icmp-echo-v1":
            return None
        target = self.hub.sessions[pair["target"]]
        listener = target.get("quality_listener", {})
        if listener.get("port") != pair["port"] or listener.get("id") != target.get("quality_setup_id"):
            return ("waiting", "等待探测端口就绪")
        if listener.get("error"):
            return ("error", listener["error"])
        return None

    def plan(self, c, now):
        nodes = {r["id"]: dict(r) for r in c.execute("SELECT * FROM nodes")}
        routes = q.paths(c)
        linked = {r[0] for r in c.execute("SELECT chain_id FROM links WHERE chain_id IS NOT NULL")}
        edges = {
            (scope, ident, a, b)
            for (scope, ident), ids in routes.items()
            if scope == "routes" or (scope == "configs" and ident not in linked)
            for a, b in zip(ids, ids[1:])
            if a != b
        }
        for link in c.execute("SELECT * FROM links WHERE state NOT IN ('draft','deleting')"):
            plan = pools.runtime_plan(link)
            if plan and "graph" in plan:
                edges.update(("links", link["id"], a, b) for a,b in pools.quality_paths(c, link["id"])[1])
        for (scope, ident, a, b), expiry in self.extra.copy().items():
            if expiry > now and a in routes.get((scope, ident), []) and b in routes[(scope, ident)]:
                edges.add((scope, ident, a, b))
        demands = []
        for scope, ident, a, b in sorted(edges):
            if a not in nodes or b not in nodes:
                continue
            for protocol in q.PROTOCOLS:
                pair = q.ensure_pair(c, nodes, a, b, protocol)
                identity = store.pair_identity(nodes, a, b, protocol)
                demands.append(
                    dict(
                        scope=scope,
                        owner_id=ident,
                        section=f"pair:{a}:{b}",
                        protocol=protocol,
                        identity=identity,
                        path_key=store.fingerprint(identity),
                        path_label="",
                        source=a,
                        target=b,
                        host=pair["target_addr"],
                        port=pair["port"],
                        rule_id=0,
                        route=[a, b],
                        reason=self.unavailable(nodes, pair),
                        pair=pair,
                    )
                )
        for link in c.execute("SELECT * FROM links WHERE state NOT IN ('draft','deleting')"):
            for section in link_quality.sections(c, link, nodes):
                for spec in section["probes"]:
                    spec.update(
                        scope="links", owner_id=link["id"], reason=link_quality.reason(self, spec, nodes)
                    )
                    demands.append(spec)
        return nodes, demands

    def bind(self, c, now):
        nodes, demands = self.plan(c, now)
        store.bind(c, demands, now)
        tasks = {}
        for demand in demands:
            if demand["task_id"] and not demand["reason"]:
                tasks.setdefault(demand["task_id"], []).append(demand)
        return nodes, tasks

    async def setup(self, nodes, demands, now):
        # Only UDP needs a peer listener. ICMP and target TCP do not open one.
        needed = set()
        for d in demands:
            if d["protocol"] == "udp-echo-v1" and d["target"] and not q.stopped(nodes, d):
                needed.update((d["source"], d["target"]))
        for nid, session in list(self.hub.sessions.items()):
            if not session.get("quality_capable") or session.get("paused"):
                continue
            port = nodes[nid]["quality_port"] if nid in needed else 0
            listener = session.get("quality_listener")
            retry = (not listener or listener.get("error")) and now - session.get("quality_setup_at", 0) >= 30
            if session.get("quality_port") == port and not retry:
                continue
            session.update(
                quality_setup_at=now,
                quality_port=port,
                quality_setup_id=secrets.token_hex(16),
                quality_key=secrets.token_hex(32),
            )
            session.pop("quality_listener", None)
            try:
                await asyncio.wait_for(
                    session["send"](
                        dict(
                            type="quality_setup",
                            id=session["quality_setup_id"],
                            port=port,
                            key=session["quality_key"],
                        )
                    ),
                    2,
                )
            except (ConnectionError, asyncio.TimeoutError):
                continue

    def current_session(self, task):
        spec = task["spec"]
        if self.hub.sessions.get(spec["source"]) is not task["session"]:
            return False
        if spec["target"]:
            target = self.hub.sessions.get(spec["target"])
            if target is not task["target_session"]:
                return False
            if spec["protocol"] == "udp-echo-v1" and target.get("quality_setup_id") != task["setup_id"]:
                return False
        return True

    async def tick(self):
        now = int(time.time())
        if self.last_tick is not None and not 0 <= now - self.last_tick <= 5:
            self.streams.clear()
        self.last_tick = now
        self.extra = {k: v for k, v in self.extra.items() if v > now}
        nodes, demands = await self.hub.database(lambda c: self.plan(c, now))
        await self.setup(nodes, demands, now)
        _, tasks = await self.hub.database(lambda c: self.bind(c, now))
        self.streams = {k: v for k, v in self.streams.items() if k in tasks}
        if now >= self.cleanup_at:
            await self.hub.database(lambda c: store.cleanup(c, now - q.RETENTION))
            self.cleanup_at = now + 60
        self.last = {k: v for k, v in self.last.items() if k in tasks}
        missing = set(tasks) - self.last.keys()
        if missing:
            # Preserve the cadence across server restarts and short consumer changes.
            restored = await self.hub.database(
                lambda c: {
                    key: c.execute(
                        "SELECT COALESCE(MAX(slot),0) FROM probe_samples WHERE task_id=?", (key,)
                    ).fetchone()[0]
                    for key in missing
                }
            )
            self.last.update(restored)
        for op, task in list(self.pending.items()):
            if task["task_id"] not in tasks or not self.current_session(task):
                self.pending.pop(op, None)
            elif now - task["at"] >= 8:
                self.pending.pop(op, None)
                await self.hub.database(
                    lambda c: store.record(c, task["task_id"], task["at"], "error", error="探测结果未返回",
                                           epoch=task["epoch"], previous_slot=task["previous_slot"])
                )
        busy = {t["task_id"] for t in self.pending.values()}
        self.budget = {n: t for n, t in self.budget.items() if t >= now - 1}
        for task_id, specs in sorted(tasks.items(), key=lambda item: self.last.get(item[0], 0)):
            if task_id in busy or now - self.last.get(task_id, 0) < q.INTERVAL:
                continue
            spec = specs[0]
            touched = {n for n in spec["route"] if n}
            if any(self.budget.get(n) == now for n in touched):
                continue
            if sum(t["spec"]["source"] == spec["source"] for t in self.pending.values()) >= 2:
                continue
            for n in touched:
                self.budget[n] = now
            self.last[task_id] = now
            source = self.hub.sessions[spec["source"]]
            target = self.hub.sessions.get(spec["target"])
            session_key = (source.get("id", id(source)), target.get("id", id(target)) if target else None,
                           target.get("quality_setup_id") if target else None)
            stream = self.streams.get(task_id)
            if stream is None or stream["session"] != session_key:
                stream = self.streams[task_id] = dict(epoch=secrets.token_hex(16), previous=None, session=session_key)
            op = secrets.token_hex(16)
            self.pending[op] = dict(
                task_id=task_id,
                spec=spec,
                at=now,
                session=source,
                target_session=target,
                setup_id=target.get("quality_setup_id") if target else None,
                epoch=stream["epoch"], previous_slot=stream["previous"],
            )
            stream["previous"] = now
            message = dict(
                type="quality_probe",
                id=op,
                protocol=spec["protocol"],
                host=spec["host"],
                port=spec["port"],
                key=target.get("quality_key", "") if target else "",
            )
            if spec["rule_id"]:
                message["rule_id"] = spec["rule_id"]
            try:
                await asyncio.wait_for(source["send"](message), 2)
            except (ConnectionError, asyncio.TimeoutError):
                failed = self.pending.pop(op)
                await self.hub.database(
                    lambda c: store.record(c, task_id, now, "error", error="探测指令发送失败",
                                           epoch=failed["epoch"], previous_slot=failed["previous_slot"])
                )

    async def result(self, nid, session, data):
        task = self.pending.get(data.get("id"))
        if not task or task["session"] is not session or task["spec"]["source"] != nid:
            return
        if not self.current_session(task):
            self.pending.pop(data["id"], None)
            return
        status, rtt = data.get("status"), data.get("rtt")
        if status not in ("ok", "timeout", "error"):
            return
        if status == "ok" and (
            type(rtt) not in (int, float) or not math.isfinite(rtt) or not 0 <= rtt <= 4000
        ):
            return
        self.pending.pop(data["id"], None)

        def save(c):
            _, tasks = self.bind(c, int(time.time()))
            if task["task_id"] in tasks and self.current_session(task):
                store.record(
                    c,
                    task["task_id"],
                    task["at"],
                    status,
                    rtt if status == "ok" else None,
                    data.get("error", ""),
                    data.get("resolved_ip", ""),
                    epoch=task["epoch"], previous_slot=task["previous_slot"],
                )

        await self.hub.database(save)

    async def run(self):
        while True:
            try:
                await self.tick()
            except (ConnectionError, asyncio.TimeoutError):
                pass
            except Exception:
                logging.exception("network quality scheduler")
            await asyncio.sleep(1)
