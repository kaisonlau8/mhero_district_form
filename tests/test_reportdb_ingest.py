import json
import os
import sqlite3

import pytest

from conftest import ROOT, needs_real_data
from reportdb import config, ingest
from reportdb.db import connect, migrate, schema_version
from reportdb.stores import upsert_stores
from test_reportdb_calc import make_data


def test_migrate_idempotent_and_seed(tmp_path):
    c = connect(tmp_path / "a.sqlite3")
    v = migrate(c)
    assert v == migrate(c) == schema_version(c) >= 1
    assert c.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 113
    assert [r[0] for r in c.execute("SELECT name FROM regions ORDER BY sort_order")] == ["华北", "华中1", "华南", "西南", "华东", "西北", "华中2"]
    assert c.execute("SELECT COUNT(*) FROM stores WHERE direct_order IS NOT NULL").fetchone()[0] == 4
    assert c.execute("SELECT COUNT(*) FROM spare_parts WHERE counted=1").fetchone()[0] == 32


def test_ingest_idempotent_and_history(conn):
    data, origin = make_data(conn)
    r1 = ingest.ingest_datasets(conn, "2026-01-01", data, source="t", origin_renewal=origin)
    r2 = ingest.ingest_datasets(conn, "2026-01-01", data, source="t", origin_renewal=origin)
    assert r1["rows"] == r2["rows"]
    assert conn.execute("SELECT COUNT(*) FROM first_service").fetchone()[0] == 5
    ingest.ingest_datasets(conn, "2026-01-02", data, source="t", origin_renewal=origin)
    assert conn.execute("SELECT COUNT(DISTINCT snapshot_date) FROM first_service").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM report_results").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM renewal WHERE origin='backlog'").fetchone()[0] == 2
    # 归一化：首保 H 列重写、库存 M 列数字化
    assert [r[0] for r in conn.execute("SELECT done FROM first_service WHERE snapshot_date='2026-01-01' ORDER BY row_no")][:3] == ["是", "否", "否"]
    assert conn.execute("SELECT typeof(available_qty) FROM stock_items WHERE snapshot_date='2026-01-01' AND part_no='22806000' AND store_name LIKE '%南宁%'").fetchone()[0] == "integer"
    assert conn.execute("SELECT typeof(available_qty) FROM stock_items WHERE snapshot_date='2026-01-01' AND part_no='91401000'").fetchone()[0] == "text"
    # 清理
    old = ingest.prune(conn, 1)
    assert sorted(old) == ["2026-01-01", "2026-01-02"]


def test_recompute_remap(conn):
    data, origin = make_data(conn)
    ingest.ingest_datasets(conn, "2026-01-01", data, source="t", origin_renewal=origin)
    before = conn.execute("SELECT COUNT(*) FROM first_service WHERE region IS NULL").fetchone()[0]
    upsert_stores(conn, [{"code": "NOSUCH", "name": "不在门店表的店", "region": "华北"}], authoritative=False)
    ingest.recompute(conn, "2026-01-01", remap=True)
    after = conn.execute("SELECT COUNT(*) FROM first_service WHERE region IS NULL").fetchone()[0]
    assert (before, after) == (1, 0)


def test_store_upsert_rules(conn):
    n0 = conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0]
    res = upsert_stores(conn, [{"code": "NEW001", "name": "新店", "region": "华东"},
                               {"code": "MSCSDS001", "name": "改名后的店", "region": "华南"}], authoritative=False)
    assert (res["added"], res["updated"], res["deactivated"]) == (1, 1, 0)
    assert conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0] == n0 + 1
    assert conn.execute("SELECT region FROM stores WHERE code='MSCSDS001'").fetchone()[0] == "华南"
    # 直营标记不被同步覆盖；新店追加在末尾
    assert conn.execute("SELECT MAX(seq) FROM stores").fetchone()[0] == conn.execute("SELECT seq FROM stores WHERE code='NEW001'").fetchone()[0]
    import pytest
    from reportdb.stores import StoreSyncAborted
    with pytest.raises(StoreSyncAborted):  # 安全闸：名单明显偏少 → 整次放弃
        upsert_stores(conn, [{"code": "NEW001", "name": "新店", "region": "华东"}])
    assert conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0] == n0 + 1
    res = upsert_stores(conn, [{"code": "NEW001", "name": "新店", "region": "华东"}], min_ratio=0)  # 显式放宽才会真停用
    assert res["deactivated"] == n0
    assert conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0] == 1


def test_pipeline_hook_default_off(monkeypatch):
    assert config.enabled() is False
    src = (ROOT / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert 'os.getenv("MHERO_REPORTDB"' in src
    monkeypatch.setenv("MHERO_REPORTDB", "1")
    assert config.enabled() is True


def test_hook_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTDB_PATH", str(tmp_path / "h.sqlite3"))
    res = ingest.ingest_after_pipeline(tmp_path / "empty")
    assert res["ok"] is False and "error" in res


@needs_real_data
def test_cli_real_files(tmp_path, capsys):
    db = tmp_path / "r.sqlite3"
    rc = ingest.main(["--dir", str(ROOT / "download"), "--date", "2026-10-09", "--db", str(db), "--region-mode", "store"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["rows"] == {"stock": 21396, "task": 3324, "first-service": 2488, "second-service": 558, "new-policy": 214, "renewal": 5629}
    assert out["calc_ms"] < 5000
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM report_results").fetchone()[0] == 5


def test_dms_region_rules(conn):
    """区域取 DMS 导出区域列（归一）；DMS 区域为空 → 自动剔除（分子分母都不计）；区域未知 → 退回多维表格区域。"""
    from reportdb import exclusions
    from reportdb.stores import norm_region
    from test_reportdb_calc import D1, row

    assert norm_region("华中1区") == norm_region("华中 1") == "华中1" and norm_region("华北区") == "华北" and norm_region(" ") is None
    data, origin = make_data(conn)
    code = conn.execute("SELECT code FROM stores WHERE name=?", (D1,)).fetchone()[0]
    data["first-service"] = [r[:] for r in data["first-service"]] + [
        row(len(data["first-service"][0]), {1: "", 2: code, 3: D1, 7: "否"}),            # DMS 无区域
        row(len(data["first-service"][0]), {1: "华东区", 2: code, 3: D1, 7: "否"}),      # DMS 区域覆盖多维表格
    ]
    ingest.ingest_datasets(conn, "2026-02-01", data, source="t", origin_renewal=origin)
    regs = {r[0]: r[1] for r in conn.execute("SELECT row_no, region FROM first_service WHERE snapshot_date='2026-02-01'")}
    n = len(regs)
    assert regs[n] == regs[n - 1] or regs[n - 1] is not None
    assert regs[n] == "华东"
    q = exclusions.excluded_counts(conn, "2026-02-01")["first_service"]
    assert q["no_region_rows"] == 1 and q["no_region_stores"][0]["store"] == D1
    # 无区域行不进分母：区域表里首保总数 = 非空区域行数（在门店表内）
    from reportdb import calc
    sheet = calc.compute(conn, "2026-02-01")["summary"]
    total_first = sum(1 for r in conn.execute("SELECT 1 FROM first_service WHERE snapshot_date='2026-02-01' AND in_scope=1 AND trim(COALESCE(src_region,''))<>''"))
    nat = [b for b in sheet.blocks if b.title == "首保实施率"][0].total
    assert nat[1] == total_first
