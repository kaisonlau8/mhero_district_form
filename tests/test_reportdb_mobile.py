"""Chromium layout regression with synthetic reportdb data served by TestClient.

Install Playwright and its Chromium, or point REPORTDB_CHROMIUM at an isolated
Chromium binary. Set REPORTDB_MOBILE_SHOTS to save the 390px audit screenshots.
"""
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from reportdb import exemptions, spare
from reportdb.auth import service
from reportdb.ingest import ingest_datasets
from reportdb.webapp import create_app
from reportdb.weekly_report import import_operation
from test_reportdb_calc import D1, make_data
from test_reportdb_exemptions import SNAP1, rows_for, vin_of
from test_reportdb_web import login
from test_reportdb_weekly import _op_meta, east_blob, national_blob


@pytest.fixture(scope="module")
def chromium():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        binary = os.environ.get("REPORTDB_CHROMIUM")
        if not binary and not Path(p.chromium.executable_path).is_file():
            pytest.skip("Install Playwright Chromium or set REPORTDB_CHROMIUM")
        browser = p.chromium.launch(executable_path=binary, headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def audit_site(conn, tmp_path):
    data, _ = make_data(conn)
    data["stock"] = data["stock"] * 3  # 分页首屏超过自动折叠阈值
    for ind, key in [("task", "task"), ("first_service", "first-service"),
                     ("second_service", "second-service"), ("new_policy", "new-policy"), ("renewal", "renewal")]:
        data[key] = rows_for(conn, ind)
    ingest_datasets(conn, SNAP1, data, source="mobile-test", origin_renewal=["renewal"] * len(data["renewal"]))
    uid = service.create_user(conn, "mobile-admin", "mobile-test-password", role="admin")
    supervisor = service.create_user(conn, "mobile-supervisor", "mobile-test-password", role="supervisor",
                                     supervisor_name="测试督导")
    conn.execute("UPDATE stores SET supervisor='测试督导' WHERE name=?", (D1,))
    conn.commit()
    import_operation(conn, national_blob(), **_op_meta())
    import_operation(conn, east_blob(), **_op_meta(filename="east.xlsx"))
    import_operation(conn, east_blob(), **_op_meta(span="quarter_base", period_start="2026-07-01",
                                                  period_end="2026-08-20", as_of="2026-11-20"))
    import_operation(conn, national_blob(), **_op_meta(span="year", period_start="2026-01-01"))
    with TestClient(create_app(tmp_path / "t.sqlite3")) as client:
        login(client, "mobile-admin", "mobile-test-password")
        draft = spare.save_draft(conn, "mobile-admin", "测试备件.xlsx", [
            {"code": "22806000", "name": "测试备件", "counted": 1, "standard_qty": None, "group_name": ""},
        ])
        user = SimpleNamespace(id=uid, username="mobile-admin", role="admin")
        created = exemptions.request_exemptions(conn, user, "first_service", SNAP1,
                                                 vins=[vin_of("first_service", "A1")], reason="测试申请")
        store = conn.execute("SELECT code FROM stores WHERE name=?", (D1,)).fetchone()[0]
        routes = ["/login", "/", *[f"/tables/{slug}" for slug in (
            "summary", "region-indicators", "direct-stores", "spare-parts")],
            *[f"/data/{key}" for key in data], "/stores", f"/stores/{store}",
            "/reports/daily", "/reports/weekly?week=2026-11-16", "/reports/weekly/demo",
            "/reports/weekly/demo?view=year", "/reports/weekly/demo?view=indicators",
            "/reports/weekly/import", "/account/password", "/exemptions/new", "/exemptions/mine",
            "/exclusions/new", "/settings/exclusion-rules", "/settings/spare-parts",
            f"/settings/spare-parts/preview/{draft}", "/admin/users", f"/admin/users/{supervisor}/stores",
            "/admin/exemptions", f"/admin/exemptions/{created['created'][0]}"]
        yield client, routes


@pytest.mark.parametrize("width", [360, 390, 430, 1280])
def test_reportdb_layout(chromium, audit_site, width):
    client, routes = audit_site
    mobile = width < 768
    context = chromium.new_context(viewport={"width": width, "height": 844}, is_mobile=mobile,
                                   has_touch=mobile, reduced_motion="reduce", service_workers="block")
    anon = TestClient(client.app)

    def serve(route):
        url = urlsplit(route.request.url)
        if url.netloc != "testserver":
            return route.abort()
        res = (anon if url.path == "/login" else client).get(url.path + ("?" + url.query if url.query else ""),
                                                             follow_redirects=False)
        route.fulfill(status=res.status_code, headers=dict(res.headers), body=res.content)

    context.route("**/*", serve)
    page = context.new_page()
    failures, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def check(ok, message):
        if not ok:
            failures.append(f"{path}: {message}")

    def fits():
        scroll, view = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        check(view == width and scroll <= view + 1, f"整页宽度 {scroll}/{view}，预期 {width}")

    try:
        for path in routes:
            response = page.goto("http://testserver" + path)
            assert response.status == 200, path
            page.evaluate("document.fonts.ready.then(() => true)")
            fits()
            burger = page.locator(".burger, [data-burger], .nav-toggle")
            visible = page.locator(":is(.burger, [data-burger], .nav-toggle):visible")
            has_nav = path != "/login"
            expect_burger = mobile and has_nav
            check(burger.count() == int(has_nav), "汉堡 DOM 数量不符")
            check(visible.count() == int(expect_burger), "可见汉堡数量不符")
            if expect_burger and visible.count() == 1:
                rect = visible.bounding_box()
                check(rect["y"] < 16 and 0 <= width - rect["x"] - rect["width"] <= 24, "汉堡不在右上")
            else:
                rect = None
            hit = rect and visible.evaluate("el => {const r = el.getBoundingClientRect(); "
                                            "return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));}")
            if rect:
                check(hit, "汉堡触控区被遮住")
            if hit:
                visible.click()
                check(page.locator("#mainmenu").get_attribute("aria-hidden") == "false", "抽屉未打开")
                fits()
                if path == "/":
                    page.get_by_role("button", name="明细数据").click()
                    check(page.locator('#mainmenu a[href^="/data/stock"]').is_visible(), "明细子菜单不可见")
                    page.locator("#nav-backdrop").click(position={"x": 12, "y": 140})
                    check(visible.get_attribute("aria-expanded") == "false", "遮罩未关闭抽屉")
                    visible.click()
                page.keyboard.press("Escape")
                check(visible.get_attribute("aria-expanded") == "false", "Escape 未关闭抽屉")
                check(page.evaluate("document.activeElement.id") == "burger", "关闭抽屉后焦点未归还")
            if path.startswith("/data/"):
                row = page.locator("table.sticky1 tbody tr").first
                check(row.evaluate("el => getComputedStyle(el).display") == ("block" if mobile else "table-row"),
                      "明细未按断点切换卡片/桌面表格")
                check(row.locator("td[data-card-title]").count() == 1 and row.locator("td[data-label]").count() > 1,
                      "明细缺少有数据的标题与字段标签")
                if mobile:
                    check(row.is_visible() and row.bounding_box()["height"] > 30, "主明细卡片被折叠")
                    check(row.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), "明细卡片内部横溢")
            if path in {"/", "/tables/summary"} and mobile:
                sheet = page.locator(".sheet").first
                check(sheet.evaluate("el => el.scrollWidth > el.clientWidth"), "总表应仅在表内横滚")
                check(sheet.locator("tbody tr:first-child td:nth-child(2)").evaluate(
                    "el => getComputedStyle(el).position") == "sticky", "总表区域列未固定")
            if path in {"/reports/weekly/demo", "/reports/weekly/demo?view=year"}:
                check(page.locator(".vbar").count() > 0, "未覆盖有数据的经营竖柱图")
                check(page.locator(".vplot").evaluate_all("els => els.every(el => {"
                      "const r = el.getBoundingClientRect(); return r.right <= innerWidth + 1 && "
                      "el.scrollWidth <= el.clientWidth + 1; })"), "图表容器横溢")
                check(page.locator(".varea").first.evaluate("el => getComputedStyle(el).display")
                      == ("grid" if mobile else "flex"), "经营竖柱未按断点切换布局")
                check(page.locator(".hero-cards").first.evaluate(
                    "el => getComputedStyle(el).gridTemplateColumns.split(' ').length")
                    == (1 if mobile else 3), "经营摘要卡列数不符")
            if path == "/reports/weekly/demo?view=indicators":
                scroll = page.get_by_role("region", name="服务指标表")
                sheet = scroll.locator("table.kpi-sheet")
                assert sheet.count() == 1, "未覆盖有数据的服务指标大表"
                check(sheet.locator("tr.is-nation td.lab").inner_text() == "全国"
                      and sheet.locator("td.bad[data-rate]").count() > 0
                      and sheet.get_by_role("columnheader", name="原因").count() == 1
                      and not page.locator(".metric-card").count(), "服务指标未保留 PR22 大表、红标和原因列")
                check(sheet.locator("tbody tr").first.evaluate("el => getComputedStyle(el).display") == "table-row",
                      "服务指标大表被卡片化")
                check(scroll.evaluate("el => { el.scrollLeft = el.scrollWidth; return el.scrollLeft > 0; }")
                      == mobile, "服务指标未按断点仅在大表容器内横滚")
                scroll.evaluate("el => { el.scrollLeft = 0; }")
            if path == "/reports/weekly?week=2026-11-16":
                east = page.locator('#shot-quarter [data-chart="orders"] .vgroup').filter(has_text="华东")
                check(east.locator('.vbar.prev[data-value="6"]').count() == 1, "未覆盖华东季度对比期")
                year = page.locator("#shot-year")
                east_year = year.locator('[data-chart="orders"] .vgroup').filter(has_text="华东")
                check(east_year.locator(".vslot").get_by_text("未导入", exact=True).count() == 2
                      and not east_year.locator(".vbar").count(),
                      "华东缺年度角色时不得复用季度数据")
                check("未导入" in year.locator('[data-card="orders"] .sub').inner_text(), "全国缺年度对比期时不得回填")
            if path == "/reports/weekly/import" and mobile:
                fields = page.locator('form[action="/reports/weekly/import"] :is(input, select):visible')
                check(fields.evaluate_all("els => els.slice(0, -1).every(el => "
                      "el.getBoundingClientRect().width >= 240 && el.getBoundingClientRect().height >= 44)"),
                      "周报导入栏未铺满，或触控高度不足")
            if path == "/admin/users" and mobile:
                check(page.locator("table tbody tr").evaluate_all("els => els.every(el => "
                      "el.scrollWidth <= el.clientWidth + 1)"), "用户卡片内部横溢")
            if width == 390 and os.environ.get("REPORTDB_MOBILE_SHOTS"):
                out = Path(os.environ["REPORTDB_MOBILE_SHOTS"])
                out.mkdir(parents=True, exist_ok=True)
                url = urlsplit(path)
                filename = url.path.strip("/").replace("/", "-") or "home"
                if url.query.startswith("view="):
                    filename += "-" + url.query.removeprefix("view=")
                page.screenshot(path=out / f"{filename}.png", full_page=True, animations="disabled")
            fits()
            check(not errors, f"脚本错误：{errors}")
            errors.clear()
    finally:
        context.close()
    assert not failures, "\n" + "\n".join(failures)
