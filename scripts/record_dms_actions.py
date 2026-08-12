#!/usr/bin/env python3
"""Record DMS UI actions for mhero_district_form.

Uses in-page step buffer + polling (more reliable than CDP expose_function
when re-attaching to an already-open Chromium).
"""

from __future__ import annotations

import faulthandler
faulthandler.enable()

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
ACC_SCRIPTS = Path("/Users/i/myCode/m-hero/accident-vehicle-reminder/scripts")
sys.path.insert(0, str(ACC_SCRIPTS))

os.environ.setdefault("DFMC_DMS_SESSION_HOME", str(Path.home() / "dms-shared-session"))

from dfmc_browser_utils import (  # noqa: E402
    connect_browser_over_cdp,
    ensure_cdp_browser_running,
    find_dms_page,
)
from time_utils import beijing_strftime  # noqa: E402
import record_dms_actions as base  # noqa: E402

RECORDINGS_DIR = ROOT / "recordings"


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        fh.flush()


def force_inject(page) -> str:
    try:
        page.evaluate(
            """() => {
              window.__DMS_RECORDER_INSTALLED__ = false;
              window.__DMS_RECORDED_STEPS__ = window.__DMS_RECORDED_STEPS__ || [];
              return true;
            }"""
        )
    except Exception as exc:
        return f"clear-failed:{exc}"
    try:
        result = page.evaluate(base.INJECT_JS)
        return str(result)
    except Exception as exc:
        return f"inject-failed:{exc}"


def _install_signal_logs() -> None:
    import signal
    def _handler(signum, frame):
        print(f"[signal] received {signum}", flush=True)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(s, _handler)
        except Exception:
            pass


def main() -> int:
    _install_signal_logs()
    parser = argparse.ArgumentParser(description="录制 DMS 操作到 mhero_district_form")
    parser.add_argument("--stop-file", default="")
    parser.add_argument("--session-name", default="district-form")
    args = parser.parse_args()

    stop_file = Path(args.stop_file) if args.stop_file else None
    if stop_file and stop_file.exists():
        stop_file.unlink()

    session_home = Path(os.environ["DFMC_DMS_SESSION_HOME"]).expanduser().resolve()
    state_file = session_home / ".runtime" / "browser-state.json"
    port = ensure_cdp_browser_running(state_file, session_home)

    stamp = beijing_strftime("%Y%m%d%H%M%S")
    session_dir = RECORDINGS_DIR / f"{stamp}-{args.session_name}"
    session_dir.mkdir(parents=True, exist_ok=False)
    events_path = session_dir / "events.jsonl"
    summary_path = session_dir / "summary.json"

    collected: list[dict[str, Any]] = []
    stop = threading.Event()
    lock = threading.Lock()

    def remember(step: dict[str, Any]) -> None:
        with lock:
            collected.append(step)
            append_jsonl(events_path, step)

    print("=" * 60, flush=True)
    print("区域报表 DMS 录制已就绪", flush=True)
    print(f"CDP 端口: {port}", flush=True)
    print(f"输出目录: {session_dir}", flush=True)
    print("请从首页完整操作（含服务管理报表年份/季度）。", flush=True)
    if stop_file:
        print(f"停止文件: {stop_file}", flush=True)
    print("=" * 60, flush=True)

    def wait_stop() -> None:
        if not stop_file:
            while not stop.is_set():
                time.sleep(0.5)
            return
        while not stop.is_set():
            if stop_file.exists():
                break
            time.sleep(0.3)
        stop.set()

    threading.Thread(target=wait_stop, daemon=True).start()

    with sync_playwright() as pw:
        browser = connect_browser_over_cdp(pw, port)
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            print("[ERROR] 没有可用 browser context", flush=True)
            return 1

        page = find_dms_page(context) or (context.pages[0] if context.pages else None)
        if page is None:
            print("[ERROR] 没有 DMS 页面", flush=True)
            return 1

        def on_download(download) -> None:
            step = {
                "type": "download",
                "suggested_filename": download.suggested_filename,
                "url": download.url,
                "ts": int(time.time() * 1000),
                "page_url": page.url,
            }
            remember(step)
            print(f"[download] {download.suggested_filename}", flush=True)

        for p in context.pages:
            try:
                p.on("download", on_download)
            except Exception:
                pass
        context.on("page", lambda p: p.on("download", on_download))

        inject_result = force_inject(page)
        print(f"[recorder] inject={inject_result} url={page.url}", flush=True)
        remember({
            "type": "recorder_started",
            "href": page.url,
            "title": page.title(),
            "ts": int(time.time() * 1000),
            "inject": inject_result,
        })

        # Self-check: push a synthetic marker into the page buffer and drain it.
        try:
            page.evaluate(
                """() => {
                  window.__DMS_RECORDED_STEPS__ = window.__DMS_RECORDED_STEPS__ || [];
                  window.__DMS_RECORDED_STEPS__.push({
                    type: 'self_check',
                    href: location.href,
                    title: document.title || '',
                    ts: Date.now()
                  });
                  return window.__DMS_RECORDER_INSTALLED__ === true;
                }"""
            )
            for step in base.drain_steps(page):
                remember(step)
                print(f"[{step.get('type')}] self-check ok", flush=True)
        except Exception as exc:
            print(f"[recorder] self-check failed: {exc}", flush=True)

        last_url = page.url
        last_heartbeat = time.time()
        print("[recorder] 开始监听…", flush=True)

        while not stop.is_set():
            try:
                current = find_dms_page(context) or page
                if current is not page:
                    page = current
                    print(f"[recorder] switched page -> {page.url[:120]}", flush=True)
                    print(f"[recorder] inject={force_inject(page)}", flush=True)
                    try:
                        page.on("download", on_download)
                    except Exception:
                        pass

                if page.url != last_url:
                    remember({
                        "type": "url_changed",
                        "from": last_url,
                        "to": page.url,
                        "ts": int(time.time() * 1000),
                    })
                    print(f"[nav] {page.url[:120]}", flush=True)
                    last_url = page.url
                    print(f"[recorder] inject={force_inject(page)}", flush=True)

                # Re-inject only if page lost the flag (SPA soft navigations usually keep it).
                base.ensure_injector(page)
                for step in base.drain_steps(page):
                    remember(step)
                    kind = step.get("type")
                    target = step.get("target") or {}
                    label = (
                        target.get("text")
                        or (target.get("attrs") or {}).get("placeholder")
                        or target.get("selector")
                        or ""
                    )
                    print(f"[{kind}] {str(label)[:80]}", flush=True)

                now = time.time()
                if now - last_heartbeat >= 5:
                    print(
                        f"[heartbeat] steps={len(collected)} url={page.url[:100]}",
                        flush=True,
                    )
                    last_heartbeat = now
            except Exception as exc:
                print(f"[recorder] loop error: {exc}", flush=True)
                time.sleep(0.5)
                continue
            time.sleep(0.25)

        try:
            for step in base.drain_steps(page):
                remember(step)
        except Exception:
            pass

    if stop_file and stop_file.exists():
        stop_file.unlink(missing_ok=True)

    summary = {
        "recorded_at": beijing_strftime("%Y-%m-%d %H:%M:%S"),
        "session_name": args.session_name,
        "session_dir": str(session_dir),
        "step_count": len(collected),
        "events_file": str(events_path),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("=" * 60, flush=True)
    print(f"录制结束，共 {len(collected)} 步", flush=True)
    print(f"事件文件: {events_path}", flush=True)
    print(f"摘要: {summary_path}", flush=True)
    print("=" * 60, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
