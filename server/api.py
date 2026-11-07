from email.message import Message
import io
import ipaddress
import json
import os
from urllib.parse import parse_qs
import re
import sys
import authentication
import database
import errors
import passive_sync
import resources
import settings
import subscriptions


class Request:
    """Framework-independent HTTP request/response adapter; business actions live in Resources."""

    panel_path = "/"
    sync_path = "/"

    def __init__(self, method, path, headers, body, peer, *, panel_path=None, sync_path=None):
        self.command, self.path = method, path
        self.client_address = (peer, 0)
        self.headers = Message()
        for key, value in headers:
            self.headers[key] = value
        self.rfile = io.BytesIO(body)
        self.cookies = []
        self.response = (500, b"", [])
        self.resources = resources.Resources()
        if panel_path is not None:
            self.panel_path = panel_path
        if sync_path is not None:
            self.sync_path = sync_path

    def reply(self, code, data, ctype="text/plain; charset=utf-8", headers=()):
        self.response = (
            code,
            data if isinstance(data, bytes) else data.encode(),
            [("Content-Type", ctype), *headers],
        )

    def not_found(self):
        self.reply(404, settings.NOT_FOUND)

    def send_json(self, code, obj):
        headers = [("Cache-Control", "no-store")] + [("Set-Cookie", c) for c in self.cookies]
        self.reply(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", headers)

    def route(self, method):
        self.cookies = []
        try:
            path, _, query = self.path.partition("?")
            if path == self.sync_path:
                return passive_sync.handle(self) if method == "POST" else self.not_found()
            if method == "GET" and re.fullmatch("/sub/[0-9a-f]{64}", path):
                fmt = parse_qs(query).get("format", ["v2ray"])
                if len(fmt) != 1 or fmt[0] not in ("clash", "v2ray"):
                    return self.not_found()
                conn = database.db()
                try:
                    conn.execute("BEGIN")
                    result = subscriptions.export(conn, path.rsplit("/", 1)[1], fmt[0])
                finally:
                    conn.close()
                if result is None:
                    return self.not_found()
                return self.reply(
                    200,
                    result[0],
                    result[1],
                    [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")],
                )
            if path == self.panel_path and method == "GET":
                return self.reply(302, "", headers=[("Location", self.panel_path + "/")])
            prefix = self.panel_path + "/"
            if path.startswith(prefix):
                sub = path[len(prefix) :]
                if sub.startswith("api/"):
                    return self.api(method, sub[4:], parse_qs(query))
                if method == "GET":
                    return self.static(sub or "index.html")
            self.not_found()
        except Exception as e:
            print(f"{method} error: {e!r}", file=sys.stderr)
            try:
                self.not_found()
            except Exception:
                pass

    def source_ip(self):
        """真实来源 IP。直连来源为本机或内网（反向代理、Docker 网桥）时才信任 X-Forwarded-For，
        公网客户端直连时自带的该请求头会被忽略，无法伪造。"""
        ip = self.client_address[0]
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            peer = ipaddress.ip_address(ip)
            if peer.is_private or peer.is_loopback:
                ip = xff.split(",")[-1].strip()
        return ip

    def static(self, name):
        if name not in settings.STATIC:
            return self.not_found()
        with open(os.path.join(settings.WEB_DIR, name), "rb") as f:
            data = f.read()
        self.reply(200, data, settings.STATIC[name], [("Cache-Control", "no-cache")])

    def api(self, method, route, query):
        conn = database.db()
        try:
            if method != "GET" and (not self.headers.get("Content-Type", "").startswith("application/json")):
                self.close_connection = True
                return self.send_json(400, {"error": "请求格式错误"})
            limit = 1024 * 1024 if route == "topology-preview" or route == "topology" or re.fullmatch(r"topology/\d+", route) else settings.MAX_API_BODY
            body = self.read_json(limit) if method != "GET" else {}
            if route == "login" and method == "POST":
                return authentication.api_login(self, conn, body)
            session = authentication.auth(self, conn)
            if not session:
                return self.send_json(401, {"error": "未登录"})
            if route == "logout" and method == "POST":
                with conn:
                    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session,))
                self.cookies.append(authentication.session_cookie(self, "", 0))
                return self.send_json(200, {})
            if route == "me" and method == "GET":
                return self.send_json(
                    200,
                    {
                        "username": database.setting(conn, "admin_user"),
                        "version": settings.VERSION,
                        "must_change": database.setting(conn, "must_change") == "1",
                    },
                )
            if route == "account" and method == "POST":
                return authentication.api_account(self, conn, body, session)
            if database.setting(conn, "must_change") == "1":
                return self.send_json(403, {"error": "请先修改默认账号和密码", "must_change": True})
            parts = route.split("/")
            handler = None
            if parts == ["nodes"]:
                handler = {"GET": self.resources.api_nodes, "POST": self.resources.api_node_add}.get(method)
            elif len(parts) == 2 and parts[0] == "nodes" and parts[1].isdigit():
                handler = {
                    "GET": self.resources.api_node_get,
                    "PATCH": self.resources.api_node_edit,
                    "DELETE": self.resources.api_node_del,
                }.get(method)
            elif len(parts) == 3 and parts[0] == "nodes" and parts[1].isdigit() and (parts[2] == "rekey"):
                handler = {"POST": self.resources.api_node_rekey}.get(method)
            elif parts in (["configs"], ["chains"]):
                handler = {"GET": self.resources.api_configs, "POST": self.resources.api_config_save}.get(
                    method
                )
            elif len(parts) == 2 and parts[0] in ("configs", "chains") and parts[1].isdigit():
                handler = {
                    "PATCH": self.resources.api_config_save,
                    "DELETE": self.resources.api_chain_del,
                }.get(method)
            elif parts == ["routes"]:
                handler = {"GET": self.resources.api_routes, "POST": self.resources.api_route_save}.get(
                    method
                )
            elif parts == ["topology"]:
                handler = {"POST": self.resources.api_graph_save}.get(method)
            elif len(parts) == 2 and parts[0] == "topology" and parts[1].isdigit():
                handler = {"POST": self.resources.api_graph_save}.get(method)
            elif parts == ["topology-preview"]:
                handler = {"POST": self.resources.api_graph_preview}.get(method)
            elif parts in (["pools"], ["links"]):
                handler = {
                    "GET": self.resources.api_pool_list,
                    "POST": self.resources.api_pool_save
                    if parts[0] == "pools"
                    else self.resources.api_link_save,
                }.get(method)
            elif len(parts) == 2 and parts[0] in ("pools", "links") and parts[1].isdigit():
                handler = (
                    {"PATCH": self.resources.api_pool_save, "DELETE": self.resources.api_pool_delete}
                    if parts[0] == "pools"
                    else {
                        "GET": self.resources.api_link_get,
                        "PATCH": self.resources.api_link_save,
                        "DELETE": self.resources.api_link_delete,
                    }
                ).get(method)
            elif len(parts) == 3 and parts[0] == "links" and parts[1].isdigit() and (parts[2] == "action"):
                handler = {"POST": self.resources.api_link_action}.get(method)
            elif len(parts) == 2 and parts[0] == "routes" and parts[1].isdigit():
                handler = {
                    "PATCH": self.resources.api_route_save,
                    "DELETE": self.resources.api_route_del,
                }.get(method)
            elif parts == ["targets"]:
                handler = {"GET": self.resources.api_targets, "POST": self.resources.api_target_save}.get(
                    method
                )
            elif parts == ["subscriptions"]:
                handler = {
                    "GET": self.resources.api_subscriptions,
                    "POST": self.resources.api_subscription_save,
                }.get(method)
            elif len(parts) == 2 and parts[0] == "subscriptions" and parts[1].isdigit():
                handler = {
                    "PATCH": self.resources.api_subscription_save,
                    "DELETE": self.resources.api_subscription_delete,
                }.get(method)
            elif len(parts) == 2 and parts[0] == "targets" and parts[1].isdigit():
                handler = {
                    "PATCH": self.resources.api_target_save,
                    "DELETE": self.resources.api_target_del,
                }.get(method)
            elif len(parts) == 2 and parts[0] in settings.KINDS and (parts[1] == "order"):
                handler = {"POST": self.resources.api_order}.get(method)
                parts = [parts[0]]
            elif parts == ["groups"]:
                handler = {"POST": self.resources.api_group_add}.get(method)
            elif len(parts) == 2 and parts[0] == "groups" and parts[1].isdigit():
                handler = {
                    "PATCH": self.resources.api_group_edit,
                    "DELETE": self.resources.api_group_del,
                }.get(method)
            elif parts == ["settings"]:
                handler = {"GET": self.resources.api_settings, "PATCH": self.resources.api_settings_save}.get(
                    method
                )
            if not handler:
                return self.send_json(404, {"error": "接口不存在"})
            args = (
                [int(parts[1])]
                if len(parts) > 1
                else [parts[0]]
                if handler == self.resources.api_order
                else []
            )
            self.resources.query = query
            if method != "GET":
                conn.execute("BEGIN IMMEDIATE")
            with conn:
                result = handler(conn, body, *args)
            self.send_json(200, result)
        except errors.UserError as e:
            self.send_json(e.status, {"error": str(e), "field": e.field})
        finally:
            conn.close()

    def body_length(self, limit):
        lengths = self.headers.get_all("Content-Length", [])
        value = lengths[0].strip() if lengths else "0"
        try:
            if (
                len(lengths) > 1
                or self.headers.get("Transfer-Encoding") is not None
                or (not re.fullmatch("[0-9]+", value))
            ):
                raise ValueError
            length = int(value)
        except ValueError:
            self.close_connection = True
            raise errors.UserError("请求格式错误")
        if length > limit:
            self.close_connection = True
            raise errors.UserError("请求过大")
        return length

    def read_json(self, limit=settings.MAX_API_BODY):
        length = self.body_length(limit)
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise errors.UserError("请求格式错误")
        if not isinstance(body, dict):
            raise errors.UserError("请求格式错误")
        return body
