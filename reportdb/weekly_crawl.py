"""周五经营采集的计划、筛选核对和导入。这里不打开浏览器。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from decimal import Decimal

from .weekly_parse import norm_region, parse_operation_xlsx, quarter_compare, year_compare
from .weekly_report import SnapshotConflict, WeeklyImportError, import_operation

SCHEDULE_ID = "district-form-weekly"
SCHEDULE_TIME = "13:30"
SCHEDULE_WEEKDAY = "friday"
DMS_BROWSER_DOC = "https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md"
SPANS = ("quarter", "quarter_base", "year", "year_base")
EMPTY_MARKS = {"", "请选择", "请输入", "please choose", "please input"}
MAX_ATTEMPTS = 3
SUMMARY_KEYS = ("orders", "vehicles", "output")


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
        "note": "只生成计划。未连接 DMS，现场端到端尚未执行。生产命令要另加 --live，并且不能和每天 08:30 同时跑。",
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


def accept_operation_result(job: dict, got: dict) -> bytes:
    """明细、本次汇总请求、新下载的 Excel 必须同时成功，并且文件汇总等于这次汇总。

    页面上还留着上一笔查询的数字、汇总请求 504、或者把明细车辆数加总，都不能导入。
    """
    if got.get("summary_error"):
        raise WeeklyCrawlError(f"汇总请求失败：{got['summary_error']}")
    if not got.get("detail_ok"):
        raise WeeklyCrawlError("本次查询没有成功返回明细")
    if got.get("summary_source") == "detail_sum":
        raise WeeklyCrawlError("车辆数不能把明细或分月去重值相加，必须用本次汇总行")
    if not got.get("summary_ok") or got.get("summary_unchanged") or got.get("summary_source") != "response":
        shown = got.get("visible_summary") or "页面上的旧汇总"
        raise WeeklyCrawlError(f"汇总不是本次查询的结果，不采用页面残留数字：{shown}")
    if not got.get("download_is_new"):
        raise WeeklyCrawlError("没有本次新下载的 Excel，不把已有文件当成新导出")
    summary = got.get("summary") or {}
    blob = got.get("blob") or b""
    if not blob:
        raise WeeklyCrawlError("没有本次新下载的 Excel")
    parsed = parse_operation_xlsx(blob)
    if parsed["scope"] != job["scope"]:
        raise WeeklyCrawlError(f"Excel 范围是「{parsed['scope']}」，这次查询是「{job['scope']}」")
    for key in SUMMARY_KEYS:
        if key not in summary:
            raise WeeklyCrawlError(f"本次汇总缺少{key}")
        if Decimal(str(summary[key])) != parsed["summary"][key]:
            raise WeeklyCrawlError(
                f"Excel 的{key}是 {parsed['summary'][key]}，本次汇总是 {summary[key]}，不一致，不导入"
            )
    return blob


def prepare_export(form, filters: dict) -> tuple[dict, dict]:
    """先设筛选并读回，不对就不要查询；查询后再读回，不对就不要导出。"""
    form.set_filters(filters)
    before = form.read_filters()
    bad = readback_mismatches(filters, before)
    if bad:
        raise WeeklyCrawlError("查询前筛选不一致：" + "；".join(bad))
    form.query()
    after = form.read_filters()
    bad = readback_mismatches(filters, after)
    if bad:
        raise WeeklyCrawlError("查询后筛选不一致：" + "；".join(bad))
    return before, after


def run_weekly_crawl(conn, *, as_of: str | date, regions: list[str], fetch, output_dir: Path,
                     actor: str = "weekly-crawl", resume: bool = True) -> dict:
    """串行调用 fetch。失败的范围记下来，已导入的留下，不把部分成功说成全部完成。"""
    day = _as_date(as_of)
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
        saved = output_dir / f"{job['span']}_{job['scope']}.xlsx"
        previous_bytes = saved.read_bytes() if saved.exists() else None
        last_error = ""
        imported_ok = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                got = fetch(job)
                before = got.get("readback_before")
                after = got.get("readback_after")
                bad = [f"查询前{item}" for item in readback_mismatches(job["filters"], before)]
                bad += [f"查询后{item}" for item in readback_mismatches(job["filters"], after)]
                if bad:
                    raise WeeklyCrawlError("筛选读回不一致：" + "；".join(bad))
                blob = accept_operation_result(job, got)
                saved.write_bytes(blob)
                meta_path = output_dir / f"{job['span']}_{job['scope']}.json"
                meta_path.write_text(json.dumps({
                    "span": job["span"], "scope": job["scope"], "filters": job["filters"],
                    "readback_before": before, "readback_after": after,
                    "summary": {key: str(got["summary"][key]) for key in SUMMARY_KEYS},
                    "exported_at": got["exported_at"], "file": saved.name,
                    "summary_body_parsed": bool(got.get("summary_body_parsed")),
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
            manifest["done"][job["key"]] = {"status": "failed", "error": last_error, "attempts": MAX_ATTEMPTS}
            results.append({**job, "status": "failed", "error": last_error})
        manifest["complete"] = False
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [item for item in results if item["status"] == "failed"]
    manifest["complete"] = not failed
    manifest["verified_against_dms"] = False
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "complete": not failed,
        "verified_against_dms": False,
        "results": results,
        "failed": [item["key"] for item in failed],
        "schedule_id": SCHEDULE_ID,
        "time": SCHEDULE_TIME,
    }


def _spans(as_of: date) -> dict:
    quarter = quarter_compare(as_of)
    year = year_compare(as_of)
    return {
        "quarter": quarter["current"],
        "quarter_base": quarter["base"],
        "year": year["current"],
        "year_base": year["base"],
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
