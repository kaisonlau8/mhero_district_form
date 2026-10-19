"""OIDC 授权码登录（Casdoor / Keycloak / 任意标准 IdP）。用 REPORTDB_AUTH_BACKEND=oidc 启用。

流程：/auth/oidc/login → IdP 授权页（state + PKCE，签名 cookie 暂存）→ /auth/oidc/callback
→ 服务端用授权码换 access_token → 调 userinfo 取用户信息（不依赖 id_token 验签）→ 本系统建/更新用户并发会话。
环境变量：
  REPORTDB_OIDC_ISSUER          例 https://casdoor.example.com （用 /.well-known/openid-configuration 发现端点）
  REPORTDB_OIDC_CLIENT_ID / REPORTDB_OIDC_CLIENT_SECRET
  REPORTDB_OIDC_REDIRECT_URI    例 https://reports.example.com/auth/oidc/callback
  REPORTDB_OIDC_SCOPES          默认 "openid profile email"
  REPORTDB_OIDC_USERNAME_CLAIM  默认 preferred_username（Casdoor 可用 name）
  REPORTDB_OIDC_ADMIN_USERS     逗号分隔的用户名，映射为 admin
  REPORTDB_OIDC_ADMIN_GROUPS / REPORTDB_OIDC_SUPERVISOR_GROUPS（或 _SUPERVISOR_USERS） / REPORTDB_OIDC_FULL_DETAIL_GROUPS
                                逗号分隔，匹配 userinfo 里的 groups/roles（Casdoor 的角色名如 supervisor）。督导的负责门店按其显示名匹配多维表格「督导」
未列入的用户默认为 viewer（可看表、明细脱敏）；设 REPORTDB_OIDC_REQUIRE_ROLE=1 则直接拒绝（生产：只有 督导/管理员 两种角色）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

from itsdangerous import BadSignature, URLSafeTimedSerializer

from .base import AuthBackend, AuthError, Identity
from . import service

STATE_COOKIE = "reportdb_oidc"


def _http_json(url: str, data: dict | None = None, headers: dict | None = None, timeout: float = 15) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    # Cloudflare in front of the IdP rejects Python's default User-Agent (HTTP 403 / error 1010)
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "mhero-reportdb/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _override(username: str) -> str | None:
    try:
        from mhero_auth.roles import override_for
    except Exception:  # noqa: BLE001 — 共享登录组件不可用：没有单独角色
        return None
    return override_for(username, "report")


def _csv(name: str) -> set[str]:
    return {x.strip() for x in os.getenv(name, "").split(",") if x.strip()}


class OidcBackend(AuthBackend):
    name = "oidc"
    supports_password_login = False
    manages_users = False

    def __init__(self, conn_factory):
        self.conn_factory = conn_factory
        self._disc: dict | None = None
        self.issuer = os.getenv("REPORTDB_OIDC_ISSUER", "").rstrip("/")
        self.client_id = os.getenv("REPORTDB_OIDC_CLIENT_ID", "")
        self.client_secret = os.getenv("REPORTDB_OIDC_CLIENT_SECRET", "")
        self.redirect_uri = os.getenv("REPORTDB_OIDC_REDIRECT_URI", "")
        if not (self.issuer and self.client_id and self.redirect_uri):
            raise RuntimeError("OIDC 需要配置 REPORTDB_OIDC_ISSUER / CLIENT_ID / REDIRECT_URI")

    def _endpoints(self) -> dict:
        if self._disc is None:
            self._disc = _http_json(self.issuer + "/.well-known/openid-configuration")
        return self._disc

    def _serializer(self) -> URLSafeTimedSerializer:
        conn = self.conn_factory()
        try:
            return URLSafeTimedSerializer(service.secret_key(conn), salt="reportdb-oidc")
        finally:
            conn.close()

    def login_redirect(self, request, next_url):
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
             "scope": os.getenv("REPORTDB_OIDC_SCOPES", "openid profile email"), "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        url = self._endpoints()["authorization_endpoint"] + "?" + urllib.parse.urlencode(q)
        cookie = self._serializer().dumps({"s": state, "v": verifier, "n": next_url})
        return url, {STATE_COOKIE: cookie}

    def map_identity(self, info: dict) -> Identity:
        claim = os.getenv("REPORTDB_OIDC_USERNAME_CLAIM", "preferred_username")
        username = str(info.get(claim) or info.get("preferred_username") or info.get("name") or info.get("sub") or "").strip()
        if not username:
            raise AuthError("IdP 没有返回用户名")
        groups = set()
        for k in ("groups", "roles"):
            v = info.get(k) or []
            groups |= {(g.get("name") if isinstance(g, dict) else str(g)) for g in (v if isinstance(v, list) else [v])}
        is_admin = username in _csv("REPORTDB_OIDC_ADMIN_USERS") or bool(groups & _csv("REPORTDB_OIDC_ADMIN_GROUPS"))
        is_ro = bool(groups & (_csv("REPORTDB_OIDC_READONLY_ADMIN_GROUPS") or {"mhero-readonly-admin"}))
        is_sup = username in _csv("REPORTDB_OIDC_SUPERVISOR_USERS") or bool(groups & _csv("REPORTDB_OIDC_SUPERVISOR_GROUPS"))
        role = "admin" if is_admin else ("readonly_admin" if is_ro else ("supervisor" if is_sup else "viewer"))  # 统一身份（admin 优先）
        if not is_admin:        # 黄页「账号管理」里给本系统单独设的角色（覆盖统一身份；全局超级管理员不可被覆盖）
            ov = _override(username)
            if ov:
                role = {"user": "viewer"}.get(ov, ov)
        full = role in ("admin", "readonly_admin", "supervisor") or bool(groups & _csv("REPORTDB_OIDC_FULL_DETAIL_GROUPS"))
        if os.getenv("REPORTDB_OIDC_REQUIRE_ROLE", "0").strip().lower() in ("1", "true", "yes", "on") and role == "viewer":
            # 统一登录只有 督导(supervisor) / 管理员(admin) 两种角色；没有被授予任何一个的账号不建本地用户、不发会话
            raise AuthError("你的账号没有访问本系统的权限，请联系管理员。", 403, str(info.get("displayName") or info.get("name") or username))
        if os.getenv("MHERO_AUTH_REQUIRE_APP", os.getenv("REPORTDB_OIDC_REQUIRE_APP", "0")).strip().lower() in ("1", "true", "yes", "on"):
            # 统一账号管理：只有被授予 mhero-app-report（或全局管理员 mhero-admin）的账号才能进入本系统
            if "mhero-app-report" not in groups and "mhero-admin" not in groups and not is_admin and not is_ro:
                raise AuthError("你的账号没有开通「区域报表」的访问权限，请联系管理员。", 403, str(info.get("displayName") or info.get("name") or username))
        return Identity(username, str(info.get("displayName") or info.get("name") or username), role, full, "oidc", global_admin=is_admin)

    def complete_login(self, conn, request):
        params = request.query_params
        if params.get("error"):
            raise AuthError("IdP 拒绝登录：" + str(params.get("error_description") or params.get("error")))
        try:
            saved = self._serializer().loads(request.cookies.get(STATE_COOKIE, ""), max_age=600)
        except BadSignature:
            raise AuthError("登录状态已过期，请重新登录")
        if not params.get("state") or params.get("state") != saved["s"] or not params.get("code"):
            raise AuthError("登录校验失败（state 不匹配）")
        ep = self._endpoints()
        tok = _http_json(ep["token_endpoint"], {
            "grant_type": "authorization_code", "code": params["code"], "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret, "code_verifier": saved["v"]})
        if "access_token" not in tok:
            raise AuthError("IdP 没有返回 access_token")
        info = _http_json(ep["userinfo_endpoint"], headers={"Authorization": "Bearer " + tok["access_token"]})
        return self.map_identity(info), saved.get("n") or "/"

    def logout_redirect(self, request):
        try:
            end = self._endpoints().get("end_session_endpoint")
        except Exception:  # noqa: BLE001
            return None
        if end and self.redirect_uri:       # Casdoor only accepts a registered redirect URI; the callback sends a code-less visit on to /login
            end += ("&" if "?" in end else "?") + urllib.parse.urlencode({"post_logout_redirect_uri": self.redirect_uri, "client_id": self.client_id})
        return end


def get_backend(conn_factory) -> AuthBackend:
    from .local import LocalBackend

    kind = os.getenv("REPORTDB_AUTH_BACKEND", "local").strip().lower()
    if kind == "oidc":
        return OidcBackend(conn_factory)
    if kind != "local":
        raise RuntimeError(f"未知的 REPORTDB_AUTH_BACKEND={kind}（可选 local / oidc）")
    return LocalBackend()
