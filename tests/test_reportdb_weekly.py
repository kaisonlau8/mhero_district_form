"""猛士周报：经营/KPI 解析、同环比、不可覆盖的周快照。"""
import io
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from reportdb.auth import service
from reportdb.db import connect, migrate

OP_HEADERS = [
    "区域名称", "门店编码", "门店名称", "工单数量", "车辆数", "业务类型", "总产值",
    "工时产值", "备件产值", "其他项目产值", "备件成本", "备件利润", "毛利", "毛利率",
    "平均每车产值", "平均每单产值", "入场频次",
]
KPI_HEADERS = [
    "区域", "门店编码", "门店名称", "周期", "总达成率",
    "首保实施率-应保", "首保实施率-实保", "首保实施率",
    "二保实施率-应保", "二保实施率-实保", "二保实施率",
    "新保实施率-交付量", "新保实施率-投保量", "新保实施率",
    "续保实施率-应续保", "续保实施率-实续保", "去年同期交付未新保车辆数量", "续保实施率",
    "品情活动-入场车辆数", "品情活动-无漏做车辆数", "品情活动实施及时率",
    "投诉单量", "投诉-分钟响应量", "投诉-小时方案量", "投诉响应及时率",
    "咨询单量", "咨询-分钟响应量", "咨询-小时方案量", "咨询响应及时率",
    "综合响应及时率", "巡检得分",
]


def xlsx(headers, rows, sheet="数据"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(headers)
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def op_row(region, code, name, orders, vehicles, biz, output, labor, parts, other):
    return [region, code, name, orders, vehicles, biz, output, labor, parts, other,
            "0", "0", "0", "0%", "0", "0", "1"]


def kpi_row(region, code, name, *, first=(0, 0), second=(0, 0), new=(0, 0),
            renewal=(0, 0, 0), campaign=(0, 0), complaint=(0, 0, 0), consult=(0, 0, 0),
            period="2026年Q4", total="0%", inspect="0%"):
    def pct(n, d):
        return "0%" if not d else f"{round(100 * n / d)}%"

    f_den, f_num = first
    s_den, s_num = second
    n_den, n_num = new
    r_den, r_num, r_back = renewal
    c_den, c_num = campaign
    p_den, p_min, p_hour = complaint
    z_den, z_min, z_hour = consult
    r_base = r_den + r_back
    p_rate = "0%" if not p_den else f"{round(100 * (p_min + p_hour) / (2 * p_den))}%"
    z_rate = "0%" if not z_den else f"{round(100 * (z_min + z_hour) / (2 * z_den))}%"
    both = p_den + z_den
    resp = "0%" if not both else f"{round(100 * (p_min + p_hour + z_min + z_hour) / (2 * both))}%"
    return [
        region, code, name, period, total,
        str(f_den), str(f_num), pct(f_num, f_den),
        str(s_den), str(s_num), pct(s_num, s_den),
        str(n_den), str(n_num), pct(n_num, n_den),
        str(r_den), str(r_num), str(r_back), pct(r_num, r_base),
        str(c_den), str(c_num), pct(c_num, c_den),
        str(p_den), str(p_min), str(p_hour), p_rate,
        str(z_den), str(z_min), str(z_hour), z_rate,
        resp, inspect,
    ]


def national_blob():
    rows = [
        op_row("全国", "全国", "全国", "10", "8", "定期保养", "100.00", "40.00", "50.00", "10.00"),
        op_row("全国", "全国", "全国", "5", "5", "仅备件销售", "20.00", "0.00", "20.00", "0.00"),
        op_row("汇总", "", "", "15", "9", "", "120.00", "40.00", "70.00", "10.00"),
    ]
    return xlsx(OP_HEADERS, rows, "经营报表导出20261120")


def east_blob():
    rows = [
        op_row("华东区", "S1", "店一", "4", "3", "定期保养", "30.00", "10.00", "20.00", "0.00"),
        op_row("华东区", "S1", "店一", "2", "2", "仅备件销售", "10.00", "0.00", "10.00", "0.00"),
        op_row("汇总", "", "", "6", "4", "", "40.00", "10.00", "30.00", "0.00"),
    ]
    return xlsx(OP_HEADERS, rows, "经营报表导出20261120")


def kpi_blob():
    """华南 9/20=45%，华东 50/100=50%，全国 59/120。百分比平均不是 59/120。"""
    rows = [
        kpi_row("华南区", "A", "甲店", first=(10, 8), renewal=(10, 4, 10), campaign=(10, 9),
                complaint=(5, 5, 4), total="12.00%"),
        kpi_row("华南区", "B", "乙店", first=(10, 1)),
        kpi_row("华东区", "C", "丙店", first=(100, 50)),
        kpi_row("", "S-BLANK", "未分区店", first=(0, 0), new=(0, 0)),
        kpi_row("", "S-ZERO", "分母空率", new=(0, 0)),
    ]
    # 新保分母 0 时源表写成 100%，加权必须忽略这个百分数。
    rows[-1][KPI_HEADERS.index("新保实施率")] = "100%"
    return xlsx(KPI_HEADERS, rows, "门店考核指标报表导出20261120")


def test_operation_uses_summary_and_does_not_add_vehicles():
    from reportdb.weekly_parse import parse_operation_xlsx

    got = parse_operation_xlsx(national_blob())
    assert got["grain"] == "national"
    assert got["scope"] == "全国"
    assert got["summary"]["orders"] == Decimal("15")
    assert got["summary"]["vehicles"] == Decimal("9")
    assert got["summary"]["output"] == Decimal("120.00")
    assert got["detail_sums"]["vehicles"] == Decimal("13")
    assert got["detail_sums"]["orders"] == Decimal("15")
    assert got["summary"]["labor"] + got["summary"]["parts"] + got["summary"]["other"] == got["summary"]["output"]
    assert "仅备件销售" in got["business_types"]
    assert "regions" not in got


def test_region_operation_normalizes_name():
    from reportdb.weekly_parse import parse_operation_xlsx

    got = parse_operation_xlsx(east_blob())
    assert got["grain"] == "region"
    assert got["scope"] == "华东"
    assert got["summary"]["orders"] == Decimal("6")
    assert got["summary"]["vehicles"] == Decimal("4")
    assert got["detail_sums"]["vehicles"] == Decimal("5")


def test_mixed_region_operation_is_rejected():
    from reportdb.weekly_parse import WeeklyParseError, parse_operation_xlsx

    rows = [
        op_row("华东区", "S1", "店一", "1", "1", "定期保养", "1.00", "1.00", "0.00", "0.00"),
        op_row("华南区", "S2", "店二", "1", "1", "定期保养", "1.00", "1.00", "0.00", "0.00"),
        op_row("汇总", "", "", "2", "2", "", "2.00", "2.00", "0.00", "0.00"),
    ]
    with pytest.raises(WeeklyParseError, match="多个区域"):
        parse_operation_xlsx(xlsx(OP_HEADERS, rows))


def test_kpi_weighted_rate_blank_region_and_zero_denominator():
    from reportdb.weekly_parse import parse_kpi_xlsx

    got = parse_kpi_xlsx(kpi_blob())
    assert got["row_count"] == 5
    assert got["unique_stores"] == 5
    assert got["duplicate_groups"] == 0
    assert got["blank_region_rows"] == 2
    assert got["rollup_ok"] is True
    assert got["periods"] == ["2026年Q4"]
    first = got["national"]["first"]
    assert first["num"] == Decimal("59")
    assert first["den"] == Decimal("120")
    assert first["rate"] == Decimal("59") / Decimal("120")
    # 三店百分比 80、10、50 的平均是 46.666…，加权不是这个数。
    assert first["rate"] != (Decimal("80") + Decimal("10") + Decimal("50")) / Decimal("300")
    assert got["regions"]["华南"]["first"]["rate"] == Decimal("9") / Decimal("20")
    assert got["regions"]["华东"]["first"]["num"] == Decimal("50")
    assert got["regions"]["无区域"]["first"]["den"] == Decimal("0")
    assert got["regions"]["无区域"]["first"]["rate"] is None
    renewal = got["national"]["renewal"]
    assert renewal["den"] == Decimal("20")
    assert renewal["num"] == Decimal("4")
    assert got["national"]["new"]["den"] == Decimal("0")
    assert got["national"]["new"]["rate"] is None
    assert "总达成率" in got["unverified_columns"]
    assert "巡检得分" in got["unverified_columns"]


def test_kpi_duplicate_store_period_refuses_rollup():
    from reportdb.weekly_parse import parse_kpi_xlsx

    rows = [
        kpi_row("华南区", "A", "甲", first=(1, 1)),
        kpi_row("华南区", "A", "甲重复", first=(4, 1)),
    ]
    got = parse_kpi_xlsx(xlsx(KPI_HEADERS, rows))
    assert got["duplicate_groups"] == 1
    assert got["rollup_ok"] is False
    assert got["national"] == {}


def test_change_missing_and_zero_base():
    from reportdb.weekly_parse import change

    assert change(None, Decimal("3"))["status"] == "missing"
    assert change(Decimal("3"), None)["rate"] is None
    zero = change(Decimal("5"), Decimal("0"))
    assert zero["status"] == "zero_base"
    assert zero["rate"] is None
    assert zero["amount"] == Decimal("5")
    ok = change(Decimal("15"), Decimal("10"))
    assert ok["status"] == "ok"
    assert ok["rate"] == Decimal("0.5")


def test_quarter_and_year_windows():
    from reportdb.weekly_parse import align_progress, quarter_compare, year_compare

    q1 = quarter_compare(date(2026, 2, 15))
    assert q1["current"] == {"start": date(2026, 1, 1), "end": date(2026, 2, 15)}
    assert q1["base"] == {"start": date(2025, 10, 1), "end": date(2025, 11, 15)}
    assert q1["label_base"] == "2025Q4"
    assert "实现默认" in q1["progress_note"]
    q2 = quarter_compare(date(2026, 5, 20))
    assert q2["current"] == {"start": date(2026, 4, 1), "end": date(2026, 5, 20)}
    assert q2["base"] == {"start": date(2026, 1, 1), "end": date(2026, 2, 19)}
    assert q2["label_base"] == "2026Q1"
    done = quarter_compare(date(2026, 6, 30))
    assert done["complete"] is True and done["capped"] is False
    assert done["current"] == {"start": date(2026, 4, 1), "end": date(2026, 6, 30)}
    assert done["base"] == {"start": date(2026, 1, 1), "end": date(2026, 3, 31)}
    assert align_progress(date(2026, 1, 1), date(2026, 3, 31), 90) == (date(2026, 3, 31), True)
    year = year_compare(date(2026, 11, 20))
    assert year["current"] == {"start": date(2026, 1, 1), "end": date(2026, 11, 20)}
    assert year["base"] == {"start": date(2025, 1, 1), "end": date(2025, 11, 20)}
    leap = year_compare(date(2024, 2, 29))
    assert leap["base"]["end"] == date(2023, 2, 28)
    assert "2月28日" in leap["progress_note"]
    march = year_compare(date(2024, 3, 1))
    assert march["base"]["end"] == date(2023, 3, 1)


def _op_meta(**over):
    meta = dict(week_start="2026-11-16", span="quarter", period_start="2026-10-01", period_end="2026-11-20",
                exported_at="2026-11-20 14:25:35", timezone="+08:00", filename="op.xlsx", actor="test")
    meta.update(over)
    return meta


def test_week_shaped_and_november_sample_are_rejected(conn):
    from reportdb.weekly_report import WeeklyImportError, import_operation

    for span, start, end in (
        ("quarter", "2026-11-01", "2026-11-19"),
        ("year", "2026-11-01", "2026-11-19"),
        ("quarter", "2026-11-16", "2026-11-19"),
        ("quarter_base", "2026-11-09", "2026-11-12"),
    ):
        with pytest.raises(WeeklyImportError, match="都不能当作"):
            extra = {"as_of": "2026-11-20"} if span.endswith("_base") else {}
            import_operation(conn, national_blob(), **_op_meta(span=span, period_start=start, period_end=end, **extra))
    assert conn.execute("SELECT COUNT(*) FROM weekly_imports").fetchone()[0] == 0


def test_snapshot_idempotent_conflict_and_revision(conn):
    from reportdb.weekly_report import SnapshotConflict, import_operation

    first = import_operation(conn, national_blob(), **_op_meta())
    again = import_operation(conn, national_blob(), **_op_meta())
    assert again["idempotent"] is True
    assert again["id"] == first["id"]
    assert conn.execute("SELECT COUNT(*) FROM weekly_imports").fetchone()[0] == 1
    other = national_blob() + b" "
    # 尾部空字节会改变 xlsx，openpyxl 仍可读；若读失败则改一个明细数字。
    try:
        from reportdb.weekly_parse import parse_operation_xlsx
        parse_operation_xlsx(other)
        changed = other
    except Exception:
        changed = xlsx(OP_HEADERS, [
            op_row("全国", "全国", "全国", "11", "8", "定期保养", "100.00", "40.00", "50.00", "10.00"),
            op_row("全国", "全国", "全国", "5", "5", "仅备件销售", "20.00", "0.00", "20.00", "0.00"),
            op_row("汇总", "", "", "16", "9", "", "120.00", "40.00", "70.00", "10.00"),
        ])
    with pytest.raises(SnapshotConflict):
        import_operation(conn, changed, **_op_meta())
    saved = import_operation(conn, changed, **_op_meta(), allow_revision=True)
    assert saved["revision"] == 2
    rows = conn.execute("SELECT status, revision FROM weekly_imports ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("superseded", 1), ("active", 2)]


def test_region_gap_is_kept(conn):
    from reportdb.weekly_report import build_weekly_page, import_operation

    import_operation(conn, national_blob(), **_op_meta())
    import_operation(conn, east_blob(), **_op_meta(filename="east.xlsx"))
    page = build_weekly_page(conn, "2026-11-16", today=date(2026, 11, 20))
    op = page["operation"]["quarter"]
    assert page["operation"]["imported"] is True
    assert op["current"]["start"] == "2026-10-01" and op["current"]["end"] == "2026-11-20"
    assert op["base"]["start"] == "2026-07-01" and op["base"]["end"] == "2026-08-20"
    assert "实现默认" in op["progress_note"]
    by = {row["region"]: row for row in op["rows"]}
    assert by["全国"]["orders"]["current"] == "15"
    assert by["华东"]["orders"]["current"] == "6"
    assert by["华东"]["orders"]["current"] != "15"
    assert by["华南"]["orders"]["current"] == "未导入"
    assert by["全国"]["vehicles"]["current"] == "9"
    assert "不摊分" in op["reconcile"]["warning"]
    assert "华北" in op["reconcile"]["missing_regions"]
    assert op["reconcile"]["gaps"]["orders"] == "9"
    assert op["reconcile"]["vehicles_compared"] is False
    dumped = json.dumps(op, ensure_ascii=False)
    assert "万元" not in dumped
    assert "不含税" not in dumped
    assert "未注明" in dumped


def test_blank_role_week_row_is_not_quarter_data(conn):
    from reportdb.weekly_report import build_weekly_page

    conn.execute(
        """INSERT INTO weekly_imports(
               kind, week_start, scope, compare_role, period_start, period_end, exported_at, sha256,
               revision, status, created_at, quality_json, summary_json, payload)
           VALUES ('operation','2026-11-16','全国','','2026-11-16','2026-11-19',
                   '2026-11-20 14:25:35 +08:00','abc',1,'active','2026-11-20',
                   '[]','{"summary":{"orders":"6246","vehicles":"1","output":"1.00","labor":"1.00","parts":"0.00","other":"0.00"}}',
                   X'00')"""
    )
    conn.commit()
    page = build_weekly_page(conn, "2026-11-16", today=date(2026, 11, 20))
    assert page["operation"]["quarter"]["rows"][0]["orders"]["current"] == "未导入"
    assert "6246" not in json.dumps(page["operation"], ensure_ascii=False)


def test_quarter_base_zero_and_year_missing(conn):
    from reportdb.weekly_report import build_weekly_page, import_operation

    import_operation(conn, national_blob(), **_op_meta())
    zero = xlsx(OP_HEADERS, [
        op_row("全国", "全国", "全国", "0", "0", "定期保养", "0.00", "0.00", "0.00", "0.00"),
        op_row("汇总", "", "", "0", "0", "", "0.00", "0.00", "0.00", "0.00"),
    ])
    import_operation(conn, zero, **_op_meta(
        span="quarter_base", period_start="2026-07-01", period_end="2026-08-20",
        as_of="2026-11-20", filename="base.xlsx"))
    page = build_weekly_page(conn, "2026-11-16", today=date(2026, 11, 20))
    national = next(r for r in page["operation"]["quarter"]["rows"] if r["region"] == "全国")
    assert national["orders"]["current"] == "15"
    assert national["orders"]["base"] == "0"
    assert national["orders"]["change"] == "不可比"
    year = next(r for r in page["operation"]["year"]["rows"] if r["region"] == "全国")
    assert year["orders"]["current"] == "未导入"
    assert year["orders"]["change"] == "未导入"


def test_blank_region_not_taken_from_store_table(conn):
    from reportdb.weekly_report import build_weekly_page, import_kpi

    conn.execute(
        "INSERT INTO stores(code,name,region,seq,active,source) VALUES ('S-BLANK','未分区店','华南',999,1,'test')"
    )
    conn.commit()
    import_kpi(conn, kpi_blob(), week_start="2026-11-16", as_of="2026-11-20",
               exported_at="2026-11-20 09:00:00", filename="kpi.xlsx", actor="test")
    page = build_weekly_page(conn, "2026-11-16", today=date(2026, 11, 20))
    cells = {c["region"]: c for c in page["kpi"]["metrics"][0]["cells"]}
    assert page["kpi"]["metrics"][0]["key"] == "first"
    assert cells["无区域"]["rate_text"] == "不可比"
    assert cells["华南"]["sub"] == "9/20"
    assert cells["华南"]["below"] is True
    assert cells["华东"]["below"] is False
    assert page["kpi"]["quarter_start"] == "2026-10-01"
    assert page["kpi"]["as_of"] == "2026-11-20"
    assert "2026-11-20 09:00:00" in page["kpi"]["exported_at"]
    assert page["commentary"]["华南"] == ""


def test_commentary_starts_empty_and_persists(conn):
    from reportdb.weekly_report import build_weekly_page, save_commentary

    save_commentary(conn, "2026-11-16", "华南", "   ", "sup")
    assert build_weekly_page(conn, "2026-11-16", today=date(2026, 11, 20))["commentary"]["华南"] == ""
    save_commentary(conn, "2026-11-16", "华南", "到店减少", "sup")
    assert build_weekly_page(conn, "2026-11-16", today=date(2026, 11, 20))["commentary"]["华南"] == "到店减少"
    assert "到店减少" in conn.execute("SELECT detail FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_current_week_without_import_is_blank(conn):
    from reportdb.weekly_report import build_weekly_page

    page = build_weekly_page(conn, None, today=date(2026, 11, 20))
    assert page["week_start"] == "2026-11-16"
    assert page["operation"]["imported"] is False
    assert page["kpi"]["imported"] is False
    text = json.dumps(page, ensure_ascii=False)
    assert "6402712.72" not in text
    assert "6246" not in text
    assert page["operation"]["quarter"]["current"] is None
    assert page["operation"]["year"]["current"] is None


def test_collector_is_disabled_and_schedule_untouched():
    from reportdb.weekly_report import collect_weekly_sources

    with pytest.raises(NotImplementedError, match="SHARED_DMS_BROWSER"):
        collect_weekly_sources()
    root = Path(__file__).resolve().parent.parent
    settings = json.loads((root / "config" / "crawl_settings.json").read_text())
    assert settings["schedule"] == "08:30"
    pipeline = (root / "scripts" / "run_pipeline.py").read_text()
    assert "weekly_import" not in pipeline
    assert "collect_weekly_sources" not in pipeline


def _login(client, username, password):
    import re
    page = client.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    got = client.post("/login", data={"username": username, "password": password, "csrf": token, "next": "/"},
                      follow_redirects=False)
    assert got.status_code == 303
    return token


def test_weekly_page_nav_and_roles(conn, tmp_path):
    service.create_user(conn, "boss", "boss-password-1", role="admin")
    service.create_user(conn, "viewer1", "viewer-password-1", role="viewer")
    service.create_user(conn, "sup1", "supervisor-password-1", role="supervisor")
    conn.commit()
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    from reportdb.webapp import create_app

    viewer = TestClient(create_app(db))
    _login(viewer, "viewer1", "viewer-password-1")
    page = viewer.get("/reports/weekly")
    assert page.status_code == 200
    assert "猛士周报" in page.text
    assert 'href="/reports/weekly"' in page.text
    assert "收款工单数" in page.text
    assert "未导入" in page.text
    assert "不是严格来厂台次" in page.text
    assert "万元" not in page.text
    assert "不含税" not in page.text
    assert "未注明" in page.text
    assert "6402712" not in page.text
    assert viewer.get("/reports/weekly/import").status_code == 403
    admin = TestClient(create_app(db))
    _login(admin, "boss", "boss-password-1")
    assert "猛士周报" in admin.get("/").text or admin.get("/reports/weekly").status_code == 200
    form = admin.get("/reports/weekly/import")
    assert form.status_code == 200
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', form.text).group(1)
    posted = admin.post(
        "/reports/weekly/import",
        data={
            "csrf": csrf, "kind": "operation", "week": "2026-11-16", "span": "quarter",
            "period_start": "2026-10-01", "period_end": "2026-11-20",
            "exported_at": "2026-11-20 14:25:35", "timezone": "+08:00",
            "stat_type": "收款", "report_type": "日报",
        },
        files={"file": ("national.xlsx", national_blob(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    shown = admin.get("/reports/weekly", params={"week": "2026-11-16"})
    assert "经营数据：季度环比 / 年度同比" in shown.text
    assert "2026-10-01" in shown.text and "2026-08-20" in shown.text
    assert "实现默认" in shown.text
    assert "上周同进度" not in shown.text and "364" not in shown.text
    assert ">15<" in shown.text or "15" in shown.text
    assert "未填写" in shown.text
    assert "weekly-topbar-flat" in shown.text
    shot_op = shown.text.split('id="shot-operation"', 1)[1].split('id="shot-kpi"', 1)[0]
    shot_kpi = shown.text.split('id="shot-kpi"', 1)[1].split('id="weekly-edit"', 1)[0]
    assert 'class="bar-chart"' in shot_op
    assert "<textarea" not in shot_kpi
    assert "<details class=\"weekly-audit\">" in shot_op
    assert "<details class=\"weekly-audit\" open" not in shot_op
    assert "不摊分" in shot_op
    link = re.search(r'href="(/reports/weekly/imports/\d+/source.xlsx)"', shown.text)
    assert link
    assert admin.get(link.group(1)).status_code == 200
    assert viewer.get(link.group(1)).status_code == 403
    sup = TestClient(create_app(db))
    _login(sup, "sup1", "supervisor-password-1")
    sup_page = sup.get("/reports/weekly", params={"week": "2026-11-16"})
    csrf2 = re.search(r'name="csrf" value="([^"]+)"', sup_page.text).group(1)
    saved = sup.post("/reports/weekly/commentary", data={
        "csrf": csrf2, "week": "2026-11-16", "region": "华东", "body": "",
    }, follow_redirects=False)
    assert saved.status_code == 303
    again = sup.get("/reports/weekly", params={"week": "2026-11-16"}).text
    assert "出口车挂店导致" not in again


def test_cli_import(conn, tmp_path, monkeypatch):
    blob = tmp_path / "east.xlsx"
    blob.write_bytes(east_blob())
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setenv("REPORTDB_PATH", str(db))
    from reportdb.weekly_import import main

    code = main([
        "operation", "--file", str(blob), "--week", "2026-11-16", "--span", "quarter",
        "--period-start", "2026-10-01", "--period-end", "2026-11-20",
        "--exported-at", "2026-11-20 14:25:35", "--actor", "cli",
    ])
    assert code == 0
    row = conn.execute("SELECT scope, status FROM weekly_imports").fetchone()
    assert row["scope"] == "华东" and row["status"] == "active"
    again = main([
        "operation", "--file", str(blob), "--week", "2026-11-16", "--span", "quarter",
        "--period-start", "2026-10-01", "--period-end", "2026-11-20",
        "--exported-at", "2026-11-20 14:25:35",
    ])
    assert again == 0
    assert conn.execute("SELECT COUNT(*) FROM weekly_imports").fetchone()[0] == 1


def _sample_dir():
    env = os.environ.get("MHERO_WEEKLY_SAMPLE_DIR", "").strip()
    candidates = [Path(env)] if env else []
    candidates.append(Path("/home/ubuntu/.cursor/projects/agent-repos-mhero-district-form/uploads"))
    for folder in candidates:
        if folder.is_dir() and any(folder.glob("*.xlsx")):
            return folder
    return None


def test_real_samples_parse_and_do_not_bind_as_current_week(conn):
    folder = _sample_dir()
    if folder is None:
        pytest.skip("本机没有经营/KPI 样本")
    from reportdb.weekly_parse import WeeklyParseError, parse_kpi_xlsx, parse_operation_xlsx
    from reportdb.weekly_report import WeeklyImportError, build_weekly_page, import_operation

    national = east = kpi = None
    national_bytes = None
    for path in folder.glob("*.xlsx"):
        blob = path.read_bytes()
        try:
            parsed = parse_operation_xlsx(blob)
        except WeeklyParseError:
            parsed = None
        if parsed and parsed["summary"]["orders"] == Decimal("6246"):
            national, national_bytes = parsed, blob
        elif parsed and parsed["summary"]["orders"] == Decimal("728"):
            east = parsed
        else:
            try:
                book = parse_kpi_xlsx(blob)
            except WeeklyParseError:
                book = None
            if book and book["row_count"] == 774:
                kpi = book
    assert national and east and kpi and national_bytes
    assert national["scope"] == "全国"
    assert national["summary"]["vehicles"] == Decimal("3135")
    assert national["summary"]["output"] == Decimal("6402712.72")
    assert national["detail_sums"]["vehicles"] != national["summary"]["vehicles"]
    assert national["summary"]["labor"] + national["summary"]["parts"] + national["summary"]["other"] == national["summary"]["output"]
    assert east["scope"] == "华东"
    assert east["summary"]["vehicles"] == Decimal("370")
    assert east["summary"]["output"] == Decimal("631854.40")
    assert kpi["unique_stores"] == 774
    assert kpi["duplicate_groups"] == 0
    assert kpi["blank_region_rows"] == 596
    assert kpi["periods"] == ["2026年Q4"]
    assert kpi["rollup_ok"] is True
    assert kpi["national"]["first"]["num"] == Decimal("980")
    assert kpi["national"]["first"]["den"] == Decimal("2488")
    assert kpi["regions"]["无区域"]["first"]["den"] == Decimal("1476")
    for span in ("quarter", "year"):
        with pytest.raises(WeeklyImportError):
            import_operation(
                conn, national_bytes, week_start="2026-11-16", span=span, period_start="2026-11-01",
                period_end="2026-11-19", exported_at="2026-11-20 14:25:35", filename="sample.xlsx", actor="test",
            )
    page = build_weekly_page(conn, None, today=date(2026, 11, 20))
    assert page["operation"]["imported"] is False
    assert "6402712.72" not in json.dumps(page, ensure_ascii=False)
