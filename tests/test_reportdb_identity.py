"""选择身份：管理员预览督导（只读）、OIDC 下身份 / 停用同步到 Casdoor、强制下线、系统开通检查、督导批量导入。"""
import json, os, re, sys, time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

AUTH_DIR = Path(__file__).resolve().parents[2] / "m-hero-auth"
sys.path[:0] = [str(AUTH_DIR), str(AUTH_DIR / "tests")]

from reportdb.auth import AuthError, service
from reportdb.auth.oidc import OidcBackend
from reportdb.db import connect
from reportdb.webapp import create_app
from test_reportdb_web import app_env, login, _fast_hash  # noqa: F401


def csrf_of(c, path="/admin/users"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


@pytest.fixture()
def sup(app_env):
    conn = connect(app_env)
    service.create_user(conn, "sup1", "sup1-password-12", role="supervisor", supervisor_name="张三", display_name="张三")
    conn.close()
    return app_env


def test_admin_preview_is_read_only_and_exitable(sup):
    admin = TestClient(create_app(sup)); login(admin, "boss", "boss-password-1")
    conn = connect(sup); uid = service.get_user(conn, "sup1")["id"]; conn.close()
    t = csrf_of(admin)
    r = admin.post(f"/admin/preview/{uid}/start", data={"csrf": t}, follow_redirects=False)
    assert r.status_code == 303
    page = admin.get("/exemptions/new").text
    assert 'id="preview-banner"' in page and "张三" in page             # sees what the supervisor sees (apply page)
    assert admin.get("/admin/users").status_code == 403                  # now a supervisor, not an admin
    for path in ("/exemptions/request", "/exclusions/request"):
        r = admin.post(path, data={"csrf": t, "indicator": "task", "vin": "X", "reason": "x"})
        assert r.status_code == 403 and "预览模式只读" in r.text
    r = admin.post("/admin/preview/exit", data={"csrf": csrf_of(admin, "/")}, follow_redirects=False)
    assert r.status_code == 303 and admin.get("/admin/users").status_code == 200
    conn = connect(sup)
    acts = [x["action"] for x in conn.execute("SELECT action FROM audit_log")]
    assert "preview.start" in acts and "preview.exit" in acts


def test_non_admin_cannot_preview_or_fake_cookie(sup):
    s = TestClient(create_app(sup)); login(s, "sup1", "sup1-password-12")
    conn = connect(sup); boss = service.get_user(conn, "boss")["id"]; conn.close()
    assert s.post(f"/admin/preview/{boss}/start", data={"csrf": csrf_of(s, "/")}).status_code == 403
    s.cookies.set("reportdb_preview", str(boss))                       # a forged cookie changes nothing for non-admins
    assert s.get("/admin/users").status_code == 403
    v = TestClient(create_app(sup)); login(v, "viewer1", "viewer-password-1")
    v.cookies.set("reportdb_preview", "1")
    assert 'id="preview-banner"' not in v.get("/").text


def test_preview_cannot_target_admin_or_disabled(sup):
    admin = TestClient(create_app(sup)); login(admin, "boss", "boss-password-1")
    conn = connect(sup); boss = service.get_user(conn, "boss")["id"]; conn.close()
    r = admin.post(f"/admin/preview/{boss}/start", data={"csrf": csrf_of(admin)}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_revoke_file_force_logout(sup, monkeypatch, tmp_path):
    rv = tmp_path / "rv.json"; monkeypatch.setenv("MHERO_AUTH_REVOKE_FILE", str(rv))
    s = TestClient(create_app(sup)); login(s, "sup1", "sup1-password-12")
    assert s.get("/tables/summary").status_code == 200
    rv.write_text(json.dumps({"sup1": time.time() + 5}))
    assert s.get("/tables/summary", follow_redirects=False).status_code == 303
    rv.write_text("garbage")
    s = TestClient(create_app(sup)); login(s, "sup1", "sup1-password-12")
    assert s.get("/tables/summary").status_code == 200   # corrupt file never locks anyone out


# ---------------------------------------------------------------- OIDC mode: identity changes go to Casdoor
@pytest.fixture()
def oidc_app(sup, monkeypatch):
    from fakes import FakeCasdoor
    from mhero_auth.accounts import AccountService
    for k, v in {"REPORTDB_AUTH_BACKEND": "oidc", "REPORTDB_OIDC_ISSUER": "https://idp.example", "REPORTDB_OIDC_CLIENT_ID": "cid",
                 "REPORTDB_OIDC_REDIRECT_URI": "http://testserver/auth/oidc/callback"}.items():
        monkeypatch.setenv(k, v)
    f = FakeCasdoor(); f.seed("boss", "admin"); f.seed("sup1", "supervisor", ["report"])
    app = create_app(sup)
    app.state.account_bridge = AccountService(f, audit_path="", revoke_path="")
    conn = connect(sup)
    service.create_session  # users already exist (auth_source local, fine for the session cookie)
    tok, _ = service.create_session(conn, service.get_user(conn, "boss")["id"])
    conn.close()
    c = TestClient(app); c.cookies.set("reportdb_session", tok)
    return c, f, sup


def test_oidc_level_change_syncs_casdoor_and_guards(oidc_app):
    c, f, db = oidc_app
    t = csrf_of(c)
    conn = connect(db); uid = service.get_user(conn, "sup1")["id"]; bid = service.get_user(conn, "boss")["id"]; conn.close()
    r = c.post(f"/admin/users/{uid}/update", data={"role": "admin", "action": "save", "csrf": t}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and "sup1" in f.roles["mhero-admin"] and "sup1" not in f.roles["mhero-supervisor"]
    r = c.post(f"/admin/users/{uid}/update", data={"role": "viewer", "action": "save", "csrf": t}, follow_redirects=False)
    assert "err=" in r.headers["location"]                              # viewer does not exist under SSO
    r = c.post(f"/admin/users/{bid}/update", data={"role": "supervisor", "action": "save", "csrf": t}, follow_redirects=False)
    assert "err=" in r.headers["location"] and "boss" in f.roles["mhero-admin"]       # cannot demote self
    r = c.post(f"/admin/users/{uid}/update", data={"action": "toggle", "csrf": t}, follow_redirects=False)
    assert f.users["sup1"]["isForbidden"] is True


def test_oidc_without_bridge_refuses_instead_of_diverging(oidc_app):
    c, f, db = oidc_app
    c.app.state.account_bridge = None
    c.app.state._bridge_cache = None
    conn = connect(db); uid = service.get_user(conn, "sup1")["id"]; conn.close()
    r = c.post(f"/admin/users/{uid}/update", data={"role": "admin", "action": "save", "csrf": csrf_of(c)}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    conn = connect(db); assert service.get_user(conn, "sup1")["role"] == "supervisor"


def test_oidc_require_app(monkeypatch):
    monkeypatch.setenv("REPORTDB_OIDC_ISSUER", "https://sso.example.test"); monkeypatch.setenv("REPORTDB_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("REPORTDB_OIDC_REDIRECT_URI", "https://r.example.test/cb")
    monkeypatch.setenv("REPORTDB_OIDC_ADMIN_GROUPS", "mhero-admin"); monkeypatch.setenv("REPORTDB_OIDC_SUPERVISOR_GROUPS", "mhero-supervisor")
    b = OidcBackend(lambda: None)
    sup_only = {"preferred_username": "x", "roles": [{"name": "mhero-supervisor"}]}
    assert b.map_identity(sup_only).role == "supervisor"                    # flag OFF (default): unchanged
    monkeypatch.setenv("MHERO_AUTH_REQUIRE_APP", "1")
    with pytest.raises(AuthError) as e:
        b.map_identity(sup_only)
    assert e.value.status == 403
    ok = {"preferred_username": "x", "roles": [{"name": "mhero-supervisor"}, {"name": "mhero-app-report"}]}
    assert b.map_identity(ok).role == "supervisor"
    assert b.map_identity({"preferred_username": "a", "roles": [{"name": "mhero-admin"}]}).role == "admin"      # global admin: no app role needed


# ---------------------------------------------------------------- bulk import of supervisors
def _directory(tmp_path):
    def st(code, name, phone, open_id="o"):
        return {"code": code, "name": "店" + code, "region": "华北", "supervisor": {"name": name, "phone": phone, "open_id": open_id}}
    stores = [st("S1", "甲", "13800000001"), st("S2", "甲", "13800000001"), st("S3", "乙", "13800000002"),
              st("S4", "丙", ""), st("S5", "丁", "12345"), st("S6", "戊", "13800000003"), st("S7", "己", "13800000003"),
              st("S8", "庚", "13800000009"), st("S9", "庚", "13800000008"), st("S10", "庚", "13800000008"),
              {"code": "S11", "name": "x", "region": "华北", "supervisor": None}]
    stores[6]["supervisor"]["name"] = "己"           # 戊 / 己 share 13800000003
    p = tmp_path / "dir.json"; p.write_text(json.dumps({"stores": stores}, ensure_ascii=False))
    return p, stores


def test_import_plan_and_masking(tmp_path):
    from reportdb import import_supervisors as imp
    _, stores = _directory(tmp_path)
    plan = imp.plan_from_directory(stores)
    got = {p["name"]: (p["phone"], len(p["stores"])) for p in imp.importable(plan)}
    assert got == {"甲": ("13800000001", 2), "乙": ("13800000002", 1), "庚": ("13800000008", 2)}   # 庚: phone with most stores
    assert plan["no_phone"] == [("丙", "S4")] and plan["bad_phone"][0][0] == "丁" and plan["no_supervisor"] == ["S11"]
    assert "13800000003" in plan["shared_phone"] and plan["multi_phone"] == {"庚": ["13800000008", "13800000009"]}
    out = imp.render(plan)
    assert "138****0001" in out and "13800000001" not in out and "13800000003" not in out


def test_import_dry_run_changes_nothing_and_apply_is_idempotent(tmp_path, monkeypatch, capsys):
    from fakes import FakeCasdoor
    from reportdb import import_supervisors as imp
    from mhero_auth import accounts as acc
    path, stores = _directory(tmp_path)
    db = tmp_path / "r.sqlite3"
    c = connect(db); from reportdb.db import migrate; migrate(c)
    for s in stores:
        c.execute("INSERT INTO stores(code,name,region,seq,active) VALUES (?,?,?,?,1)", (s["code"], s["name"], "华北", 1))
    c.commit(); c.close()
    assert imp.main(["--directory", str(path), "--db", str(db)]) == 0
    c = connect(db); assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    # apply with a fake Casdoor service
    f = FakeCasdoor(); f.seed("13800000001", "admin")          # pre-existing account: must be left alone
    svc = acc.AccountService(f, audit_path=str(tmp_path / "a.log"))
    PW = "Init-Pass-77x"
    f_people = imp.importable(imp.plan_from_directory(stores))
    for p in f_people:
        svc.create_bulk("import", p["phone"], p["name"], PW, "supervisor", ["report"])
    assert f.users["13800000002"]["needUpdatePassword"] is True and f.users["13800000002"]["displayName"] == "乙"
    assert "13800000002" in f.roles["mhero-supervisor"] and "13800000002" in f.roles["mhero-app-report"]
    assert "13800000001" in f.roles["mhero-admin"] and "13800000001" not in f.roles.get("mhero-supervisor", set())   # untouched
    n_calls = len(f.calls)
    for p in f_people:
        assert svc.create_bulk("import", p["phone"], p["name"], PW, "supervisor", ["report"]) == "exists"
    assert len(f.calls) == n_calls                                       # idempotent: nothing written second time
    assert PW not in open(tmp_path / "a.log").read()
    c = connect(db)
    r1 = imp.apply_reportdb(c, f_people); r2 = imp.apply_reportdb(c, f_people)
    assert r1 == {"new_users": 3, "store_links": 5} and r2 == {"new_users": 0, "store_links": 0}
    assert c.execute("SELECT role,supervisor_name,auth_source FROM users WHERE username='13800000008'").fetchone()[:] == ("supervisor", "庚", "oidc")
    with pytest.raises(acc.AccountError):
        svc.create_bulk("import", "abc", "x", PW, "supervisor", [])
    with pytest.raises(acc.AccountError):
        svc.create_bulk("import", "13800000077", "x", "short", "supervisor", [])
