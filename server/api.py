from email.message import Message
from functools import partial
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
import static_files
import subscriptions
import target_addresses


def source_address(peer, forwarded=None):
    """Use the last proxy hop under the same trust rule for HTTP and WebSocket."""
    try:
        ip = ipaddress.ip_address(peer)
        if forwarded and (ip.is_private or ip.is_loopback):
            return str(ipaddress.ip_address(forwarded.split(',')[-1].strip()))
    except ValueError:
        pass
    return peer


class Request:
    """Framework-independent HTTP request/response adapter; business actions live in Resources."""

    panel_path = "/"
    sync_path = "/"

    def __init__(self, method, path, headers, body, peer, *, panel_path=None, sync_path=None, failover_engine=None):
        self.command, self.path = method, path
        self.client_address = (peer, 0)
        self.headers = Message()
        for key, value in headers:
            self.headers[key] = value
        self.rfile = io.BytesIO(body)
        self.cookies = []
        self.response = (500, b"", [])
        self.resources = resources.Resources(failover_engine=failover_engine)
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
        return source_address(self.client_address[0], self.headers.get("X-Forwarded-For"))

    def static(self, name):
        found = static_files.response(name, self.headers.get("Accept-Encoding", ""))
        if found is None:
            return self.not_found()
        data, ctype, headers = found
        self.reply(200, data, ctype, headers)

    def api(self, method, route, query):
        conn = database.db()
        try:
            if method != "GET" and (not self.headers.get("Content-Type", "").startswith("application/json")):
                self.close_connection = True
                return self.send_json(400, {"error": "请求格式错误"})
            limit = 1024 * 1024 if route == "topology-preview" or route == "topologies" or re.fullmatch(r"topologies/\d+", route) else settings.MAX_API_BODY
            body = self.read_json(limit) if method != "GET" else {}
            if route == "login" and method == "POST":
                return authentication.api_login(self, conn, body)
            target = re.fullmatch(r"targets(?:/([0-9]{1,19}))?", route)
            if target and ((method == "POST" and not target[1]) or (method == "PATCH" and target[1])):
                conn.execute("BEGIN")
                with conn:
                    caller = authentication.auth(self, conn)
                    hosts = self.resources.target_lookup(conn, body, int(target[1]) if target[1] else None,
                                                         caller.account_id) if caller and not caller.admin else None
                if hosts is not None:
                    # DNS may block. Resolve outside the SQLite write transaction and
                    # check the actual saved endpoints against this result again.
                    self.resources.resolved_targets = target_addresses.resolve_hosts(hosts)
            # Authorization and writes share one transaction with grants and revocation.
            conn.execute("BEGIN" if method == "GET" else "BEGIN IMMEDIATE")
            with conn:
                session = authentication.auth(self, conn)
                if not session:
                    return self.send_json(401, {"error": "未登录"})
                if route == "logout" and method == "POST":
                    conn.execute("DELETE FROM sessions WHERE token_hash=?", (session.token_hash,))
                    self.cookies.append(authentication.session_cookie(self, "", 0))
                    return self.send_json(200, {})
                if route == "me" and method == "GET":
                    result = dict(username=session.username, version=settings.VERSION,
                                  must_change=session.must_change, admin=session.admin)
                elif route == "account" and method == "POST":
                    return authentication.api_account(self, conn, body, session)
                else:
                    if session.must_change:
                        return self.send_json(403, {"error": "请先修改默认账号和密码", "must_change": True})
                    result = self.panel(conn, method, route, query, body, session)
            authentication.renew(self, conn, session)
            self.send_json(200, result)
        except errors.UserError as e:
            self.send_json(e.status, {"error": str(e), "field": e.field})
        finally:
            conn.close()

    @staticmethod
    def require_admin(session):
        if not session.admin:
            raise errors.UserError("仅管理员可操作", status=403)

    def panel(self, conn, method, route, query, body, session):
        self.resources.account_id = session.account_id
        parts = route.split("/")
        handler = None
        if parts == ["accounts"]:
            self.require_admin(session)
            handler = {"GET": self.resources.api_accounts, "POST": self.resources.api_account_save}.get(method)
        elif len(parts) == 2 and parts[0] == "accounts" and re.fullmatch(r"[0-9]+", parts[1]):
            self.require_admin(session)
            handler = {"PATCH": self.resources.api_account_save, "DELETE": self.resources.api_account_delete}.get(method)
        elif (len(parts) == 3 and parts[0] == "accounts" and re.fullmatch(r"[0-9]+", parts[1])
              and parts[2] == "login" and method == "POST"):
            handler = partial(authentication.login_as_child, self, session)
        elif parts == ["nodes"]:
            handler = {"GET": self.resources.api_nodes, "POST": self.resources.api_node_add}.get(method)
        elif len(parts) == 2 and parts[0] == "nodes" and re.fullmatch(r"[0-9]+", parts[1]):
            handler = {
                "GET": self.resources.api_node_get,
                "PATCH": self.resources.api_node_edit,
                "DELETE": self.resources.api_node_del,
            }.get(method)
        elif len(parts) == 3 and parts[0] == "nodes" and re.fullmatch(r"[0-9]+", parts[1]) and (parts[2] == "rekey"):
            handler = {"POST": self.resources.api_node_rekey}.get(method)
        elif parts == ["configs"]:
            handler = {"GET": self.resources.api_configs}.get(method)
        elif parts == ["external-config-preview"]:
            handler = {"POST": self.resources.api_external_preview}.get(method)
        elif parts == ["external-configs"]:
            handler = {"POST": self.resources.api_external_save}.get(method)
        elif len(parts) == 2 and parts[0] == "external-configs" and re.fullmatch(r"[0-9]+", parts[1]):
            handler = {"PATCH": self.resources.api_external_save, "DELETE": self.resources.api_external_delete}.get(method)
        elif len(parts) == 3 and parts[0] == 'configs' and re.fullmatch(r"[0-9]+", parts[1]) and parts[2] == 'name':
            handler = {'PATCH': self.resources.api_config_rename}.get(method)
        elif parts == ["topologies"]:
            handler = {"GET": self.resources.api_topologies, "POST": self.resources.api_topology_save}.get(method)
        elif parts == ["topology-preview"]:
            handler = {"POST": self.resources.api_topology_preview}.get(method)
        elif len(parts) == 2 and parts[0] == "topologies" and re.fullmatch(r"[0-9]+", parts[1]):
            handler = {"GET": self.resources.api_topology_get, "POST": self.resources.api_topology_save,
                       "DELETE": self.resources.api_topology_delete}.get(method)
        elif len(parts) == 3 and parts[0] == "topologies" and re.fullmatch(r"[0-9]+", parts[1]) and parts[2] == "action":
            handler = {"POST": self.resources.api_topology_action}.get(method)
        elif len(parts) == 3 and parts[0] == "topologies" and re.fullmatch(r"[0-9]+", parts[1]) and parts[2] == "failover-preview":
            handler = {"GET": self.resources.api_failover_preview}.get(method)
        elif parts == ["targets"]:
            handler = {"GET": self.resources.api_targets, "POST": self.resources.api_target_save}.get(
                method
            )
        elif parts == ["subscriptions"]:
            handler = {
                "GET": self.resources.api_subscriptions,
                "POST": self.resources.api_subscription_save,
            }.get(method)
        elif len(parts) == 2 and parts[0] == "subscriptions" and re.fullmatch(r"[0-9]+", parts[1]):
            handler = {
                "PATCH": self.resources.api_subscription_save,
                "DELETE": self.resources.api_subscription_delete,
            }.get(method)
        elif len(parts) == 2 and parts[0] == "targets" and re.fullmatch(r"[0-9]+", parts[1]):
            handler = {
                "PATCH": self.resources.api_target_save,
                "DELETE": self.resources.api_target_del,
            }.get(method)
        elif len(parts) == 2 and parts[0] in settings.KINDS and (parts[1] == "order"):
            handler = {"POST": self.resources.api_order}.get(method)
            parts = [parts[0]]
        elif parts == ["groups"]:
            handler = {"POST": self.resources.api_group_add}.get(method)
        elif parts == ["tags"]:
            handler = {"GET": self.resources.api_tags, "POST": self.resources.api_tag_save}.get(method)
        elif len(parts) == 2 and parts[0] == "tags" and re.fullmatch(r"[0-9]+", parts[1]):
            handler = {"PATCH": self.resources.api_tag_save, "DELETE": self.resources.api_tag_delete}.get(method)
        elif len(parts) == 2 and parts[0] == "groups" and re.fullmatch(r"[0-9]+", parts[1]):
            handler = {
                "PATCH": self.resources.api_group_edit,
                "DELETE": self.resources.api_group_del,
            }.get(method)
        elif parts == ["settings"]:
            handler = {"GET": self.resources.api_settings, "PATCH": self.resources.api_settings_save}.get(
                method
            )
        if not handler:
            raise errors.UserError("接口不存在", status=404)
        if len(parts) > 1 and (len(parts[1]) > 19 or int(parts[1]) >= 2**63):
            raise errors.UserError("无效的对象 ID", status=404)
        args = (
            [int(parts[1])]
            if len(parts) > 1
            else [parts[0]]
            if handler == self.resources.api_order
            else []
        )
        self.resources.query = query
        if not session.admin and handler not in {
            self.resources.api_nodes, self.resources.api_node_get, self.resources.api_tags,
            self.resources.api_configs, self.resources.api_config_rename, self.resources.api_external_preview,
            self.resources.api_external_save, self.resources.api_external_delete,
            self.resources.api_topologies, self.resources.api_topology_get, self.resources.api_topology_preview,
            self.resources.api_topology_save, self.resources.api_topology_delete, self.resources.api_topology_action,
            self.resources.api_failover_preview, self.resources.api_targets, self.resources.api_target_save,
            self.resources.api_target_del, self.resources.api_subscriptions, self.resources.api_subscription_save,
            self.resources.api_subscription_delete, self.resources.api_order, self.resources.api_group_add,
            self.resources.api_group_edit, self.resources.api_group_del,
        }:
            self.require_admin(session)
        return handler(conn, body, *args)

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
