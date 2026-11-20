"""猛士周报的 Excel 解析与同环比。不读数据库，不访问 DMS。"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from openpyxl import load_workbook

class WeeklyParseError(ValueError):
    pass


OPERATION_HEADERS = (
    "区域名称", "门店编码", "门店名称", "工单数量", "车辆数", "业务类型", "总产值",
    "工时产值", "备件产值", "其他项目产值", "备件成本", "备件利润", "毛利", "毛利率",
    "平均每车产值", "平均每单产值", "入场频次",
)
OPERATION_COUNTS = ("工单数量", "车辆数", "总产值", "工时产值", "备件产值", "其他项目产值")
SUMMARY_LABELS = {"汇总", "合计"}

KPI_IDENTITY = ("区域", "门店编码", "门店名称", "周期")
UNVERIFIED_COLUMNS = (
    "总达成率", "巡检得分", "工装配备率", "常用备件配备率", "标准化餐食配备得分",
    "调研数", "9-10分数量", "6分及以下数量", "NSS得分",
)


@dataclass(frozen=True)
class MetricSpec:
    key: str
    title: str
    num_cols: tuple[str, ...]
    den_cols: tuple[str, ...]
    den_scale: int
    rate_col: str
    note: str
    required: bool


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("first", "首保实施率", ("首保实施率-实保",), ("首保实施率-应保",), 1, "首保实施率", "实保/应保", True),
    MetricSpec("second", "二保实施率", ("二保实施率-实保",), ("二保实施率-应保",), 1, "二保实施率", "实保/应保", True),
    MetricSpec("new", "新保实施率", ("新保实施率-投保量",), ("新保实施率-交付量",), 1, "新保实施率", "投保量/交付量", True),
    MetricSpec(
        "renewal", "续保实施率", ("续保实施率-实续保",),
        ("续保实施率-应续保", "去年同期交付未新保车辆数量"),
        1, "续保实施率", "实续保/（应续保+去年同期交付未新保）", True,
    ),
    MetricSpec(
        "campaign", "品情活动实施及时率", ("品情活动-无漏做车辆数",), ("品情活动-入场车辆数",),
        1, "品情活动实施及时率", "无漏做车辆数/入场车辆数", False,
    ),
    MetricSpec(
        "complaint", "投诉响应及时率", ("投诉-分钟响应量", "投诉-小时方案量"), ("投诉单量",),
        2, "投诉响应及时率", "(分钟响应+小时方案)/(2×投诉单量)", False,
    ),
    MetricSpec(
        "consult", "咨询响应及时率", ("咨询-分钟响应量", "咨询-小时方案量"), ("咨询单量",),
        2, "咨询响应及时率", "(分钟响应+小时方案)/(2×咨询单量)", False,
    ),
    MetricSpec(
        "response", "综合响应及时率",
        ("投诉-分钟响应量", "投诉-小时方案量", "咨询-分钟响应量", "咨询-小时方案量"),
        ("投诉单量", "咨询单量"),
        2, "综合响应及时率", "(四项响应量之和)/(2×(投诉单量+咨询单量))", False,
    ),
)


def norm_region(value: str) -> str:
    """DMS 区域：去空白；结尾「区」去掉（华东区→华东）。空字符串是「无区域」，不查门店表。"""
    text = re.sub(r"\s+", "", value or "")
    if not text:
        return "无区域"
    if text.endswith("区") and len(text) > 1:
        return text[:-1]
    return text


def change(current: Decimal | None, base: Decimal | None) -> dict:
    if current is None or base is None:
        return {"status": "missing", "amount": None, "rate": None}
    amount = current - base
    if base == 0:
        return {"status": "zero_base", "amount": amount, "rate": None}
    return {"status": "ok", "amount": amount, "rate": amount / base}


SAME_PROGRESS_NOTE = (
    "未结束期间默认按同进度累计，避免把本季已经过的几天和上一季的整季放在一起比。"
    "这是实现时采用的默认，不是业务方另行指定的规则。"
)


def align_progress(base_start: date, base_end: date, elapsed_days: int) -> tuple[date, bool]:
    """从上季（或对比期）起日加上已过天数。超过对比期末日则封顶。"""
    end = base_start + timedelta(days=elapsed_days)
    if end > base_end:
        return base_end, True
    return end, False


def quarter_compare(as_of: date) -> dict:
    """本季度累计对上一季度。未结束时对比上季起相同已过天数；已结束则对比完整上季。"""
    quarter = (as_of.month - 1) // 3 + 1
    q_start, q_end = quarter_span(as_of.year, quarter)
    if not (q_start <= as_of <= q_end):
        raise WeeklyParseError("截止日不在所属季度内")
    prev_year, prev_quarter = (as_of.year - 1, 4) if quarter == 1 else (as_of.year, quarter - 1)
    p_start, p_end = quarter_span(prev_year, prev_quarter)
    elapsed = (as_of - q_start).days
    complete = as_of == q_end
    capped = False
    if complete:
        b_start, b_end = p_start, p_end
        progress = "本季已结束，季度环比使用上一季度的完整期间。"
    else:
        b_start = p_start
        b_end, capped = align_progress(p_start, p_end, elapsed)
        progress = "同进度（实现默认，不是业务指定）：本季起日至截止日，对上季起相同已过天数。"
        if capped:
            progress += "上季较短，对比止日封顶为上季末日。"
    return {
        "kind": "quarter",
        "as_of": as_of,
        "complete": complete,
        "capped": capped,
        "elapsed_days": elapsed,
        "current": {"start": q_start, "end": as_of},
        "base": {"start": b_start, "end": b_end},
        "progress_note": progress,
        "default_note": "" if complete else SAME_PROGRESS_NOTE,
        "label_current": f"{as_of.year}Q{quarter}",
        "label_base": f"{prev_year}Q{prev_quarter}",
    }


def year_compare(as_of: date) -> dict:
    """本年度累计对上一年度。未结束时对到去年同月同日；2月29日在去年不存在则取2月28日。"""
    current_start = date(as_of.year, 1, 1)
    year_end = date(as_of.year, 12, 31)
    complete = as_of == year_end
    leap_note = ""
    if complete:
        base_end = date(as_of.year - 1, 12, 31)
        progress = "本年已结束，年度同比使用上一年度的完整期间。"
    else:
        try:
            base_end = date(as_of.year - 1, as_of.month, as_of.day)
        except ValueError:
            base_end = date(as_of.year - 1, 2, 28)
            leap_note = "今年2月29日在去年不存在，对比止日取2月28日。"
        progress = "同进度（实现默认，不是业务指定）：本年1月1日至截止日，对去年1月1日至同月同日。" + leap_note
    return {
        "kind": "year",
        "as_of": as_of,
        "complete": complete,
        "capped": False,
        "current": {"start": current_start, "end": year_end if complete else as_of},
        "base": {"start": date(as_of.year - 1, 1, 1), "end": base_end},
        "progress_note": progress,
        "default_note": "" if complete else SAME_PROGRESS_NOTE,
        "leap_note": leap_note,
    }


def quarter_span(year: int, quarter: int) -> tuple[date, date]:
    start = date(year, (quarter - 1) * 3 + 1, 1)
    if quarter == 4:
        end = date(year, 12, 31)
    else:
        end = date(year, quarter * 3 + 1, 1) - timedelta(days=1)
    return start, end


def parse_operation_xlsx(blob: bytes) -> dict:
    sheet, rows = _sheet(blob)
    header, body = _table(rows, OPERATION_HEADERS)
    summary_rows, detail = [], []
    for row in body:
        if row["区域名称"] in SUMMARY_LABELS:
            summary_rows.append(row)
        elif any(row.values()):
            detail.append(row)
    if len(summary_rows) != 1:
        raise WeeklyParseError(f"需要恰好一行「汇总」，实际 {len(summary_rows)} 行")
    if not detail:
        raise WeeklyParseError("没有明细行")
    summary = summary_rows[0]
    scopes = {norm_region(row["区域名称"]) for row in detail}
    if scopes == {"全国"}:
        for row in detail:
            if row["门店编码"] not in {"", "全国"} or row["门店名称"] not in {"", "全国"}:
                raise WeeklyParseError("全国文件出现具体门店，不能拆成区域")
        grain, scope = "national", "全国"
    elif len(scopes) == 1:
        scope = next(iter(scopes))
        grain = "region"
    else:
        raise WeeklyParseError("一份经营 Excel 出现多个区域，请按区域分别导出：" + "、".join(sorted(scopes)))

    totals = {key: _require(summary, key) for key in OPERATION_COUNTS}
    detail_sums = {key: sum((_require(row, key) for row in detail), Decimal(0)) for key in ("工单数量", "车辆数", "总产值")}
    types: list[str] = []
    for row in detail:
        if row["业务类型"] and row["业务类型"] not in types:
            types.append(row["业务类型"])
    quality = []
    if detail_sums["车辆数"] != totals["车辆数"]:
        quality.append(
            f"车辆数明细相加为 {detail_sums['车辆数']}，汇总行为 {totals['车辆数']}。采用汇总行，不把明细车辆数相加。"
        )
    if detail_sums["工单数量"] != totals["工单数量"]:
        quality.append(
            f"工单数量明细相加为 {detail_sums['工单数量']}，汇总行为 {totals['工单数量']}。采用汇总行。"
        )
    parts_sum = totals["工时产值"] + totals["备件产值"] + totals["其他项目产值"]
    if parts_sum != totals["总产值"]:
        quality.append(f"工时+备件+其他为 {parts_sum}，汇总总产值 {totals['总产值']}，差额 {totals['总产值'] - parts_sum}。保留汇总行，不改写。")
    else:
        quality.append("总产值取汇总行，与工时+备件+其他一致。")
    quality.append("金额单位与是否含税：源表未注明。")
    return {
        "sheet_name": sheet,
        "header": header,
        "grain": grain,
        "scope": scope,
        "summary": {
            "orders": totals["工单数量"],
            "vehicles": totals["车辆数"],
            "output": totals["总产值"],
            "labor": totals["工时产值"],
            "parts": totals["备件产值"],
            "other": totals["其他项目产值"],
        },
        "detail_rows": len(detail),
        "business_types": types,
        "detail_sums": {
            "orders": detail_sums["工单数量"],
            "vehicles": detail_sums["车辆数"],
            "output": detail_sums["总产值"],
        },
        "quality": quality,
    }


def parse_kpi_xlsx(blob: bytes) -> dict:
    """只用于辨认 DMS 服务管理主表。周报指标不读它，改读 calc.compute 的总表。"""
    sheet, rows = _sheet(blob)
    present_metrics = []
    required_headers = list(KPI_IDENTITY)
    for spec in METRICS:
        cols = spec.num_cols + spec.den_cols + (spec.rate_col,)
        if spec.required or all(col in _header_set(rows) for col in cols):
            present_metrics.append(spec)
            required_headers.extend(cols)
    header, body = _table(rows, tuple(dict.fromkeys(required_headers)))
    specs = [spec for spec in present_metrics if all(col in header for col in spec.num_cols + spec.den_cols)]
    unverified = [name for name in UNVERIFIED_COLUMNS if name in header]
    periods, blanks, keys = [], 0, {}
    empty_counts = 0
    for index, row in enumerate(body, start=2):
        if not any(row.values()):
            continue
        period = row["周期"]
        if period:
            periods.append(period)
        if not re.sub(r"\s+", "", row["区域"]):
            blanks += 1
        code = row["门店编码"]
        identity = (code, period) if code else (code, row["门店名称"], period)
        keys[identity] = keys.get(identity, 0) + 1
        for spec in specs:
            for col in spec.num_cols + spec.den_cols:
                if row[col] == "":
                    empty_counts += 1
                    break
        row["_line"] = index
    data_rows = [row for row in body if any(row.values())]
    duplicate_groups = sum(1 for count in keys.values() if count > 1)
    unique_periods = sorted(set(periods))
    blockers = []
    if duplicate_groups:
        blockers.append(f"门店+周期重复 {duplicate_groups} 组，拒绝加权汇总")
    if len(unique_periods) != 1:
        blockers.append("周期不是唯一值，拒绝加权汇总")
    if any(not row["周期"] for row in data_rows):
        blockers.append("存在空白周期")
    if empty_counts:
        blockers.append(f"有 {empty_counts} 个数量单元格为空，不能按 0 计入")
    result = {
        "sheet_name": sheet,
        "row_count": len(data_rows),
        "unique_stores": len(keys),
        "duplicate_groups": duplicate_groups,
        "blank_region_rows": blanks,
        "periods": unique_periods,
        "rollup_ok": not blockers,
        "blockers": blockers,
        "unverified_columns": unverified,
        "source_rate_mismatches": 0,
        "zero_denominator_rates": 0,
        "metrics": [spec.key for spec in specs],
        "national": {},
        "regions": {},
        "quality": list(blockers),
    }
    if blockers:
        return result
    national = {spec.key: _zero_cell(spec) for spec in specs}
    regions: dict[str, dict] = {}
    mismatches = zero_rates = 0
    for row in data_rows:
        region = norm_region(row["区域"])
        bucket = regions.setdefault(region, {spec.key: _zero_cell(spec) for spec in specs})
        for spec in specs:
            num = sum((_decimal(row[col]) for col in spec.num_cols), Decimal(0))
            den = sum((_decimal(row[col]) for col in spec.den_cols), Decimal(0))
            for target in (national[spec.key], bucket[spec.key]):
                target["num"] += num
                target["den"] += den
            shown = _percent_points(row.get(spec.rate_col, ""))
            if den == 0:
                if shown not in (None, Decimal(0)):
                    zero_rates += 1
            else:
                expect = num / (den * spec.den_scale) * 100
                if shown is None or abs(expect - shown) > Decimal("0.51"):
                    mismatches += 1
    for cell in national.values():
        _finish(cell)
    for bucket in regions.values():
        for cell in bucket.values():
            _finish(cell)
    result["national"] = national
    result["regions"] = regions
    result["source_rate_mismatches"] = mismatches
    result["zero_denominator_rates"] = zero_rates
    if mismatches:
        result["quality"].append(f"有 {mismatches} 个源百分数与分子/分母不一致，全国和区域达成率按分子/分母重算。")
    if zero_rates:
        result["quality"].append(f"有 {zero_rates} 个分母为 0 的格子源表仍写了百分数，这些达成率按不可比处理。")
    if blanks:
        result["quality"].append(f"DMS 区域为空 {blanks} 行，标为无区域，未用门店表或飞书改归属。全国加权包含这些行。")
    if unverified:
        result["quality"].append("公式未核验、不纳入加权：" + "、".join(unverified) + "。")
    return result


def _zero_cell(spec: MetricSpec) -> dict:
    return {"num": Decimal(0), "den": Decimal(0), "den_scale": spec.den_scale, "rate": None}


def _finish(cell: dict) -> None:
    if cell["den"] == 0:
        cell["rate"] = None
    else:
        cell["rate"] = cell["num"] / (cell["den"] * cell["den_scale"])


def _header_set(rows: list[list[str]]) -> set[str]:
    return set(rows[0]) if rows else set()


def _sheet(blob: bytes) -> tuple[str, list[list[str]]]:
    if not blob:
        raise WeeklyParseError("文件为空")
    try:
        # 这些 DMS 导出的 dimension 可能只写成 A1，read_only 会把后面的列丢掉。
        book = load_workbook(io.BytesIO(blob), read_only=False, data_only=True)
    except Exception as exc:
        raise WeeklyParseError("不是可读取的 xlsx") from exc
    try:
        ws = book[book.sheetnames[0]]
        rows = [[_cell_str(value) for value in row] for row in ws.iter_rows(values_only=True)]
        name = ws.title
    finally:
        book.close()
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows:
        raise WeeklyParseError("工作表是空的")
    return name, rows


def _table(rows: list[list[str]], required: tuple[str, ...]) -> tuple[list[str], list[dict]]:
    header = list(rows[0])
    while header and header[-1] == "":
        header.pop()
    if any(not name for name in header):
        raise WeeklyParseError("表头有空列")
    if len(header) != len(set(header)):
        raise WeeklyParseError("表头有重复列")
    missing = [name for name in required if name not in header]
    if missing:
        raise WeeklyParseError("缺少列：" + "、".join(missing))
    body = []
    width = len(header)
    for raw in rows[1:]:
        padded = list(raw) + [""] * (width - len(raw))
        item = {header[i]: padded[i] for i in range(width)}
        body.append(item)
    return header, body


def _cell_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _decimal(value: str) -> Decimal:
    text = (value or "").strip().replace(",", "")
    if text == "":
        raise WeeklyParseError("数量为空")
    if text.endswith("%"):
        raise WeeklyParseError("数量列不应是百分数")
    try:
        return Decimal(text)
    except Exception as exc:
        raise WeeklyParseError(f"无法解析数字：{value}") from exc


def _require(row: dict, column: str) -> Decimal:
    if row.get(column, "") == "":
        raise WeeklyParseError(f"「{column}」为空，不能当成 0")
    return _decimal(row[column])


def _percent_points(value: str) -> Decimal | None:
    text = (value or "").strip().replace(",", "")
    if text == "":
        return None
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    try:
        number = Decimal(text)
    except Exception:
        return None
    if not percent and abs(number) <= 1 and number != 0:
        return number * 100
    return number
