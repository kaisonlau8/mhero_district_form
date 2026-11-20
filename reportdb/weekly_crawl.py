"""周五经营采集的计划、筛选核对和导入。这里不打开浏览器。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .weekly_parse import norm_region, quarter_compare, year_compare
from .weekly_report import SnapshotConflict, WeeklyImportError, import_operation

SCHEDULE_ID = "district-form-weekly"
SCHEDULE_TIME = "13:30"
SCHEDULE_WEEKDAY = "friday"
DMS_BROWSER_DOC = "https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md"
SPANS = ("quarter", "quarter_base", "year", "year_base")
EMPTY_MARKS = {"", "请选择", "请输入", "please choose", "please input"}


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
                "key": f"{span}:{scope}",
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
    manifest = _load_manifest(manifest_path) if resume else {"done": {}}
    results = []
    for job in plan_jobs(day, regions):
        previous = manifest["done"].get(job["key"]) or {}
        if resume and previous.get("status") == "imported":
            results.append({**job, "status": "skipped"})
            continue
        try:
            got = fetch(job)
            before = got.get("readback_before")
            after = got.get("readback_after")
            bad = [f"查询前{item}" for item in readback_mismatches(job["filters"], before)]
            bad += [f"查询后{item}" for item in readback_mismatches(job["filters"], after)]
            if bad:
                raise WeeklyCrawlError("筛选读回不一致：" + "；".join(bad))
            blob = got["blob"]
            saved = output_dir / f"{job['span']}_{job['scope']}.xlsx"
            saved.write_bytes(blob)
            meta_path = output_dir / f"{job['span']}_{job['scope']}.json"
            meta_path.write_text(json.dumps({
                "span": job["span"], "scope": job["scope"], "filters": job["filters"],
                "readback_before": before, "readback_after": after,
                "exported_at": got["exported_at"], "file": saved.name,
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
                "file": saved.name,
            }
            results.append({**job, "status": "imported", "idempotent": imported["idempotent"]})
        except (WeeklyCrawlError, WeeklyImportError, SnapshotConflict, OSError, KeyError, RuntimeError) as exc:
            manifest["done"][job["key"]] = {"status": "failed", "error": str(exc)}
            results.append({**job, "status": "failed", "error": str(exc)})
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
