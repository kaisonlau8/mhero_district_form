"""统一登录账号同步进报表库：未登录过的 SSO 账号也出现在用户管理里；规则与登录一致；不动门店分配 / 本地账号。"""
import re, sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

AUTH_DIR = Path(__file__).resolve().parents[2] / "m-hero-auth"
sys.path[:0] = [str(AUTH_DIR), str(AUTH_DIR / "tests")]

from reportdb.auth import service, sso_sync
from reportdb.auth.oidc import OidcBackend
from reportdb.db import connect
from reportdb.webapp import create_app
from test_reportdb_web import app_env, login, _fast_hash  # noqa: F401


@pytest.fixture()
def env(app_env, monkeypatch):
    for k, v in {"REPORTDB_AUTH_BACKEND": "oidc", "REPORTDB_OIDC_ISSUER": "https://idp.example", "REPORTDB_OIDC_CLIENT_ID": "cid",
                 "REPORTDB_OIDC_REDIRECT_URI": "http://testserver/auth/oidc/callback", "REPORTDB_OIDC_ADMIN_GROUPS": "mhero-admin",
                 "REPORTDB_OIDC_SUPERVISOR_GROUPS": "mhero-supervisor", "REPORTDB_OIDC_REQUIRE_ROLE": "1", "MHERO_AUTH_REQUIRE_APP": "1",
                 "REPORTDB_SSO_SYNC_INTERVAL": "0"}.items():
        monkeypatch.setenv(k, v)
    sso_sync._LAST["at"] = 0.0
    from fakes import FakeCasdoor
    from mhero_auth.accounts import AccountService
    f = FakeCasdoor()
    f.seed("boss", "admin")
    f.seed("13700000001", "supervisor", ["report"], display="杨督导")      # never logged in to the report system
    f.seed("13700000002", "supervisor", ["nss"], display="无报表权限")      # no report app → not created
    f.seed("13700000003", "", ["report"], display="无身份")                 # no level → REQUIRE_ROLE refuses
    f.seed("13700000004", "supervisor", ["report"], disabled=True, display="已停用")
    f.seed("13700000005", "admin", [], display="胡管理")                   # global admin: no app role needed
    return app_env, f, AccountService(f, audit_path="", revoke_path="")


def snap_sync(db, svc):
    conn = connect(db)
    try:
        return sso_sync.sync_users(conn, svc.snapshot(), OidcBackend(lambda: connect(db)))
    finally:
        conn.close()


def test_sync_creates_missing_with_login_rules(env):
    db, f, svc = env
    conn = connect(db)
    service.create_user(conn, "keep-local", "keep-local-pass-1", role="admin")                     # local break-glass account
    conn.close()
    st = snap_sync(db, svc)
    assert sorted(st["created"]) == ["13700000001", "13700000005"]          # boss already exists as a local account
    assert st["skipped_no_access"] == 2 and st["skipped_disabled"] == 1
    conn = connect(db)
    u = service.get_user(conn, "13700000001")
    assert (u["role"], u["auth_source"], u["display_name"], u["supervisor_name"], u["disabled"]) == ("supervisor", "oidc", "杨督导", "杨督导", 0)
    assert u["password_hash"] in (None, "") and u["last_login"] is None
    assert conn.execute("SELECT COUNT(*) FROM user_stores WHERE user_id=?", (u["id"],)).fetchone()[0] == 0   # no stores granted
    assert service.get_user(conn, "13700000005")["role"] == "admin"
    assert service.get_user(conn, "13700000002") is None and service.get_user(conn, "13700000003") is None
    assert service.get_user(conn, "keep-local")["auth_source"] == "local"
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='sso.sync'").fetchone()[0] == 1
    conn.close()
    assert snap_sync(db, svc)["created"] == []                                                  # idempotent


def test_sync_updates_existing_but_keeps_stores_and_never_reenables(env):
    db, f, svc = env
    snap_sync(db, svc)
    conn = connect(db)
    uid = service.get_user(conn, "13700000001")["id"]
    code = conn.execute("SELECT code FROM stores LIMIT 1").fetchone()
    if code:
        with conn:
            conn.execute("INSERT INTO user_stores(user_id,store_code) VALUES (?,?)", (uid, code[0]))
    with conn:
        conn.execute("UPDATE users SET supervisor_name='手工姓名' WHERE id=?", (uid,))
    conn.close()
    f.roles["mhero-supervisor"].discard("13700000001"); f.roles["mhero-admin"].add("13700000001")      # promoted in Casdoor
    f.users["13700000005"]["isForbidden"] = True; f.users["boss"]["isForbidden"] = True
    st = snap_sync(db, svc)
    assert "13700000001" in st["updated"] and st["disabled"] == ["13700000005"]          # local boss untouched
    conn = connect(db)
    u = service.get_user(conn, "13700000001")
    assert u["role"] == "admin" and u["supervisor_name"] == "手工姓名"
    assert conn.execute("SELECT COUNT(*) FROM user_stores WHERE user_id=?", (uid,)).fetchone()[0] == (1 if code else 0)
    conn.close()
    f.users["13700000005"]["isForbidden"] = False
    snap_sync(db, svc)
    conn = connect(db); assert service.get_user(conn, "13700000005")["disabled"] == 1
    assert service.get_user(conn, "boss")["disabled"] == 0; conn.close()   # only disables, never re-enables


def test_local_without_access_is_reported_not_deleted(env):
    db, f, svc = env
    snap_sync(db, svc)
    f.roles["mhero-app-report"].discard("13700000001")
    st = snap_sync(db, svc)
    assert st["local_without_access"] == ["13700000001"]
    conn = connect(db); assert service.get_user(conn, "13700000001") is not None; conn.close()


def test_admin_button_and_page_autosync(env):
    db, f, svc = env
    app = create_app(db)
    app.state.account_bridge = svc
    conn = connect(db)
    tok, _ = service.create_session(conn, service.get_user(conn, "boss")["id"])
    conn.close()
    c = TestClient(app); c.cookies.set("reportdb_session", tok)
    page = c.get("/admin/users").text                                    # page load auto-syncs
    assert "13700000001" in page and "从 SSO 同步" in page
    f.seed("13700000009", "supervisor", ["report"], display="新督导")
    t = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/admin/users/sync-sso", data={"csrf": t}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    conn = connect(db); assert service.get_user(conn, "13700000009")["role"] == "supervisor"; conn.close()
    assert c.post("/admin/users/sync-sso", data={"csrf": "bad"}, follow_redirects=False).status_code in (400, 403)


def test_button_without_bridge_reports_error(env):
    db, f, svc = env
    app = create_app(db)
    app.state.account_bridge = None; app.state._bridge_cache = None
    conn = connect(db)
    tok, _ = service.create_session(conn, service.get_user(conn, "boss")["id"])
    conn.close()
    c = TestClient(app); c.cookies.set("reportdb_session", tok)
    t = re.search(r'name="csrf" value="([^"]+)"', c.get("/admin/users").text).group(1)
    r = c.post("/admin/users/sync-sso", data={"csrf": t}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_audit_fixes_case_conflict_disable_without_access_role_change_resets_detail(env):
    db, f, svc = env
    snap_sync(db, svc)
    conn = connect(db)
    service.create_user(conn, "Mixed", None, role="supervisor", auth_source="oidc")
    uid5 = service.get_user(conn, "13700000005")["id"]
    tok, _ = service.create_session(conn, uid5)
    conn.close()
    f.seed("mixed", "admin", [], display="另一个人")
    f.roles["mhero-admin"].discard("13700000005"); f.roles.setdefault("mhero-supervisor", set()).add("13700000005")
    f.roles.setdefault("mhero-app-report", set()).add("13700000005")
    st = snap_sync(db, svc)
    assert st["case_conflicts"] == ["mixed"]
    conn = connect(db)
    assert service.get_user(conn, "Mixed")["role"] == "supervisor"                        # not promoted via NOCASE match
    u5 = service.get_user(conn, "13700000005")
    assert u5["role"] == "supervisor" and u5["full_detail"] == 0                           # admin detail flag recomputed
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (uid5,)).fetchone()[0] == 0   # old session revoked
    conn.close()
    f.roles["mhero-app-report"].discard("13700000001"); f.users["13700000001"]["isForbidden"] = True    # lost access AND disabled
    st = snap_sync(db, svc)
    assert "13700000001" in st["disabled"]
    conn = connect(db); assert service.get_user(conn, "13700000001")["disabled"] == 1; conn.close()


def test_autosync_failure_does_not_break_page(env, monkeypatch):
    db, f, svc = env
    app = create_app(db)
    class Boom:
        def snapshot(self): return [{"name": None}]          # malformed → sync_users raises
    app.state.account_bridge = Boom()
    conn = connect(db)
    tok, _ = service.create_session(conn, service.get_user(conn, "boss")["id"])
    conn.close()
    c = TestClient(app); c.cookies.set("reportdb_session", tok)
    assert c.get("/admin/users").status_code == 200
