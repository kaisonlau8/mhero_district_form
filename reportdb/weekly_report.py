"""猛士周报快照：元数据校验、不可静默覆盖、页面模型。不采集 DMS。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .auth import service
from .weekly_parse import (
    METRICS,
    change,
    norm_region,
    parse_kpi_xlsx,
    parse_operation_xlsx,
    quarter_compare,
    quarter_span,
    year_compare,
)

OPERATION_SPANS = {"quarter", "quarter_base", "year", "year_base"}

DMS_BROWSER_DOC = "https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md"
PERIOD_RE = re.compile(r"^(\d{4})年Q([1-4])$")
EXPORTED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\s*([+-]\d{2}:\d{2}))?$")
ZONE_RE = re.compile(r"^[+-]\d{2}:\d{2}$")
MONEY_FIELDS = ("output", "labor", "parts", "other")
COUNT_FIELDS = ("orders", "vehicles")


class WeeklyImportError(ValueError):
    pass


class SnapshotConflict(Exception):
    pass


def collect_weekly_sources(**_kwargs):
    """自动采集入口。本轮故意不可用，避免和每日 08:30 抢 DMS 浏览器。"""
    raise NotImplementedError(
        "周报尚未启用自动采集。本函数不会登录 DMS，也不会修改 08:30 调度。"
        "以后若要采集，必须复用 scripts/dfmc_browser_utils.acquire_export_lock，并遵守 "
        f"{DMS_BROWSER_DOC} 的浏览器锁与错峰，不能与每日流水线并发导出。"
        "当前请使用 python -m reportdb.weekly_import 或页面上的 Excel 导入。"
    )


def import_operation(conn, blob: bytes, *, week_start: str, period_start: str, period_end: str,
                     exported_at: str, timezone: str = "+08:00", stat_type: str = "收款",
                     report_type: str = "日报", scope: str | None = None, filename: str = "",
                     actor: str = "", allow_revision: bool = False, span: str = "",
                     as_of: str | None = None) -> dict:
    if stat_type != "收款":
        raise WeeklyImportError("经营周报只接受统计类型「收款」，不接受结算或其他类型")
    if report_type != "日报":
        raise WeeklyImportError("经营周报只接受报表类型「日报」")
    if span not in OPERATION_SPANS:
        raise WeeklyImportError("经营比较必须指定 quarter、quarter_base、year 或 year_base。不再按周几天做环比或同比。")
    week = _monday(week_start)
    start, end = _date(period_start), _date(period_end)
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
    }
    return _save(conn, blob, {
        "kind": "operation",
        "compare_role": span,
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


def import_kpi(conn, blob: bytes, *, week_start: str, as_of: str, exported_at: str,
               timezone: str = "+08:00", year: int | None = None, quarter: int | None = None,
               filename: str = "", actor: str = "", allow_revision: bool = False) -> dict:
    week = _monday(week_start)
    cutoff = _date(as_of)
    if not (week <= cutoff <= week + timedelta(days=6)):
        raise WeeklyImportError("截至日必须落在所选周内。季度累计从季度第一天起算，但这次导出要归到该周。")
    parsed = parse_kpi_xlsx(blob)
    if len(parsed["periods"]) != 1:
        raise WeeklyImportError("KPI 周期不唯一或为空，不能确定季度起日")
    matched = PERIOD_RE.fullmatch(parsed["periods"][0])
    if not matched:
        raise WeeklyImportError(f"无法识别周期「{parsed['periods'][0]}」，期望如 2026年Q4")
    file_year, file_quarter = int(matched.group(1)), int(matched.group(2))
    if year is not None and int(year) != file_year:
        raise WeeklyImportError(f"填写年份 {year} 与文件周期 {file_year} 不一致")
    if quarter is not None and int(quarter) != file_quarter:
        raise WeeklyImportError(f"填写季度 {quarter} 与文件周期 Q{file_quarter} 不一致")
    q_start, q_end = quarter_span(file_year, file_quarter)
    if not (q_start <= cutoff <= q_end):
        raise WeeklyImportError(f"截至日 {cutoff.isoformat()} 不在 {file_year}Q{file_quarter}（{q_start} 至 {q_end}）内")
    summary = {
        "rollup_ok": parsed["rollup_ok"],
        "blockers": parsed["blockers"],
        "row_count": parsed["row_count"],
        "unique_stores": parsed["unique_stores"],
        "duplicate_groups": parsed["duplicate_groups"],
        "blank_region_rows": parsed["blank_region_rows"],
        "periods": parsed["periods"],
        "metrics": parsed["metrics"],
        "unverified_columns": parsed["unverified_columns"],
        "source_rate_mismatches": parsed["source_rate_mismatches"],
        "zero_denominator_rates": parsed["zero_denominator_rates"],
        "national": {key: _cell_out(value) for key, value in parsed["national"].items()},
        "regions": {
            region: {key: _cell_out(value) for key, value in cells.items()}
            for region, cells in parsed["regions"].items()
        },
        "sheet_name": parsed["sheet_name"],
    }
    return _save(conn, blob, {
        "kind": "kpi",
        "compare_role": "",
        "week_start": week.isoformat(),
        "scope": "全国",
        "period_start": q_start.isoformat(),
        "period_end": cutoff.isoformat(),
        "quarter_start": q_start.isoformat(),
        "year": file_year,
        "quarter": file_quarter,
        "stat_type": "",
        "report_type": "服务管理报表主表",
        "exported_at": _exported(exported_at, timezone),
        "source_filename": Path(filename).name,
        "sheet_name": parsed["sheet_name"],
        "quality": parsed["quality"],
        "summary": summary,
        "actor": actor or "",
        "allow_revision": allow_revision,
    })


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
    kpi_row = active.get(("kpi", week_start.isoformat(), "全国", ""))
    kpi = _kpi_section(kpi_row, known)
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
        "kpi": kpi,
        "commentary": notes,
        "history": history,
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
        "title": "经营数据：季度环比 / 年度同比",
        "imported": quarter["imported"] or year["imported"],
        "note": "收款工单数按收款时间统计已收款工单，含仅备件销售，不是严格来厂台次。车辆数是各文件汇总行的去重值，不可跨业务类型或区域相加。金额单位与是否含税源表未注明。比较用的是季度累计和年度累计，不是这一周的绝对数。",
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
        for field in ("orders", "output", "vehicles"):
            cur = None if cur_vals is None else cur_vals[field]
            bas = None if base_vals is None else base_vals[field]
            row[field] = {
                "current": _fmt(cur, money=field == "output"),
                "base": _fmt(bas, money=field == "output"),
                "change": "不可比" if mismatch else _fmt_change(change(cur, bas)),
            }
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
        "series_current": "本季" if span == "quarter" else "本年",
        "series_base": "上季" if span == "quarter" else "去年",
        "change_label": "季度环比" if span == "quarter" else "年度同比",
        "current": period,
        "base": base_period,
        "rows": rows,
        "reconcile": _reconcile(national, rows, known) if national is not None else None,
        "exports": exports,
        "quality": quality,
    }


def _attach_pair_bars(rows: list[dict]) -> None:
    """本期和对比期各自画柱。未导入保持 None，不画成 0。"""
    for field in ("orders", "output"):
        pairs = []
        for row in rows:
            pairs.append((
                None if row[field]["current"] in {"未导入", "不可比", ""} else Decimal(row[field]["current"]),
                None if row[field]["base"] in {"未导入", "不可比", ""} else Decimal(row[field]["base"]),
            ))
        peak = max((value for pair in pairs for value in pair if value is not None and value > 0), default=None)
        for row, (cur, bas) in zip(rows, pairs):
            row[field]["current_bar"] = _bar_width(cur, peak)
            row[field]["base_bar"] = _bar_width(bas, peak)


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
        if field in {"labor", "parts", "other"}:
            total = sum((_parse_shown(by_region[region][field]) for region in counted), Decimal(0))
        elif field == "orders":
            total = sum((_parse_shown(by_region[region]["orders"]["current"]) for region in counted), Decimal(0))
        else:
            total = sum((_parse_shown(by_region[region]["output"]["current"]) for region in counted), Decimal(0))
        base = national_values[field]
        sums[field] = _fmt(total, money=money)
        gaps[field] = _fmt(base - total, money=money)
    missing_text = "、".join(missing) if missing else "无"
    mismatch_text = ""
    if mismatched:
        mismatch_text = " 区间与全国不一致、未纳入对账：" + "、".join(mismatched) + "。"
    warning = (
        f"已导入区域收款工单数合计 {sums['orders']}，全国 {_fmt(national_values['orders'])}，差额 {gaps['orders']}。"
        f"总产值差额 {gaps['output']}。未导入区域：{missing_text}。差额保留，不摊分。"
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


def _kpi_section(row, known: list[str]) -> dict:
    if row is None:
        return {
            "imported": False,
            "quarter_start": "",
            "as_of": "",
            "exported_at": "",
            "filename": "",
            "row_count": None,
            "unique_stores": None,
            "blank_region_rows": None,
            "duplicate_groups": None,
            "blockers": [],
            "quality": ["本周季度累计指标未导入。"],
            "unverified_columns": [],
            "metrics": [],
            "periods_label": "",
        }
    summary = json.loads(row["summary_json"])
    columns = ["全国", *known]
    for region in summary.get("regions", {}):
        if region not in columns and region != "无区域":
            columns.append(region)
    columns.append("无区域")
    metrics = []
    if summary.get("rollup_ok"):
        national_cells = {key: _cell_in(value) for key, value in summary.get("national", {}).items()}
        region_cells = {
            region: {key: _cell_in(value) for key, value in cells.items()}
            for region, cells in summary.get("regions", {}).items()
        }
        for spec in METRICS:
            if spec.key not in national_cells:
                continue
            national_rate = national_cells[spec.key]["rate"]
            cells = []
            for region in columns:
                source = national_cells.get(spec.key) if region == "全国" else region_cells.get(region, {}).get(spec.key)
                if source is None:
                    cells.append({"region": region, "rate_text": "不可比", "sub": "0/0", "below": False})
                    continue
                below = (
                    region != "全国"
                    and source["rate"] is not None
                    and national_rate is not None
                    and source["rate"] < national_rate
                )
                cells.append({
                    "region": region,
                    "rate_text": _fmt_pct(source["rate"]),
                    "sub": _ratio_text(source),
                    "below": below,
                })
            metrics.append({"key": spec.key, "title": spec.title, "note": spec.note, "cells": cells})
    return {
        "imported": True,
        "quarter_start": row["quarter_start"] or "",
        "as_of": row["period_end"] or "",
        "exported_at": row["exported_at"] or "",
        "filename": row["source_filename"] or "",
        "row_count": summary.get("row_count"),
        "unique_stores": summary.get("unique_stores"),
        "blank_region_rows": summary.get("blank_region_rows"),
        "duplicate_groups": summary.get("duplicate_groups"),
        "blockers": summary.get("blockers") or [],
        "quality": json.loads(row["quality_json"]),
        "unverified_columns": summary.get("unverified_columns") or [],
        "metrics": metrics,
        "periods_label": "、".join(summary.get("periods") or []),
    }


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
        quant = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if value == quant:
            return f"{quant:.2f}"
        return format(value, "f")
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
    return Decimal(text)


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
