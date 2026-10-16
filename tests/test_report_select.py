import json, os, sys, time
from pathlib import Path

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import report_select as rs  # noqa: E402

SUMMARY = ["直营店指标", "区域指标排序", "直营店指标排序", "总表", "常备件备库率"]
DETAIL = SUMMARY + ["备件库存明细", "招揽实施率", "首保", "二保", "新保", "续保"]


def mk(p: Path, sheets, age=0):
    wb = Workbook(); wb.active.title = sheets[0]
    for n in sheets[1:]:
        wb.create_sheet(n)
    wb.save(p); t = time.time() - age; os.utime(p, (t, t))
    return p


@pytest.fixture
def out(tmp_path):
    return tmp_path


def test_reportdb_engine_ignores_legacy_and_detail_files_even_if_newer(out):
    good = mk(out / "区域各指标情况一览1008.xlsx", SUMMARY, age=1000)
    mk(out / "区域各指标情况一览1009.xlsx", DETAIL, age=10)                      # old Excel engine: 11 sheets, newest
    mk(out / "区域各指标情况一览_2026Q3_新模板.xlsx", SUMMARY, age=5)             # legacy marker
    mk(out / "区域各指标情况一览0812_公式版.xlsx", SUMMARY, age=1)               # legacy sidecar
    assert rs.latest_report(out, "reportdb") == str(good)


def test_manifest_wins(out):
    a = mk(out / "区域各指标情况一览1009_083100.xlsx", SUMMARY, age=500)
    mk(out / "区域各指标情况一览1010.xlsx", SUMMARY, age=1)
    (out / "pipeline_manifest.json").write_text(json.dumps({"engine": "reportdb", "status": "ok", "report": "/Users/i/x/output/" + a.name}))
    assert rs.latest_report(out, "reportdb") == str(a)


def test_manifest_pointing_at_detail_file_is_refused(out):
    d = mk(out / "区域各指标情况一览1009.xlsx", DETAIL)
    (out / "pipeline_manifest.json").write_text(json.dumps({"engine": "reportdb", "status": "ok", "report": d.name}))
    assert rs.latest_report(out, "reportdb") == ""


def test_manifest_of_excel_engine_or_failed_run_is_not_trusted(out):
    d = mk(out / "区域各指标情况一览1009.xlsx", SUMMARY)
    (out / "pipeline_manifest.json").write_text(json.dumps({"status": "error", "report": d.name}))
    assert rs.latest_report(out, "reportdb") == str(d)      # falls back to content-checked scan


def test_nothing_safe_returns_empty(out):
    mk(out / "区域各指标情况一览1009.xlsx", DETAIL)
    assert rs.latest_report(out, "reportdb") == ""
    assert rs.latest_report(out / "nope", "reportdb") == ""


def test_excel_engine_keeps_legacy_behaviour(out):
    mk(out / "区域各指标情况一览1009.xlsx", DETAIL, age=100)
    side = mk(out / "区域各指标情况一览1009_公式版.xlsx", DETAIL, age=200)
    assert rs.latest_report(out, "excel") == str(side)       # prefers the formula sidecar of the newest
    assert rs.latest_report(out, "") == str(side)            # default engine = excel
    only = out / "x"; only.mkdir(); n = mk(only / "区域各指标情况一览1.xlsx", DETAIL)
    assert rs.latest_report(only, "excel") == str(n)


def test_lock_files_ignored(out):
    mk(out / "~$区域各指标情况一览1009.xlsx", SUMMARY)
    assert rs.latest_report(out, "reportdb") == ""
