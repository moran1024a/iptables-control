from aiohttp import web, WSMsgType
import asyncio
import json
import secrets
import time
import agent_v3
import api
import authentication
import lifecycle
import database
import errors
import probes
import execution
import settings
import wake


class Hub:
    def __init__(self):
        self.sessions = {}
        self.queries = {}
        self.quality = probes.Monitor(self)
        self.passive_wake = wake.Monitor(self)
        self.execution = execution.Monitor(self)

    async def database(self, fn, *, readonly=False):

        def run():
            conn = database.db()
            try:
                with conn:
                    conn.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
                    return fn(conn)
            finally:
                conn.close()

        return await asyncio.to_thread(run)

    async def authenticate(self, request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise web.HTTPNotFound()
        key = auth[7:]
        row = await self.database(
            lambda c: c.execute("SELECT * FROM nodes WHERE key=?", (key,)).fetchone()
        )
        if row is None:
            raise web.HTTPNotFound()
        return dict(row)

    def notify(self):
        self.passive_wake.event.set()
        for session in self.sessions.values():
            session["wake"].set()

    async def query(self, nid, body):
        kind = body.get("kind")
        if kind not in (
            "inventory",
            "ports",
            "port_detail",
            "connections",
            "rules",
            "network",
            "logs",
            "diagnose",
        ):
            raise web.HTTPBadRequest(text="无效查询类型")
        session = self.sessions.get(nid)
        if not session or session.get("paused"):
            return web.json_response({"error": "主动 Agent 未连接"}, status=409)
        if sum((key[0] == nid for key in self.queries)) >= 2:
            return web.json_response({"error": "节点已有查询正在执行"}, status=429)
        op = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.queries[nid, op] = (session["id"], future)
        try:
            await session["send"]({"type": "query", "id": op, "query": body})
            result = await asyncio.wait_for(future, 32 if kind == "diagnose" else 12)
            if (
                kind == "inventory"
                and isinstance(result.get("data"), dict)
                and "interfaces" in result["data"]
            ):
                await self.database(
                    lambda c: agent_v3.save_snapshot(c, nid, "inventory", result["data"])
                )
            return web.json_response(result)
        except asyncio.TimeoutError:
            return web.json_response({"error": "查询超时，请重试"}, status=504)
        except ConnectionResetError:
            return web.json_response({"error": "节点连接已断开"}, status=409)
        finally:
            self.queries.pop((nid, op), None)

    async def websocket(self, request):
        node = await self.authenticate(request)
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1024 * 1024)
        await ws.prepare(request)
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 10)
        except (ValueError, asyncio.TimeoutError, TypeError):
            await ws.close(code=1008)
            return ws
        if (
            not isinstance(hello, dict)
            or hello.get("type") != "hello"
            or hello.get("protocol") != 3
        ):
            await ws.close(code=1008)
            return ws
        try:
            capabilities = agent_v3.validate_capabilities(hello.get("capabilities", []))
        except errors.UserError:
            await ws.close(code=1008, message=b"invalid capabilities")
            return ws
        nid = node["id"]
        lock = asyncio.Lock()
        session = {
            "id": secrets.token_hex(16),
            "wake": asyncio.Event(),
            "ws": ws,
            "paused": False,
            "quality_capable": "network_quality" in capabilities,
        }

        async def send(message):
            async with lock:
                await ws.send_json({**message, "session": session["id"]})

        session["send"] = send
        old = self.sessions.get(nid)
        self.sessions[nid] = session
        if old:
            await old["ws"].close(code=1000, message=b"replaced")
        await send({"type": "welcome", "heartbeat": 10, "timeout": 30})
        status = {
            "version": str(hello.get("version", ""))[:40],
            "auto_update": bool(hello.get("auto_update", node["auto_update"])),
            "generation": hello.get("generation", 0),
            "hash": hello.get("hash", ""),
            "source_ip": api.source_address(
                request.remote or "", request.headers.get("X-Forwarded-For")
            ),
            "state": "ok",
        }
        await self.database(
            lambda c: c.execute(
                "UPDATE nodes SET installation_id=?,boot_id=?,capabilities=?,connection_state='connected',runtime_state='unknown',runtime_error='',runtime_at=0 WHERE id=?",
                (
                    str(hello.get("installation_id", ""))[:64],
                    str(hello.get("boot_id", ""))[:64],
                    json.dumps(capabilities),
                    nid,
                ),
            )
        )
        inflight = None

        async def reconcile(received=False):
            nonlocal inflight

            def prepare(c):
                n = c.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
                if n is None or n["key"] != node["key"]:
                    return {"action": "revoked"}
                if received:
                    agent_v3.packet(c, n, status=status, observe_only=True)
                if (
                    n["desired_mode"] != "active"
                    or n["manual_required"]
                    or n["installed_agent"] != "active"
                ):
                    return {"action": "pause"}
                if inflight is not None:
                    return {"action": "waiting"}
                if status.get("auto_update") and agent_v3.newer(
                    settings.COMPONENTS["active_agent"], status["version"]
                ):
                    return {"action": "update"}
                if "held_ports" in status:
                    agent_v3.refresh_held_ports(c, n, status)
                return agent_v3.packet(c, n, status=status, observe=False)

            result = await self.database(prepare)
            action = result.get("action")
            if action == "update":
                await send({"type": "update", "version": settings.COMPONENTS["active_agent"]})
            elif action == "apply":
                inflight = {k: result[k] for k in ("op", "generation", "hash")}
                inflight["deadline"] = time.monotonic() + 600
                await send({"type": "config", **result})
            elif action in ("pause", "revoked"):
                session["paused"] = True
                await send({"type": "quiesce", "reason": action})
            elif action == "blocked":
                await send({"type": "blocked", "error": result.get("err", "")})
            elif action == "same":
                await send({"type": "ready"})

        receive = wake = None
        try:
            await reconcile(received=True)
            receive = asyncio.create_task(ws.receive())
            wake = asyncio.create_task(session["wake"].wait())
            while not ws.closed and self.sessions.get(nid) is session:
                timeout = max(0, inflight["deadline"] - time.monotonic()) if inflight else None
                done, _ = await asyncio.wait(
                    (receive, wake), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if inflight and time.monotonic() >= inflight["deadline"]:
                    await ws.close(code=1011, message=b"configuration receipt timeout")
                    break
                if self.sessions.get(nid) is not session:
                    break
                if wake in done:
                    session["wake"].clear()
                    await reconcile()
                    wake = asyncio.create_task(session["wake"].wait())
                if receive not in done:
                    continue
                message = receive.result()
                if message.type != WSMsgType.TEXT:
                    break
                data = json.loads(message.data)
                if not isinstance(data, dict) or data.get("session") != session["id"]:
                    raise ValueError("invalid session")
                kind = data.get("type")
                if kind == "quiesced":
                    await self.database(
                        lambda c: c.execute(
                            "UPDATE nodes SET connection_state='paused' WHERE id=?", (nid,)
                        )
                    )
                    break
                if session["paused"]:
                    receive = asyncio.create_task(ws.receive())
                    continue
                if kind == "heartbeat":
                    await self.database(lambda c: c.execute(
                        "UPDATE nodes SET last_seen=? WHERE id=? AND key=? AND desired_mode='active' AND manual_required=0",
                        (int(time.time()), nid, node["key"]),
                    ))
                    await reconcile()
                elif kind == "status":
                    status.update(
                        {
                            k: data[k]
                            for k in (
                                "generation",
                                "hash",
                                "state",
                                "error",
                                "auto_update",
                                "held_ports",
                                "forwarding",
                            )
                            if k in data
                        }
                    )
                    await reconcile(received=True)
                elif kind == "apply_result":
                    if not inflight or any(
                        data.get(k) != inflight[k] for k in ("op", "generation", "hash")
                    ):
                        await send({"type": "apply_ack", "id": data.get("op"), "result": "expired"})
                        receive = asyncio.create_task(ws.receive())
                        continue

                    def ack(c):
                        n = c.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
                        return agent_v3.acknowledge(c, n, data) if n else {"result": "deleted"}

                    result = await self.database(ack)
                    await send({"type": "apply_ack", "id": data.get("op"), **result})
                    inflight = None
                    status.update(
                        generation=data.get("generation", 0),
                        hash=data.get("hash", ""),
                        state="ok" if data.get("ok") else "error",
                        error=data.get("error", ""),
                        held_ports=data.get("held_ports", []),
                    )
                    if result.get("result") == "deleted":
                        await send({"type": "quiesce", "reason": "deleted"})
                        session["paused"] = True
                    elif result.get("result") != "failed":
                        await reconcile()
                elif kind in ("metrics", "inventory", "ports"):
                    await self.database(
                        lambda c: agent_v3.save_snapshot(c, nid, kind, data.get("data"))
                    )
                elif kind == "traffic":
                    result = await self.database(
                        lambda c: agent_v3.accept_traffic(c, nid, data.get("data"))
                    )
                    await send({"type": "traffic_ack", "id": data.get("id"), "data": result})
                elif kind == "quality_listener":
                    if data.get("id") == session.get("quality_setup_id"):
                        session["quality_listener"] = data
                elif kind == "quality_result":
                    await self.quality.result(nid, session, data)
                elif kind == "query_result":
                    pending = self.queries.get((nid, data.get("id")))
                    if pending and pending[0] == session["id"] and (not pending[1].done()):
                        pending[1].set_result(data)
                receive = asyncio.create_task(ws.receive())
        except (ValueError, TypeError, errors.UserError) as exc:
            if not ws.closed:
                await send({"type": "error", "error": str(exc)[:500]})
        except ConnectionResetError:
            pass
        finally:
            for task in (receive, wake):
                if task:
                    task.cancel()
            if self.sessions.get(nid) is session:
                self.sessions.pop(nid, None)
                await self.database(
                    lambda c: c.execute(
                        "UPDATE nodes SET connection_state=CASE WHEN connection_state='paused' THEN 'paused' ELSE 'disconnected' END WHERE id=?",
                        (nid,),
                    )
                )
            await ws.close()
        return ws


def create_app():
    conn = database.db()
    try:
        panel_path, sync_path = database.setting(conn, "panel_path"), database.setting(
            conn, "sync_path"
        )
    finally:
        conn.close()
    hub = Hub()
    app = web.Application(client_max_size=1024 * 1024)
    app["hub"] = hub
    prefix = sync_path + "/v3"

    async def asset(request):
        await hub.authenticate(request)
        try:
            path, digest = agent_v3.artifact(request.match_info["name"])
        except errors.UserError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        return web.FileResponse(
            path, headers={"X-Content-SHA256": digest, "Cache-Control": "no-store"}
        )

    async def final_traffic(request):
        node = await hub.authenticate(request)
        data = await request.json()
        try:
            result = await hub.database(lambda c: agent_v3.accept_traffic(c, node["id"], data))
        except errors.UserError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"ack": result})

    async def dispatch(request):
        body = await request.read()
        h = api.Request(
            request.method,
            request.raw_path,
            request.headers.items(),
            body,
            request.remote or "",
            panel_path=panel_path,
            sync_path=sync_path,
        )
        if request.path == panel_path + "/api/quality":
            if request.method != "POST" or request.content_type != "application/json":
                raise web.HTTPBadRequest()
            if not await hub.database(
                lambda c: authentication.auth(h, c) and database.setting(c, "must_change") != "1"
            ):
                return web.json_response({"error": "未登录或需要修改默认密码"}, status=401)
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError()
                response = web.json_response(await hub.quality.view(payload))
                for cookie in h.cookies:
                    response.headers.add("Set-Cookie", cookie)
                return response
            except errors.UserError as exc:
                return web.json_response({"error": str(exc), "field": exc.field}, status=exc.status)
            except (ValueError, TypeError):
                raise web.HTTPBadRequest()
        parts = request.path.removeprefix(panel_path + "/api/").split("/")
        if (
            request.path.startswith(panel_path + "/api/")
            and len(parts) == 3
            and (parts[0] == "nodes")
            and parts[1].isdigit()
            and (parts[2] == "query")
        ):

            def authorized(c):
                return authentication.auth(h, c) and database.setting(c, "must_change") != "1"

            if not await hub.database(authorized):
                return web.json_response({"error": "未登录或需要修改默认密码"}, status=401)
            if request.method != "POST" or request.content_type != "application/json":
                raise web.HTTPBadRequest()
            try:
                query = json.loads(body)
                if not isinstance(query, dict):
                    raise ValueError()
            except ValueError as exc:
                raise web.HTTPBadRequest() from exc
            return await hub.query(int(parts[1]), query)
        await asyncio.to_thread(h.route, request.method)
        code, data, headers = h.response
        response = web.Response(status=code, body=data)
        for key, value in headers:
            if key == "Set-Cookie":
                response.headers.add(key, value)
            else:
                response.headers[key] = value
        if request.method != "GET":
            hub.notify()
        return response

    async def start(_app):
        hub.quality.task = asyncio.create_task(hub.quality.run())
        hub.passive_wake.task = asyncio.create_task(hub.passive_wake.run())
        hub.execution.task = asyncio.create_task(hub.execution.run())

    async def shutdown(_app):
        if hub.execution.task:
            hub.execution.task.cancel()
            await asyncio.gather(hub.execution.task, return_exceptions=True)
        if hub.passive_wake.task:
            hub.passive_wake.task.cancel()
            await asyncio.gather(hub.passive_wake.task, return_exceptions=True)
        if hub.quality.task:
            hub.quality.task.cancel()
            await asyncio.gather(hub.quality.task, return_exceptions=True)
        await asyncio.gather(
            *(
                x["ws"].close(code=1001, message=b"server shutdown")
                for x in list(hub.sessions.values())
            )
        )

    app.router.add_get(prefix + "/ws", hub.websocket)
    app.router.add_get(prefix + "/artifacts/{name}", asset)
    app.router.add_post(prefix + "/traffic", final_traffic)
    app.router.add_route("*", "/{path:.*}", dispatch)
    app.on_startup.append(start)
    app.on_shutdown.append(shutdown)
    return app


def serve(host, port):
    import threading

    threading.Thread(target=lifecycle.cleanup_loop, daemon=True).start()
    app = create_app()
    web.run_app(app, host=host, port=port, print=None)
