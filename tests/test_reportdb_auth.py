import re

import pytest
from fastapi.testclient import TestClient

from reportdb.auth import AuthBackend, AuthError, Identity, service
from reportdb.auth.oidc import OidcBackend
from reportdb.webapp import create_app
from test_reportdb_web import PAGES, app_env, anon, client, login, viewer, _fast_hash  # noqa: F401


def csrf_of(c, path="/admin/users"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


def test_everything_requires_login(anon):
    for p in PAGES + ["/settings/spare-parts", "/admin/users", "/account/password"]:
        r = anon.get(p, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), p
    for p in ["/api/tables", "/api/quality", "/api/data/task", "/export/tables/summary.xlsx", "/api/stores"]:
        assert anon.get(p).status_code == 401, p
    assert anon.get("/api/health").json() == {"status": "ok"}
    assert anon.get("/api/docs").status_code == 404


def test_login_logout_and_next(app_env):
    c = TestClient(create_app(app_env))
    r = c.get("/tables/summary", follow_redirects=False)
    assert "next=%2Ftables%2Fsummary" in r.headers["location"]
    login(c, "boss", "boss-password-1")
    assert c.get("/tables/summary").status_code == 200
    cookie = c.cookies.get("reportdb_session")
    assert cookie
    r = c.post("/logout", data={"csrf": "bad"}, follow_redirects=False)
    assert r.status_code == 403
    r = c.post("/logout", data={"csrf": csrf_of(c, "/")}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/tables/summary", follow_redirects=False).status_code == 303


def test_wrong_password_and_generic_message(app_env):
    c = TestClient(create_app(app_env))
    r = login(c, "boss", "nope-nope-nope", expect=401)
    assert "用户名或密码错误" in r.text
    r = login(c, "ghost", "whatever-123456", expect=401)
    assert "用户名或密码错误" in r.text


def test_login_csrf_required(app_env):
    c = TestClient(create_app(app_env))
    c.get("/login")
    r = c.post("/login", data={"username": "boss", "password": "boss-password-1", "csrf": "forged"}, follow_redirects=False)
    assert r.status_code == 403
    c2 = TestClient(create_app(app_env))  # 没有 cookie
    assert c2.post("/login", data={"username": "boss", "password": "boss-password-1", "csrf": "x"}).status_code == 403


def test_lockout_after_failures(app_env):
    service.limiter.hits.clear()
    c = TestClient(create_app(app_env))
    for _ in range(5):
        login(c, "viewer1", "bad-password-xx", expect=401)
    r = login(c, "viewer1", "viewer-password-1", expect=429)  # 正确密码也被锁
    assert "锁定" in r.text
    # 管理员可解锁
    adm = TestClient(create_app(app_env))
    login(adm, "boss", "boss-password-1")
    uid = re.search(r'/admin/users/(\d+)/update', adm.get("/admin/users").text.split("viewer1")[1]).group(1)
    r = adm.post(f"/admin/users/{uid}/update", data={"action": "unlock", "csrf": csrf_of(adm)}, follow_redirects=False)
    assert r.status_code == 303
    login(c, "viewer1", "viewer-password-1")
    service.limiter.hits.clear()


def test_ip_rate_limit(app_env, monkeypatch):
    service.limiter.hits.clear()
    c = TestClient(create_app(app_env))
    for i in range(service.IP_MAX_FAILS):
        login(c, f"nobody{i}", "bad-password-xx", expect=401)
    login(c, "boss", "boss-password-1", expect=429)
    service.limiter.hits.clear()


def test_role_gating(viewer, client):
    for p in ("/settings/spare-parts", "/admin/users"):
        assert viewer.get(p).status_code == 403
        assert client.get(p).status_code == 200
    assert viewer.get("/tables/summary").status_code == 200


def test_admin_manages_users(app_env, client):
    tok = csrf_of(client)
    r = client.post("/admin/users/create", data={"username": "new1", "password": "short", "role": "viewer", "csrf": tok}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post("/admin/users/create", data={"username": "new1", "password": "new-password-123", "role": "viewer", "csrf": tok}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    nc = TestClient(create_app(app_env))
    login(nc, "new1", "new-password-123")
    html = client.get("/admin/users").text
    uid = re.search(r'/admin/users/(\d+)/password', html.split("new1")[1]).group(1)
    # 重置密码 → 旧会话失效
    client.post(f"/admin/users/{uid}/password", data={"password": "another-password-9", "csrf": tok})
    assert nc.get("/tables/summary", follow_redirects=False).status_code == 303
    login(nc, "new1", "another-password-9")
    # 停用 → 立即失效且不能再登录
    client.post(f"/admin/users/{uid}/update", data={"action": "toggle", "csrf": tok})
    assert nc.get("/tables/summary", follow_redirects=False).status_code == 303
    login(nc, "new1", "another-password-9", expect=401)
    # CSRF 缺失被拒
    assert client.post(f"/admin/users/{uid}/update", data={"action": "toggle"}).status_code == 403


def test_cannot_remove_last_admin(app_env, conn_after=None):
    from reportdb.db import connect
    conn = connect(app_env)
    with pytest.raises(AuthError):
        service.update_user(conn, service.get_user(conn, "boss")["id"], "t", disabled=True)
    with pytest.raises(AuthError):
        service.update_user(conn, service.get_user(conn, "boss")["id"], "t", role="viewer")


def test_change_own_password(app_env, viewer):
    tok = csrf_of(viewer, "/account/password")
    r = viewer.post("/account/password", data={"old": "wrong", "new": "brand-new-pass-1", "new2": "brand-new-pass-1", "csrf": tok}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = viewer.post("/account/password", data={"old": "viewer-password-1", "new": "brand-new-pass-1", "new2": "brand-new-pass-1", "csrf": tok}, follow_redirects=False)
    assert r.headers["location"].startswith("/login")
    c = TestClient(create_app(app_env))
    login(c, "viewer1", "brand-new-pass-1")


def test_password_hash_format():
    h = service.hash_password("some-long-password", 1000)
    assert h.startswith("pbkdf2_sha256$1000$") and "some-long-password" not in h
    assert service.verify_password("some-long-password", h) and not service.verify_password("x", h)
    assert not service.verify_password("x", None) and not service.verify_password("x", "garbage")


def test_cookie_flags(app_env):
    c = TestClient(create_app(app_env), base_url="https://testserver")
    r = login(c, "boss", "boss-password-1")
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc and "secure" in sc


def test_next_is_not_open_redirect(app_env):
    c = TestClient(create_app(app_env))
    r = c.get("/login?next=//evil.com")
    assert 'value="/"' in r.text and "evil.com" not in r.text


def test_cli_create_admin(tmp_path, monkeypatch):
    from reportdb import users
    db = str(tmp_path / "u.sqlite3")
    monkeypatch.setenv("REPORTDB_ADMIN_PASSWORD", "cli-admin-password")
    assert users.main(["--db", db, "create-admin", "--username", "root1"]) == 0
    assert users.main(["--db", db, "create-admin", "--username", "root1"]) == 1  # 已存在
    monkeypatch.setenv("REPORTDB_ADMIN_PASSWORD", "short")
    assert users.main(["--db", db, "create-admin", "--username", "root2"]) == 1  # 弱密码
    monkeypatch.delenv("REPORTDB_ADMIN_PASSWORD")
    assert users.main(["--db", db, "create-admin", "--username", "root3"]) == 1  # 非交互且无环境变量


# ------------------------------------------------------------------ 可插拔 / OIDC
def test_backend_is_pluggable(app_env, monkeypatch):
    class Dummy(AuthBackend):
        name = "dummy"
        supports_password_login = False

        def login_redirect(self, request, next_url):
            return "https://idp.example/authorize", {}

    monkeypatch.setattr("reportdb.webapp.get_backend", lambda cf: Dummy())
    c = TestClient(create_app(app_env))
    r = c.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://idp.example/authorize"
    assert c.post("/login", data={"username": "a", "password": "b", "csrf": "x"}).status_code in (400, 403)


def test_unknown_backend_rejected(monkeypatch):
    from reportdb.auth import get_backend
    monkeypatch.setenv("REPORTDB_AUTH_BACKEND", "ldap")
    with pytest.raises(RuntimeError):
        get_backend(lambda: None)


def test_oidc_flow_with_mocked_idp(app_env, monkeypatch):
    import urllib.parse
    from reportdb.auth import oidc

    for k, v in {"REPORTDB_AUTH_BACKEND": "oidc", "REPORTDB_OIDC_ISSUER": "https://idp.example", "REPORTDB_OIDC_CLIENT_ID": "cid",
                 "REPORTDB_OIDC_CLIENT_SECRET": "sec", "REPORTDB_OIDC_REDIRECT_URI": "http://testserver/auth/oidc/callback",
                 "REPORTDB_OIDC_ADMIN_USERS": "alice"}.items():
        monkeypatch.setenv(k, v)

    def fake_http(url, data=None, headers=None, timeout=15):
        if url.endswith("openid-configuration"):
            return {"authorization_endpoint": "https://idp.example/auth", "token_endpoint": "https://idp.example/token",
                    "userinfo_endpoint": "https://idp.example/userinfo"}
        if url.endswith("/token"):
            assert data["code"] == "CODE" and data["code_verifier"]
            return {"access_token": "AT"}
        assert headers["Authorization"] == "Bearer AT"
        return {"preferred_username": "alice" if "alice" in OIDC_USER else "bob", "name": "Alice A"}

    OIDC_USER = ["alice"]
    monkeypatch.setattr(oidc, "_http_json", fake_http)
    c = TestClient(create_app(app_env))
    r = c.get("/tables/summary", follow_redirects=False)
    assert r.headers["location"].startswith("/login")
    r = c.get("/login?next=/tables/summary", follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith("https://idp.example/auth?") and "code_challenge_method=S256" in loc
    state = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["state"][0]
    assert c.get("/auth/oidc/callback?code=CODE&state=WRONG", follow_redirects=False).status_code == 401
    r = c.get(f"/auth/oidc/callback?code=CODE&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tables/summary"
    assert c.get("/tables/summary").status_code == 200
    assert c.get("/admin/users").status_code == 200  # alice 映射为 admin
    assert "单点登录" in c.get("/admin/users").text
    page = c.get("/tables/summary").text      # SSO user menu: Casdoor personal center link instead of local 修改密码
    assert 'id="sso-account-link" href="https://idp.example/account"' in page and "账号设置" in page and "/account/password" not in page
    monkeypatch.setenv("REPORTDB_SSO_ACCOUNT_URL", "https://sso.example/me")
    assert 'href="https://sso.example/me"' in c.get("/tables/summary").text
    # 本地登录接口在 OIDC 模式下不可用
    assert c.post("/login", data={"username": "boss", "password": "boss-password-1", "csrf": "x"}).status_code in (400, 403)


def test_every_route_is_guarded(app_env):
    """防遗漏：除登录相关与探活外，所有路由都必须带登录依赖。"""
    app = create_app(app_env)
    open_paths = {"/login", "/api/health", "/auth/oidc/login", "/auth/oidc/logout", "/auth/oidc/callback"}
    for r in app.routes:
        path = getattr(r, "path", "")
        if not hasattr(r, "dependant") or path in open_paths:
            continue
        names = {d.call.__name__ for d in r.dependant.dependencies}
        assert names & {"login_required", "admin_required", "requester_required"}, path


def test_oidc_require_role_rejects_unmapped(monkeypatch):
    monkeypatch.setenv("REPORTDB_OIDC_ISSUER", "https://sso.example.test")
    monkeypatch.setenv("REPORTDB_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("REPORTDB_OIDC_REDIRECT_URI", "https://r.example.test/auth/oidc/callback")
    monkeypatch.setenv("REPORTDB_OIDC_ADMIN_GROUPS", "mhero-admin")
    monkeypatch.setenv("REPORTDB_OIDC_SUPERVISOR_GROUPS", "mhero-supervisor")
    be = OidcBackend(lambda: None)
    assert be.map_identity({"name": "a", "roles": ["mhero-admin"]}).role == "admin"
    assert be.map_identity({"name": "s", "roles": ["mhero-supervisor"]}).role == "supervisor"
    assert be.map_identity({"name": "n", "roles": []}).role == "viewer"          # legacy default (flag off)
    monkeypatch.setenv("REPORTDB_OIDC_REQUIRE_ROLE", "1")
    with pytest.raises(AuthError) as e:
        be.map_identity({"name": "n", "roles": ["mhero-viewer"]})
    assert e.value.status == 403
    assert be.map_identity({"name": "s", "roles": ["mhero-supervisor"]}).role == "supervisor"


def test_local_backend_has_no_sso_account_link(app_env):
    c = TestClient(create_app(app_env))
    assert c.get("/login").status_code == 200
    assert "sso-account-link" not in c.get("/login").text


def test_oidc_no_app_role_shows_friendly_403(app_env, monkeypatch):
    """REQUIRE_APP 路径：没开通「区域报表」→ 403 友好页（带姓名、返回黄页、退出登录），不是纯文本。"""
    import urllib.parse
    from reportdb.auth import oidc

    for k, v in {"REPORTDB_AUTH_BACKEND": "oidc", "REPORTDB_OIDC_ISSUER": "https://idp.example", "REPORTDB_OIDC_CLIENT_ID": "cid",
                 "REPORTDB_OIDC_CLIENT_SECRET": "sec", "REPORTDB_OIDC_REDIRECT_URI": "http://testserver/auth/oidc/callback",
                 "REPORTDB_OIDC_SUPERVISOR_GROUPS": "mhero-supervisor", "MHERO_AUTH_REQUIRE_APP": "1"}.items():
        monkeypatch.setenv(k, v)

    def fake_http(url, data=None, headers=None, timeout=15):
        if url.endswith("openid-configuration"):
            return {"authorization_endpoint": "https://idp.example/auth", "token_endpoint": "https://idp.example/token",
                    "userinfo_endpoint": "https://idp.example/userinfo"}
        if url.endswith("/token"):
            return {"access_token": "AT"}
        return {"preferred_username": "zz", "displayName": "测试<督导>", "roles": [{"name": "mhero-supervisor"}]}

    monkeypatch.setattr(oidc, "_http_json", fake_http)
    c = TestClient(create_app(app_env))
    loc = c.get("/login?next=/", follow_redirects=False).headers["location"]
    state = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["state"][0]
    r = c.get(f"/auth/oidc/callback?code=CODE&state={state}", follow_redirects=False)
    assert r.status_code == 403 and "text/html" in r.headers["content-type"]
    assert "返回黄页" in r.text and "退出登录" in r.text and 'href="https://oa-m-hero.41box.com/"' in r.text
    assert "测试&lt;督导&gt;" in r.text and "<督导>" not in r.text and "区域报表" in r.text
    assert "reportdb_session" not in "".join(r.headers.get_list("set-cookie")) or "Max-Age=0" in "".join(r.headers.get_list("set-cookie"))
    lo = c.get("/auth/oidc/logout", follow_redirects=False)
    assert lo.status_code == 303
