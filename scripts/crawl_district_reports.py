#!/usr/bin/env python3
"""Crawl the 7 DMS source reports needed by 区域报表自动生成.

Replay source: recordings/20260812095608-district-form

Flow:
  1) 保养提醒任务 → Query → Export
  2) 门店库存查询 → Query → 导出
  3) 服务管理报表 → 年份/季度 → Query → 5 个导出按钮
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from playwright.sync_api import Error, Page, sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
load_dotenv(PLUGIN_ROOT / ".env")

from dfmc_browser_utils import (  # noqa: E402
    DEFAULT_TARGET_URL,
    acquire_export_lock,
    connect_browser_over_cdp,
    ensure_cdp_browser_running,
    find_dms_page,
    goto_dms_route,
    get_default_state_file,
    get_export_lock_path,
    get_session_home,
    release_export_lock,
)
from time_utils import beijing_now, beijing_strftime, ensure_beijing_tz  # noqa: E402

ensure_beijing_tz()

DMS_HOST = "m-dms.dfmc.com.cn"
CRAWLER_OWNER = "mhero_district_form"

ROUTE_REMINDER = "/aftermarketMange/customerManagement/maintenanceReminderTask"
ROUTE_STOCK = "/partsModule/branchWarehouseManagement/sparePartsInventoryInquiry"
ROUTE_SERVICE = "/aftermarketMange/reportManagement/serviceManagementReport"

# (汇总表指标列名, 左下角导出按钮文案, 保存文件名前缀)
# 必须先点指标列切换明细表，再点对应「xx导出」；点通用「导出」会下成汇总表。
SERVICE_EXPORTS = (
    ("首保实施率", "首保实施率导出", "首保实施率"),
    ("二保实施率", "二保实施率导出", "二保实施率"),
    ("新保实施率", "新保投保率导出", "新保投保率"),
    ("去年同期交付未新保车辆", "去年同期交付未新保车辆导出", "去年同期交付未新保车辆"),
    ("续保实施率", "续保投保率导出", "续保投保率"),
)

SETTINGS_PATH = PLUGIN_ROOT / "config" / "crawl_settings.json"


def default_year_quarter() -> tuple[int, int]:
    now = beijing_now()
    return now.year, (now.month - 1) // 3 + 1


def load_settings() -> dict[str, Any]:
    year, quarter = default_year_quarter()
    data = {
        "year": year,
        "quarter": quarter,
        "schedule": "08:30",
        "notify_feishu": True,
    }
    if SETTINGS_PATH.exists():
        try:
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    data["year"] = int(data.get("year") or year)
    data["quarter"] = int(data.get("quarter") or quarter)
    data["schedule"] = str(data.get("schedule") or "08:30")
    data["notify_feishu"] = bool(data.get("notify_feishu", True))
    return data


def save_settings(
    year: int,
    quarter: int,
    *,
    notify_feishu: bool | None = None,
    schedule: str | None = None,
) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = load_settings()
    payload["year"] = int(year)
    payload["quarter"] = int(quarter)
    if notify_feishu is not None:
        payload["notify_feishu"] = bool(notify_feishu)
    if schedule is not None:
        payload["schedule"] = str(schedule)
    SETTINGS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def validate_logged_in(page: Page) -> None:
    url = page.url
    if DMS_HOST not in url:
        raise RuntimeError(f"Browser not on DMS site. Current URL: {url}")
    if "/login" in url.lower():
        raise RuntimeError("Browser is on the login page. Log in first.")
    if page.locator("input[type='password']").count() > 0:
        raise RuntimeError("Login page detected. Log in first.")


def navigate_hash(page: Page, route: str) -> None:
    landed = goto_dms_route(page, route)
    print(f"  Opened {route} ({landed[:100]})")


def click_by_texts(page: Page, texts: list[str], selectors: str = "button, .el-button, a, span, li, div") -> str:
    payload = {"texts": texts, "selectors": selectors}
    clicked = page.evaluate(
        """(payload) => {
          const wanted = payload.texts.map((t) => String(t).replace(/\\s+/g, '').toLowerCase());
          const nodes = document.querySelectorAll(payload.selectors);
          for (const node of nodes) {
            const text = (node.innerText || node.textContent || '').replace(/\\s+/g, '');
            if (!text) continue;
            const lower = text.toLowerCase();
            for (const w of wanted) {
              if (lower === w || lower.includes(w)) {
                node.click();
                return text;
              }
            }
          }
          return '';
        }""",
        payload,
    )
    return clicked or ""


def click_query(page: Page) -> None:
    clicked = click_by_texts(
        page,
        ["查询", "query"],
        "section.mixButton button, .u-btn-right button, .right-btn button, button.el-button",
    )
    if clicked:
        print(f"  Clicked query ({clicked!r})")
        try:
            page.wait_for_selector(".el-table__body-wrapper tbody tr, .el-table__row", timeout=20_000)
        except Error:
            print("  [WARN] table rows not visible within 20s")
        page.wait_for_timeout(1000)
    else:
        print("  [INFO] query button not found, continue")


def set_download_dir(cdp_session: Any, output_dir: Path) -> None:
    try:
        cdp_session.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(output_dir), "eventsEnabled": True},
        )
    except Exception as exc:
        print(f"  [WARN] setDownloadBehavior failed: {exc}")


def wait_new_download(output_dir: Path, before: dict[str, float], timeout_s: float = 180) -> Optional[Path]:
    deadline = time.monotonic() + timeout_s
    new_file: Optional[Path] = None
    stable_size: Optional[int] = None
    stable_since = 0.0
    while time.monotonic() < deadline:
        time.sleep(0.5)
        for path in output_dir.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.name.startswith("crawl_manifest"):
                continue
            if path.suffix.lower() not in {".xlsx", ".xls", ".csv", ".crdownload"}:
                continue
            if path.name in before:
                try:
                    if path.stat().st_mtime <= before[path.name]:
                        continue
                except OSError:
                    continue
            if path.suffix.lower() == ".crdownload":
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= 0:
                continue
            if new_file is None or path != new_file:
                new_file = path
                stable_size = size
                stable_since = time.monotonic()
                continue
            if size == stable_size and time.monotonic() - stable_since >= 1.5:
                return path
    return new_file


def click_named_button(page: Page, label: str, selectors: str) -> str:
    """Playwright click for Element UI buttons (JS click is unreliable for some controls)."""
    wanted = re.sub(r"\s+", "", label).lower()
    loc = page.locator(selectors)
    count = loc.count()
    for i in range(count):
        btn = loc.nth(i)
        try:
            text = re.sub(r"\s+", "", (btn.inner_text() or "")).lower()
        except Error:
            continue
        if text == wanted or wanted in text:
            btn.click(force=True)
            return (btn.inner_text() or label).strip()
    # fallback evaluate exact-ish match
    clicked = click_by_texts(page, [label], selectors)
    return clicked


def click_export_and_save(
    page: Page,
    output_dir: Path,
    *,
    button_texts: list[str],
    save_prefix: str,
) -> Optional[Path]:
    before = {f.name: f.stat().st_mtime for f in output_dir.iterdir() if f.is_file()}
    clicked = ""
    for text in button_texts:
        clicked = click_named_button(
            page,
            text,
            "section.mixButton .u-btn-left button, .u-btn-left button, section.mixButton button",
        )
        if clicked:
            break
    if not clicked:
        raise RuntimeError(f"导出按钮未找到: {button_texts}")
    print(f"  Clicked export ({clicked!r}), waiting...")
    new_file = wait_new_download(output_dir, before)
    if new_file is None:
        print("  [WARN] no download within timeout")
        return None
    stamp = beijing_strftime("%Y%m%d_%H%M%S")
    target = output_dir / f"{save_prefix}{stamp}{new_file.suffix.lower() or '.xlsx'}"
    try:
        if new_file != target:
            new_file.rename(target)
        print(f"  Saved: {target.name}")
        return target
    except OSError as exc:
        print(f"  Rename failed ({exc}), keep {new_file}")
        return new_file


def select_service_metric(page: Page, column_label: str, export_label: str) -> None:
    """Click summary-table leaf column cell to switch detail pane + export button."""
    summary = page.locator(".el-table").first
    header = summary.locator("thead th.is-leaf").filter(has_text=re.compile(f"^{re.escape(column_label)}$"))
    if header.count() == 0:
        header = summary.locator("thead th").filter(has_text=column_label)
    if header.count() == 0:
        raise RuntimeError(f"未找到指标列: {column_label}")

    cls = header.first.get_attribute("class") or ""
    match = re.search(r"(el-table_\d+_column_\d+(?:_column_\d+)?)", cls)
    if not match:
        raise RuntimeError(f"无法解析指标列 class: {column_label} ({cls})")
    token = match.group(1)
    cell = summary.locator(f"tbody tr").first.locator(f"td.{token}")
    if cell.count() == 0:
        cell = page.locator(f"td.{token}")
    if cell.count() == 0:
        raise RuntimeError(f"未找到指标单元格: {column_label} ({token})")
    target = cell.first
    target.scroll_into_view_if_needed()
    target.click(force=True)
    page.wait_for_timeout(800)

    # Wait until the named export button appears (must not be the generic 导出).
    deadline = time.monotonic() + 15
    texts: list[str] = []
    want = export_label.replace(" ", "")
    while time.monotonic() < deadline:
        texts = page.evaluate(
            """() => [...document.querySelectorAll('.u-btn-left button')]
              .map((b) => (b.innerText || '').replace(/\\s+/g, ''))"""
        )
        if any(want in (t or "") for t in texts):
            print(f"  Metric ready: {column_label} -> {export_label}")
            return
        page.wait_for_timeout(300)
    raise RuntimeError(f"切换指标后未出现导出按钮 {export_label}（当前: {texts}）")


def set_service_year_quarter(page: Page, year: int, quarter: int) -> None:
    """Element UI year picker needs a real Playwright click; JS click() won't open the panel."""
    print(f"  Set year={year}, quarter={quarter}")
    year_input = page.locator("#datePicker input.el-input__inner, #datePicker input").first
    year_input.wait_for(state="visible", timeout=20_000)
    year_input.click(force=True)
    page.wait_for_selector(".el-picker-panel.el-date-picker", state="visible", timeout=10_000)
    page.wait_for_timeout(300)

    year_cell = page.locator(
        ".el-picker-panel.el-date-picker:visible .el-year-table td.available a.cell, "
        ".el-picker-panel.el-date-picker:visible .el-year-table a.cell"
    ).filter(has_text=str(year))
    if year_cell.count() == 0:
        # Decade navigation if needed
        for _ in range(6):
            header = page.locator(".el-picker-panel.el-date-picker:visible .el-date-picker__header").inner_text()
            print(f"  Year panel header: {header!r}")
            nums = [int(x) for x in __import__("re").findall(r"\d{4}", header)]
            if nums and year < min(nums):
                page.locator(".el-picker-panel.el-date-picker:visible .el-date-picker__prev-btn").first.click()
            elif nums and year > max(nums):
                page.locator(".el-picker-panel.el-date-picker:visible .el-date-picker__next-btn").first.click()
            else:
                break
            page.wait_for_timeout(250)
            year_cell = page.locator(
                ".el-picker-panel.el-date-picker:visible .el-year-table td.available a.cell, "
                ".el-picker-panel.el-date-picker:visible .el-year-table a.cell"
            ).filter(has_text=str(year))
            if year_cell.count():
                break
    if year_cell.count() == 0:
        raise RuntimeError(f"未找到年份选项: {year}")
    year_cell.first.click(force=True)
    print(f"  Selected year {year}")
    page.wait_for_timeout(400)

    # 3rd filter column = quarter (placeholder "Please Choose"), per recording
    quarter_input = page.locator(
        "div.el-row >> nth=0 >> div.el-col-6 >> nth=2 >> .el-select input.el-input__inner"
    )
    if quarter_input.count() == 0:
        quarter_input = page.locator("div.table-col div.el-col-6").nth(2).locator(".el-select input")
    quarter_input.first.click(force=True)
    page.wait_for_timeout(400)
    q_item = page.locator(
        ".el-select-dropdown.el-popper:visible li.el-select-dropdown__item"
    ).filter(has_text=str(quarter))
    if q_item.count() == 0:
        # fallback: exact text match via evaluate on visible dropdown
        clicked = page.evaluate(
            """(quarter) => {
              const items = [...document.querySelectorAll('.el-select-dropdown.el-popper')]
                .filter((d) => getComputedStyle(d).display !== 'none')
                .flatMap((d) => [...d.querySelectorAll('li.el-select-dropdown__item')]);
              for (const item of items) {
                const text = (item.innerText || '').trim();
                if (text === String(quarter)) { item.click(); return text; }
              }
              return '';
            }""",
            quarter,
        )
        if not clicked:
            raise RuntimeError(f"未找到季度选项: {quarter}")
        print(f"  Selected quarter {clicked}")
    else:
        q_item.first.click(force=True)
        print(f"  Selected quarter {quarter}")
    page.wait_for_timeout(500)


def crawl_simple_export(
    page: Page,
    *,
    route: str,
    title: str,
    export_texts: list[str],
    save_prefix: str,
    output_dir: Path,
) -> Optional[Path]:
    print(f"==> {title}")
    navigate_hash(page, route)
    click_query(page)
    return click_export_and_save(page, output_dir, button_texts=export_texts, save_prefix=save_prefix)


def crawl_service_reports(
    page: Page,
    *,
    year: int,
    quarter: int,
    output_dir: Path,
) -> dict[str, Optional[Path]]:
    print("==> 服务管理报表")
    navigate_hash(page, ROUTE_SERVICE)
    set_service_year_quarter(page, year, quarter)
    click_query(page)
    results: dict[str, Optional[Path]] = {}
    for column_label, export_label, prefix in SERVICE_EXPORTS:
        print(f"  Exporting {prefix}...")
        select_service_metric(page, column_label, export_label)
        path = click_export_and_save(
            page,
            output_dir,
            button_texts=[export_label],
            save_prefix=prefix,
        )
        results[prefix] = path
        page.wait_for_timeout(800)
    return results


def crawl(
    *,
    year: int | None = None,
    quarter: int | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    settings = load_settings()
    year = int(year or settings["year"])
    quarter = int(quarter or settings["quarter"])
    if quarter < 1 or quarter > 4:
        raise ValueError("quarter must be 1..4")

    out_dir = output_dir or (PLUGIN_ROOT / "download")
    out_dir.mkdir(parents=True, exist_ok=True)
    state_file = get_default_state_file(PLUGIN_ROOT)

    print(f"Session home: {get_session_home(PLUGIN_ROOT)}")
    print(f"Query year/quarter: {year}/Q{quarter}")
    cdp_port = ensure_cdp_browser_running(state_file, plugin_root=PLUGIN_ROOT)
    print(f"Browser CDP port: {cdp_port}")

    files: dict[str, str] = {}
    status = "unknown"

    lock = None
    try:
        with sync_playwright() as pw:
            browser = connect_browser_over_cdp(pw, cdp_port)
            context = browser.contexts[0]
            context.set_default_timeout(15_000)
            # Register before touching DMS page so keepalive cannot refresh mid-prep.
            lock = acquire_export_lock(
                PLUGIN_ROOT, CRAWLER_OWNER, schedule_id="district-form"
            )
            try:
                page = find_dms_page(context)
                if page is None:
                    page = context.new_page()
                    page.goto(DEFAULT_TARGET_URL, wait_until="domcontentloaded", timeout=20_000)
                    page.wait_for_timeout(1500)
                validate_logged_in(page)

                if dry_run:
                    status = "dry_run"
                else:
                    # Clear previous source workbooks so report build only sees this run.
                    for old in out_dir.glob("*"):
                        if old.name.startswith("crawl_manifest"):
                            continue
                        if old.suffix.lower() in {".xlsx", ".xls", ".csv", ".crdownload"} or old.name.startswith("~$"):
                            try:
                                old.unlink()
                            except OSError:
                                pass
                    cdp_session = context.new_cdp_session(page)
                    set_download_dir(cdp_session, out_dir)
                    p1 = crawl_simple_export(
                        page,
                        route=ROUTE_REMINDER,
                        title="保养提醒任务",
                        export_texts=["Export", "导出"],
                        save_prefix="保养提醒任务列表",
                        output_dir=out_dir,
                    )
                    p2 = crawl_simple_export(
                        page,
                        route=ROUTE_STOCK,
                        title="门店库存查询",
                        export_texts=["导出", "Export"],
                        save_prefix="门店备件库存导出",
                        output_dir=out_dir,
                    )
                    service_files = crawl_service_reports(
                        page, year=year, quarter=quarter, output_dir=out_dir
                    )
                    for path in (p1, p2, *service_files.values()):
                        if path:
                            files[path.name] = str(path)
                    expected = 7
                    status = "ok" if len(files) >= expected else "partial"
            finally:
                release_export_lock(lock, owner=CRAWLER_OWNER, plugin_root=PLUGIN_ROOT)
    except Exception as exc:
        print(f"Fatal error: {exc}")
        status = "fatal_error"
        try:
            release_export_lock(
                get_export_lock_path(PLUGIN_ROOT),
                owner=CRAWLER_OWNER,
                plugin_root=PLUGIN_ROOT,
            )
        except Exception:
            pass

    manifest = {
        "crawledAt": beijing_strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "year": year,
        "quarter": quarter,
        "files": files,
        "fileCount": len(files),
    }
    manifest_path = out_dir / "crawl_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest: {manifest_path}")
    print(f"Status: {status}, files={len(files)}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl 7 DMS reports for district form")
    parser.add_argument("--year", type=int, default=0)
    parser.add_argument("--quarter", type=int, default=0)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--save-settings", action="store_true", help="Persist year/quarter to config")
    args = parser.parse_args()

    year = args.year or None
    quarter = args.quarter or None
    if args.save_settings and year and quarter:
        save_settings(year, quarter)

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    result = crawl(year=year, quarter=quarter, output_dir=output_dir, dry_run=args.dry_run)
    return 0 if result.get("status") in {"ok", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
