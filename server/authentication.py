import hashlib
import hmac
import http.cookies
import re
import secrets
import threading
import time
import database
import errors
import security
import settings

LOGIN_FAILS = {}
LOGIN_FAILS_LOCK = threading.Lock()


def session_cookie(self, token, max_age):
    c = f"{settings.SESSION_COOKIE}={token}; Path={self.panel_path}/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
    if self.headers.get("X-Forwarded-Proto") == "https":
        c += "; Secure"
    return c


def auth(self, conn):
    """返回当前会话的令牌哈希；剩余有效期不足 6 天时自动续期。"""
    try:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
    except http.cookies.CookieError:
        return None
    if settings.SESSION_COOKIE not in cookie:
        return None
    token = cookie[settings.SESSION_COOKIE].value
    th = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time())
    row = conn.execute("SELECT expires FROM sessions WHERE token_hash = ?", (th,)).fetchone()
    if not row or row["expires"] < now:
        return None
    if row["expires"] - now < settings.SESSION_TTL - 86400:
        with database.atomic(conn):
            conn.execute(
                "UPDATE sessions SET expires = ? WHERE token_hash = ?", (now + settings.SESSION_TTL, th)
            )
        self.cookies.append(session_cookie(self, token, settings.SESSION_TTL))
    return th


def api_login(self, conn, body):
    ip = self.source_ip()
    now = time.time()
    with LOGIN_FAILS_LOCK:
        fails = LOGIN_FAILS.get(ip)
        if fails and fails[1] > now:
            return self.send_json(429, {"error": f"尝试次数过多，请 {int(fails[1] - now) + 1} 秒后再试"})
    user_ok = hmac.compare_digest(str(body.get("username", "")).encode(), database.setting(conn, "admin_user").encode())
    pass_ok = security.verify_password(str(body.get("password", "")), database.setting(conn, "admin_hash"))
    if not (user_ok and pass_ok):
        with LOGIN_FAILS_LOCK:
            fails = LOGIN_FAILS.setdefault(ip, [0, 0])
            fails[0] += 1
            if fails[0] >= settings.LOGIN_MAX_FAILS:
                LOGIN_FAILS[ip] = [0, now + settings.LOGIN_LOCK]
        return self.send_json(401, {"error": "用户名或密码错误"})
    with LOGIN_FAILS_LOCK:
        LOGIN_FAILS.pop(ip, None)
    token = secrets.token_urlsafe(32)
    with database.atomic(conn):
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?)",
            (hashlib.sha256(token.encode()).hexdigest(), int(now) + settings.SESSION_TTL),
        )
    self.cookies.append(session_cookie(self, token, settings.SESSION_TTL))
    self.send_json(200, {"must_change": database.setting(conn, "must_change") == "1"})


def api_account(self, conn, body, session):
    if not security.verify_password(
        str(body.get("current_password", "")), database.setting(conn, "admin_hash")
    ):
        raise errors.UserError("当前密码错误", "current_password")
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    must_change = database.setting(conn, "must_change") == "1"
    if not re.fullmatch("[A-Za-z0-9_.@-]{1,32}", username):
        raise errors.UserError("用户名为 1-32 位字母、数字或 _ . @ -", "username")
    if (password or must_change) and (not 8 <= len(password) <= 128):
        raise errors.UserError("密码长度为 8-128 位", "password")
    if must_change:
        if username == settings.DEFAULT_USER:
            raise errors.UserError("用户名不能使用默认值", "username")
        if password == settings.DEFAULT_PASS:
            raise errors.UserError("密码不能使用默认值", "password")
    with database.atomic(conn):
        database.set_setting(conn, "admin_user", username)
        if password:
            database.set_setting(conn, "admin_hash", security.hash_password(password))
        database.set_setting(conn, "must_change", "0")
        conn.execute("DELETE FROM sessions WHERE token_hash != ?", (session,))
    self.send_json(200, {"username": username})
