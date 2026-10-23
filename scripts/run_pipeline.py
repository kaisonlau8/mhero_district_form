#!/usr/bin/env python3
"""Crawl DMS reports then build 区域各指标情况一览 Excel."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(PLUGIN_ROOT))
load_dotenv(PLUGIN_ROOT / ".env")

from crawl_district_reports import crawl, load_settings, save_settings  # noqa: E402
from pipeline_fallback import (  # noqa: E402
    format_crawl_error,
    recover_dms_session,
    redact_text,
    resolve_backoff_seconds,
    resolve_max_attempts,
    should_notify_pipeline,
    will_retry_crawl,
)
from time_utils import beijing_strftime, ensure_beijing_tz  # noqa: E402

ensure_beijing_tz()


def collect_source_files(download_dir: Path) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for path in sorted(download_dir.glob("*.xlsx")):
        if path.name.startswith("~$"):
            continue
        files.append((path.name, path.read_bytes()))
    return files


def build_from_download(download_dir: Path, output_dir: Path) -> Path:
    from app.processor import ReportBuildError, build_report
    from app.runtime import assets_dir

    template_path = assets_dir() / "report_template.xlsx"
    if not template_path.exists():
        raise FileNotFoundError(f"Missing template: {template_path}")

    uploaded = collect_source_files(download_dir)
    report_bytes, output_name = build_report(
        uploaded,
        template_path.read_bytes(),
        template_path.name,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / output_name
    # Avoid overwrite collisions within the same day.
    if out_path.exists():
        stem = out_path.stem
        out_path = output_dir / f"{stem}_{beijing_strftime('%H%M%S')}.xlsx"
    out_path.write_bytes(report_bytes)
    return out_path


def _persist_and_notify(
    result: dict,
    *,
    dry_run: bool,
    skip_notify: bool,
    will_retry: bool = False,
) -> dict:
    result["finishedAt"] = beijing_strftime("%Y-%m-%d %H:%M:%S")
    manifest = PLUGIN_ROOT / "output" / "pipeline_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not should_notify_pipeline(dry_run=dry_run, skip_notify=skip_notify, will_retry=will_retry):
        return result

    try:
        from feishu_webhook import notify_pipeline_result

        notify = notify_pipeline_result(result)
        result["feishu"] = notify
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"[WARN] feishu notify failed: {exc}")
        result["feishu"] = {"ok": False, "error": str(exc)}
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _normalize_recovery(recovery: object) -> dict:
    if not isinstance(recovery, dict):
        recovery = {"ok": False, "action": "recover", "status": "error", "detail": "bad recovery result"}
    return {
        "ok": bool(recovery.get("ok")),
        "action": str(recovery.get("action") or ""),
        "status": str(recovery.get("status") or ""),
        "detail": redact_text(str(recovery.get("detail") or ""))[:300],
    }


def run_pipeline(
    *,
    year: int | None = None,
    quarter: int | None = None,
    skip_crawl: bool = False,
    dry_run: bool = False,
    skip_notify: bool | None = None,
    max_attempts: int | None = None,
    retry_backoff_seconds: float | None = None,
    recover=None,
) -> dict:
    settings = load_settings()
    year = int(year or settings["year"])
    quarter = int(quarter or settings["quarter"])
    if skip_notify is None:
        # Respect console-saved toggle for launchd / default runs.
        skip_notify = not bool(settings.get("notify_feishu", True))
    max_attempts = resolve_max_attempts(max_attempts)
    backoff = resolve_backoff_seconds(retry_backoff_seconds)
    download_dir = PLUGIN_ROOT / "download"
    output_dir = PLUGIN_ROOT / "output"

    result = {
        "startedAt": beijing_strftime("%Y-%m-%d %H:%M:%S"),
        "year": year,
        "quarter": quarter,
        "status": "unknown",
        "crawl": {},
        "report": "",
        "error": "",
        "attempt": 0,
        "maxAttempts": max_attempts,
    }
    try:
        if not skip_crawl:
            if dry_run:
                print("[pipeline] attempt 1/1 dry-run")
                crawl_result = crawl(year=year, quarter=quarter, output_dir=download_dir, dry_run=True)
                result["crawl"] = crawl_result
                result["attempt"] = 1
                result["status"] = "dry_run"
                print("[pipeline] final status=dry_run attempts=1")
                return _persist_and_notify(result, dry_run=True, skip_notify=True)

            crawl_result: dict = {}
            for attempt in range(1, max_attempts + 1):
                print(f"[pipeline] attempt {attempt}/{max_attempts}")
                try:
                    crawl_result = crawl(
                        year=year,
                        quarter=quarter,
                        output_dir=download_dir,
                        dry_run=False,
                    )
                except Exception as exc:
                    crawl_result = {
                        "status": "fatal_error",
                        "error": format_crawl_error(exc),
                        "year": year,
                        "quarter": quarter,
                        "files": {},
                        "fileCount": 0,
                    }
                    print(f"Fatal error: {crawl_result['error']}")
                if not isinstance(crawl_result, dict):
                    crawl_result = {
                        "status": "fatal_error",
                        "error": "crawl returned no manifest",
                        "fileCount": 0,
                        "files": {},
                    }
                result["crawl"] = crawl_result
                result["attempt"] = attempt
                status = str(crawl_result.get("status") or "")
                if status in {"ok", "partial"}:
                    if status == "partial":
                        print("[WARN] crawl partial, still attempting report build")
                    break

                err = redact_text(str(crawl_result.get("error") or status))[:500]
                retry = will_retry_crawl(
                    attempt=attempt,
                    max_attempts=max_attempts,
                    crawl=crawl_result,
                )
                if not retry:
                    print(f"[pipeline] attempt {attempt}/{max_attempts} failed: {err} (no retry)")
                    result["status"] = f"crawl_{status}"
                    result["error"] = err
                    print(f"[pipeline] final status={result['status']} attempts={attempt}")
                    return _persist_and_notify(result, dry_run=False, skip_notify=skip_notify)

                print(f"[pipeline] attempt {attempt}/{max_attempts} failed: {err} (recoverable, will retry)")
                recover_fn = recover if recover is not None else recover_dms_session
                try:
                    recovery = _normalize_recovery(recover_fn(PLUGIN_ROOT))
                except Exception as exc:
                    recovery = _normalize_recovery(
                        {
                            "ok": False,
                            "action": "recover",
                            "status": "error",
                            "detail": format_crawl_error(exc),
                        }
                    )
                result["recovery"] = recovery
                print(
                    f"[pipeline] recovery action={recovery['action']} "
                    f"status={recovery['status']} detail={recovery['detail']}"
                )
                if not recovery["ok"]:
                    print("[pipeline] recovery unusable; not retrying")
                    result["status"] = f"crawl_{status}"
                    result["error"] = (
                        f"{err}; recovery unusable: action={recovery['action']} "
                        f"status={recovery['status']} detail={recovery['detail']}"
                    )
                    print(f"[pipeline] final status={result['status']} attempts={attempt}")
                    return _persist_and_notify(result, dry_run=False, skip_notify=skip_notify)

                result["status"] = "retrying"
                result["error"] = err
                _persist_and_notify(result, dry_run=False, skip_notify=True, will_retry=True)
                if backoff > 0:
                    print(f"[pipeline] backoff {backoff:g}s before attempt {attempt + 1}")
                    time.sleep(backoff)
            else:
                status = str((crawl_result or {}).get("status") or "fatal_error")
                result["status"] = f"crawl_{status}"
                result["error"] = redact_text(str((crawl_result or {}).get("error") or "crawl failed"))[:500]
                print(f"[pipeline] final status={result['status']} attempts={result.get('attempt')}")
                return _persist_and_notify(result, dry_run=False, skip_notify=skip_notify)

            if str(crawl_result.get("status") or "") not in {"ok", "partial"}:
                result["status"] = f"crawl_{crawl_result.get('status')}"
                result["error"] = redact_text(str(crawl_result.get("error") or ""))[:500]
                print(f"[pipeline] final status={result['status']} attempts={result.get('attempt')}")
                return _persist_and_notify(result, dry_run=False, skip_notify=skip_notify)

        # --- 报表引擎：MHERO_REPORT_ENGINE=reportdb → 入库 + 系统计算 + 系统导出日报（不再用 Excel 模板公式）；默认 excel（旧路径）---
        engine = os.getenv("MHERO_REPORT_ENGINE", "excel").strip().lower()
        if engine == "reportdb":
            from reportdb.pipeline import run as reportdb_run

            rdb = reportdb_run(download_dir, output_dir)
            result["engine"] = "reportdb"
            result["reportdb"] = {k: v for k, v in rdb.items() if k != "report"}
            report_path = Path(rdb["report"])
        else:
            report_path = build_from_download(download_dir, output_dir)
        # --- /engine ---
        result["report"] = str(report_path)
        result["status"] = "ok"
        result["error"] = ""
        print(f"Report generated: {report_path}")
        attempts = result.get("attempt") or (0 if skip_crawl else 1)
        print(f"[pipeline] final status=ok attempts={attempts}")
        # --- reportdb 可选钩子：默认关闭，MHERO_REPORTDB=1 才执行；engine=reportdb 时已隐含入库，不再重复；失败只记录，不影响 Excel/推送 ---
        if engine != "reportdb" and os.getenv("MHERO_REPORTDB", "").strip().lower() in {"1", "true", "yes", "on"}:
            try:
                from reportdb.ingest import ingest_after_pipeline

                result["reportdb"] = ingest_after_pipeline(download_dir)
                print(f"[reportdb] {result['reportdb']}")
            except Exception as exc:  # noqa: BLE001
                result["reportdb"] = {"ok": False, "error": str(exc)}
                print(f"[WARN] reportdb ingest failed: {exc}")
        # --- /reportdb ---
    except Exception as exc:
        result["status"] = "error"
        result["error"] = redact_text(str(exc))[:500]
        print(f"[ERROR] {result['error']}")
        print(f"[pipeline] final status=error attempts={result.get('attempt') or 0}")

    return _persist_and_notify(result, dry_run=dry_run, skip_notify=skip_notify)


def main() -> int:
    parser = argparse.ArgumentParser(description="District form daily pipeline")
    parser.add_argument("--year", type=int, default=0)
    parser.add_argument("--quarter", type=int, default=0)
    parser.add_argument("--skip-crawl", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--save-settings", action="store_true")
    parser.add_argument("--skip-notify", action="store_true", help="Skip Feishu webhook")
    parser.add_argument("--notify", action="store_true", help="Force Feishu webhook notify")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=0,
        help="Full crawl attempts including the first (default 2, cap 3). 1 disables retry.",
    )
    args = parser.parse_args()

    year = args.year or None
    quarter = args.quarter or None
    if args.save_settings and year and quarter:
        # Preserve notify toggle unless explicitly forced by flags in same call.
        notify_flag = None
        if args.notify:
            notify_flag = True
        elif args.skip_notify:
            notify_flag = False
        save_settings(year, quarter, notify_feishu=notify_flag)

    if args.notify and args.skip_notify:
        print("[WARN] both --notify and --skip-notify set; using --skip-notify")
        skip_notify: bool | None = True
    elif args.notify:
        skip_notify = False
    elif args.skip_notify:
        skip_notify = True
    else:
        skip_notify = None

    result = run_pipeline(
        year=year,
        quarter=quarter,
        skip_crawl=args.skip_crawl,
        dry_run=args.dry_run,
        skip_notify=skip_notify,
        max_attempts=args.max_attempts or None,
    )
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
