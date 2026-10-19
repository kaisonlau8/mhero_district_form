"""门店指标：与首页区域行一致（含修正结果 / 剔除 / 自动剔除），页面与权限。"""
import pytest
from fastapi.testclient import TestClient

from reportdb import calc, exemptions as ex, store_indicators as si
from reportdb.auth import service
from reportdb.db import connect
from reportdb.webapp import create_app
from test_reportdb_calc import D1, D2, row
from test_reportdb_exemptions import SPEC, SNAP1, U, load, vin_of  # noqa: F401
from test_reportdb_correction_exclusion_split import SHARED, SHARED2, load_shared
from test_reportdb_web import login  # noqa: F401

KEY2BLOCK = {"task": "task", "first_service": "first", "second_service": "second", "new_policy": "new", "renewal": "renewal"}


def region_rows(conn, snap):
    sh = calc.compute(conn, snap)["summary"]
    out = {}
    for b in sh.blocks:
        for r in b.rows:
            out[(r[0], b.key)] = (r[1], (r[1] - r[2]) if b.key == "task" else r[2])
    return out


def assert_consistent(conn, snap):
    data = si.compute(conn, snap)
    home = region_rows(conn, snap)
    regs = [r["name"] for r in conn.execute("SELECT name FROM regions")]
    tot = si.region_totals(data)
    checked = 0
    for rg in regs:
        for key in si.ORDER:
            den, hit = tot.get(rg.casefold(), {}).get(key, [0, 0])
            h_den, h_num = home[(rg, KEY2BLOCK[key])]
            num = (den - hit) if key == "task" else hit
            assert (den, num) == (h_den, h_num), (rg, key)
            checked += 1
    assert checked == len(regs) * 5
    # 门店合计 == 各区域合计之和（无门店被丢）
    for key in si.ORDER:
        assert sum(s["ind"][key]["den"] for s in data["stores"]) == sum(v[key][0] for v in tot.values() if key in v)
    return data


def test_store_numbers_equal_region_rows_through_corrections_and_exclusions(conn):
    conn.execute("UPDATE stores SET supervisor='张督导' WHERE name=?", (D1,))
    conn.commit()
    load_shared(conn, SNAP1)
    admin, sup = U(conn, "admin", "admin"), U(conn, "sup1", "supervisor", "张督导")
    d0 = assert_consistent(conn, SNAP1)
    s0 = next(s for s in d0["stores"] if s["name"] == D1)
    assert all(s0["ind"][k]["den"] == 3 for k in si.ORDER)
    # 批准修正结果（否→是）：分子+1，已修正=1
    r = ex.request_exemptions(conn, sup, "second_service", SNAP1, vins=[SHARED], category="其他", reason="补录")
    ex.review(conn, admin, r["created"], "approve", "ok")
    d1 = assert_consistent(conn, SNAP1)
    s1 = next(s for s in d1["stores"] if s["name"] == D1)
    assert s1["ind"]["second_service"]["hit"] == s0["ind"]["second_service"]["hit"] + 1
    assert s1["ind"]["second_service"]["corrected"] == 1 and s1["ind"]["first_service"]["corrected"] == 0
    # 待审批剔除：不生效，只计待审批
    e = ex.request_exclusions(conn, sup, SNAP1, items=[(SHARED2, "出口")])
    d2 = assert_consistent(conn, SNAP1)
    s2 = next(s for s in d2["stores"] if s["name"] == D1)
    assert s2["pending_exclude"] == 1 and s2["ind"]["first_service"]["den"] == 3
    # 批准整车剔除：五个指标分母都 -1，已剔除(整车)=1
    ex.review(conn, admin, e["created"], "approve", "ok")
    d3 = assert_consistent(conn, SNAP1)
    s3 = next(s for s in d3["stores"] if s["name"] == D1)
    for k in si.ORDER:
        assert s3["ind"][k]["den"] == 2 and s3["ind"][k]["ex_vin"] == 1, k
    assert s3["pending_exclude"] == 0


def test_no_region_auto_exclusion_counted(conn):
    load_shared(conn, SNAP1)
    for ind in ex.INDICATORS.values():   # 模拟 DMS 无区域：D1 的一行 src_region 置空
        conn.execute(f"UPDATE {ind.table} SET src_region='' WHERE snapshot_date=? AND vin=?", (SNAP1, "OTHER00000000003"))
    conn.commit()
    calc.compute_and_store(conn, SNAP1)
    d = assert_consistent(conn, SNAP1)
    s = next(s for s in d["stores"] if s["name"] == D1)
    assert all(s["ind"][k]["ex_noregion"] == 1 and s["ind"][k]["den"] == 2 for k in si.ORDER)


@pytest.fixture()
def client(tmp_path, conn):
    conn.execute("UPDATE stores SET supervisor='张督导' WHERE name=?", (D1,))
    conn.commit()
    load_shared(conn, SNAP1)
    service.create_user(conn, "admin", "admin-password-1", role="admin")
    service.create_user(conn, "sup1", "sup1-password-12", role="supervisor", supervisor_name="张督导")
    service.create_user(conn, "view1", "view1-password-12", role="viewer")
    conn.close()
    out = {}
    for u, p in (("admin", "admin-password-1"), ("sup1", "sup1-password-12"), ("view1", "view1-password-12")):
        c = TestClient(create_app(tmp_path / "t.sqlite3"))
        login(c, u, p)
        out[u] = c
    return out


def test_pages(client, tmp_path):
    c0 = connect(tmp_path / "t.sqlite3")
    code = next(s["code"] for s in si.compute(c0, SNAP1)["stores"] if s["name"] == D1)
    c0.close()
    assert client["view1"].get("/stores").status_code == 403 and client["view1"].get(f"/stores/{code}").status_code == 403   # 用户只看汇总
    for who in ("admin", "sup1"):
        c = client[who]
        h = c.get("/stores").text
        assert D1 in h and D2 in h and "/stores/" in h and "门店指标" in h
        assert D2 not in c.get("/stores?region=不存在").text
        assert D1 in c.get("/stores?q=" + D1[:6]).text
        d = c.get(f"/stores/{code}")
        assert d.status_code == 200 and "首保实施率" in d.text and "已修正" in d.text and "已剔除" in d.text
        assert "/data/first-service?" in d.text and "store=" in d.text
        assert c.get("/stores/NOPE").status_code == 404
        assert c.get("/stores?sort=first_service").status_code == 200


def test_requires_login(tmp_path, conn):
    conn.close()
    c = TestClient(create_app(tmp_path / "t.sqlite3"))
    r = c.get("/stores", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "/login" in r.headers.get("location", "")
