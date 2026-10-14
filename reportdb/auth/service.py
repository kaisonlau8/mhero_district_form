"""用户、密码哈希、会话、锁定与限流、审计（与具体登录后端无关）。"""
from __future__ import annotations

import base64
import hashlib
import json
import hmac
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from .base import AuthError, Identity

MIN_PASSWORD = 10
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
IP_WINDOW = 5 * 60
IP_MAX_FAILS = 10
SESSION_COOKIE = "reportdb_session"
ROLES = ("admin", "supervisor", "viewer")


def _iters() -> int:
    return int(os.getenv("REPORTDB_PBKDF2_ITERS", "600000"))


# ------------------------------------------------------------------ 密码
def hash_password(password: str, iters: int | None = None) -> str:
    """pbkdf2_sha256$迭代次数$盐(b64)$哈希(b64)（标准库实现，无额外依赖）。"""
    iters = iters or _iters()
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, iters, salt, h = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(h))
    except Exception:  # noqa: BLE001
        return False


_DUMMY = None


def _dummy_hash() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("dummy-password-for-timing")
    return _DUMMY


def check_password_policy(username: str, password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"密码至少 {MIN_PASSWORD} 位", 400)
    if password.lower() == username.lower():
        raise AuthError("密码不能与用户名相同", 400)


# ------------------------------------------------------------------ 审计
def audit(conn: sqlite3.Connection, actor: str | None, action: str, detail: str = "", ip: str = "") -> None:
    with conn:
        conn.execute("INSERT INTO audit_log(at,actor,action,detail,ip) VALUES (?,?,?,?,?)",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), actor, action, detail, ip))


# ------------------------------------------------------------------ 用户
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_user(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()


def create_user(conn: sqlite3.Connection, username: str, password: str | None, *, role: str = "viewer",
                full_detail: bool = False, display_name: str = "", created_by: str = "system",
                auth_source: str = "local", supervisor_name: str = "") -> int:
    username = username.strip()
    if not username or len(username) > 64 or any(c.isspace() for c in username):
        raise AuthError("用户名不能为空、不能含空白、最长 64 位", 400)
    if role not in ROLES:
        raise AuthError("角色只能是 admin / supervisor / viewer", 400)
    if get_user(conn, username):
        raise AuthError(f"用户 {username} 已存在", 409)
    if auth_source == "local":
        check_password_policy(username, password or "")
    with conn:
        cur = conn.execute(
            "INSERT INTO users(username,display_name,password_hash,role,full_detail,auth_source,created_at,created_by,supervisor_name)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (username, display_name or username, hash_password(password) if (password and auth_source == "local") else None,
             role, 1 if full_detail else 0, auth_source, _now(), created_by,
             (supervisor_name or (display_name or username)) if role == "supervisor" else None))
    audit(conn, created_by, "user.create", f"{username} role={role}")
    return cur.lastrowid


def set_password(conn: sqlite3.Connection, username: str, password: str, actor: str) -> None:
    u = get_user(conn, username)
    if not u:
        raise AuthError("用户不存在", 404)
    check_password_policy(u["username"], password)
    with conn:
        conn.execute("UPDATE users SET password_hash=?, failed_count=0, locked_until=0 WHERE id=?",
                     (hash_password(password), u["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))  # 改密码即让旧会话失效
    audit(conn, actor, "user.password", u["username"])


def update_user(conn: sqlite3.Connection, user_id: int, actor: str, *, role: str | None = None,
                full_detail: bool | None = None, disabled: bool | None = None, supervisor_name: str | None = None) -> None:
    u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        raise AuthError("用户不存在", 404)
    new_role = role or u["role"]
    new_disabled = u["disabled"] if disabled is None else int(disabled)
    if new_role not in ROLES:
        raise AuthError("角色只能是 admin / supervisor / viewer", 400)
    if u["role"] == "admin" and (new_role != "admin" or new_disabled):
        others = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND id<>?", (user_id,)).fetchone()[0]
        if others == 0:
            raise AuthError("不能停用/降级最后一个管理员", 400)
    with conn:
        conn.execute("UPDATE users SET role=?, full_detail=?, disabled=?, supervisor_name=? WHERE id=?",
                     (new_role, u["full_detail"] if full_detail is None else int(full_detail), new_disabled,
                      (supervisor_name if supervisor_name is not None else u["supervisor_name"]) or (u["display_name"] if new_role == "supervisor" else None),
                      user_id))
        if new_disabled or new_role != u["role"]:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    audit(conn, actor, "user.update", f"{u['username']} role={new_role} disabled={new_disabled}")


def identity_from_row(u: sqlite3.Row) -> Identity:
    return Identity(u["username"], u["display_name"] or u["username"], u["role"],
                    bool(u["full_detail"]) or u["role"] == "admin", u["auth_source"])


# ------------------------------------------------------------------ 限流 / 锁定
class _IpLimiter:
    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def _trim(self, q: deque, now: float):
        while q and now - q[0] > IP_WINDOW:
            q.popleft()

    def blocked(self, ip: str) -> bool:
        with self.lock:
            q = self.hits[ip]
            self._trim(q, time.time())
            return len(q) >= IP_MAX_FAILS

    def fail(self, ip: str):
        with self.lock:
            q = self.hits[ip]
            now = time.time()
            self._trim(q, now)
            q.append(now)

    def reset(self, ip: str):
        with self.lock:
            self.hits.pop(ip, None)


limiter = _IpLimiter()


def authenticate_local(conn: sqlite3.Connection, username: str, password: str, ip: str) -> Identity:
    if limiter.blocked(ip):
        raise AuthError("尝试次数过多，请稍后再试", 429)
    u = get_user(conn, username or "")
    now = time.time()
    if u and u["locked_until"] > now:
        verify_password(password or "", _dummy_hash())
        limiter.fail(ip)
        raise AuthError("登录失败次数过多，账号已临时锁定，请 15 分钟后再试", 429)
    ok = verify_password(password or "", u["password_hash"] if u else _dummy_hash())
    if not u or not ok or u["disabled"] or u["auth_source"] != "local":
        limiter.fail(ip)
        if u and not ok:
            fails = u["failed_count"] + 1
            with conn:
                conn.execute("UPDATE users SET failed_count=?, locked_until=? WHERE id=?",
                             (fails, now + LOCK_SECONDS if fails >= MAX_FAILS else 0, u["id"]))
            if fails >= MAX_FAILS:
                audit(conn, u["username"], "login.locked", f"连续失败 {fails} 次", ip)
        audit(conn, username, "login.fail", "", ip)
        raise AuthError("用户名或密码错误，或账号已停用")
    with conn:
        conn.execute("UPDATE users SET failed_count=0, locked_until=0, last_login=? WHERE id=?", (_now(), u["id"]))
    limiter.reset(ip)
    audit(conn, u["username"], "login.ok", "", ip)
    return identity_from_row(u)


# ------------------------------------------------------------------ 会话
def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_hours() -> float:
    return float(os.getenv("REPORTDB_SESSION_HOURS", "12"))


def create_session(conn: sqlite3.Connection, user_id: int, ip: str = "", ua: str = "") -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = time.time()
    with conn:
        conn.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
        conn.execute("INSERT INTO sessions(id_hash,user_id,csrf,created_at,expires_at,ip,ua) VALUES (?,?,?,?,?,?,?)",
                     (_h(token), user_id, csrf, now, now + session_hours() * 3600, ip, (ua or "")[:200]))
    return token, csrf


def revoked_after(username: str) -> float:
    """Force-logout entries written by the hub 「账号管理」 (shared MHERO_AUTH_REVOKE_FILE): sessions created before are void."""
    f = os.getenv("MHERO_AUTH_REVOKE_FILE", "")
    if not f:
        return 0.0
    try:
        data = json.load(open(f, encoding="utf-8"))
        return max(float(data.get(username, 0) or 0), float(data.get("*", 0) or 0))
    except Exception:       # missing / corrupt file must never lock everyone out
        return 0.0


def load_session(conn: sqlite3.Connection, token: str | None) -> tuple[sqlite3.Row, str] | None:
    if not token:
        return None
    s = conn.execute("SELECT * FROM sessions WHERE id_hash=?", (_h(token),)).fetchone()
    if not s or s["expires_at"] < time.time():
        return None
    u = conn.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
    if not u or u["disabled"]:
        return None
    if s["created_at"] < revoked_after(u["username"]):
        with conn:
            conn.execute("DELETE FROM sessions WHERE id_hash=?", (s["id_hash"],))
        return None
    return u, s["csrf"]


def destroy_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        with conn:
            conn.execute("DELETE FROM sessions WHERE id_hash=?", (_h(token),))


def provision_external(conn: sqlite3.Connection, ident: Identity) -> sqlite3.Row:
    """外部 IdP（OIDC）登录：首次自动建账号，之后同步显示名与角色映射。"""
    u = get_user(conn, ident.username)
    with conn:
        if u is None:
            conn.execute("INSERT INTO users(username,display_name,role,full_detail,auth_source,created_at,created_by,last_login,supervisor_name)"
                         " VALUES (?,?,?,?,?,?,?,?,?)",
                         (ident.username, ident.display_name, ident.role, int(ident.full_detail), ident.auth_source,
                          _now(), "oidc", _now(), ident.display_name if ident.role == "supervisor" else None))
        else:
            if u["disabled"]:
                raise AuthError("账号已被停用")
            conn.execute("UPDATE users SET display_name=?, role=?, full_detail=?, last_login=?, "
                         "supervisor_name=COALESCE(supervisor_name, ?) WHERE id=?",
                         (ident.display_name or u["display_name"], ident.role, int(ident.full_detail), _now(),
                          ident.display_name if ident.role == "supervisor" else None, u["id"]))
    return get_user(conn, ident.username)


def secret_key(conn: sqlite3.Connection) -> str:
    from ..db import get_meta, set_meta

    key = os.getenv("REPORTDB_SECRET_KEY", "").strip() or get_meta(conn, "secret_key")
    if not key:
        key = secrets.token_hex(32)
        set_meta(conn, "secret_key", key)
    return key
