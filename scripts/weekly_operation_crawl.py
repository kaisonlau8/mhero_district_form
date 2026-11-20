#!/usr/bin/env python3
"""周五下载服务经营报表（收款 · 日报）并导入周报。

默认只打印计划，不连接 DMS。现场要真正下载时加 --live。
云端不要加 --live。本脚本沿用每日爬虫的浏览器锁、页面跳转和下载等待，
不调用 DMS 内部接口。每天 08:30 的流水线不在这里改。

现场还没跑通整段页面时，结果里的 verified_against_dms 保持 false。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from reportdb.db import connect, migrate  # noqa: E402
from reportdb.weekly_crawl import (  # noqa: E402
    DMS_BROWSER_DOC,
    SCHEDULE_ID,
    SCHEDULE_TIME,
    SCHEDULE_WEEKDAY,
    WeeklyCrawlError,
    close_owned_session,
    describe_plan,
    prepare_export,
    run_weekly_crawl,
)
from time_utils import beijing_now, ensure_beijing_tz  # noqa: E402

ensure_beijing_tz()

# 只用来进入已经录过的报表菜单，再点可见的「服务经营报表」。
# 不猜经营报表自己的地址，也没有内部接口。
MENU_ANCHOR = "/aftermarketMange/reportManagement/serviceManagementReport"
MENU_LABEL = "服务经营报表"
CRAWLER_OWNER = "mhero_district_form"


class PlaywrightForm:
    """用页面上看得见的表单项操作。读不回就不许查询或导出。"""

    def __init__(self, page):
        self.page = page

    def set_filters(self, filters: dict) -> None:
        for label, value in filters.items():
            _set_labeled(self.page, label, value)

    def read_filters(self) -> dict:
        return self.page.evaluate(
            """() => {
              const out = {};
              for (const item of document.querySelectorAll('.el-form-item')) {
                const label = (item.querySelector('.el-form-item__label')?.innerText || '')
                  .replace(/\\s+/g, '').replace(/[:：]/g, '');
                if (!label) continue;
                const input = item.querySelector('input, textarea');
                out[label] = input ? (input.value || '') : '';
              }
              return out;
            }"""
        )

    def query(self) -> None:
        from crawl_district_reports import click_query
        click_query(self.page)


def _set_labeled(page, label: str, value: str) -> None:
    item = page.locator(".el-form-item").filter(has_text=label).first
    item.wait_for(state="visible", timeout=15_000)
    select = item.locator(".el-select")
    if select.count():
        select.locator("input").first.click(force=True)
        page.wait_for_timeout(300)
        if value == "":
            clear = item.locator(".el-icon-circle-close, .el-select__caret.is-reverse").first
            if clear.count():
                clear.click(force=True)
            else:
                select.locator("input").first.fill("")
            page.keyboard.press("Escape")
            return
        option = page.locator(".el-select-dropdown:visible li").filter(has_text=value)
        if option.count() == 0:
            raise WeeklyCrawlError(f"下拉里没有「{label}」的选项「{value}」")
        option.first.click(force=True)
        page.wait_for_timeout(200)
        return
    field = item.locator("input").first
    field.click(force=True)
    field.fill(value)
    field.press("Enter")
    page.wait_for_timeout(200)


def open_operation_page(page) -> None:
    from crawl_district_reports import click_by_texts, navigate_hash

    navigate_hash(page, MENU_ANCHOR)
    clicked = click_by_texts(page, [MENU_LABEL], ".el-menu-item, .el-submenu__title, a, span, li")
    if not clicked:
        raise WeeklyCrawlError(f"菜单里没有「{MENU_LABEL}」。不会改走接口。")
    page.wait_for_selector("section.mixButton", timeout=15_000)


def _event_from_response(resp) -> dict:
    """只记录页面自己已经发出的响应，不另行请求。"""
    body = None
    try:
        body = resp.json()
    except Exception:
        body = None
    request = resp.request
    post_data = ""
    try:
        post_data = request.post_data or ""
    except Exception:
        post_data = ""
    try:
        status = int(resp.status)
    except Exception:
        status = 0
    return {"status": status, "url": resp.url or "", "post_data": post_data, "body": body}


def live_fetch(page, output_dir: Path):
    from crawl_district_reports import click_export_and_save
    from reportdb.weekly_crawl import classify_page_response, confirm_query_responses, fresh_download

    opened = {"done": False}
    seq = {"n": 0}

    def fetch(job):
        if not opened["done"]:
            open_operation_page(page)
            opened["done"] = True
        seq["n"] += 1
        query_id = f"{job['key']}#{seq['n']}"
        seen = []

        def on_response(resp):
            seen.append(_event_from_response(resp))

        page.on("response", on_response)
        try:
            before_files = {path.name: path.read_bytes() for path in output_dir.glob("*.xls*")}
            form = PlaywrightForm(page)
            readback_before, readback_after = prepare_export(form, job["filters"])
            responses = []
            for event in seen:
                item = classify_page_response(event, query_id, job["filters"])
                if item:
                    responses.append(item)
            try:
                confirm_query_responses(job["filters"], query_id, responses)
            except WeeklyCrawlError:
                return {
                    "blob": b"",
                    "exported_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "readback_before": readback_before,
                    "readback_after": readback_after,
                    "query_id": query_id,
                    "responses": responses,
                    "download_is_new": False,
                }
            path = click_export_and_save(
                page, output_dir,
                button_texts=["导出", "Export"],
                save_prefix=f"{job['span']}_{job['scope']}_",
            )
            content = path.read_bytes() if path is not None and path.exists() else b""
            return {
                "blob": content,
                "exported_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "readback_before": readback_before,
                "readback_after": readback_after,
                "query_id": query_id,
                "responses": responses,
                "download_is_new": fresh_download(before_files, path.name if path else "", content),
            }
        finally:
            page.remove_listener("response", on_response)

    return fetch


def run_live(conn, args) -> dict:
    from crawl_district_reports import set_download_dir, validate_logged_in
    from dfmc_browser_utils import (
        acquire_export_lock,
        connect_browser_over_cdp,
        ensure_cdp_browser_running,
        find_dms_page,
        get_default_state_file,
        hard_refresh_dms,
        release_export_lock,
    )
    from playwright.sync_api import sync_playwright

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    regions = _regions(conn, args.regions)
    state_file = get_default_state_file(ROOT)
    lock = None
    try:
        with sync_playwright() as pw:
            port = ensure_cdp_browser_running(state_file, plugin_root=ROOT)
            browser = connect_browser_over_cdp(pw, port)
            context = browser.contexts[0]
            lock = acquire_export_lock(ROOT, CRAWLER_OWNER, timeout_seconds=args.lock_wait, schedule_id=SCHEDULE_ID)
            page = find_dms_page(context)
            if page is None:
                raise WeeklyCrawlError("共享浏览器里没有 DMS 标签。请先在现场登录，不要在云端打开。")
            validate_logged_in(page)
            set_download_dir(context.new_cdp_session(page), output_dir)
            return run_weekly_crawl(
                conn, as_of=args.as_of or beijing_now().date(), regions=regions,
                fetch=live_fetch(page, output_dir), output_dir=output_dir,
            )
    finally:
        def _refresh():
            try:
                hard_refresh_dms(ROOT, label="post-weekly-crawl")
            except Exception as exc:
                print(f"  [WARN] 采集后刷新失败: {exc}")

        close_owned_session(
            lock,
            refresh=_refresh,
            release=lambda held: release_export_lock(held, owner=CRAWLER_OWNER, plugin_root=ROOT),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="周五采集经营报表。不加 --live 不会连接 DMS。")
    parser.add_argument("--live", action="store_true", help="在现场已登录的共享浏览器里真正下载")
    parser.add_argument("--as-of", default="", help="累计截止日 YYYY-MM-DD，默认今天")
    parser.add_argument("--regions", default="", help="逗号分隔。默认用数据库里的现行区域")
    parser.add_argument("--output-dir", default=str(ROOT / "download" / "weekly"))
    parser.add_argument("--db", default="")
    parser.add_argument("--lock-wait", type=float, default=0)
    args = parser.parse_args(argv)
    conn = connect(args.db or None)
    migrate(conn)
    if not args.live:
        regions = _regions(conn, args.regions)
        plan = describe_plan(as_of=args.as_of or beijing_now().date(), regions=regions)
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=_json_default))
        print("未连接 DMS。现场端到端尚未在本环境执行。")
        return 0
    result = run_live(conn, args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    print(f"调度建议：每周五 {SCHEDULE_TIME}，时刻表 id={SCHEDULE_ID}。说明：{DMS_BROWSER_DOC}")
    return 0 if result["complete"] else 2


def _regions(conn, text: str) -> list[str]:
    if text.strip():
        return [item.strip() for item in text.split(",") if item.strip()]
    return [row[0] for row in conn.execute("SELECT name FROM regions ORDER BY sort_order")]


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value))


if __name__ == "__main__":
    raise SystemExit(main())
