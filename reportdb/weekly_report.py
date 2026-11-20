"""猛士周报快照：元数据校验、不可静默覆盖、页面模型。不采集 DMS。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .auth import service
from . import calc, config
from .db import get_meta
from .tables import PCT0, fmt_value
from .weekly_parse import (
    change,
    norm_region,
    month_window,
    parse_operation_xlsx,
    quarter_compare,
    year_compare,
    WeeklyParseError,
)

OPERATION_SPANS = {"quarter", "quarter_base", "year", "year_base"}

DMS_BROWSER_DOC = "https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md"
EXPORTED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\s*([+-]\d{2}:\d{2}))?$")
ZONE_RE = re.compile(r"^[+-]\d{2}:\d{2}$")
MONEY_FIELDS = ("output", "labor", "parts", "other")
COUNT_FIELDS = ("orders", "vehicles")


class WeeklyImportError(ValueError):
    pass


class SnapshotConflict(Exception):
    pass


def collect_weekly_sources(*, as_of: str | None = None, regions: list[str] | None = None, live: bool = False, **_kwargs):
    """计划周五采集。live=True 也不在这里打开浏览器，现场用 scripts/weekly_operation_crawl.py --live。"""
    if live:
        raise RuntimeError(
            "不要从这里连接 DMS。现场使用 scripts/weekly_operation_crawl.py --live，"
            f"并遵守 {DMS_BROWSER_DOC} 的共用锁。不能和每天 08:30 同时导出。"
        )
    from .weekly_crawl import describe_plan
    from datetime import date as _date
    day = _date.fromisoformat(as_of) if as_of else _date.today()
    return describe_plan(as_of=day, regions=list(regions or []))


def import_operation(conn, blob: bytes, *, week_start: str, period_start: str, period_end: str,
                     exported_at: str, timezone: str = "+08:00", stat_type: str = "收款",
                     report_type: str = "日报", scope: str | None = None, filename: str = "",
                     actor: str = "", allow_revision: bool = False, span: str = "",
                     as_of: str | None = None) -> dict:
    if stat_type != "收款":
        raise WeeklyImportError("经营周报只接受统计类型「收款」，不接受结算或其他类型")
    if report_type != "日报":
        raise WeeklyImportError("经营周报只接受报表类型「日报」")
    if span != "month" and span not in OPERATION_SPANS:
        raise WeeklyImportError("经营比较必须指定 quarter、quarter_base、year、year_base 或 month。")
    week = _monday(week_start)
    start, end = _date(period_start), _date(period_end)
    if span == "month":
        if not as_of:
            raise WeeklyImportError("月度原件必须填写本次更新的截止日 as_of")
        cutoff = _date(as_of)
        if not (week <= cutoff <= week + timedelta(days=6)):
            raise WeeklyImportError("截止日必须落在所选更新周内。周次只表示这次归档，不是比较区间。")
        try:
            window = month_window(cutoff, period_start[:7])
        except WeeklyParseError as exc:
            raise WeeklyImportError(str(exc)) from exc
        if start != window["start"] or end != window["end"]:
            raise WeeklyImportError(
                f"月度区间 {start.isoformat()} 至 {end.isoformat()} 不符合 {window['month']}。"
                f"应为 {window['start'].isoformat()} 至 {window['end'].isoformat()}。"
                "不能把季度或年度文件拆成月，也不能把月中文件当成已结束月。"
            )
        role = f"month:{window['month']}"
        parsed = parse_operation_xlsx(blob)
        return _save_operation(conn, blob, parsed, role=role, week=week, start=start, end=end,
                               scope=scope, filename=filename, actor=actor, allow_revision=allow_revision,
                               stat_type=stat_type, report_type=report_type, exported_at=exported_at, timezone=timezone)
    compare = quarter_compare if span.startswith("quarter") else year_compare
    if span in {"quarter", "year"}:
        if not (week <= end <= week + timedelta(days=6)):
            raise WeeklyImportError("累计截止日必须落在所选更新周内。周次只表示这次归档，不是比较区间。")
        windows = compare(end)
        expect = windows["current"]
    else:
        if not as_of:
            raise WeeklyImportError("对比期必须填写本次更新的截止日 as_of，用来核对同进度窗口")
        cutoff = _date(as_of)
        if not (week <= cutoff <= week + timedelta(days=6)):
            raise WeeklyImportError("截止日必须落在所选更新周内。周次只表示这次归档，不是比较区间。")
        windows = compare(cutoff)
        expect = windows["base"]
    if start != expect["start"] or end != expect["end"]:
        raise WeeklyImportError(
            f"统计区间 {start.isoformat()} 至 {end.isoformat()} 不符合 {span}。"
            f"按截止日应为 {expect['start'].isoformat()} 至 {expect['end'].isoformat()}。"
            "周区间、上周同进度、减 364 天，或 11 月 1 日至 19 日，都不能当作季度环比或年度同比。"
        )
    parsed = parse_operation_xlsx(blob)
    return _save_operation(conn, blob, parsed, role=span, week=week, start=start, end=end,
                           scope=scope, filename=filename, actor=actor, allow_revision=allow_revision,
                           stat_type=stat_type, report_type=report_type, exported_at=exported_at, timezone=timezone)


def _save_operation(conn, blob, parsed, *, role, week, start, end, scope, filename, actor,
                    allow_revision, stat_type, report_type, exported_at, timezone) -> dict:
    file_scope = parsed["scope"]
    if scope:
        asked = "全国" if norm_region(scope) == "全国" else norm_region(scope)
        if asked != file_scope:
            raise WeeklyImportError(f"填写的范围「{scope}」与文件中的「{file_scope}」不一致")
    summary = {
        "grain": parsed["grain"],
        "scope": file_scope,
        "summary": {key: _s(value) for key, value in parsed["summary"].items()},
        "detail_rows": parsed["detail_rows"],
        "business_types": parsed["business_types"],
        "detail_sums": {key: _s(value) for key, value in parsed["detail_sums"].items()},
        "sheet_name": parsed["sheet_name"],
        "metric_source": "dms_service_operation_receipt",
        "field_sources": {
            "orders": "收款工单数，取该文件汇总行，不把业务类型明细相加",
            "vehicles": "车辆数，取该文件汇总行的去重值，不跨业务类型或区域相加",
            "output": "总产值，取该文件汇总行，源数据单位元，页面按千元展示",
        },
    }
    return _save(conn, blob, {
        "kind": "operation",
        "compare_role": role,
        "week_start": week.isoformat(),
        "scope": file_scope,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "quarter_start": "",
        "year": None,
        "quarter": None,
        "stat_type": stat_type,
        "report_type": report_type,
        "exported_at": _exported(exported_at, timezone),
        "source_filename": Path(filename).name,
        "sheet_name": parsed["sheet_name"],
        "quality": parsed["quality"],
        "summary": summary,
        "actor": actor or "",
        "allow_revision": allow_revision,
    })


INDICATOR_PERIOD_NOTE = (
    "服务考核指标来自 reportdb 总表，接口是 calc.compute，结果表是 report_results（table_key=summary）。"
    "首保、二保、新保、续保用的是每日流水线已经入库的明细；招揽和常备件用的是同一快照里的任务和库存。"
    "现有公式按这一整份快照汇总，没有再按季度起日至截止日截开，所以这不是另一套季度累计，也不是 DMS 服务管理主表。"
    "收款工单数、总产值、车辆数不在这里，它们只来自服务经营报表收款导出的汇总行。"
)


def capture_own_indicators(conn, week_start: str, *, allow_revision: bool = False) -> dict | None:
    """把该周已有的总表自算结果冻成周报指标快照。没有快照则什么都不写。"""
    week = _monday(week_start)
    week_end = (week + timedelta(days=6)).isoformat()
    existing = conn.execute(
        "SELECT * FROM weekly_imports WHERE kind='indicators' AND week_start=? AND scope='全国' AND compare_role='' AND status='active'",
        (week.isoformat(),),
    ).fetchone()
    if existing is not None and not allow_revision:
        return json.loads(existing["summary_json"])
    mode = config.region_mode()
    src = _latest_summary_row(conn, week.isoformat(), week_end, mode)
    if src is None:
        return None
    threshold = float(get_meta(conn, "spare_threshold", "0.8") or 0.8)
    summary = _indicators_from_sheet(json.loads(src["payload"]), src, mode, threshold)
    blob = json.dumps(summary, ensure_ascii=False).encode("utf-8")
    _save(conn, blob, {
        "kind": "indicators",
        "compare_role": "",
        "week_start": week.isoformat(),
        "scope": "全国",
        "period_start": src["snapshot_date"],
        "period_end": src["snapshot_date"],
        "quarter_start": "",
        "year": None,
        "quarter": None,
        "stat_type": "",
        "report_type": "reportdb总表",
        "exported_at": src["computed_at"],
        "source_filename": "",
        "sheet_name": "summary",
        "quality": [INDICATOR_PERIOD_NOTE],
        "summary": summary,
        "actor": "reportdb",
        "allow_revision": allow_revision or existing is not None,
    })
    return summary


def _latest_summary_row(conn, start: str, end: str, mode: str):
    sql = (
        "SELECT s.snapshot_date, s.ingested_at, r.computed_at, r.payload FROM snapshots s "
        "JOIN report_results r ON r.snapshot_date=s.snapshot_date AND r.table_key='summary' AND r.region_mode=? "
        "WHERE s.snapshot_date>=? AND s.snapshot_date<=? ORDER BY s.snapshot_date DESC LIMIT 1"
    )
    row = conn.execute(sql, (mode, start, end)).fetchone()
    if row is not None:
        return row
    snap = conn.execute(
        "SELECT snapshot_date FROM snapshots WHERE snapshot_date>=? AND snapshot_date<=? ORDER BY snapshot_date DESC LIMIT 1",
        (start, end),
    ).fetchone()
    if snap is None:
        return None
    calc.compute_and_store(conn, snap["snapshot_date"], mode)
    return conn.execute(sql, (mode, start, end)).fetchone()


def save_commentary(conn, week_start: str, region: str, body: str, actor: str) -> None:
    week = _monday(week_start).isoformat()
    text = (body or "").strip()
    region = (region or "").strip()
    if not region:
        raise WeeklyImportError("区域为空")
    with conn:
        if text == "":
            conn.execute(
                "DELETE FROM weekly_commentary WHERE week_start=? AND region=? AND metric=''",
                (week, region),
            )
        else:
            conn.execute(
                """INSERT INTO weekly_commentary(week_start,region,metric,body,updated_at,updated_by)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(week_start, region, metric) DO UPDATE SET
                     body=excluded.body, updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
                (week, region, "", text, _now(), actor or ""),
            )
    service.audit(conn, actor or "", "weekly.commentary", f"{week} {region} {text}")


def build_weekly_page(conn, week: str | None = None, *, today: date | None = None) -> dict:
    today = today or date.today()
    week_start = today - timedelta(days=today.weekday()) if not week else _monday(week)
    week_end = week_start + timedelta(days=6)
    capture_own_indicators(conn, week_start.isoformat())
    known = [row[0] for row in conn.execute("SELECT name FROM regions ORDER BY sort_order")]
    active = {}
    for row in conn.execute("SELECT * FROM weekly_imports WHERE status='active'"):
        role = row["compare_role"] if "compare_role" in row.keys() else ""
        active[(row["kind"], row["week_start"], row["scope"], role or "")] = row
    notes = {name: "" for name in ["全国", *known, "无区域"]}
    for row in conn.execute("SELECT region, body FROM weekly_commentary WHERE week_start=? AND metric=''", (week_start.isoformat(),)):
        notes[row["region"]] = row["body"] or ""
    extra = []
    for kind, start, scope, _role in active:
        if kind == "operation" and start == week_start.isoformat() and scope not in known and scope != "全国":
            extra.append(scope)
    scopes = ["全国", *known, *sorted(set(extra))]
    operation = _operation_section(active, week_start.isoformat(), scopes, known)
    operation["months"] = _month_section(active, week_start, week_end, today, scopes)
    indicators = _indicators_section(active.get(("indicators", week_start.isoformat(), "全国", "")))
    weeks = {week_start.isoformat()}
    for row in conn.execute("SELECT DISTINCT week_start FROM weekly_imports"):
        weeks.add(row[0])
    history = []
    for row in conn.execute(
        """SELECT id, kind, week_start, scope, period_start, period_end, exported_at, revision, status,
                  source_filename, sha256, created_at, created_by FROM weekly_imports
           ORDER BY week_start DESC, id DESC"""
    ):
        history.append({key: row[key] for key in row.keys()})
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "is_current_week": week_start == today - timedelta(days=today.weekday()),
        "weeks": [{"start": item, "label": _week_label(item, today)} for item in sorted(weeks, reverse=True)],
        "operation": operation,
        "indicators": indicators,
        "commentary": notes,
        "history": history,
    }


def _snapshot_as_of(active, week_start: date, week_end: date, today: date) -> date:
    found = []
    for key, row in active.items():
        if key[0] != "operation" or key[1] != week_start.isoformat():
            continue
        end = _date(row["period_end"])
        if week_start <= end <= week_end:
            found.append(end)
    if found:
        return max(found)
    if week_start <= today <= week_end:
        return today
    return week_end


def _month_section(active, week_start: date, week_end: date, today: date, scopes: list[str]) -> dict:
    as_of = _snapshot_as_of(active, week_start, week_end, today)
    columns = []
    for mon in range(1, as_of.month + 1):
        window = month_window(as_of, f"{as_of.year:04d}-{mon:02d}")
        columns.append({
            "key": window["month"],
            "label": f"{mon}月",
            "start": window["start"].isoformat(),
            "end": window["end"].isoformat(),
            "closed": window["closed"],
        })
    rows = []
    for scope in scopes:
        cells = {}
        for column in columns:
            row = active.get(("operation", week_start.isoformat(), scope, f"month:{column['key']}"))
            values = None
            if row is not None and row["period_start"] == column["start"] and row["period_end"] == column["end"]:
                values = _op_values(row)
            cells[column["key"]] = {
                "orders": _fmt(None if values is None else values["orders"]),
                "output": _fmt(None if values is None else values["output"], money=True),
                "vehicles": _fmt(None if values is None else values["vehicles"]),
                "imported": values is not None,
            }
        rows.append({"region": scope, "cells": cells})
    return {
        "as_of": as_of.isoformat(),
        "columns": columns,
        "rows": rows,
        "note": "各月分别取该月收款日报的汇总行。全国是独立汇总，不由各区相加。车辆、工单和产值都不跨月相加。没有该月原件就是未导入，不用季度或年度拆分。",
    }


def _demo_months() -> dict:
    areas = ["全国", "华北", "华东", "华南", "华中1", "西南", "西北", "华中2"]
    columns = []
    for mon in range(1, 12):
        window = month_window(date(2026, 11, 20), f"2026-{mon:02d}")
        columns.append({"key": window["month"], "label": f"{mon}月", "start": window["start"].isoformat(),
                        "end": window["end"].isoformat(), "closed": window["closed"]})
    rows = []
    for index, name in enumerate(areas):
        cells = {}
        for mon in range(1, 12):
            orders = 80 + index * 3 + mon
            vehicles = 20 + index + mon
            yuan = Decimal(18000 + index * 500 + mon * 100)
            cells[f"2026-{mon:02d}"] = {
                "orders": str(orders),
                "output": _fmt(yuan, money=True),
                "vehicles": str(vehicles),
                "imported": True,
            }
        rows.append({"region": name, "cells": cells})
    return {
        "as_of": "2026-11-20",
        "columns": columns,
        "rows": rows,
        "note": "示例数据，不是从季度或年度拆出来的。",
    }


def _operation_section(active, week: str, scopes: list[str], known: list[str]) -> dict:
    quarter_as = _role_as_of(active, week, "quarter")
    year_as = _role_as_of(active, week, "year")
    shared = quarter_as or year_as
    quarter = _compare_panel(active, week, scopes, known, "quarter", quarter_compare(quarter_as or shared) if (quarter_as or shared) else None)
    year = _compare_panel(active, week, scopes, known, "year", year_compare(year_as or shared) if (year_as or shared) else None)
    if quarter_as and year_as and quarter_as != year_as:
        quarter["quality"].append(f"季度截止日 {quarter_as.isoformat()} 与年度截止日 {year_as.isoformat()} 不一致，两张卡各自使用自己的截止日。")
        year["quality"].append(f"年度截止日 {year_as.isoformat()} 与季度截止日 {quarter_as.isoformat()} 不一致，两张卡各自使用自己的截止日。")
    return {
        "title": "经营数据：季度环比",
        "imported": quarter["imported"] or year["imported"],
        "source": "dms_service_operation_receipt",
        "note": "收款工单数、总产值、车辆数只来自 DMS 服务经营报表「收款 · 日报」导出的汇总行，不用总表自算结果代替。收款工单数按收款时间统计已收款工单，含仅备件销售，不是严格来厂台次。车辆数用汇总行去重值，不跨业务类型或区域相加。总产值源数据单位为元（2026-11-20 确认），页面按千元展示，保留两位小数。涨跌按原始元计算。是否含税未注明。页面和自动采集只比较本季度和上季度同期。库里已有的年度记录保留，不作为缺口。",
        "quarter": quarter,
        "year": year,
    }


def _role_as_of(active, week: str, role: str):
    national = active.get(("operation", week, "全国", role))
    if national is not None:
        return _date(national["period_end"])
    for key, row in active.items():
        if key[0] == "operation" and key[1] == week and key[3] == role:
            return _date(row["period_end"])
    return None


def _compare_panel(active, week: str, scopes: list[str], known: list[str], span: str, windows) -> dict:
    role = span
    base_role = span + "_base"
    national = active.get(("operation", week, "全国", role))
    rows = []
    for scope in scopes:
        current = active.get(("operation", week, scope, role))
        base = active.get(("operation", week, scope, base_role))
        aligned = True
        if windows and current is not None:
            aligned = current["period_start"] == windows["current"]["start"].isoformat() and current["period_end"] == windows["current"]["end"].isoformat()
        if national is not None and current is not None and scope != "全国":
            aligned = aligned and current["period_start"] == national["period_start"] and current["period_end"] == national["period_end"]
        cur_vals = _op_values(current) if current is not None else None
        base_vals = None
        if windows and base is not None and aligned:
            if base["period_start"] == windows["base"]["start"].isoformat() and base["period_end"] == windows["base"]["end"].isoformat():
                base_vals = _op_values(base)
        mismatch = current is not None and not aligned
        if mismatch:
            base_vals = None
        row = {"region": scope, "imported": current is not None and aligned, "period_mismatch": mismatch}
        row["yuan"] = {key: _s(val) for key, val in cur_vals.items()} if cur_vals else {}
        for field in ("orders", "output", "vehicles"):
            cur = None if cur_vals is None else cur_vals[field]
            bas = None if base_vals is None else base_vals[field]
            money = field == "output"
            cell = {
                "current": _fmt(cur, money=money),
                "base": _fmt(bas, money=money),
                "change": "不可比" if mismatch else _fmt_change(change(cur, bas)),
            }
            if money:
                cell["current_yuan"] = "" if cur is None else _s(cur)
                cell["base_yuan"] = "" if bas is None else _s(bas)
            row[field] = cell
        if cur_vals is None:
            row.update(labor="未导入", parts="未导入", other="未导入")
        else:
            row.update(
                labor=_fmt(cur_vals["labor"], money=True),
                parts=_fmt(cur_vals["parts"], money=True),
                other=_fmt(cur_vals["other"], money=True),
            )
        rows.append(row)
    _attach_pair_bars(rows)
    exports = []
    quality = []
    for item_role in (role, base_role):
        for scope in scopes:
            src = active.get(("operation", week, scope, item_role))
            if src is None:
                continue
            exports.append({"scope": scope, "role": item_role, "exported_at": src["exported_at"], "filename": src["source_filename"] or ""})
            quality.append(f"{scope}（{item_role}）：导出时间 {src['exported_at']}，文件 {src['source_filename'] or '未命名'}。")
            quality.extend(json.loads(src["quality_json"]))
    if national is None:
        quality.append("全国这份累计未导入。")
    period = None
    base_period = None
    if windows:
        period = {"start": windows["current"]["start"].isoformat(), "end": windows["current"]["end"].isoformat()}
        base_period = {"start": windows["base"]["start"].isoformat(), "end": windows["base"]["end"].isoformat()}
    return {
        "imported": any(row["imported"] for row in rows),
        "as_of": windows["as_of"].isoformat() if windows else "",
        "complete": bool(windows and windows["complete"]),
        "capped": bool(windows and windows["capped"]),
        "progress_note": windows["progress_note"] if windows else "",
        "default_note": windows["default_note"] if windows else "",
        "label_current": windows.get("label_current", "") if windows else "",
        "label_base": windows.get("label_base", "") if windows else "",
        "series_current": "本季度" if span == "quarter" else "本年度",
        "series_base": "上季度同期" if span == "quarter" else "去年同期",
        "change_label": "季度环比" if span == "quarter" else "年度同比",
        "current": period,
        "base": base_period,
        "rows": rows,
        "reconcile": _reconcile(national, rows, known) if national is not None else None,
        "exports": exports,
        "quality": quality,
    }


def _attach_pair_bars(rows: list[dict]) -> None:
    """本期和对比期各自画柱。未导入保持 None，不画成 0。各区柱按各区最大值缩放，避免全国柱把各区压扁。"""
    for field in ("orders", "output", "vehicles"):
        _paint_bars(rows, field, "current_bar", "base_bar")
        regional = [row for row in rows if row["region"] != "全国"]
        _paint_bars(regional, field, "region_current_bar", "region_base_bar")


def _bar_source(cell: dict, side: str):
    raw = cell.get(f"{side}_yuan")
    text = raw if raw not in (None, "") else cell[side]
    if text in {None, "", "未导入", "不可比"}:
        return None
    return Decimal(str(text).replace(",", ""))


def _paint_bars(rows: list[dict], field: str, cur_key: str, base_key: str) -> None:
    pairs = []
    for row in rows:
        pairs.append((_bar_source(row[field], "current"), _bar_source(row[field], "base")))
    peak = max((value for pair in pairs for value in pair if value is not None and value > 0), default=None)
    for row, (cur, bas) in zip(rows, pairs):
        row[field][cur_key] = _bar_width(cur, peak)
        row[field][base_key] = _bar_width(bas, peak)


def _bar_width(value: Decimal | None, peak: Decimal | None):
    if value is None:
        return None
    if peak is None:
        return 0
    return int((value / peak * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _op_values(row) -> dict:
    raw = json.loads(row["summary_json"])["summary"]
    return {key: Decimal(raw[key]) for key in (*COUNT_FIELDS, *MONEY_FIELDS)}


def _reconcile(national, rows, known: list[str]) -> dict:
    national_values = _op_values(national)
    counted = []
    mismatched = []
    by_region = {row["region"]: row for row in rows}
    for region in known:
        item = by_region.get(region)
        if item is None or item["orders"]["current"] == "未导入":
            continue
        if item.get("period_mismatch"):
            mismatched.append(region)
            continue
        counted.append(region)
    missing = [region for region in known if region not in counted and region not in mismatched]
    sums = {}
    gaps = {}
    for field, money in (("orders", False), ("output", True), ("labor", True), ("parts", True), ("other", True)):
        if field == "orders":
            total = sum((_parse_shown(by_region[region]["orders"]["current"]) for region in counted), Decimal(0))
        else:
            total = sum((Decimal((by_region[region].get("yuan") or {}).get(field) or "0") for region in counted), Decimal(0))
        base = national_values[field]
        sums[field] = _fmt(total, money=money)
        gaps[field] = _fmt(base - total, money=money)
    missing_text = "、".join(missing) if missing else "无"
    mismatch_text = ""
    if mismatched:
        mismatch_text = " 区间与全国不一致、未纳入对账：" + "、".join(mismatched) + "。"
    warning = (
        f"已导入区域收款工单数合计 {sums['orders']}，全国 {_fmt(national_values['orders'])}，差额 {gaps['orders']}。"
        f"总产值差额 {gaps['output']} 千元。未导入区域：{missing_text}。差额保留，不摊分。"
        f"车辆数取各自汇总行，不跨区域加总，不做对平。{mismatch_text}"
    )
    return {
        "warning": warning,
        "missing_regions": missing,
        "mismatched_regions": mismatched,
        "sums": sums,
        "gaps": gaps,
        "vehicles_compared": False,
    }


def _indicators_from_sheet(sheet: dict, src, mode: str, threshold: float) -> dict:
    blocks = {b["key"]: b for b in sheet["blocks"]}
    specs = (
        ("first", "首保实施率", False, "实绩/应保"),
        ("second", "二保实施率", False, "实绩/应保"),
        ("new", "新保投保率", False, "新车投保/新车交付量"),
        ("renewal", "续保投保率", False, "实绩续保/续保到期（含去年同期交付未新保）"),
        ("spare", "常备件备库率", False, "实备店数/应备店数"),
        ("task", "招揽实施率", True, "(应招揽-未招揽)/应招揽"),
    )
    metrics = []
    for key, title, complement, note in specs:
        block = blocks[key]
        cells = [_own_cell(block["total"], complement)]
        cells.extend(_own_cell(row, complement) for row in block["rows"])
        national_rate = cells[0]["rate"]
        for cell in cells[1:]:
            cell["below"] = cell["rate"] is not None and national_rate is not None and cell["rate"] < national_rate
        metrics.append({"key": key, "title": title, "note": note, "cells": cells})
    return {
        "status": "ready",
        "source": "reportdb.calc.compute",
        "result_table": "report_results",
        "table_key": "summary",
        "region_mode": mode,
        "snapshot_date": src["snapshot_date"],
        "ingested_at": src["ingested_at"] or "",
        "computed_at": src["computed_at"] or "",
        "period_note": INDICATOR_PERIOD_NOTE,
        "threshold": threshold,
        "metrics": metrics,
    }


def _own_cell(row, complement: bool) -> dict:
    den = int(row[1] or 0)
    second = row[2] or 0
    second_n = int(second) if isinstance(second, (int, float)) and float(second) == int(second) else 0
    num = den - second_n if complement else second_n
    rate = row[3]
    if rate is None or den == 0:
        rate_val = None
        rate_text = "不可比"
    else:
        rate_val = float(rate)
        rate_text = fmt_value(rate_val, PCT0)
    return {
        "region": row[0], "num": num, "den": den, "rate": rate_val,
        "rate_text": rate_text, "sub": f"{num}/{den}", "below": False,
    }


def _indicators_section(row) -> dict:
    if row is None:
        return {
            "status": "pending",
            "source": "reportdb.calc.compute",
            "result_table": "report_results",
            "table_key": "summary",
            "snapshot_date": "",
            "ingested_at": "",
            "computed_at": "",
            "period_note": "待计算。本周还没有 reportdb 总表结果，不用 DMS 服务管理主表填上。",
            "threshold": None,
            "metrics": [],
        }
    return json.loads(row["summary_json"])


def _save(conn, blob: bytes, meta: dict) -> dict:
    sha = hashlib.sha256(blob).hexdigest()
    role = meta.get("compare_role") or ""
    existing = conn.execute(
        "SELECT * FROM weekly_imports WHERE kind=? AND week_start=? AND scope=? AND compare_role=? AND status='active'",
        (meta["kind"], meta["week_start"], meta["scope"], role),
    ).fetchone()
    if existing is not None and existing["sha256"] == sha and _same_meta(existing, meta):
        return {"id": existing["id"], "created": False, "idempotent": True, "revision": existing["revision"], "scope": meta["scope"]}
    if existing is not None and not meta["allow_revision"]:
        raise SnapshotConflict(
            f"该周该范围已有快照（id={existing['id']}），内容或元数据不同，已拒绝覆盖。"
            "确认保留旧快照并写入新修订时，请使用修订选项。"
        )
    revision = (existing["revision"] + 1) if existing is not None else 1
    with conn:
        if existing is not None:
            conn.execute("UPDATE weekly_imports SET status='superseded' WHERE id=?", (existing["id"],))
        cur = conn.execute(
            """INSERT INTO weekly_imports(
                   kind, week_start, scope, compare_role, period_start, period_end, quarter_start, year, quarter,
                   stat_type, report_type, exported_at, source_filename, sha256, sheet_name, revision,
                   status, created_at, created_by, quality_json, summary_json, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?, ?, ?, ?, ?)""",
            (
                meta["kind"], meta["week_start"], meta["scope"], role, meta["period_start"], meta["period_end"],
                meta["quarter_start"], meta["year"], meta["quarter"], meta["stat_type"], meta["report_type"],
                meta["exported_at"], meta["source_filename"], sha, meta["sheet_name"], revision,
                _now(), meta["actor"], json.dumps(meta["quality"], ensure_ascii=False),
                json.dumps(meta["summary"], ensure_ascii=False), blob,
            ),
        )
        new_id = cur.lastrowid
        if existing is not None:
            conn.execute("UPDATE weekly_imports SET superseded_by=? WHERE id=?", (new_id, existing["id"]))
    service.audit(conn, meta["actor"], "weekly.import", f"{meta['kind']} {meta['week_start']} {meta['scope']} rev={revision} sha={sha[:12]}")
    return {"id": new_id, "created": True, "idempotent": False, "revision": revision, "scope": meta["scope"]}


def _same_meta(existing, meta: dict) -> bool:
    fields = ("period_start", "period_end", "quarter_start", "stat_type", "report_type", "exported_at")
    for field in fields:
        if (existing[field] or "") != (meta.get(field) or ""):
            return False
    for field in ("year", "quarter"):
        if (existing[field] or None) != (meta.get(field) or None):
            return False
    return True


def _cell_out(cell: dict) -> dict:
    return {"num": _s(cell["num"]), "den": _s(cell["den"]), "den_scale": cell["den_scale"]}


def _cell_in(raw: dict) -> dict:
    num, den, scale = Decimal(raw["num"]), Decimal(raw["den"]), int(raw.get("den_scale") or 1)
    return {"num": num, "den": den, "den_scale": scale, "rate": None if den == 0 else num / (den * scale)}


def _ratio_text(cell: dict) -> str:
    num, den, scale = _fmt(cell["num"]), _fmt(cell["den"]), cell["den_scale"]
    if scale == 1:
        return f"{num}/{den}"
    return f"{num}/({scale}×{den})"


def _fmt_pct(rate: Decimal | None) -> str:
    if rate is None:
        return "不可比"
    points = (rate * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{points:.2f}%"


def _fmt(value: Decimal | None, *, money: bool = False) -> str:
    if value is None:
        return "未导入"
    if money:
        quant = (value / Decimal(1000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        sign = "-" if quant < 0 else ""
        whole, frac = f"{abs(quant):.2f}".split(".")
        return sign + f"{int(whole):,}.{frac}"
    if value == value.to_integral_value():
        return str(int(value))
    return format(value, "f")


def _fmt_change(item: dict) -> str:
    if item["status"] == "missing":
        return "未导入"
    if item["status"] == "zero_base":
        return "不可比"
    text = _fmt_pct(item["rate"])
    if item["rate"] > 0:
        return "+" + text
    return text


def _parse_shown(text: str) -> Decimal:
    if text in {"", "未导入", "不可比"}:
        return Decimal(0)
    return Decimal(text.replace(",", ""))


def _week_label(start: str, today: date) -> str:
    monday = date.fromisoformat(start)
    suffix = "（本周）" if monday == today - timedelta(days=today.weekday()) else ""
    return f"{start} 至 {(monday + timedelta(days=6)).isoformat()}{suffix}"


def _monday(value: str) -> date:
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise WeeklyImportError("周次日期格式应为 YYYY-MM-DD") from exc
    if day.weekday() != 0:
        raise WeeklyImportError(f"{value} 不是周一，周次以周一为起点")
    return day


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WeeklyImportError("日期格式应为 YYYY-MM-DD") from exc


def _exported(value: str, timezone: str) -> str:
    if not ZONE_RE.fullmatch(timezone or ""):
        raise WeeklyImportError("时区格式应为 +08:00")
    matched = EXPORTED_RE.fullmatch((value or "").strip())
    if not matched:
        raise WeeklyImportError("导出时间格式应为 2026-11-20 14:25:35")
    datetime.strptime(matched.group(1) + " " + matched.group(2), "%Y-%m-%d %H:%M:%S")
    zone = matched.group(3) or timezone
    if matched.group(3) and matched.group(3) != timezone:
        raise WeeklyImportError("导出时间自带的时区与填写的时区不一致")
    return f"{matched.group(1)} {matched.group(2)} {zone}"


def _s(value: Decimal) -> str:
    return format(value, "f")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def demo_board() -> dict:
    """仅供 /reports/weekly/demo 截图。不写入数据库，也不代替真实周报。"""
    def pair(cur, base, *, money=False):
        c, b = Decimal(str(cur)), Decimal(str(base))
        item = {"current": _fmt(c, money=money), "base": _fmt(b, money=money), "change": _fmt_change(change(c, b))}
        if money:
            item["current_yuan"] = _s(c)
            item["base_yuan"] = _s(b)
        return item

    def panel(series_c, series_b, change_label, current, base, national, regions):
        rows = []

        def add(name, oc, ob, xc, xb, vc, vb):
            rows.append({
                "region": name,
                "orders": pair(oc, ob),
                "output": pair(xc, xb, money=True),
                "vehicles": pair(vc, vb),
                "labor": "—", "parts": "—", "other": "—",
                "imported": True, "period_mismatch": False,
            })

        add("全国", *national)
        for item in regions:
            add(*item)
        _attach_pair_bars(rows)
        return {
            "imported": True,
            "as_of": current[1], "complete": False, "capped": False,
            "current": {"start": current[0], "end": current[1]},
            "base": {"start": base[0], "end": base[1]},
            "series_current": series_c, "series_base": series_b, "change_label": change_label,
            "label_current": "", "label_base": "",
            "progress_note": "同进度是系统默认的比法，用来对齐还没过完的季度。",
            "default_note": "",
            "reconcile": None, "exports": [], "quality": [], "rows": rows,
        }

    quarter_regions = [
        ("华北", 186, 160, "9640000", "8820000", 92, 84),
        ("华东", 154, 171, "8010000", "9150000", 78, 88),
        ("华南", 142, 101, "7480000", "5530000", 70, 52),
        ("华中1", 96, 118, "4820000", "6040000", 46, 58),
        ("西南", 88, 74, "4160000", "3610000", 40, 35),
        ("西北", 64, 79, "2840000", "3370000", 31, 39),
        ("华中2", 42, 51, "1890000", "2240000", 20, 24),
    ]
    year_regions = [
        ("华北", 640, 590, "31020000", "28840000", 310, 290),
        ("华东", 520, 560, "26050000", "27410000", 250, 268),
        ("华南", 480, 410, "24000000", "19860000", 230, 190),
        ("华中1", 300, 340, "15040000", "16620000", 140, 160),
        ("西南", 260, 230, "12080000", "10850000", 120, 108),
        ("西北", 180, 210, "8630000", "9940000", 84, 98),
        ("华中2", 120, 140, "5410000", "6280000", 56, 66),
    ]
    quarter = panel("本季度", "上季度同期", "季度环比", ("2026-10-01", "2026-11-20"), ("2026-07-01", "2026-08-20"),
                    (980, 860, "48840000", "45210000", 470, 438), quarter_regions)
    year = panel("本年度", "去年同期", "年度同比", ("2026-01-01", "2026-11-20"), ("2025-01-01", "2025-11-20"),
                 (2860, 2620, "141050000", "129820000", 1320, 1240), year_regions)

    def metric(key, title, national, regions):
        cells = [{"region": "全国", "rate": national, "rate_text": fmt_value(national, PCT0), "sub": "", "below": False, "num": 1, "den": 1}]
        for name, rate in regions:
            if rate is None:
                cells.append({"region": name, "rate": None, "rate_text": "暂无数据", "sub": "", "below": False, "num": 0, "den": 0})
            else:
                cells.append({
                    "region": name, "rate": rate, "rate_text": fmt_value(rate, PCT0), "sub": "",
                    "below": rate < national, "num": 1, "den": 1,
                })
        return {"key": key, "title": title, "note": "", "cells": cells}

    areas = ["华北", "华东", "华南", "华中1", "西南", "西北", "华中2"]
    pack = [
        ("first", "首保实施率", 0.72, [0.86, 0.74, 0.58, 0.70, 0.81, 0.49, None]),
        ("second", "二保实施率", 0.66, [0.80, 0.62, 0.55, 0.71, 0.68, 0.44, 0.77]),
        ("new", "新保投保率", 0.81, [0.90, 0.84, 0.76, 0.69, 0.88, 0.63, 0.85]),
        ("renewal", "续保投保率", 0.54, [0.61, 0.48, 0.42, 0.57, 0.66, 0.39, 0.58]),
        ("spare", "常备件备库率", 0.78, [0.88, 0.74, 0.69, 0.81, 0.83, 0.60, 0.79]),
        ("task", "招揽实施率", 0.70, [0.82, 0.73, 0.64, 0.58, 0.76, 0.67, 0.71]),
    ]
    metrics = [metric(k, t, n, list(zip(areas, rates))) for k, t, n, rates in pack]
    commentary = {name: "" for name in ["全国", *areas]}
    commentary["华南"] = "到店比上季少"
    return {
        "demo": True,
        "week_start": "2026-11-16", "week_end": "2026-11-22", "is_current_week": False,
        "weeks": [{"start": "2026-11-16", "label": "2026-11-16 至 2026-11-22"}],
        "operation": {
            "title": "经营数据：季度环比",
            "imported": True,
            "source": "demo",
            "note": "示例",
            "quarter": quarter, "year": year,
            "months": _demo_months(),
        },
        "indicators": {
            "status": "ready",
            "source": "demo",
            "snapshot_date": "2026-11-18",
            "ingested_at": "2026-11-18 08:30:00",
            "computed_at": "2026-11-18 08:31:00",
            "period_note": "示例",
            "threshold": 0.8,
            "metrics": metrics,
        },
        "commentary": commentary,
        "history": [],
    }
