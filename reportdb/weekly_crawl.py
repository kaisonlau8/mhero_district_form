"""周五经营采集的计划、筛选核对和导入。这里不打开浏览器。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from decimal import Decimal

from .weekly_parse import norm_region, parse_operation_xlsx, quarter_compare
from .weekly_report import SnapshotConflict, WeeklyImportError, import_operation

SCHEDULE_ID = "district-form-weekly"
SCHEDULE_TIME = "14:45"
ACCIDENT_LOCK_AT = (17, 0)
ACCIDENT_PRE_MINUTES = 3
SCHEDULE_WEEKDAY = "friday"
DMS_BROWSER_DOC = "https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md"
SPANS = ("quarter", "quarter_base")
EMPTY_MARKS = {"", "请选择", "请输入", "please choose", "please input"}
MAX_ATTEMPTS = 3
SUMMARY_KEYS = ("orders", "vehicles", "output")
# 2026-11-20 18:10:20+08，页面点查询后从 Chrome Network 面板读到的 getTotal。
# 收款对应 statisticalType "2"，日报对应 reportType "1"。只证实了这一组。
# 西南区当时选中项的值是 "009"。别的区域没有读到编码，不能按名称编一个。
VERIFIED_STATISTICAL_TYPE = {"收款": "2"}
VERIFIED_REPORT_TYPE = {"日报": "1"}


class WeeklyCrawlError(RuntimeError):
    pass


def describe_plan(*, as_of: str | date, regions: list[str]) -> dict:
    day = _as_date(as_of)
    jobs = plan_jobs(day, regions)
    return {
        "live": False,
        "verified_against_dms": False,
        "schedule_id": SCHEDULE_ID,
        "time": SCHEDULE_TIME,
        "weekday": SCHEDULE_WEEKDAY,
        "doc": DMS_BROWSER_DOC,
        "note": "只生成计划，只有本季度和上季度同期。不查询年度。未连接 DMS。getTotal 的字段只记录了 2026-11-20 西南这一次页面查询，其他区没有编码，不能据此自动采集。",
        "week_start": _monday(day).isoformat(),
        "as_of": day.isoformat(),
        "jobs": jobs,
    }


def plan_jobs(as_of: date, regions: list[str]) -> list[dict]:
    spans = _spans(as_of)
    scopes = ["全国", *regions]
    jobs = []
    for span in SPANS:
        window = spans[span]
        for scope in scopes:
            filters = expected_filters(scope, window["start"], window["end"])
            jobs.append({
                "key": f"{as_of.isoformat()}:{span}:{scope}",
                "span": span,
                "scope": scope,
                "period_start": window["start"].isoformat(),
                "period_end": window["end"].isoformat(),
                "as_of": as_of.isoformat(),
                "filters": filters,
            })
    return jobs


def expected_filters(scope: str, start: date, end: date) -> dict:
    return {
        "统计类型": "收款",
        "报表类型": "日报",
        "区域": "全国" if scope == "全国" else scope,
        "业务门店": "",
        "开始日期": start.isoformat(),
        "结束日期": end.isoformat(),
    }


def readback_mismatches(expected: dict, actual: dict | None) -> list[str]:
    """查询前、查询后都要能对上。多出来的非空筛选项也算失败，避免带着没看见的条件导出。"""
    if not isinstance(actual, dict):
        return ["没有读回筛选"]
    bad = []
    seen = set()
    for key, want in expected.items():
        found = _find_actual(actual, key)
        seen.add(found[0] if found else key)
        got = "" if found is None else found[1]
        if not _same_filter(key, want, got):
            bad.append(f"{key} 读回「{got}」，应为「{want}」")
    for key, value in actual.items():
        if key in seen or key in expected:
            continue
        if _blank(value):
            continue
        bad.append(f"多出的筛选「{key}」是「{value}」")
    return bad


def fresh_download(before: dict[str, bytes], name: str, content: bytes) -> bool:
    """旧文件内容没变，或没有新字节，都不算这次导出。"""
    if not name or not content or name.endswith(".crdownload"):
        return False
    return before.get(name) != content


def confirm_query_responses(expected: dict, query_id: str, responses) -> dict:
    """只承认这次 Query 发出、且请求筛选对得上的明细和汇总响应。

    旁边的 2xx、上一笔查询、页面上的旧文字，都不算。汇总响应没观测到就失败，待人工复核。
    数字和上次相同没有关系，只要这次响应本身成功即可。
    """
    if not query_id:
        raise WeeklyCrawlError("没有本次查询标记，待人工复核")
    if not responses:
        raise WeeklyCrawlError("汇总响应未观测到，待人工复核")
    details = []
    summaries = []
    for item in responses:
        if not isinstance(item, dict) or item.get("query_id") != query_id:
            continue
        if item.get("kind") not in {"detail", "summary"}:
            continue
        if readback_mismatches(expected, item.get("filters")):
            continue
        status = int(item.get("status") or 0)
        if item.get("kind") == "summary" and status >= 400:
            raise WeeklyCrawlError(f"汇总请求失败：HTTP {status}，待人工复核")
        if not 200 <= status < 300:
            continue
        if item.get("kind") == "detail":
            details.append(item)
            continue
        if item.get("summary_from") != "response_body":
            raise WeeklyCrawlError("车辆数不能把明细或分月去重值相加，必须用本次汇总响应")
        summary = item.get("summary") or {}
        if any(key not in summary or summary[key] in ("", None) for key in SUMMARY_KEYS):
            continue
        summaries.append(summary)
    if not details:
        raise WeeklyCrawlError("明细响应未观测到，待人工复核")
    if not summaries:
        raise WeeklyCrawlError("汇总响应未观测到，待人工复核")
    first = summaries[0]
    for other in summaries[1:]:
        for key in SUMMARY_KEYS:
            if Decimal(str(other[key])) != Decimal(str(first[key])):
                raise WeeklyCrawlError("本次出现多条不一致的汇总响应，待人工复核")
    return {key: first[key] for key in SUMMARY_KEYS}


def classify_page_response(event: dict, query_id: str, expected: dict) -> dict | None:
    """只接受请求契约和本次筛选对得上的页面响应。界面上的中文标签不算请求字段。"""
    fields = parse_request_fields(event.get("url") or "", event.get("post_data") or "")
    if not request_matches_observed_contract(fields, expected):
        return None
    body = event.get("body")
    gui_filters = {
        key: expected[key]
        for key in ("统计类型", "报表类型", "区域", "业务门店", "开始日期", "结束日期")
        if key in expected
    }
    if isinstance(body, dict) and body.get("summary_from") == "detail_sum":
        return {
            "query_id": query_id,
            "kind": "summary",
            "status": int(event.get("status") or 0),
            "filters": gui_filters,
            "summary_from": "detail_sum",
            "summary": body.get("summary") or {},
        }
    kind, summary = _kind_from_body(body)
    if kind is None:
        return None
    record = {
        "query_id": query_id,
        "kind": kind,
        "status": int(event.get("status") or 0),
        "filters": gui_filters,
        "summary_from": "response_body",
    }
    if kind == "summary":
        record["summary"] = summary
    return record


def accept_operation_result(job: dict, got: dict) -> tuple[bytes, dict]:
    """返回 Excel 字节，以及和这份 Excel 对过账的那次汇总响应。"""
    summary = confirm_query_responses(job.get("filters") or {}, got.get("query_id") or "", got.get("responses"))
    if not got.get("download_is_new"):
        raise WeeklyCrawlError("没有本次新下载的 Excel，不把已有文件当成新导出")
    blob = got.get("blob") or b""
    if not blob:
        raise WeeklyCrawlError("没有本次新下载的 Excel")
    parsed = parse_operation_xlsx(blob)
    if parsed["scope"] != job["scope"]:
        raise WeeklyCrawlError(f"Excel 范围是「{parsed['scope']}」，这次查询是「{job['scope']}」")
    for key in SUMMARY_KEYS:
        if Decimal(str(summary[key])) != parsed["summary"][key]:
            raise WeeklyCrawlError(
                f"Excel 的{key}是 {parsed['summary'][key]}，本次汇总响应是 {summary[key]}，不一致，不导入"
            )
    return blob, {key: summary[key] for key in SUMMARY_KEYS}


def prepare_filters(form, filters: dict) -> dict:
    form.set_filters(filters)
    before = form.read_filters()
    bad = readback_mismatches(filters, before)
    if bad:
        raise WeeklyCrawlError("查询前筛选不一致：" + "；".join(bad))
    return before


def finish_query(form, filters: dict) -> dict:
    form.query()
    after = form.read_filters()
    bad = readback_mismatches(filters, after)
    if bad:
        raise WeeklyCrawlError("查询后筛选不一致：" + "；".join(bad))
    return after


def prepare_export(form, filters: dict) -> tuple[dict, dict]:
    """先设筛选并读回，不对就不要查询；查询后再读回，不对就不要导出。"""
    return prepare_filters(form, filters), finish_query(form, filters)


def in_accident_prewindow(now) -> bool:
    """周五 16:57 起是 17:00 事故任务的提前 3 分钟禁区。此时不再开始新的导出。"""
    cutoff_hour, cutoff_minute = ACCIDENT_LOCK_AT
    cutoff = cutoff_hour * 60 + cutoff_minute - ACCIDENT_PRE_MINUTES
    return now.weekday() == 4 and (now.hour * 60 + now.minute) >= cutoff


def past_friday_deadline(now) -> bool:
    return in_accident_prewindow(now)


def _hong_kong_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)


WEEKLY_SCHEDULE_ENTRY = {
    "id": SCHEDULE_ID,
    "name": "周报经营",
    "time": SCHEDULE_TIME,
    "owners": ["mhero_district_form"],
    "enabled": True,
}


def merge_weekly_schedule(data: dict) -> dict:
    """不把 14:45 写进共享时刻表。

    正在使用的 keepalive 只按钟点计算避让，不看星期。启用的 14:45 会让每天
    14:42 到约 15:30 都停止强刷。本仓库里的 refresh_block_reason 没有被调用，
    生产上的消费者在别的仓库；这里改了也不等于那边已改。周任务只在真正运行时持锁。
    """
    raise WeeklyCrawlError(
        "不能把 district-form-weekly 写进共享时刻表：现有 keepalive 不看星期，"
        "每天 14:42–15:30 都会改变强刷。星期感知要等实际运行 keepalive 的那份代码先改。"
    )


def run_weekly_crawl(conn, *, as_of: str | date, regions: list[str], fetch, output_dir: Path,
                     actor: str = "weekly-crawl", resume: bool = True, clock=None) -> dict:
    """串行调用 fetch。失败的范围记下来，已导入的留下，不把部分成功说成全部完成。"""
    day = _as_date(as_of)
    clock = clock or _hong_kong_now
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "weekly_manifest.json"
    manifest = _manifest_for(day, manifest_path) if resume else _fresh_manifest(day)
    results = []
    for job in plan_jobs(day, regions):
        previous = manifest["done"].get(job["key"]) or {}
        same_window = previous.get("period_start") == job["period_start"] and previous.get("period_end") == job["period_end"]
        if resume and previous.get("status") == "imported" and same_window:
            results.append({**job, "status": "skipped"})
            continue
        if in_accident_prewindow(clock()):
            results.append({
                **job, "status": "not_started",
                "error": "已到周五 16:57，不再开始新的地区或期间。正在进行的请求会做完并释放锁，不会关掉浏览器。",
            })
            continue
        saved = output_dir / f"{job['span']}_{job['scope']}.xlsx"
        previous_bytes = saved.read_bytes() if saved.exists() else None
        last_error = ""
        imported_ok = False
        attempts_used = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts_used = attempt
            if in_accident_prewindow(clock()):
                last_error = "已到周五 16:57，这一次不再重试，也不再发起新的查询。正在进行的请求会做完并释放锁，不会关掉浏览器。"
                break
            try:
                got = fetch(job)
                before = got.get("readback_before")
                after = got.get("readback_after")
                bad = [f"查询前{item}" for item in readback_mismatches(job["filters"], before)]
                bad += [f"查询后{item}" for item in readback_mismatches(job["filters"], after)]
                if bad:
                    raise WeeklyCrawlError("筛选读回不一致：" + "；".join(bad))
                blob, summary = accept_operation_result(job, got)
                saved.write_bytes(blob)
                meta_path = output_dir / f"{job['span']}_{job['scope']}.json"
                meta_path.write_text(json.dumps({
                    "span": job["span"], "scope": job["scope"], "filters": job["filters"],
                    "readback_before": before, "readback_after": after,
                    "summary": {key: str(summary[key]) for key in SUMMARY_KEYS},
                    "exported_at": got["exported_at"], "file": saved.name,
                    "summary_body_parsed": True,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                imported = import_operation(
                    conn, blob, week_start=_monday(day).isoformat(), span=job["span"],
                    period_start=job["period_start"], period_end=job["period_end"],
                    as_of=job["as_of"], exported_at=got["exported_at"], timezone="+08:00",
                    stat_type="收款", report_type="日报", scope=job["scope"],
                    filename=saved.name, actor=actor,
                )
                manifest["done"][job["key"]] = {
                    "status": "imported", "id": imported["id"], "idempotent": imported["idempotent"],
                    "file": saved.name, "period_start": job["period_start"], "period_end": job["period_end"],
                }
                results.append({**job, "status": "imported", "idempotent": imported["idempotent"], "attempts": attempt})
                imported_ok = True
                break
            except (WeeklyCrawlError, RuntimeError, OSError) as exc:
                last_error = str(exc)
                if previous_bytes is None and saved.exists() and saved.read_bytes() != previous_bytes:
                    saved.unlink(missing_ok=True)
                elif previous_bytes is not None and (not saved.exists() or saved.read_bytes() != previous_bytes):
                    saved.write_bytes(previous_bytes)
                if attempt == MAX_ATTEMPTS:
                    break
            except (WeeklyImportError, SnapshotConflict, KeyError) as exc:
                last_error = str(exc)
                if previous_bytes is not None:
                    saved.write_bytes(previous_bytes)
                elif saved.exists():
                    saved.unlink(missing_ok=True)
                break
        if not imported_ok:
            manifest["done"][job["key"]] = {"status": "failed", "error": last_error, "attempts": attempts_used}
            results.append({**job, "status": "failed", "error": last_error})
        manifest["complete"] = False
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [item["key"] for item in results if item["status"] not in {"imported", "skipped"}]
    manifest["complete"] = not failed
    manifest["gaps"] = failed
    manifest["verified_against_dms"] = False
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "complete": not failed,
        "verified_against_dms": False,
        "results": results,
        "failed": failed,
        "gaps": failed,
        "schedule_id": SCHEDULE_ID,
        "time": SCHEDULE_TIME,
    }


def _spans(as_of: date) -> dict:
    quarter = quarter_compare(as_of)
    return {
        "quarter": quarter["current"],
        "quarter_base": quarter["base"],
    }


def _find_actual(actual: dict, key: str):
    if key in actual:
        return key, actual[key]
    for name, value in actual.items():
        if _compact(name) == _compact(key):
            return name, value
    return None


def _same_filter(key: str, want, got) -> bool:
    if key in {"开始日期", "结束日期"}:
        return _date_text(got) == _date_text(want)
    if key == "区域":
        return (norm_region(got) or "") == (norm_region(want) or "")
    if key == "业务门店":
        return _blank(got) and _blank(want)
    return _compact(got) == _compact(want)


class RequestPayload(dict):
    """保留重复键。相同值可以重复，值冲突记下来，调用方必须拒绝。"""

    def __init__(self):
        super().__init__()
        self.conflicts: list[str] = []


def parse_request_fields(url: str, post_data: str) -> RequestPayload | None:
    """只解析请求自己的字段。不把整段文字里碰巧出现的值当成筛选。"""
    from urllib.parse import parse_qs, urlparse

    text = (post_data or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text, object_pairs_hook=_pairs_to_payload)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, RequestPayload):
            return data
    if text and "=" in text:
        parsed = parse_qs(text, keep_blank_values=True)
        if parsed:
            return _payload_from_lists(parsed)
    query = urlparse(url or "").query
    if query:
        parsed = parse_qs(query, keep_blank_values=True)
        if parsed:
            return _payload_from_lists(parsed)
    return None


def request_matches_observed_contract(fields: RequestPayload | None, expected: dict) -> bool:
    """对照 2026-11-20 西南 getTotal 的请求形状。对不上就拒绝，不猜另一套字段。"""
    if not isinstance(fields, RequestPayload) or fields.conflicts:
        return False
    stat = VERIFIED_STATISTICAL_TYPE.get(str(expected.get("统计类型") or ""))
    report = VERIFIED_REPORT_TYPE.get(str(expected.get("报表类型") or ""))
    area_code = str(expected.get("area_code") or "")
    if not stat or not report or not area_code:
        return False
    if fields.get("statisticalType") != stat or fields.get("reportType") != report:
        return False
    if str(fields.get("bigAreaId") or "") != area_code:
        return False
    start = _date_text(expected.get("开始日期"))
    end = _date_text(expected.get("结束日期"))
    if not start or not end:
        return False
    day_keys = {
        "dateStart": start,
        "dateEnd": end,
        "dateStart1": start,
        "dateEnd1": end,
    }
    for key, want in day_keys.items():
        if key not in fields or _date_text(fields.get(key)) != want:
            return False
    if "dirCode" not in fields or "retRepairType" not in fields:
        return False
    if not _blank(fields.get("dirCode")) or not _blank(fields.get("retRepairType")):
        return False
    for key in ("month", "monthEnd", "monthStart", "year"):
        if key in fields and not _blank(fields.get(key)):
            return False
    if "monthStartEnd" in fields and fields.get("monthStartEnd") not in ([], ""):
        return False
    if "monthStart,monthEnd" in fields and fields.get("monthStart,monthEnd") not in ([], ""):
        return False
    return True


def structured_filters_match(fields: dict | None, expected: dict) -> bool:
    return request_matches_observed_contract(fields, expected)


def _pairs_to_payload(pairs) -> RequestPayload:
    payload = RequestPayload()
    for key, value in pairs:
        if key in payload and payload[key] != value:
            payload.conflicts.append(str(key))
        payload[key] = value
    return payload


def _payload_from_lists(parsed: dict) -> RequestPayload:
    payload = RequestPayload()
    for key, values in parsed.items():
        seen = []
        for value in values:
            if value not in seen:
                seen.append(value)
        if len(seen) > 1:
            payload.conflicts.append(str(key))
        payload[key] = values[-1] if values else ""
    return payload


def _kind_from_body(body):
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(body, dict):
        return None, None
    if body.get("summary_from") == "detail_sum":
        return "summary", None
    summary = _summary_from_get_total(body)
    if summary is not None:
        return "summary", summary
    if "Total" in body and not isinstance(body.get("rows"), dict):
        try:
            Decimal(str(body.get("Total")).replace(",", ""))
        except Exception:
            return None, None
        return "detail", None
    return None, None


def _summary_from_get_total(body: dict) -> dict | None:
    if str(body.get("result")) != "1" or body.get("msg") != "查询成功":
        return None
    rows = body.get("rows")
    if not isinstance(rows, dict) or isinstance(rows, list):
        return None
    if rows.get("bigAreaName") != "汇总":
        return None
    if any(key not in rows for key in ("repairOrderNum", "vinNum", "totalAmount")):
        return None
    try:
        return {
            "orders": _plain_number(rows["repairOrderNum"]),
            "vehicles": _plain_number(rows["vinNum"]),
            "output": _plain_number(rows["totalAmount"]),
        }
    except (ArithmeticError, ValueError):
        return None


def _plain_number(value) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("empty number")
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        raise ValueError("empty number")
    return format(Decimal(text), "f")


def _blank(value) -> bool:
    return _compact(value).lower() in EMPTY_MARKS


def _date_text(value) -> str:
    text = str(value or "").strip().replace("/", "-").replace(".", "-")
    return text[:10]


def _compact(value) -> str:
    return "".join(str(value or "").split())


def _as_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def close_owned_session(lock, *, refresh, release) -> None:
    """只有拿到锁之后才刷新共享浏览器，并且刷新完成前不把锁放开。"""
    if lock is None:
        return
    try:
        refresh()
    finally:
        release(lock)


def _fresh_manifest(day: date) -> dict:
    return {"as_of": day.isoformat(), "week_start": _monday(day).isoformat(), "done": {}}


def _manifest_for(day: date, path: Path) -> dict:
    loaded = _load_manifest(path)
    if loaded.get("as_of") != day.isoformat():
        return _fresh_manifest(day)
    loaded.setdefault("done", {})
    loaded["week_start"] = _monday(day).isoformat()
    return loaded


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"done": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"done": {}}
    if not isinstance(loaded, dict) or not isinstance(loaded.get("done"), dict):
        return {"done": {}}
    return loaded
