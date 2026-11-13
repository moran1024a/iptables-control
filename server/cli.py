import argparse
import http.client
import os
import shutil
import sqlite3
import sys
import database
import errors
import migrations
import security
import settings
import static_files


def print_info(conn, listen=None):
    base = (
        database.setting(conn, "public_url", "")
        or f"http://<服务器地址>:{(listen or '0.0.0.0:8730').rpartition(':')[2]}"
    )
    print(f"  面板：    {base}{database.setting(conn, 'panel_path')}/")
    print(f"  同步路径：{database.setting(conn, 'sync_path')}")
    if database.setting(conn, "must_change") == "1":
        print(f"  默认账号：{settings.DEFAULT_USER} / {settings.DEFAULT_PASS}（首次登录后须修改）")


def cmd_serve(args):
    if not shutil.which("openssl"):
        raise errors.UserError("缺少依赖：openssl")
    if "index.html" not in static_files.files():
        raise errors.UserError(f"缺少面板文件（应位于 {settings.WEB_DIR}）；从源码运行请先在 web/ 目录执行 npm ci && npm run build")
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise errors.UserError("需要 SQLite 3.35 或以上版本")
    conn = migrations.init_db()

    host, _, port = args.listen.rpartition(":")
    print(f"ictl-server {settings.VERSION} 监听 {args.listen}", flush=True)
    print_info(conn, args.listen)
    conn.close()
    import transport

    transport.serve(host or "0.0.0.0", int(port))


def cmd_info(args):
    conn = database.read_db()
    try:
        print_info(conn)
    finally:
        conn.close()


def cmd_health(args):
    conn = database.read_db()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != settings.DATABASE_VERSION:
            raise errors.UserError(
                f"数据库尚未就绪（当前结构版本 {version}，要求 {settings.DATABASE_VERSION}）"
            )
        path = database.setting(conn, "panel_path") + "/"
    finally:
        conn.close()
    client = http.client.HTTPConnection("127.0.0.1", 8730, timeout=2)
    try:
        client.request("GET", path)
        response = client.getresponse()
        if response.status != 200 or b"<title>ictl</title>" not in response.read():
            raise errors.UserError("HTTP 服务尚未就绪")
    finally:
        client.close()


def cmd_reset_admin(args):
    conn = database.db()
    with conn:
        security.reset_admin(conn)
    print(
        f"管理员已重置为 {settings.DEFAULT_USER} / {settings.DEFAULT_PASS}，所有登录会话已失效。首次登录后须修改。"
    )


def main():
    p = argparse.ArgumentParser(prog="ictl-server", description=f"ictl 服务端 {settings.VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="运行服务（首次运行自动初始化）")
    s.add_argument("--listen", default="0.0.0.0:8730", help="监听地址，默认 0.0.0.0:8730")
    s.set_defaults(func=cmd_serve)
    s = sub.add_parser("info", help="显示面板地址和同步路径")
    s.set_defaults(func=cmd_info)
    s = sub.add_parser("health", help="只读数据库与 HTTP 就绪检查")
    s.set_defaults(func=cmd_health)
    s = sub.add_parser("reset-admin", help="重置管理员账号为默认值")
    s.set_defaults(func=cmd_reset_admin)
    args = p.parse_args()
    os.umask(63)
    try:
        args.func(args)
    except errors.UserError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
