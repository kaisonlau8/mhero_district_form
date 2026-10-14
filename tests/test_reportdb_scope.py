"""门店表（多维表格）权威性 + 不在门店表内的行不计入任何指标。"""
import pytest

from reportdb import calc, ingest
from reportdb.stores import StoreSyncAborted, upsert_stores
from test_reportdb_calc import make_data


def test_authoritative_default_deactivates(conn):
    n = conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0]
    keep = [dict(r) for r in conn.execute("SELECT code,name,region FROM stores WHERE active=1 ORDER BY seq LIMIT ?", (n - 3,))]
    res = upsert_stores(conn, keep)  # 默认 authoritative
    assert conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0] == n - 3


def test_guard_empty_and_too_small(conn):
    n = conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0]
    with pytest.raises(StoreSyncAborted):
        upsert_stores(conn, [])
    few = [dict(r) for r in conn.execute("SELECT code,name,region FROM stores LIMIT ?", (n // 2 - 1,))]
    with pytest.raises(StoreSyncAborted):
        upsert_stores(conn, few)
    assert conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0] == n  # 原样保留
    ok = [dict(r) for r in conn.execute("SELECT code,name,region FROM stores LIMIT ?", ((n + 1) // 2,))]
    upsert_stores(conn, ok)  # 恰好 50% 放行


def test_env_can_disable_authoritative(conn, monkeypatch):
    monkeypatch.setenv("REPORTDB_STORES_AUTHORITATIVE", "0")
    n = conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0]
    upsert_stores(conn, [dict(r) for r in conn.execute("SELECT code,name,region FROM stores LIMIT ?", (n - 5,))])
    assert conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0] == n


def test_unlisted_store_rows_excluded_everywhere(conn):
    data, origin = make_data(conn)
    ingest.ingest_datasets(conn, "2026-01-01", data, source="t", origin_renewal=origin)
    # 数据里已有一家不在门店表的店（NOSUCH）：in_scope=0，不进任何聚合
    assert conn.execute("SELECT COUNT(*) FROM first_service WHERE in_scope=0").fetchone()[0] == 1
    reg = calc._agg(conn, "first_service", "2026-01-01", "region", "done", "是")
    assert sum(v[0] for v in reg.values()) == 4  # 5 行里剔除 1 行
    # 再把一家有数据的店移出门店表 → 重算后同样被剔除（区域与全国都不含）
    victim = conn.execute("SELECT store_name FROM first_service WHERE in_scope=1 LIMIT 1").fetchone()[0]
    conn.execute("UPDATE stores SET active=0 WHERE name=?", (victim,))
    conn.commit()
    ingest.recompute(conn, "2026-01-01", remap=True)
    assert conn.execute("SELECT COUNT(*) FROM first_service WHERE in_scope=0 AND store_name=?", (victim,)).fetchone()[0] >= 1
    reg2 = calc._agg(conn, "first_service", "2026-01-01", "region", "done", "是")
    assert sum(v[0] for v in reg2.values()) < 4
    st = calc._agg(conn, "first_service", "2026-01-01", "store_name", "done", "是")
    assert victim not in st and "不在门店表的店" not in st
