from types import SimpleNamespace
"""剔除规则/剔除申请、督导全量可见、日报导出、reportdb 引擎流水线（桩发送器，不真发）、旧入口下线。"""
import io
import json
import os
import re
import shutil
import sys

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from conftest import ROOT, needs_real_data
from reportdb import calc, daily_report, exclusions as excl, exemptions as ex, parity
from reportdb.db import connect, migrate
from reportdb.ingest import ingest_dir
from reportdb.loaders import load_directory_sources
from reportdb.stores import region_lookup_factory
from reportdb.webapp import create_app
from test_reportdb_calc import D1, D2
from test_reportdb_exemptions import (GHOST, SNAP1, SNAP2, SPEC, U, counts, env, load, tok, vin_of, web,  # noqa: F401
                                      conn_code)
from test_reportdb_web import login, _fast_hash  # noqa: F401


# ------------------------------------------------------------------ 规则
def test_default_rules_seeded(conn):
    rules = excl.list_rules(conn)
    on = [r for r in rules if r["enabled"]]
    assert {(r["dataset_key"], r["column_name"], r["value"]) for r in on} == {
        (k, "store_name", "出口车专用") for k in ("task", "first-service", "second-service", "new-policy", "renewal")}
    assert [r for r in rules if not r["enabled"]][0]["match_type"] == "contains"


def test_rule_excludes_numerator_and_denominator(env):
    c = env.conn
    assert counts(c, "first_service", SNAP1)[D2.casefold()] == [1, 0]
    rid = excl.add_rule(c, "t", "first-service", "store_name", "equals", D2)
    excl.recompute_all(c)
    assert D2.casefold() not in counts(c, "first_service", SNAP1), "分母也不计"
    assert counts(c, "second_service", SNAP1)[D2.casefold()] == [1, 0], "只影响该数据集的规则"
    reg = counts(c, "first_service", SNAP1, "region")
    assert sum(v[0] for v in reg.values()) == 2
    assert excl.excluded_counts(c, SNAP1)["first_service"]["by_rule"] == 1
    excl.set_enabled(c, rid, False)
    excl.recompute_all(c)
    assert counts(c, "first_service", SNAP1)[D2.casefold()] == [1, 0]


def test_rule_validation_and_preview(env):
    c = env.conn
    with pytest.raises(excl.RuleError):
        excl.add_rule(c, "t", "first-service", "nope", "equals", "x")
    with pytest.raises(excl.RuleError):
        excl.add_rule(c, "t", "stock", "store_name", "equals", "x")
    with pytest.raises(excl.RuleError):
        excl.add_rule(c, "t", "first-service", "store_name", "regex", "x")
    with pytest.raises(excl.RuleError):
        excl.add_rule(c, "t", "first-service", "store_name", "equals", "  ")
    pv = excl.preview(c, "first-service", "store_name", "contains", "天津", SNAP1)
    assert pv["matched"] == 1 and pv["effective"] == 1
    pv = excl.preview(c, "first-service", "store_name", "equals", GHOST, SNAP1)
    assert pv["matched"] == 1 and pv["effective"] == 0, "本来就不在门店表的行不算新增影响"


# ------------------------------------------------------------------ 剔除申请
@pytest.mark.parametrize("ind", list(SPEC))
def test_exclude_request_removes_row_from_both(env, ind):
    c = env.conn
    # A2 是已完成的行：剔除后分子分母各 -1；A1 未完成的行：分母 -1
    r = ex.request_exclusions(c, env.sup1, SNAP1, items=[(vin_of(ind, "A2"), "出口")])
    assert len(r["created"]) == 1
    assert counts(c, ind, SNAP1)[D1.casefold()] == [2, 1], "待审批不生效"
    ex.review(c, env.admin, r["created"], "approve")
    assert counts(c, ind, SNAP1)[D1.casefold()] == [1, 0]
    assert excl.excluded_counts(c, SNAP1)[ind]["by_vin"] == 1
    ex.review(c, env.admin, r["created"], "revoke", "回退")
    assert counts(c, ind, SNAP1)[D1.casefold()] == [2, 1]


def test_exclusion_beats_exemption_and_persists(env):
    c = env.conn
    a = ex.request_exemptions(c, env.sup1, "first_service", SNAP1, vins=[vin_of("first_service", "A1")], category="其他", reason="豁免")
    ex.review(c, env.admin, a["created"], "approve")
    assert counts(c, "first_service", SNAP1)[D1.casefold()] == [2, 2]
    b = ex.request_exclusions(c, env.sup1, SNAP1, items=[(vin_of("first_service", "A1"), "其实是出口车")])
    assert len(b["created"]) == 1, "修正结果与剔除可并存，各自独立申请"
    ex.review(c, env.admin, b["created"], "approve")
    assert counts(c, "first_service", SNAP1)[D1.casefold()] == [1, 1], "该车彻底不计：分母 -1，分子不再 +1"
    assert ex.flipped_counts(c, SNAP1)["first_service"]["rows"] == 0
    load(c, SNAP2)  # 新快照继续沿用
    assert counts(c, "first_service", SNAP2)[D1.casefold()] == [1, 1]
    # 撤销剔除后豁免恢复
    ex.review(c, env.admin, b["created"], "revoke", "撤销剔除")
    assert counts(c, "first_service", SNAP2)[D1.casefold()] == [2, 2]


def test_exclude_scope_duplicates_and_rule_overlap(env):
    c = env.conn
    other = ex.request_exclusions(c, env.sup1, SNAP1, items=[(vin_of("renewal", "B1"), "x")])
    assert other["created"] == [] and "范围" in other["skipped"][0][1]
    ok = ex.request_exclusions(c, env.sup1, SNAP1, items=[(vin_of("renewal", "A1"), "x")])
    again = ex.request_exclusions(c, env.sup1, SNAP1, items=[(vin_of("renewal", "A1").lower(), "x")])
    assert len(ok["created"]) == 1 and again["created"] == [] and "已有" in again["skipped"][0][1]
    with pytest.raises(ex.ExemptionError):   # 剔除不能走修正结果入口
        ex.request_exemptions(c, env.sup1, "renewal", SNAP1, vins=["x"], category="其他", reason="x", kind="exclude")
    with pytest.raises(ex.ExemptionError):
        ex.request_exclusions(c, env.sup1, SNAP1, items=[(vin_of("renewal", "A2"), "  ")])
    excl.add_rule(c, "t", "renewal", "store_name", "equals", D1)
    cov = ex.request_exclusions(c, env.sup1, SNAP1, items=[(vin_of("renewal", "A2"), "x")])
    assert cov["created"] == [] and "剔除" in cov["skipped"][0][1]
    assert ex.list_exemptions(c, kind="exclude")[0]["kind"] == "exclude"


# ------------------------------------------------------------------ 页面：督导全量可见 / 规则页 / 日报
def test_supervisor_sees_everything_unmasked_viewer_masked(web):
    sup, view, adm = web["sup1"], web["view1"], web["admin"]
    j = sup.get("/api/data/second-service?size=50").json()
    assert j["total"] == 4 and j["masked"] is False and {r["store_name"] for r in j["rows"]} >= {D1, D2}
    assert view.get("/api/data/second-service").status_code == 403   # 用户只看汇总
    s = sup.get("/api/tables/summary").json()
    assert len(s["blocks"]) >= 6
    # 能申请剔除（自己的店），不能越权（别人的店在后台被拒）
    page = sup.get("/exemptions/new?indicator=second_service").text
    assert "申请剔除" in page and page.count('name="reason_') == 1   # 修正结果页只列本店结果为否的 A1；明细页只读
    assert 'name="reason_' not in sup.get("/data/second-service").text
    rn_other = conn_row(web["db"], "second_service", D2)
    r = sup.post("/exemptions/request", data={"indicator": "second_service", "date": SNAP1, "row_nos": rn_other, "category": "其他", "reason": "x",
                                              "kind": "exclude", "csrf": tok(sup, "/exemptions/new")}, follow_redirects=False)
    assert "err=" in r.headers["location"]   # 越权被拒
    assert ex.list_exemptions(connect(web["db"]), kind="exclude") == []


def conn_row(db, table, store):
    c = connect(db)
    try:
        return c.execute(f"SELECT row_no FROM {table} WHERE store_name=? LIMIT 1", (store,)).fetchone()[0]
    finally:
        c.close()


def test_exclusion_rules_page_flow(web):
    adm, sup = web["admin"], web["sup1"]
    assert sup.get("/settings/exclusion-rules").status_code == 403
    page = adm.get("/settings/exclusion-rules")
    assert page.status_code == 200 and "出口车专用" in page.text
    pv = adm.get("/settings/exclusion-rules", params={"pv_dataset": "first-service", "pv_column": "store_name", "pv_match": "equals", "pv_value": D2})
    assert "命中" in pv.text and "确认添加" in pv.text
    t = tok(adm, "/settings/exclusion-rules")
    assert adm.post("/settings/exclusion-rules/add", data={"dataset": "first-service", "column": "store_name", "match": "equals", "value": D2}).status_code == 403
    r = adm.post("/settings/exclusion-rules/add", data={"dataset": "first-service", "column": "store_name", "match": "equals", "value": D2, "csrf": t}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    tbl = adm.get("/api/tables/direct-stores").json()
    assert tbl["blocks"][0]["rows"][1][1] == 0, "D2 首保分母已被剔除"
    q = adm.get("/api/quality").json()
    assert q["exclusions"]["first_service"]["by_rule"] == 1 and any(x["value"] == D2 for x in q["exclusion_rules"])
    assert "剔除" in adm.get("/").text
    rid = re.findall(r"/settings/exclusion-rules/(\d+)/delete", adm.get("/settings/exclusion-rules").text)[-1]
    adm.post(f"/settings/exclusion-rules/{rid}/delete", data={"csrf": tok(adm, "/settings/exclusion-rules")})
    assert adm.get("/api/tables/direct-stores").json()["blocks"][0]["rows"][1][1] == 1


def test_daily_report_endpoint_and_layout(web):
    for who in ("view1", "sup1", "admin"):
        r = web[who].get(f"/export/daily-report.xlsx?date={SNAP1}")
        assert r.status_code == 200
        assert "%E5%8C%BA%E5%9F%9F%E5%90%84%E6%8C%87%E6%A0%87%E6%83%85%E5%86%B5%E4%B8%80%E8%A7%88" in r.headers["content-disposition"] and "0101.xlsx" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(web["view1"].get(f"/export/daily-report.xlsx?date={SNAP1}").content))
    assert wb.sheetnames == ["直营店指标", "区域指标排序", "直营店指标排序", "总表", "常备件备库率"]
    assert not [c.value for ws in wb for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert wb["总表"]["A1"].value == "首保实施率"
    # 含明细：viewer 脱敏，督导不脱敏
    # 用户（viewer）只看汇总：含明细版 403；督导不脱敏
    assert web["view1"].get(f"/export/daily-report.xlsx?date={SNAP1}&detail=1").status_code == 403
    for who, masked in (("sup1", False),):
        wb = load_workbook(io.BytesIO(web[who].get(f"/export/daily-report.xlsx?date={SNAP1}&detail=1").content))
        assert "首保" in wb.sheetnames
        vins = [row[0].value for row in wb["首保"].iter_rows(min_row=2)]
        assert (vin_of("first_service", "A1") not in vins) == masked
    page = web["view1"].get("/reports/daily")
    assert page.status_code == 200 and "区域各指标情况一览" in page.text and SNAP1 in page.text
    assert web["view1"].get("/export/daily-report.xlsx?date=1999-01-01").status_code == 404
    anon = TestClient(create_app(web["db"]))
    assert anon.get("/export/daily-report.xlsx", follow_redirects=False).status_code in (401, 303)


# ------------------------------------------------------------------ 与旧 Excel 公式引擎对账（日报文件本身）
@pytest.fixture(scope="module")
def real_db(tmp_path_factory):
    c = connect(tmp_path_factory.mktemp("real") / "r.sqlite3")
    migrate(c)
    data, _ = load_directory_sources(ROOT / "download"); data.pop("_origin_renewal")
    ingest_dir(c, ROOT / "download", "2026-10-09")
    return c, data


@pytest.mark.parity
@needs_real_data
@pytest.mark.skip(reason="口径已改（DMS 区域 + DMS 无区域自动剔除），不再与旧 Excel 引擎逐格一致")
def test_daily_xlsx_matches_legacy_engine_same_inputs(real_db):
    """日报 xlsx 的 5 张汇总表 vs 旧公式引擎（输入同样剔除不在门店表的行）：0 差异。无豁免、剔除规则对本批数据零影响。"""
    from types import SimpleNamespace

    c, data = real_db
    assert ex.flipped_counts(c, "2026-10-09")["first_service"]["rows"] == 0
    assert all(v["by_rule"] == 0 and v["by_vin"] == 0 for v in excl.excluded_counts(c, "2026-10-09").values())
    wb = load_workbook(io.BytesIO(daily_report.build_daily_report(c, "2026-10-09")), data_only=True)
    mine = {}
    canon = calc.compute(c, "2026-10-09")
    for ws in wb.worksheets:
        grid = {(cell.row, cell.column): cell.value for row in ws.iter_rows() for cell in row if cell.value is not None}
        for slug in ("summary", "direct-stores"):   # 这两张在日报里按名次 1..N 排行；按区域/门店名对回原位置再比，值必须一致
            sh = canon[slug]
            if sh.name != ws.title:
                continue
            for b in sh.blocks:
                by_label = {grid.get((b.row + 2 + i, b.col)): [grid.get((b.row + 2 + i, b.col + j)) for j in range(5)] for i in range(len(b.rows))}
                ranks = [grid.get((b.row + 2 + i, b.col + 4)) for i in range(len(b.rows))]
                nn = [r for r in ranks if r is not None]
                assert nn == sorted(nn) and ranks[:len(nn)] == nn, f"{ws.title}/{b.title} 未按名次排序"
                for i, row in enumerate(b.rows):
                    for j in range(5):
                        grid.pop((b.row + 2 + i, b.col + j), None)
                        v = by_label[row[0]][j]
                        if v is not None:
                            grid[(b.row + 2 + i, b.col + j)] = v
        mine[ws.title] = SimpleNamespace(name=ws.title, grid=lambda g=grid: g)
    ref = parity.reference_values(data, region_lookup_factory(c))
    res = parity.compare(mine, ref)
    assert res["diff_count"] == 0, res["diffs"][:10]
    assert res["cells"] > 5000 and res["max_float_delta"] < 1e-12


# ------------------------------------------------------------------ 引擎流水线（桩发送器）
@pytest.fixture()
def pipe_root(tmp_path, monkeypatch):
    import run_pipeline

    root = tmp_path / "root"
    (root / "output").mkdir(parents=True)
    (root / "download").mkdir()
    for p in (ROOT / "download").glob("*20261009_08*.xlsx"):
        shutil.copy(p, root / "download" / p.name)
    monkeypatch.setattr(run_pipeline, "PLUGIN_ROOT", root)
    monkeypatch.setenv("REPORTDB_PATH", str(tmp_path / "p.sqlite3"))
    monkeypatch.setenv("REPORTDB_SYNC_STORES", "0")
    return run_pipeline, root


@needs_real_data
def test_pipeline_reportdb_engine_dry_run_with_stub_sender(pipe_root, monkeypatch):
    run_pipeline, root = pipe_root
    import feishu_webhook

    sent = {"webhook": [], "im": []}
    monkeypatch.setattr(feishu_webhook, "post_webhook", lambda payload, **kw: sent["webhook"].append(payload) or {"ok": True, "response": {"stub": True}})
    monkeypatch.setattr(feishu_webhook, "notify_national_im", lambda result: sent["im"].append(result["report"]) or {"ok": True, "stub": True})
    monkeypatch.setenv("MHERO_REPORT_ENGINE", "reportdb")
    monkeypatch.setattr(run_pipeline, "build_from_download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应走 Excel 旧路径")))
    res = run_pipeline.run_pipeline(skip_crawl=True, skip_notify=False)
    assert res["status"] == "ok" and res["engine"] == "reportdb"
    rpt = root / "output" / "区域各指标情况一览1009.xlsx" if False else __import__("pathlib").Path(res["report"])
    assert rpt.is_file() and re.fullmatch(r"区域各指标情况一览\d{4}(_\d{6})?\.xlsx", rpt.name) and rpt.parent == root / "output"
    assert res["reportdb"]["ok"] and res["reportdb"]["rows"]["first-service"] == 2488
    # 卡片与旧路径同一个生成函数、同样的字段
    card = sent["webhook"][0]["card"]
    assert card["header"]["title"]["content"] == "区域报表已生成"
    body = card["elements"][0]["text"]["content"]
    assert f"**文件**：{rpt.name}" in body and "**状态**：ok" in body and card["elements"][1]["actions"][0]["text"]["content"] == "下载报表"
    assert sent["webhook"][0] == feishu_webhook.build_pipeline_card(res)
    assert sent["im"] == [res["report"]] and res["feishu"]["ok"] is True
    manifest = json.loads((root / "output" / "pipeline_manifest.json").read_text(encoding="utf-8"))
    assert manifest["engine"] == "reportdb"
    # 隐含入库：库里已有当天快照，网页立即可看
    c = connect(os.environ["REPORTDB_PATH"])
    assert c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    # 报表内容 = 库里重算的结果
    wb = load_workbook(rpt, data_only=True)
    assert wb["总表"]["E3"].value == 1 and wb["直营店指标"]["E3"].value == 1   # 第一行 = 名次 1


@needs_real_data
def test_pipeline_skip_notify_sends_nothing(pipe_root, monkeypatch):
    run_pipeline, root = pipe_root
    import feishu_webhook

    monkeypatch.setattr(feishu_webhook, "post_webhook", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发送")))
    monkeypatch.setenv("MHERO_REPORT_ENGINE", "reportdb")
    res = run_pipeline.run_pipeline(skip_crawl=True, skip_notify=True)
    assert res["status"] == "ok" and "feishu" not in res


def test_pipeline_default_engine_is_excel(pipe_root, monkeypatch):
    run_pipeline, root = pipe_root
    monkeypatch.delenv("MHERO_REPORT_ENGINE", raising=False)
    called = []
    monkeypatch.setattr(run_pipeline, "build_from_download", lambda d, o: called.append(1) or (o / "区域各指标情况一览0101.xlsx"))
    import reportdb.pipeline as rp
    monkeypatch.setattr(rp, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("默认不应走 reportdb 引擎")))
    res = run_pipeline.run_pipeline(skip_crawl=True, skip_notify=True)
    assert called and res["status"] == "ok" and "engine" not in res


def test_pipeline_reportdb_engine_failure_is_reported(pipe_root, monkeypatch):
    run_pipeline, root = pipe_root
    for p in (root / "download").glob("*.xlsx"):
        p.unlink()
    monkeypatch.setenv("MHERO_REPORT_ENGINE", "reportdb")
    res = run_pipeline.run_pipeline(skip_crawl=True, skip_notify=True)
    assert res["status"] == "error" and res["error"]


# ------------------------------------------------------------------ 旧 Excel 管理入口下线
def test_legacy_fastapi_upload_page_retired(monkeypatch):
    from app import main as legacy

    c = TestClient(legacy.app)
    monkeypatch.delenv("MHERO_REPORT_ENGINE", raising=False)
    assert c.get("/").status_code == 200
    monkeypatch.setenv("MHERO_REPORT_ENGINE", "reportdb")
    assert c.get("/").status_code == 410 and "已下线" in c.get("/").text
    assert c.post("/api/generate", files={"files": ("a.xlsx", b"x")}).status_code == 410
    assert c.get("/api/health").status_code == 200


def test_legacy_web_console_template_management_retired(monkeypatch):
    try:
        import web_console
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"web_console 依赖不可用: {exc}")
    monkeypatch.setattr(web_console, "_browser_ok", lambda: (False, {}))
    monkeypatch.setattr(web_console, "get_session_home", lambda root: root / ".session")
    cl = web_console.app.test_client()
    monkeypatch.setenv("MHERO_REPORT_ENGINE", "reportdb")
    for method, path in (("get", "/api/template"), ("get", "/api/template/download"), ("post", "/api/template")):
        r = getattr(cl, method)(path)
        assert r.status_code == 410, path
    page = cl.get("/").get_data(as_text=True)
    assert 'id="templateInfo"' not in page and "系统统一计算" in page
    monkeypatch.delenv("MHERO_REPORT_ENGINE")
    assert cl.get("/api/template").status_code == 200 and 'id="templateInfo"' in cl.get("/").get_data(as_text=True)


# ------------------------------------------------------------------ 首页：Excel 式总表
def test_home_page_excel_like_table(web):
    html = web["view1"].get(f"/?date={SNAP1}").text
    assert 'class="xl"' in html and SNAP1 in html and "下载日报 xlsx" in html and f"/export/daily-report.xlsx?date={SNAP1}" in html
    # 行 = 全国 + 区域（顺序同总表），列 = 招揽/首保/二保/新保/续保/备库率
    # （排序列头的升序指示已从文本「▲」换成 Ant 图标 #i-caret-up，断言等价更新）
    heads = re.findall(r'<a href="/\?date=[^"]+&sort=\w+"[^>]*>([^<]+?)\s*(?:<span class="arr">.*?</span>)?\s*<small>', html)
    assert "综合排名" not in html and "名次平均" not in html
    assert heads == ["招揽实施率", "首保实施率", "二保实施率", "新保投保率", "续保投保率", "常备件备库率"]
    rows = re.findall(r'<tr class="(tot|)"><td class="pos">[^<]*</td><td>([^<]+)</td>', html)
    assert rows[0] == ("tot", "全国") and len(rows) >= 2
    # 率与总表 API 一致（全国行）
    tbl = {b["title"]: b for b in web["view1"].get(f"/api/tables/summary?date={SNAP1}").json()["blocks"]}
    first = tbl["首保实施率"]["total"]
    assert f"{first[2]}/{first[1]}" in html


def _home_rows(client, sort):
    h = client.get(f"/?date={SNAP1}&sort={sort}").text
    return h, re.findall(r'<tr class=""><td class="pos">([^<]*)</td><td>([^<]+)</td>(.*?)</tr>', h, re.S)


def test_home_sorted_by_rank_and_clickable(web):
    c = web["view1"]
    h, rows = _home_rows(c, "")
    assert 'tr class="tot"' in h and h.index('class="tot"') < h.index('<tr class=""')        # 全国固定最上
    assert "综合排名" not in h and rows[0][0] == "1"
    for key, j in (("first", 1), ("second", 2), ("renewal", 4)):
        h, rows = _home_rows(c, key)
        ranks = []
        for r in rows:
            cell = re.findall(r'<td class="[^"]*"><span class="v">.*?</td>', r[2], re.S)[j]
            m = re.search(r'<span class="rk">第(\d+)</span>', cell)
            ranks.append(int(m.group(1)) if m else 99)
        assert ranks == sorted(ranks), key
        pos = [r[0] for r in rows]
        assert pos[0] == "1" and all(p in ("-", "") or int(p) == x for p, x in zip(pos, ranks) if x != 99)
        assert f"sort={key}" in h and '<use href="#i-caret-up"/>' in h
    _, bad = _home_rows(c, "nonsense")
    assert [r[1] for r in bad] == [r[1] for r in _home_rows(c, "")[1]]


def test_home_secondary_area(web):
    view = web["view1"].get(f"/?date={SNAP1}").text
    # 用户只看汇总：没有明细入口、透明度区和管理链接
    assert "<details class=\"qa\"" not in view and "/data/" not in view.split("ant-chip-group-label")[-1]
    assert "/admin/users" not in view and "/settings/exclusion-rules" not in view
    html = web["sup1"].get(f"/?date={SNAP1}").text
    # 次要区域：明细链接、透明度默认折叠；管理链接只对管理员
    assert "<details class=\"qa\">" in html and "<details class=\"qa\" open" not in html
    assert "/admin/users" not in html and "/settings/exclusion-rules" not in html
    adm = web["admin"].get(f"/?date={SNAP1}").text
    assert "/admin/users" in adm and "/settings/exclusion-rules" in adm and "剔除规则" in adm
    for d in ("stock", "task", "first-service", "second-service", "new-policy", "renewal"):
        assert f"/data/{d}?date={SNAP1}" in html
    for slug in ("direct-stores", "region-indicators", "spare-parts"):
        assert f"/tables/{slug}?date={SNAP1}" in html
    assert "region-ranking" not in html and "direct-ranking" not in html
    assert "@media print" in html and "position:sticky" in html


def test_home_requires_login(web):
    assert TestClient(create_app(web["db"])).get("/", follow_redirects=False).status_code in (303, 401)


# ------------------------------------------------------------------ 导航：分级菜单 + 品牌名
def _nav(html):
    return html[html.index('<header class="topbar"'):html.index("</header>")]


def test_nav_hierarchy_by_role(web):
    v = _nav(web["view1"].get(f"/tables/summary?date={SNAP1}").text)
    assert 'class="brand"' in v and ">区域报表<" in v and "自建库" not in v
    for t in ("首页", "报表", "日报下载", "总表", "区域指标", "直营店指标", "常备件备库率"):
        assert t in v
    assert "明细数据" not in v and "门店指标" not in v   # 用户只看汇总
    assert v.count('aria-haspopup="true"') == 2   # 报表 / 用户菜单
    assert "排序" not in v and "督导申请" not in v and ">管理<" not in v and "/admin/" not in v and "/settings/" not in v
    assert 'class="mi on"' in v and "报表</button>" in v   # 当前所在分组高亮
    assert v.count('class="cur"') == 1 and 'aria-current="page"' in v and "退出登录" in v and "修改密码" in v
    s = _nav(web["sup1"].get("/").text)
    assert "督导申请" in s and "我的申请" in s and ">管理<" not in s and "/admin/users" not in s
    a = _nav(web["admin"].get("/").text)
    assert "督导申请" in a and "用户管理" in a and "备件名单设置" in a and "剔除规则" in a and "修正结果 / 剔除审批" in a and "申请修正结果" in a and "申请剔除" in a


def test_mobile_nav_drawer_hooks(web):
    """移动端抽屉：页面保留 #burger/#nav-backdrop/setDrawer 钩子与文案；动效契约在设计系统
    （antd-lite.css 经 ds_css 内联进页面）：右侧滑入抽屉 + 遮罩淡入淡出 + 退出延迟隐藏。"""
    html = web["admin"].get("/").text
    nav = _nav(html)
    assert 'id="burger"' in nav and 'aria-controls="mainmenu"' in nav and 'aria-label="打开菜单"' in nav
    assert 'href="#i-menu"' in nav and 'href="#i-close"' in nav
    assert 'id="nav-backdrop"' in html and 'aria-label="关闭菜单"' in html
    assert "setDrawer" in html and "max-width:860px" in html
    # 抽屉动效（设计系统，内联在页面里）：300ms 右侧滑入 + 遮罩 rgba(0,0,0,.45) 淡入淡出
    assert "translateX(100%)" in html and "translateX(0)" in html
    assert "cubic-bezier(.23, 1, .32, 1)" in html
    assert "rgba(0, 0, 0, .45)" in html
    assert "@media (max-width: 860px)" in html and "prefers-reduced-motion: reduce" in html
    # 页面自带的 .nav-backdrop 遮罩元素：桌面隐藏、移动端随 body.nav-open 淡入并锁定滚动
    assert "body.nav-open .nav-backdrop" in html
    assert "body.nav-open { overflow: hidden; }" in html
    for label in ("首页", "报表", "明细数据", "退出登录", "修改密码", "用户管理"):
        assert label in nav


def test_nav_user_menu_adapts_to_sso(web):
    app = web["admin"].app
    assert "修改密码" in _nav(web["admin"].get("/").text)
    import reportdb.webapp as wa
    # 本地账号才显示修改密码：把该用户标成非本地来源
    c = connect(web["db"]); c.execute("UPDATE users SET auth_source='oidc' WHERE username='admin'"); c.commit(); c.close()
    nav = _nav(web["admin"].get("/").text)
    assert "修改密码" not in nav and "单点登录" in nav and "退出登录" in nav


def test_brand_name_everywhere(web):
    for path in ("/", "/reports/daily", "/tables/summary"):
        t = web["admin"].get(path).text
        assert "自建库" not in t and "<title>" in t
    assert "自建库" not in TestClient(create_app(web["db"])).get("/login").text


def test_table_pages_and_xlsx_ordered_by_rank(web):
    c = web["view1"]
    # 区域指标 / 直营店指标页：每个指标表按名次 1..N（名次为空排最后）
    for slug in ("region-indicators", "direct-stores"):
        page = c.get(f"/tables/{slug}?date={SNAP1}").text
        for card in page.split('<h2 class="ant-card-head-title">')[2:]:
            ranks = [int(x) for x in re.findall(r'<td class="num" data-v="(\d+)">\d+</td></tr>', card)]
            assert ranks == sorted(ranks), (slug, card[:30])
    # 总表页 = 全国 + 区域宽表，可点列头排序；旧地址重定向、旧 API 键仍可用
    assert "&sort=first" in c.get(f"/tables/summary?date={SNAP1}").text
    r = c.get(f"/tables/region-ranking?date={SNAP1}", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("/tables/region-indicators?date=")
    assert c.get(f"/tables/direct-ranking?date={SNAP1}", follow_redirects=False).headers["location"].startswith("/tables/direct-stores")
    assert c.get(f"/api/tables/region-ranking?date={SNAP1}").status_code == 200
    assert c.get(f"/api/tables/direct-ranking?date={SNAP1}").status_code == 200
    assert [t["slug"] for t in c.get("/api/tables").json()] == ["summary", "region-indicators", "direct-stores", "spare-parts"]
    assert c.get(f"/api/tables/region-indicators?date={SNAP1}").json()["slug"] == "region-ranking"
    for pg in ("/", f"/tables/summary?date={SNAP1}", f"/tables/region-indicators?date={SNAP1}"):
        assert "排序</" not in c.get(pg).text.split("</header>")[0]
    # 日报 xlsx：sheet 名与版式不变；总表/直营店指标的行按名次 1..N（只改行序，值不变）
    wb = load_workbook(io.BytesIO(c.get(f"/export/daily-report.xlsx?date={SNAP1}").content))
    assert wb.sheetnames == ["直营店指标", "区域指标排序", "直营店指标排序", "总表", "常备件备库率"]
    sheets = calc.compute(connect(web["db"]), SNAP1)
    for slug in ("summary", "direct-stores", "region-ranking", "direct-ranking"):
        ws = wb[sheets[slug].name]
        for b in sheets[slug].blocks:
            vals = [ws.cell(b.row + 2 + i, b.col + 4).value for i in range(len(b.rows))]
            nums = [v for v in vals if isinstance(v, int)]
            assert nums == sorted(nums) and vals[:len(nums)] == nums, (slug, b.title)


# ------------------------------------------------------------------ 每辆车各填各的理由 / 批量行 / 筛选
def test_per_vehicle_reasons_web_and_lines(web):
    sup, adm = web["sup1"], web["admin"]
    html = sup.get("/exemptions/new?indicator=first_service").text
    assert "申请修正结果理由" in html and "提交申请" in html and 'name="pick_' not in html and 'name="kind" value="exclude"' not in html
    rn = re.search(r'name="reason_(\d+)"', html).group(1)
    t = tok(sup, "/exemptions/new")
    # 没填理由：不提交，页面提示
    r = sup.post("/exemptions/request", data={"indicator": "first_service", "date": SNAP1, f"reason_{rn}": "  ", "csrf": t}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = sup.post("/exemptions/request", data={"indicator": "first_service", "date": SNAP1, f"reason_{rn}": "出口车，挂靠本店", "csrf": t}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    c = connect(web["db"])
    row = ex.list_exemptions(c, indicator="first_service")[0]
    assert row["reason"] == "出口车，挂靠本店" and row["kind"] == "exempt" and row["reason_category"] == "其他"
    # 申请页：已有待审批 → 「已有申请」列显示状态；明细页只读
    html = sup.get("/exemptions/new?indicator=first_service").text
    assert "修正结果待审批" in html
    assert f'name="reason_{rn}"' not in sup.get("/data/first-service").text
    # 不支持批量粘贴：lines 字段被忽略，页面也没有粘贴框
    v = vin_of("renewal", "A1")
    r = sup.post("/exemptions/request", data={"indicator": "renewal", "date": SNAP1, "kind": "exclude", "csrf": t,
                                              "lines": f"{v}\t出口车A\n"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and ex.list_exemptions(c, indicator="renewal") == []
    assert not hasattr(ex, "parse_lines") and "<textarea" not in sup.get("/exemptions/new").text and 'name="lines"' not in sup.get("/exemptions/new").text
    # 勾选：有勾选框的页面表单，必须「勾选 + 理由」才提交
    html = sup.get("/exemptions/new?indicator=renewal").text
    assert 'name="pick_' not in html and "是否申请" not in html
    rn2 = re.search(r'name="reason_(\d+)"', html).group(1)
    r = sup.post("/exemptions/request", data={"indicator": "renewal", "date": SNAP1, "kind": "exclude", "csrf": t,
                                              f"reason_{rn2}": "出口车A"}, follow_redirects=False)   # 无勾选框字段=脚本调用，按理由提交
    got = ex.list_exemptions(c, indicator="renewal")
    assert len(got) == 1 and got[0]["kind"] == "exempt" and got[0]["reason"] == "出口车A"
    # 两辆车各自的理由
    items = [(vin_of("task", "A1"), "理由甲"), (vin_of("task", "A2"), "理由乙")]
    with pytest.raises(ex.ExemptionError):
        ex.request_exemptions(c, env_sup(c), "task", SNAP1, items=items, kind="exempt")   # 多辆：整体拒绝
    ex.request_exemptions(c, env_sup(c), "task", SNAP1, items=items[:1], kind="exempt")
    ex.request_exemptions(c, env_sup(c), "task", SNAP1, items=items[1:], kind="exempt")   # 各自单独提交
    assert "理由甲" in [x["reason"] for x in ex.list_exemptions(c, indicator="task")]   # 理由乙那辆若已完成会被跳过（保留原规则）
    # 管理员页显示理由列、可按关键字/日期/区域筛选，导出尊重筛选
    page = adm.get("/admin/exemptions?status=").text
    assert "申请理由" in page and "出口车，挂靠本店" in page and 'name="date_from"' in page
    assert "出口车A" not in adm.get("/admin/exemptions?status=&q=挂靠本店").text
    assert "出口车A" in adm.get("/admin/exemptions?status=&kind=exempt").text
    assert "出口车A" not in adm.get("/admin/exemptions?status=&date_from=2999-01-01").text
    x = load_workbook(io.BytesIO(adm.get("/admin/exemptions.xlsx?kind=exempt&status=").content))
    assert all(r[1] == "修正结果" for r in list(x.active.iter_rows(min_row=2, values_only=True)))
    mine = sup.get("/exemptions/mine?kind=exempt").text
    assert "出口车A" in mine and "出口车，挂靠本店" in mine
    assert "出口车A" not in sup.get("/exemptions/mine?kind=exclude").text


def env_sup(c):
    from types import SimpleNamespace
    r = c.execute("SELECT * FROM users WHERE username='sup1'").fetchone()
    return SimpleNamespace(id=r["id"], username="sup1", role="supervisor", supervisor_name=r["supervisor_name"] or "", can_request=True)


def test_detail_filters_server_side_and_sticky(web):
    adm = web["admin"]
    allj = adm.get(f"/api/data/first-service?date={SNAP1}&size=50").json()
    todo = adm.get(f"/api/data/first-service?date={SNAP1}&decision=todo&size=50").json()
    done = adm.get(f"/api/data/first-service?date={SNAP1}&decision=done&size=50").json()
    assert todo["total"] + done["total"] == allj["total"] and todo["total"] > 0 and done["total"] > 0
    assert all(r["done"] == "是" for r in done["rows"]) and all(r["done"] != "是" for r in todo["rows"])
    scoped = adm.get(f"/api/data/first-service?date={SNAP1}&scope_only=1").json()
    assert scoped["total"] == allj["total"] - 1          # 不在门店表的店那一行被去掉
    assert adm.get(f"/api/data/first-service?date={SNAP1}&store={D1}").json()["total"] == 2
    assert adm.get(f"/api/data/first-service?date={SNAP1}&decision=excluded").json()["total"] == 0
    page = adm.get(f"/data/first-service?date={SNAP1}&decision=todo&scope_only=1&size=25").text
    assert 'name="decision"' in page and 'name="scope_only"' in page and "筛选后" in page
    assert "decision=todo" in page and "scope_only=1" in page            # 导出链接/翻页沿用当前筛选
    x = load_workbook(io.BytesIO(adm.get(f"/export/data/first-service.xlsx?date={SNAP1}&decision=done").content))
    assert x.active.max_row - 1 == done["total"]
    # 豁免后可筛「已豁免」
    c = connect(web["db"])
    rows = ex.request_exemptions(c, env_sup(c), "first_service", SNAP1, vins=[vin_of("first_service", "A1")], reason="r")
    ex.review(c, type("A", (), {"role": "admin", "username": "admin"})(), rows["created"], "approve")
    assert adm.get(f"/api/data/first-service?date={SNAP1}&decision=exempt").json()["total"] == 1
    page = adm.get(f"/data/first-service?date={SNAP1}").text
    sel = page.split('name="decision"', 1)[1].split("</select>", 1)[0]
    assert "已修正" not in sel and "已剔除" not in sel and 'value="exempt"' not in sel and 'value="excluded"' not in sel
    assert '<span class="ant-tag ant-tag-success">已修正</span>' in page


def _decision_options(html: str) -> tuple[list[str], list[str]]:
    sel = html.split('name="decision"', 1)[1].split("</select>", 1)[0]
    values = re.findall(r'<option\b[^>]*\bvalue="([^"]*)"', sel)
    labels = re.findall(r"<option\b[^>]*>(.*?)</option>", sel)
    return values, labels


def test_completion_where_sql_task_vs_others():
    """招揽的 hit 是未完成；其余指标的 hit 是已完成。实施率口径不走这条。"""
    task = ex.INDICATORS["task"]
    assert task.hit_is_incomplete is True and task.hit == "待处理"
    assert ex.completion_where(task, "done") == ("COALESCE(task_status,'')<>?", ["待处理"])
    assert ex.completion_where(task, "todo") == ("COALESCE(task_status,'')=?", ["待处理"])
    assert ex.completion_where(task, "exempt") is None
    for key in ("first_service", "second_service", "new_policy", "renewal"):
        ind = ex.INDICATORS[key]
        assert ind.hit_is_incomplete is False and ind.hit == "是"
        assert ex.completion_where(ind, "done") == (f"COALESCE({ind.flag_col},'')=?", ["是"])
        assert ex.completion_where(ind, "todo") == (f"COALESCE({ind.flag_col},'')<>?", ["是"])


def test_task_done_filter_excludes_pending_and_dropdown(web):
    """招揽：已完成排除待处理，未完成只有待处理。各明细下拉只有全部/已完成/未完成。"""
    adm = web["admin"]
    for key in ("task", "first-service", "second-service", "new-policy", "renewal"):
        values, labels = _decision_options(adm.get(f"/data/{key}?date={SNAP1}").text)
        assert values == ["", "done", "todo"], key
        assert labels == ["全部", "已完成", "未完成"], key
        assert "（" not in "".join(labels)

    c = connect(web["db"])
    for vin, status in (
        (vin_of("task", "A1"), "预约失败"),
        (vin_of("task", "A2"), "待处理"),
        (vin_of("task", "B1"), "处理中"),
        (vin_of("task", "Z1"), "暂存"),
    ):
        n = c.execute(
            "UPDATE task_items SET task_status=? WHERE snapshot_date=? AND upper(trim(vin))=?",
            (status, SNAP1, vin),
        ).rowcount
        assert n == 1, vin
    src = dict(c.execute(
        "SELECT * FROM task_items WHERE snapshot_date=? AND upper(trim(vin))=?",
        (SNAP1, vin_of("task", "A1")),
    ).fetchone())
    src["row_no"] = c.execute("SELECT COALESCE(MAX(row_no),0)+1 FROM task_items WHERE snapshot_date=?", (SNAP1,)).fetchone()[0]
    src["vin"] = "LDPNEWSUCCESS0000001"
    src["task_status"] = "预约成功"
    cols = list(src)
    c.execute(
        f"INSERT INTO task_items ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [src[col] for col in cols],
    )
    c.commit()

    done = adm.get(f"/api/data/task?date={SNAP1}&decision=done&size=50").json()
    todo = adm.get(f"/api/data/task?date={SNAP1}&decision=todo&size=50").json()
    allj = adm.get(f"/api/data/task?date={SNAP1}&size=50").json()
    assert todo["total"] == 1 and {r["task_status"] for r in todo["rows"]} == {"待处理"}
    assert "待处理" not in {r["task_status"] for r in done["rows"]}
    assert {r["task_status"] for r in done["rows"]} == {"预约失败", "处理中", "暂存", "预约成功"}
    assert done["total"] + todo["total"] == allj["total"] == 5
    exported = load_workbook(io.BytesIO(adm.get(f"/export/data/task.xlsx?date={SNAP1}&decision=done").content))
    exported_status = [r[6] for r in exported.active.iter_rows(min_row=2, values_only=True)]
    assert exported_status and "待处理" not in exported_status

    # 实施率不变：未招揽数仍是「待处理」。D1=预约失败+待处理+预约成功，D2=处理中；暂存那辆不在门店表。
    task_block = next(b for b in calc.compute(c, SNAP1)["direct-stores"].blocks if b.key == "task")
    by_store = {r[0]: r for r in task_block.rows}
    assert by_store[D1][:4] == [D1, 3, 1, pytest.approx(2 / 3)]
    assert by_store[D2][:4] == [D2, 1, 0, 1.0]
    c.close()

    for key in ("first-service", "second-service", "new-policy", "renewal"):
        done = adm.get(f"/api/data/{key}?date={SNAP1}&decision=done&size=50").json()
        todo = adm.get(f"/api/data/{key}?date={SNAP1}&decision=todo&size=50").json()
        allj = adm.get(f"/api/data/{key}?date={SNAP1}&size=50").json()
        assert done["total"] > 0 and todo["total"] > 0, key
        assert all(r["done"] == "是" for r in done["rows"]), key
        assert all((r["done"] or "") != "是" for r in todo["rows"]), key
        assert done["total"] + todo["total"] == allj["total"], key

    page = adm.get(f"/data/task?date={SNAP1}&decision=done").text
    assert 'value="done" selected' in page or 'value="done"  selected' in page
    assert "已完成（" not in page


def test_region_filter_on_every_filter_page(web):
    from urllib.parse import quote
    adm, sup = web["admin"], web["sup1"]
    c = connect(web["db"])
    reg = c.execute("SELECT region FROM stores WHERE name=?", (D1,)).fetchone()[0]
    other = [r[0] for r in c.execute("SELECT name FROM regions") if r[0] != reg][0]
    # ① 所有明细页：区域下拉 + 服务端筛选 + 翻页/导出沿用
    for key in ("stock", "task", "first-service", "second-service", "new-policy", "renewal"):
        base = adm.get(f"/api/data/{key}?date={SNAP1}&size=500").json()["total"]
        a = adm.get(f"/api/data/{key}?date={SNAP1}&region={quote(reg)}&size=500").json()
        b = adm.get(f"/api/data/{key}?date={SNAP1}&region={quote(other)}&size=500").json()
        assert a["total"] + b["total"] <= base and a["total"] > 0 or key == "stock", key
        page = adm.get(f"/data/{key}?date={SNAP1}&region={quote(reg)}").text
        assert 'name="region"' in page and f"<option selected>{reg}</option>" in page, key
        assert f"region={quote(reg)}" in page.split("导出 xlsx")[0].rsplit("href=", 1)[-1] or f"region={quote(reg)}" in page, key
        x = load_workbook(io.BytesIO(adm.get(f"/export/data/{key}.xlsx?date={SNAP1}&region={quote(reg)}").content))
        assert x.active.max_row - 1 == a["total"], key
    # 门店下拉随区域联动
    p = adm.get(f"/data/first-service?date={SNAP1}&region={quote(reg)}").text
    assert D1 in p.split('name="store"')[1].split("</select>")[0]
    # ② 我的申请
    t = tok(sup, "/exemptions/new")
    rn = re.search(r'name="reason_(\d+)"', sup.get("/exemptions/new?indicator=first_service").text).group(1)
    sup.post("/exemptions/request", data={"indicator": "first_service", "date": SNAP1, f"reason_{rn}": "区域测试", "csrf": t})
    assert 'name="region"' in sup.get("/exemptions/mine").text
    assert "区域测试" in sup.get(f"/exemptions/mine?region={quote(reg)}").text
    assert "区域测试" not in sup.get(f"/exemptions/mine?region={quote(other)}").text
    # ③ 豁免审批 + 导出
    assert 'name="region"' in adm.get("/admin/exemptions").text
    assert "区域测试" in adm.get(f"/admin/exemptions?region={quote(reg)}").text
    assert "区域测试" not in adm.get(f"/admin/exemptions?region={quote(other)}").text
    rows = lambda r: list(load_workbook(io.BytesIO(adm.get(f"/admin/exemptions.xlsx?status=&region={quote(r)}").content)).active.iter_rows(min_row=2, values_only=True))
    assert len(rows(reg)) >= 1 and len(rows(other)) == 0


def test_single_item_submission_only(web):
    """逐条提交：每行自己的表单/按钮；一次请求带多于 1 辆车一律整体拒绝；理由必填。"""
    sup = web["sup1"]
    html = sup.get("/exemptions/new?indicator=first_service").text
    assert 'name="pick_' not in html and "是否申请" not in html and "申请修正结果（否→是）" not in html
    rn = re.search(r'name="reason_(\d+)"', html).group(1)
    assert f'form="f{rn}"' in html and f'id="f{rn}"' in html and html.count("提交申请</button>") == html.count('name="reason_')
    t = tok(sup, "/exemptions/new")
    base = {"indicator": "first_service", "date": SNAP1, "csrf": t}
    c = connect(web["db"])
    # 没填理由 → 不提交
    r = sup.post("/exemptions/request", data={**base, f"reason_{rn}": ""}, follow_redirects=False)
    assert "err=" in r.headers["location"] and ex.list_exemptions(c, indicator="first_service") == []
    # 带 2 辆（哪怕都有理由）→ 整体拒绝，一条都不建
    r = sup.post("/exemptions/request", data={**base, f"reason_{rn}": "出口车", "reason_999999": "x"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and "1+%E8%BE%86" in r.headers["location"] and ex.list_exemptions(c, indicator="first_service") == []
    r = sup.post("/exemptions/request", data={**base, "row_nos": [rn, "999999"], "reason": "x"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and ex.list_exemptions(c, indicator="first_service") == []
    # 模块层同样拒绝
    import pytest
    with pytest.raises(ex.ExemptionError):
        ex.request_exemptions(c, SimpleNamespace(role="admin", username="x"), "first_service", SNAP1, items=[(1, "a"), (2, "b")])
    # 1 辆 + 理由 → 提交，回到申请页并带行内提示/保持筛选
    r = sup.post("/exemptions/request", data={**base, f"reason_{rn}": "出口车", "back": "/exemptions/new?indicator=first_service&region=X"}, follow_redirects=False)
    loc = r.headers["location"]
    assert "msg=" in loc and "indicator=first_service" in loc and "region=X" in loc and f"#r-{rn}" in loc and len(ex.list_exemptions(c, indicator="first_service")) == 1
    # 重复提交同一辆 → 拒绝（已有待审批）
    r = sup.post("/exemptions/request", data={**base, f"reason_{rn}": "再来一次"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and len(ex.list_exemptions(c, indicator="first_service")) == 1
    assert "我的申请明细" in sup.get("/exemptions/new?indicator=first_service").text
    d = sup.get("/data/first-service").text
    assert "我的申请明细" not in d and 'name="pick_' not in d


def test_region_filter_on_every_page(web):
    sup, adm = web["sup1"], web["admin"]
    reg = [r["name"] for r in connect(web["db"]).execute("SELECT name FROM regions ORDER BY sort_order")][0]
    pages = ["/", "/tables/summary", "/tables/region-ranking", "/tables/direct-stores", "/data/first-service", "/data/stock", "/exemptions/new",
             "/exemptions/mine", "/reports/daily"]
    for u in pages:
        h = sup.get(u).text
        assert 'name="region"' in h, u
        assert sup.get(u + ("&" if "?" in u else "?") + "region=" + reg).status_code == 200, u
    for u in ["/admin/exemptions", "/admin/users", "/settings/exclusion-rules", "/settings/spare-parts"]:
        assert 'name="region"' in adm.get(u).text, u
        assert adm.get(u + "?region=" + reg).status_code == 200, u
    # 首页只剩该区域一行 + 全国
    h = sup.get("/?region=" + reg).text
    assert h.count("<tr class=\"\"") + h.count("<tr class=\"tot\"") <= 2
