#!/usr/bin/env python3
"""Crawl DMS reports then build 区域各指标情况一览 Excel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(PLUGIN_ROOT))
load_dotenv(PLUGIN_ROOT / ".env")

from crawl_district_reports import crawl, load_settings, save_settings  # noqa: E402
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


def _persist_and_notify(result: dict, *, dry_run: bool, skip_notify: bool) -> dict:
    result["finishedAt"] = beijing_strftime("%Y-%m-%d %H:%M:%S")
    manifest = PLUGIN_ROOT / "output" / "pipeline_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if dry_run or skip_notify:
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


def run_pipeline(
    *,
    year: int | None = None,
    quarter: int | None = None,
    skip_crawl: bool = False,
    dry_run: bool = False,
    skip_notify: bool | None = None,
) -> dict:
    settings = load_settings()
    year = int(year or settings["year"])
    quarter = int(quarter or settings["quarter"])
    if skip_notify is None:
        # Respect console-saved toggle for launchd / default runs.
        skip_notify = not bool(settings.get("notify_feishu", True))
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
    }

    try:
        if not skip_crawl:
            crawl_result = crawl(year=year, quarter=quarter, output_dir=download_dir, dry_run=dry_run)
            result["crawl"] = crawl_result
            if dry_run:
                result["status"] = "dry_run"
                return _persist_and_notify(result, dry_run=True, skip_notify=True)
            if crawl_result.get("status") not in {"ok", "partial"}:
                result["status"] = f"crawl_{crawl_result.get('status')}"
                return _persist_and_notify(result, dry_run=dry_run, skip_notify=skip_notify)
            if crawl_result.get("status") == "partial":
                print("[WARN] crawl partial, still attempting report build")

        report_path = build_from_download(download_dir, output_dir)
        result["report"] = str(report_path)
        result["status"] = "ok"
        print(f"Report generated: {report_path}")
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        print(f"[ERROR] {exc}")

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
    )
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
