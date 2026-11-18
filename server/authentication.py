import hashlib
import hmac
import http.cookies
import secrets
import threading
import time
from dataclasses import dataclass
import accounts
import database
import errors
import security
import settings

LOGIN_FAILS = {}
LOGIN_FAILS_LOCK = threading.Lock()
LOGIN_PENDING = 0
LOGIN_CONCURRENCY = 16
LOGIN_SOURCES = 4096
DUMMY_HASH = security.hash_password(secrets.token_hex(24))


@dataclass(frozen=True)
class Session:
    token_hash: str
    expires: int
    account_id: int | None
    username: str
    must_change: bool

    @property
    def admin(self):
        return self.account_id is None


def session_cookie(self, token, max_age):
    c = f"{settings.SESSION_COOKIE}={token}; Path={self.panel_path}/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
    if self.headers.get("X-Forwarded-Proto") == "https":
        c += "; Secure"
    return c


def auth(self, conn):
    """Read the session and current account in the request's transaction."""
    try:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
    except http.cookies.CookieError:
        return None
    if settings.SESSION_COOKIE not in cookie:
        return None
    token = cookie[settings.SESSION_COOKIE].value
    th = hashlib.sha256(token.encode()).hexdigest()
    row = conn.execute(
        "SELECT s.expires,s.account_id,a.username FROM sessions s LEFT JOIN accounts a ON a.id=s.account_id "
        f"WHERE s.token_hash=? AND s.expires>=? AND (s.account_id IS NULL OR ({accounts.ACTIVE_SQL}))",
        (th, int(time.time())),
    ).fetchone()
    if row is None:
        return None
    admin = row["account_id"] is None
    return Session(th, row["expires"], row["account_id"],
                   database.setting(conn, "admin_user") if admin else row["username"],
                   admin and database.setting(conn, "must_change") == "1")


def needs_renewal(session, now=None):
    return session.expires - (int(time.time()) if now is None else now) < settings.SESSION_TTL - 86400


def renew(self, conn, session):
    now = int(time.time())
    if not needs_renewal(session, now):
        return
    with database.atomic(conn):
        updated = conn.execute(
            "UPDATE sessions SET expires=? WHERE token_hash=? AND expires>=?",
            (now + settings.SESSION_TTL, session.token_hash, now),
        ).rowcount
    if updated:
        token = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))[settings.SESSION_COOKIE].value
        self.cookies.append(session_cookie(self, token, settings.SESSION_TTL))


def credentials(conn, username):
    if hmac.compare_digest(username.encode(), database.setting(conn, "admin_user").encode()):
        return None, database.setting(conn, "admin_hash"), True
    row = conn.execute(f"SELECT id,password_hash,({accounts.ACTIVE_SQL}) FROM accounts a WHERE username=?",
                       (username,)).fetchone()
    return tuple(row) if row else (None, DUMMY_HASH, False)


def issue_session(self, conn, account_id):
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions(token_hash,expires,account_id) VALUES (?,?,?)",
        (hashlib.sha256(token.encode()).hexdigest(), int(time.time()) + settings.SESSION_TTL, account_id),
    )
    return session_cookie(self, token, settings.SESSION_TTL)


def login_as_child(self, session, conn, body, ident):
    self.require_admin(session)
    account = conn.execute(f"SELECT revision,({accounts.ACTIVE_SQL}) AS active FROM accounts a WHERE id=?",
                           (ident,)).fetchone()
    if account is None:
        raise errors.UserError("子账户不存在", status=404)
    if not account["active"]:
        raise errors.UserError("只能登录已启用且未到期的子账户", status=409)
    accounts.check_revision(account, body)
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (session.token_hash,))
    self.cookies.append(issue_session(self, conn, ident))
    return {}


def api_login(self, conn, body):
    global LOGIN_PENDING
    ip = self.source_ip()
    now = time.time()
    with LOGIN_FAILS_LOCK:
        for address, item in list(LOGIN_FAILS.items()):
            if not item["pending"] and item["expires"] <= now:
                del LOGIN_FAILS[address]
        fails = LOGIN_FAILS.get(ip)
        if fails and fails["locked_until"] > now:
            return self.send_json(429, {"error": f"尝试次数过多，请 {int(fails['locked_until'] - now) + 1} 秒后再试"})
        if (LOGIN_PENDING >= LOGIN_CONCURRENCY
                or fails and fails["failures"] + fails["pending"] >= settings.LOGIN_MAX_FAILS
                or fails is None and len(LOGIN_FAILS) >= LOGIN_SOURCES):
            return self.send_json(429, {"error": "登录尝试过多，请稍后再试"})
        if fails is None:
            fails = LOGIN_FAILS[ip] = dict(failures=0, pending=0, locked_until=0, expires=now + settings.LOGIN_LOCK)
        fails["pending"] += 1
        LOGIN_PENDING += 1
    authenticated = False
    try:
        username = str(body.get("username", ""))
        account_id, stored, enabled = credentials(conn, username)
        pass_ok = security.verify_password(str(body.get("password", "")), stored)
        authenticated = enabled and pass_ok
    finally:
        # Reserve and settle attempts under the same lock, including failed hashes.
        with LOGIN_FAILS_LOCK:
            LOGIN_PENDING -= 1
            fails["pending"] -= 1
            fails["expires"] = time.time() + settings.LOGIN_LOCK
            if authenticated:
                fails["failures"] = 0
                fails["locked_until"] = 0
                if not fails["pending"]:
                    LOGIN_FAILS.pop(ip, None)
            else:
                fails["failures"] += 1
                if fails["failures"] >= settings.LOGIN_MAX_FAILS:
                    fails["locked_until"] = fails["expires"]
    if not authenticated:
        return self.send_json(401, {"error": "用户名或密码错误"})
    with database.atomic(conn):
        # Password resets and disablement may have raced the expensive verification.
        if credentials(conn, username) != (account_id, stored, enabled):
            return self.send_json(401, {"error": "用户名或密码错误"})
        cookie = issue_session(self, conn, account_id)
    self.cookies.append(cookie)
    self.send_json(200, {"must_change": account_id is None and database.setting(conn, "must_change") == "1"})


def api_account(self, conn, body, session):
    admin = session.admin
    old = None if admin else accounts.child(conn, session.account_id)
    stored = database.setting(conn, "admin_hash") if admin else old["password_hash"]
    if not security.verify_password(
        str(body.get("current_password", "")), stored
    ):
        raise errors.UserError("当前密码错误", "current_password")
    username = accounts.username(body.get("username"))
    password = str(body.get("password", ""))
    if password or session.must_change:
        accounts.password(password)
    with database.atomic(conn):
        hashed = security.hash_password(password) if password else stored
        if admin:
            if session.must_change and username.lower() == settings.DEFAULT_USER.lower():
                raise errors.UserError("用户名不能使用默认值", "username")
            if conn.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                raise errors.UserError("用户名已使用", "username")
            database.set_setting(conn, "admin_user", username)
            database.set_setting(conn, "admin_hash", hashed)
            database.set_setting(conn, "must_change", "0")
        else:
            if username != session.username:
                raise errors.UserError("子账户名称由管理员修改", "username")
            if not password:
                raise errors.UserError("请输入新密码", "password")
            conn.execute("UPDATE accounts SET password_hash=? WHERE id=?", (hashed, session.account_id))
        conn.execute("DELETE FROM sessions WHERE account_id IS ? AND token_hash!=?",
                     (session.account_id, session.token_hash))
    self.send_json(200, {"username": username})
