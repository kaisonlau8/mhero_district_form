"""与 Excel 公式引擎（模板公式 + app/excel_values.py）逐格对账。慢：每项约 40 秒。"""
import os
from pathlib import Path

import pytest

from conftest import ROOT, needs_real_data
from reportdb import calc, parity
from reportdb.db import connect, migrate
from reportdb.ingest import ingest_dir, ingest_report
from reportdb.loaders import load_directory_sources, load_report_workbook
from reportdb.stores import region_lookup_factory

pytestmark = [pytest.mark.parity,
              pytest.mark.skip(reason="口径已改：区域取 DMS 导出区域列、DMS 无区域的行自动剔除，不再与旧 Excel 模板（门店表区域）逐格一致；保留作历史对账参考"),
              pytest.mark.skipif(os.getenv("REPORTDB_SKIP_PARITY") == "1", reason="REPORTDB_SKIP_PARITY=1")]
REPORT = lambda d: ROOT / "output" / f"区域各指标情况一览{d}.xlsx"


def _check(res):
    assert res["diff_count"] == 0, res["diffs"][:10]
    assert res["max_float_delta"] < 1e-12
    assert res["cells"] > 5000


@needs_real_data
def test_parity_today_dir_store_mode(tmp_path):
    """今天 08:30 的 7 份源文件 → 新代码 vs 模板公式（区域按门店表归属）。"""
    c = connect(tmp_path / "p.sqlite3"); migrate(c)
    data, _ = load_directory_sources(ROOT / "download"); data.pop("_origin_renewal")
    ingest_dir(c, ROOT / "download", "2026-10-09", recompute=False)
    mine = calc.compute(c, "2026-10-09", "store")
    ref = parity.reference_values(data, region_lookup_factory(c))
    _check(parity.compare(mine, ref))


@needs_real_data
def test_parity_today_report_source_mode(tmp_path):
    """生产实际产出的 区域各指标情况一览1009.xlsx（沿用 DMS 大区文本，区域表因此全 0）→ 逐格一致。"""
    c = connect(tmp_path / "p.sqlite3"); migrate(c)
    data = load_report_workbook(REPORT("1009"))
    ingest_report(c, REPORT("1009"), "2026-10-09", recompute=False)
    mine = calc.compute(c, "2026-10-09", "source")
    ref = parity.reference_values(data, None)
    res = parity.compare(mine, ref)
    _check(res)
    # 直营店层与常备件层不依赖区域，与生产结果一致
    g = mine["direct-stores"].grid()
    assert g[(3, 2)] == 17 and g[(4, 2)] == 24


@pytest.mark.parametrize("day", ["1008", "0930"])
def test_parity_other_days_store_mode(tmp_path, day):
    if not REPORT(day).exists():
        pytest.skip("历史日报不存在")
    c = connect(tmp_path / "p.sqlite3"); migrate(c)
    data = load_report_workbook(REPORT(day))
    ingest_report(c, REPORT(day), "2026-" + day[:2] + "-" + day[2:], recompute=False)
    mine = calc.compute(c, "2026-" + day[:2] + "-" + day[2:], "store")
    _check(parity.compare(mine, parity.reference_values(data, region_lookup_factory(c))))
