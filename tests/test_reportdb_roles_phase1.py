"""权限一期：区域报表里 超级管理员 / 本系统超级管理员 / 只读管理员 / 督导 / 用户 的逐角色矩阵。"""
import re

import pytest
from fastapi.testclient import TestClient

from reportdb import exemptions as ex
from reportdb.auth import service
from reportdb.db import connect
from reportdb.webapp import create_app
from test_reportdb_exemptions import SNAP1, D1, D2  # noqa: F401
from test_reportdb_correction_exclusion_split import load_shared

ROLES = {   # name: (role, auth_source, global_admin)
    "gadmin": ("admin", "oidc", 1),        # 全局超级管理员（19893173727 这类）
    "sysadmin": ("admin", "oidc", 0),      # 黄页里只把「区域报表」设为超级管理员
    "roadmin": ("readonly_admin", "oidc", 0),
    "sup": ("supervisor", "oidc", 0),
    "user": ("viewer", "oidc", 0),
    "localadm": ("admin", "local", 0),     # 本地应急管理员
}


@pytest.fixture()
def web(tmp_path, conn):
    conn.execute("UPDATE stores SET supervisor='张督导' WHERE name=?", (D1,))
    conn.commit()
    load_shared(conn, SNAP1)
    out = {"db": tmp_path / "t.sqlite3"}
    for name, (role, src, ga) in ROLES.items():
        uid = service.create_user(conn, name, None if src == "oidc" else "local-pass-1234", role=role, auth_source=src,
                                  supervisor_name="张督导" if role == "supervisor" else "")
        conn.execute("UPDATE users SET global_admin=? WHERE id=?", (ga, uid))
        conn.commit()
        tok, _ = service.create_session(conn, uid)
        c = TestClient(create_app(out["db"])); c.cookies.set("reportdb_session", tok)
        out[name] = c
    conn.close()
    return out


def csrf(c, path="/"):
    m = re.search(r'name="csrf" value="([^"]+)"', c.get(path).text) or re.search(r'name="csrf-token" content="([^"]+)"', c.get(path).text)
    return m.group(1) if m else ""


GET_MATRIX = {
    # path: roles that get 200
    "/": set(ROLES),
    "/tables/summary": set(ROLES),
    "/reports/daily": set(ROLES),
    "/data/first-service": set(ROLES) - {"user"},
    "/stores": set(ROLES) - {"user"},
    "/api/quality": set(ROLES) - {"user"},
    "/exemptions/mine": {"gadmin", "sysadmin", "sup", "localadm"},
    "/admin/exemptions": {"gadmin", "sysadmin", "roadmin", "localadm"},
    "/admin/exemptions.xlsx": {"gadmin", "sysadmin", "roadmin", "localadm"},
    "/settings/spare-parts": {"gadmin", "sysadmin", "roadmin", "localadm"},
    "/settings/exclusion-rules": {"gadmin", "sysadmin", "roadmin", "localadm"},
    "/export/admin/exclusion-rules.xlsx": {"gadmin", "sysadmin", "roadmin", "localadm"},
    "/admin/users": {"gadmin", "localadm"},
    "/export/admin/users.xlsx": {"gadmin", "localadm"},
}


@pytest.mark.parametrize("path", list(GET_MATRIX))
def test_get_matrix(web, path):
    for who in ROLES:
        code = web[who].get(path).status_code
        assert (code == 200) == (who in GET_MATRIX[path]), (who, path, code)


def test_writes_by_role(web):
    db = web["db"]
    # 督导提一条申请，供审批
    sup = web["sup"]
    new = sup.get("/exemptions/new?indicator=first_service").text
    rn = re.search(r'name="reason_(\d+)"', new).group(1)
    r = sup.post("/exemptions/request", data={"indicator": "first_service", "date": SNAP1, f"reason_{rn}": "x", "csrf": csrf(sup, "/exemptions/new")},
                 follow_redirects=False)
    assert "msg=" in r.headers["location"]
    eid = connect(db).execute("SELECT id FROM exemptions").fetchone()[0]
    # 只读管理员 / 用户 / 督导：审批 403
    for who in ("roadmin", "user", "sup"):
        c = web[who]
        assert c.post("/admin/exemptions/review", data={"action": "approve", "ids": eid, "csrf": csrf(c)}).status_code == 403, who
    # 只读管理员 / 用户：不能申请
    for who in ("roadmin", "user"):
        c = web[who]
        assert c.post("/exemptions/request", data={"indicator": "first_service", "date": SNAP1, f"reason_{rn}": "x", "csrf": csrf(c)}).status_code == 403, who
    # 只读管理员：设置类写操作 403；账号管理 403
    ro = web["roadmin"]; t = csrf(ro)
    assert ro.post("/settings/exclusion-rules/add", data={"csrf": t, "dataset": "first_service", "column": "vin", "value": "x"}).status_code == 403
    assert ro.post("/admin/users/sync-sso", data={"csrf": t}).status_code == 403
    assert ro.post("/admin/users/1/update", data={"csrf": t, "action": "unlock"}).status_code == 403
    page = ro.get("/admin/exemptions").text
    assert "readonly-banner" in page and "b.disabled = true" in page
    # 本系统超级管理员：可以审批，但不能管账号
    sa = web["sysadmin"]
    r = sa.post("/admin/exemptions/review", data={"action": "approve", "ids": eid, "comment": "ok", "csrf": csrf(sa, "/admin/exemptions")},
                follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert sa.post("/admin/users/sync-sso", data={"csrf": csrf(sa)}).status_code == 403
    assert "/admin/users" not in sa.get("/").text
    # 全局超级管理员：账号管理可进；门店手工分配已停用
    ga = web["gadmin"]
    uid = connect(db).execute("SELECT id FROM users WHERE username='sup'").fetchone()[0]
    r = ga.post(f"/admin/users/{uid}/stores", data={"codes": "X", "csrf": csrf(ga, "/admin/users")}, follow_redirects=False)
    assert r.status_code == 303 and connect(db).execute("SELECT COUNT(*) FROM user_stores").fetchone()[0] == 0


def test_supervisor_scope_from_feishu_only(web):
    c = connect(web["db"])
    by_name = ex.supervisor_stores(c, "sup", "张督导")
    assert {r["name"] for r in by_name} == {D1}
    c.execute("UPDATE stores SET supervisor_phone='13700000009' WHERE name=?", (D2,)); c.commit()
    assert {r["name"] for r in ex.supervisor_stores(c, "13700000009", "")} == {D2}
    assert ex.supervisor_stores(c, "13700000010", "") == []


def test_nav_by_role(web):
    nav = lambda who: web[who].get("/").text
    assert "明细数据" not in nav("user") and ">管理<" not in nav("user")
    assert "明细数据" in nav("sup") and ">管理<" not in nav("sup")
    assert ">管理<" in nav("roadmin") and "用户管理" not in nav("roadmin") and "只读管理员" in nav("roadmin")
    assert "用户管理" in nav("gadmin") and "超级管理员" in nav("gadmin")
    assert "本系统超级管理员" in nav("sysadmin") and "用户管理" not in nav("sysadmin")


def test_oidc_mapping_roles_and_report_override(monkeypatch, tmp_path):
    import json
    from reportdb.auth.oidc import OidcBackend
    for k, v in {"REPORTDB_OIDC_ISSUER": "https://sso.example.test", "REPORTDB_OIDC_CLIENT_ID": "cid",
                 "REPORTDB_OIDC_REDIRECT_URI": "https://r.example.test/cb", "REPORTDB_OIDC_ADMIN_GROUPS": "mhero-admin",
                 "REPORTDB_OIDC_SUPERVISOR_GROUPS": "mhero-supervisor", "MHERO_AUTH_REQUIRE_APP": "1"}.items():
        monkeypatch.setenv(k, v)
    ov = tmp_path / "ov.json"
    ov.write_text(json.dumps({"13700000001": {"report": "admin"}, "13700000002": {"report": "user"}, "boss": {"report": "user"}}))
    monkeypatch.setenv("MHERO_AUTH_ROLE_OVERRIDES_FILE", str(ov))
    b = OidcBackend(lambda: None)
    m = lambda u, *roles: b.map_identity({"preferred_username": u, "roles": [{"name": r} for r in roles]})
    boss = m("boss", "mhero-admin")
    assert boss.role == "admin" and boss.global_admin is True                              # 全局超级管理员不受单独角色影响
    ro = m("ro", "mhero-readonly-admin")                                                     # 只读管理员不需要逐系统开通
    assert ro.role == "readonly_admin" and ro.full_detail and not ro.global_admin
    sa = m("13700000001", "mhero-supervisor", "mhero-app-report")
    assert sa.role == "admin" and sa.global_admin is False                                   # 本系统超级管理员
    assert m("13700000002", "mhero-supervisor", "mhero-app-report").role == "viewer"        # 本系统降为用户
    assert m("u", "mhero-app-report").role == "viewer"                                       # 无身份 = 用户
    with pytest.raises(Exception):
        m("u2", "mhero-supervisor")                                                          # 未开通区域报表


def test_phone_beats_name_and_local_merge_refused(web):
    from reportdb.auth.base import AuthError, Identity
    c = connect(web["db"])
    # D1 写了别人的手机号：同名督导不能再按姓名匹配
    c.execute("UPDATE stores SET supervisor_phone='13700000001' WHERE name=?", (D1,)); c.commit()
    assert ex.supervisor_stores(c, "sup", "张督导") == []
    assert {r["name"] for r in ex.supervisor_stores(c, "13700000001", "")} == {D1}
    # 统一登录不能合并进同名本地账号（本地应急管理员）
    with pytest.raises(AuthError):
        service.provision_external(c, Identity("localadm", "x", "admin", True, "oidc", global_admin=False))
    with pytest.raises(AuthError):
        service.provision_external(c, Identity("LOCALADM", "x", "admin", True, "oidc", global_admin=False))
    assert service.get_user(c, "localadm")["auth_source"] == "local"
