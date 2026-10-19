"""只读 Web：每张汇总表一个页面 + 每个明细数据集一个页面 + JSON API。

启动：python -m reportdb.webapp --port 9013   （默认只监听 127.0.0.1）
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import hmac
import os
import re
import time

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import calc, config
from .datasets import BY_KEY, DATASETS, Dataset
from .db import connect, get_meta, migrate
from .export import multi_rows_to_xlsx, rows_to_xlsx, sheets_to_xlsx
from . import daily_report as daily_mod
from . import exclusions as excl_mod
from . import exemptions as ex_mod
from . import spare as spare_mod
from .auth import AuthError, get_backend, service, sso_sync
from .forbidden_page import DEFAULT_HUB_URL, render_no_access
from .auth.oidc import STATE_COOKIE
from .masking import mask_row, mask_vin
from . import store_indicators as si_mod
from dataclasses import replace

from .tables import PCT1 as PCT1_FMT, Sheet, fmt_value

TABLE_TITLES = {   # 网页上展示的 4 张表（每张都带名次，可按名次排序；不再单列「排序」页）
    "summary": ("总表", "全国 + 各区域 × 六项指标，点列头按该指标名次排序"),
    "region-indicators": ("区域指标", "每个指标一张表，区域按名次 1…N 排列"),
    "direct-stores": ("直营店指标", "直营店 × 六项指标，每个指标按名次 1…N 排列"),
    "spare-parts": ("常备件备库率", "各特约店 × 常备件库存（>0 计为已备）"),
}
# 旧地址/旧 API 键仍可用：页面重定向，API/导出按计算层原键取数
LEGACY_PAGES = {"region-ranking": "region-indicators", "direct-ranking": "direct-stores"}
CALC_SLUG = {"region-indicators": "region-ranking"}
EXPORT_NAMES = {"summary": "总表", "region-ranking": "区域指标", "direct-stores": "直营店指标", "spare-parts": "常备件备库率"}  # 网页导出的 sheet 名（日报 xlsx 不受影响）
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_TPL = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# 设计系统（antd-lite.css + Ant Design Icons 本地 sprite）：进程内缓存，模板里
# 用 <style>{{ ds_css|safe }}</style> 与 <body> 开头的 {{ ds_sprite|safe }} 注入。
_DS_DIR = Path(__file__).resolve().parent / "assets" / "ds"
_DS_CACHE: dict[str, str] = {}


def _ds_asset(name: str) -> str:
    if name not in _DS_CACHE:
        _DS_CACHE[name] = (_DS_DIR / name).read_text(encoding="utf-8")
    return _DS_CACHE[name]


_TPL.env.globals["ds_css"] = _ds_asset("antd-lite.css")
_TPL.env.globals["ds_sprite"] = _ds_asset("sprite.svg")
_TPL.env.globals["ds_cards"] = _ds_asset("cards.js")
_TPL.env.globals["ds_feedback"] = _ds_asset("feedback.js")

# 共享断行过滤器（design-system/linebreak.py 由 sync.sh 同步到 assets/ds/）
from .assets.ds.linebreak import ds_wbr_filename, nobreak_tail  # noqa: E402

_TPL.env.filters["nobreak_tail"] = nobreak_tail
_TPL.env.filters["ds_wbr_filename"] = ds_wbr_filename

# 品牌静态资源（logo / favicon；无鉴权挂载，登录页 favicon 也要能用）
from starlette.staticfiles import StaticFiles as _StaticFiles


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="区域报表", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/ds/brand", _StaticFiles(directory=str(_DS_DIR / "brand")), name="mhero-brand")
    path = Path(db_path) if db_path else config.db_path()
    boot = connect(path)
    migrate(boot)
    boot.close()

    def get_conn():
        conn = connect(path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ 认证
    def conn_factory():
        return connect(path)

    backend = get_backend(conn_factory)
    app.state.auth_backend = backend

    PREVIEW_COOKIE = "reportdb_preview"

    class NotAuthenticated(Exception):
        pass

    class Principal:
        def __init__(self, row, csrf):
            self.id, self.username, self.role = row["id"], row["username"], row["role"]
            self.display_name = row["display_name"] or row["username"]
            self.full_detail = bool(row["full_detail"]) or row["role"] in ("admin", "readonly_admin", "supervisor")  # 督导 / 只读管理员看未脱敏明细
            self.auth_source, self.csrf = row["auth_source"], csrf
            self.supervisor_name = row["supervisor_name"] or ""
            self.can_request = row["role"] in ("supervisor", "admin")
            self.can_view_admin = row["role"] in ("admin", "readonly_admin")      # 只读管理员：管理页只能看
            keys = row.keys()
            # 账号 / 权限管理只归统一登录的全局超级管理员；本地应急管理员（auth_source=local）同样视为全局
            self.global_admin = row["role"] == "admin" and (row["auth_source"] == "local" or bool(row["global_admin"] if "global_admin" in keys else 0))
            self.preview_admin = ""      # 管理员「以某位督导身份」只读预览时，这里是真实管理员的用户名

    def client_ip(request: Request) -> str:
        h = request.headers
        return (h.get("cf-connecting-ip") or (h.get("x-forwarded-for") or "").split(",")[0].strip()
                or (request.client.host if request.client else "-"))

    def secure_cookie(request: Request) -> bool:
        return (os.getenv("REPORTDB_COOKIE_SECURE", "") == "1" or request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto", "") == "https")

    def login_required(request: Request, conn=Depends(get_conn)) -> Principal:
        got = service.load_session(conn, request.cookies.get(service.SESSION_COOKIE))
        if not got:
            raise NotAuthenticated()
        p = Principal(*got)
        pv = request.cookies.get(PREVIEW_COOKIE)
        if pv and p.role == "admin" and pv.isdigit():
            t = conn.execute("SELECT * FROM users WHERE id=? AND role='supervisor' AND disabled=0", (int(pv),)).fetchone()
            if t:
                # 只读预览：以该督导的身份看页面（看到的范围、可申请的车辆与他一致），但任何写操作都被拒绝
                if request.method not in ("GET", "HEAD") and request.url.path not in ("/admin/preview/exit", "/logout"):
                    raise HTTPException(403, "预览模式只读：请先「退出预览」")
                q = Principal(t, p.csrf)
                q.preview_admin = p.username
                p = q
        if p.role == "viewer" and not _summary_path(request):
            raise HTTPException(403, "你的账号是「用户」，只能查看汇总报表（首页 / 报表 / 日报），不能查看明细")
        request.state.principal = p
        return p

    _SUMMARY_PREFIXES = ("/tables/", "/export/tables/", "/export/tables-view/", "/api/tables")
    _SUMMARY_EXACT = {"/", "/export/home.xlsx", "/api/snapshots", "/api/health", "/reports/daily", "/export/daily-history.xlsx",
                      "/export/daily-report.xlsx", "/account/password", "/logout", "/admin/preview/exit"}

    def _summary_path(request: Request) -> bool:
        """用户（viewer）只看汇总：首页、各汇总表、日报（不含明细版）。"""
        path = request.url.path
        if path == "/export/daily-report.xlsx" and request.query_params.get("detail") not in (None, "", "0"):
            return False
        return path in _SUMMARY_EXACT or path.startswith(_SUMMARY_PREFIXES)

    def admin_required(p: Principal = Depends(login_required)) -> Principal:
        """写操作 / 审批 / 设置：本系统超级管理员（全局超级管理员，或黄页里给区域报表单独设为超级管理员）。"""
        if p.role != "admin":
            raise HTTPException(403, "只读管理员只能查看，不能修改" if p.role == "readonly_admin" else "需要管理员权限")
        return p

    def admin_view_required(p: Principal = Depends(login_required)) -> Principal:
        """管理页的查看（审批列表、规则、备件设置及其导出）：超级管理员或只读管理员。"""
        if not p.can_view_admin:
            raise HTTPException(403, "需要管理员权限")
        return p

    def account_admin_required(p: Principal = Depends(login_required)) -> Principal:
        """账号 / 权限管理：只有全局超级管理员（或本地应急管理员）。"""
        if not p.global_admin:
            raise HTTPException(403, "账号与权限管理只限全局超级管理员")
        return p

    def check_csrf(p: Principal, token: str) -> None:
        if not token or not hmac.compare_digest(token.encode(), p.csrf.encode()):
            raise HTTPException(403, "表单已过期或来源不合法，请刷新页面重试")

    @app.exception_handler(NotAuthenticated)
    def _unauth(request: Request, exc: NotAuthenticated):
        if request.url.path.startswith(("/api/", "/export/")):
            return JSONResponse({"detail": "未登录"}, status_code=401)
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?" + urlencode({"next": nxt}), status_code=303)

    def safe_next(n: str | None) -> str:
        return n if n and n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"

    def start_session(request: Request, conn, row, resp):
        token, _ = service.create_session(conn, row["id"], client_ip(request), request.headers.get("user-agent", ""))
        resp.set_cookie(service.SESSION_COOKIE, token, httponly=True, samesite="lax", secure=secure_cookie(request),
                        max_age=int(service.session_hours() * 3600), path="/")
        return resp

    def page(request, tpl, conn, user, **kw):
        ctx = common(conn, request, None, user, need_snapshot=False, **kw)
        return _TPL.TemplateResponse(request, tpl, ctx)


    # ------------------------------------------------------------------ helpers
    def snapshots(conn) -> list[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM snapshots ORDER BY snapshot_date DESC")]

    def pick_date(conn, date: str | None) -> str:
        snaps = [s["snapshot_date"] for s in snapshots(conn)]
        if not snaps:
            raise HTTPException(404, "还没有任何快照，请先运行 python -m reportdb.ingest")
        if date:
            if date not in snaps:
                raise HTTPException(404, f"没有 {date} 的快照")
            return date
        return snaps[0]

    def load_sheet(conn, date: str, slug: str, mode: str | None) -> tuple[Sheet, str, str]:
        mode = mode or config.region_mode()
        row = conn.execute("SELECT payload, computed_at FROM report_results WHERE snapshot_date=? AND table_key=? "
                           "AND region_mode=?", (date, slug, mode)).fetchone()
        if row:
            try:
                return Sheet.from_dict(json.loads(row["payload"])), row["computed_at"], mode
            except KeyError:  # 旧版本落库的结果缺字段：现算
                pass
        sheets = calc.compute(conn, date, mode)  # 该口径尚未落库：现算（毫秒级）
        return sheets[slug], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mode

    def common(conn, request: Request, date: str | None, user=None, **kw) -> dict:
        ctx = _common(conn, request, date, user, **kw)
        flips = ex_mod.flipped_counts(conn, ctx["date"]) if ctx["date"] else {}
        ctx["exempt_total"] = sum(v["rows"] for v in flips.values())
        ctx["pending_exemptions"] = ex_mod.pending_count(conn) if user and user.role == "admin" else 0
        ctx["can_request_nav"] = bool(user and user.can_request)
        return ctx

    def _sso_account_url() -> str:
        return (os.getenv("REPORTDB_SSO_ACCOUNT_URL") or os.getenv("SSO_ACCOUNT_URL")
                or (os.getenv("REPORTDB_OIDC_ISSUER", "").rstrip("/") or "https://sso-m-hero.41box.com") + "/account").strip()

    def _common(conn, request: Request, date: str | None, user=None, *, need_snapshot: bool = True, **extra) -> dict:
        snaps = snapshots(conn)
        if need_snapshot or snaps:
            cur = pick_date(conn, date)
            info = next(s for s in snaps if s["snapshot_date"] == cur)
        else:
            cur, info = "", {}
        return {
            "request": request, "date": cur, "snaps": snaps, "snap": info, "user": user,
            "csrf": getattr(user, "csrf", ""), "is_admin": bool(user and user.role == "admin"),
            "can_view_admin": bool(user and getattr(user, "can_view_admin", False)),
            "is_global_admin": bool(user and getattr(user, "global_admin", False)),
            "summary_only": bool(user and user.role == "viewer"),
            "stores_synced_at": get_meta(conn, "stores_directory_synced_at") or get_meta(conn, "stores_synced_at"),
            "can_change_password": bool(user and backend.manages_users and user.auth_source == "local"),
            # SSO (OIDC) users manage their password in the IdP's personal center (Casdoor /account)
            "account_url": _sso_account_url() if user and not backend.manages_users else "",
            "mode": config.region_mode(), "table_titles": TABLE_TITLES, "datasets": DATASETS, **extra,
        }

    HOME_ORDER = ["task", "first", "second", "new", "renewal", "spare"]   # 首页列顺序：招揽、首保、二保、新保、续保、备库率
    HOME_SUB = {"task": ("应招揽", "已招揽"), "first": ("应保", "实绩"), "second": ("应保", "实绩"), "new": ("新车交付", "已投保"),
                "renewal": ("到期", "已续保"), "spare": ("应备店", "达标店")}

    def home_table(sheet: Sheet, sort: str = "", region: str = "") -> dict:
        """首页「像 Excel 一样」的总表：行 = 全国 + 各区域（顺序同总表 sheet），列 = 各指标的率，分子/分母作副行，排名作角标。"""
        blocks = {b.key: b for b in sheet.blocks}
        keys = [k for k in HOME_ORDER if k in blocks]
        labels = [r[0] for r in blocks[keys[0]].rows]

        def cell(key, row, is_total):
            b = blocks[key]
            den, hit = row[1], row[2]
            if key == "task":   # 总表里该块第 3 列是「未招揽数」
                hit = (row[1] - row[2]) if row[1] is not None and row[2] is not None else None
            return {"v": fmt_value(row[3], b.fmts[3]), "raw": row[3], "hit": hit, "den": den, "rank": None if is_total else row[4]}

        cols = [{"key": k, "title": blocks[k].title, "sub": "/".join(reversed(HOME_SUB[k])), "rate_h": blocks[k].headers[3]} for k in keys]
        rows = [{"label": lab, "total": False, "cells": [cell(k, blocks[k].rows[i], False) for k in keys]} for i, lab in enumerate(labels)]
        sort = sort if sort in keys else keys[0]   # 默认按第一个指标（招揽）名次；不再有综合排名

        def key_of(r):
            return r["cells"][keys.index(sort)]["rank"]
        order = sorted(range(len(rows)), key=lambda i: (key_of(rows[i]) is None, key_of(rows[i]) if key_of(rows[i]) is not None else 0, i))  # 并列：保持原区域顺序
        rows = [rows[i] for i in order]
        prev, pos = None, 0
        for n, r in enumerate(rows, 1):   # 名次：并列同号（竞赛排名 1,2,2,4）
            kv = key_of(r)
            if kv is None:
                r["pos"] = None
            else:
                if kv != prev:
                    pos = n
                    prev = kv
                r["pos"] = pos
        if region:   # 区域筛选：只留该区域一行（名次仍是全部区域里的名次），全国合计行保留作对照
            rows = [r for r in rows if r["label"].casefold() == region.casefold()]
        rows.insert(0, {"label": "全国", "total": True, "pos": None, "cells": [cell(k, blocks[k].total, True) for k in keys]})
        # 相对高低（非考核目标）：每列区域里最高=绿、最低=橙；并列取全部
        for j, _ in enumerate(keys):
            vals = [r["cells"][j]["raw"] for r in rows[1:] if r["cells"][j]["raw"] is not None]
            if len(set(vals)) > 1 and not region:
                hi, lo = max(vals), min(vals)
                for r in rows[1:]:
                    v = r["cells"][j]["raw"]
                    r["cells"][j]["tone"] = "hi" if v == hi else ("lo" if v == lo else "")
        return {"cols": cols, "rows": rows, "sort": sort,
                "sort_title": blocks[sort].title + "名次"}


    def store_regions(conn) -> dict[str, str]:
        return {(r["name"] or "").casefold(): r["region"] for r in conn.execute("SELECT name,region FROM stores")}

    def stores_of(conn, region: str = "") -> list[str]:
        q = "SELECT name FROM stores WHERE active=1" + (" AND region=?" if region else "") + " ORDER BY name"
        return [r[0] for r in conn.execute(q, (region,) if region else ())]

    def regions(conn) -> list[str]:
        return [r["name"] for r in conn.execute("SELECT name FROM regions ORDER BY sort_order")]

    def render_blocks(sheet: Sheet, region: str, q: str, srmap: dict[str, str]) -> list[dict]:
        out = []
        level_region = sheet.slug in ("summary", "region-ranking")
        for b in sheet.blocks:
            rows = []
            src_rows = b.rows
            if sheet.slug in ("summary", "direct-stores") and b.rank_col is not None:   # 展示按名次 1..N（并列保持原顺序，名次为空排最后）；库里与计算层的顺序不变
                src_rows = sorted(b.rows, key=lambda r: (r[b.rank_col] is None, r[b.rank_col] if r[b.rank_col] is not None else 0))
            for row in src_rows:
                label = str(row[0] or "")
                if region:
                    reg = label if level_region else srmap.get(label.casefold(), "")
                    if reg.casefold() != region.casefold():
                        continue
                if q and q.casefold() not in label.casefold():
                    continue
                rows.append(row)

            def cellify(row, is_total=False):
                cells = []
                for j, v in enumerate(row):
                    fm = b.fmts[j] if j < len(b.fmts) else None
                    cls = ""
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        cls = "num"
                    cells.append({"t": fmt_value(v, fm), "v": v if cls else (v or ""), "cls": cls})
                return cells

            out.append({
                "title": b.title, "headers": b.headers, "rank_col": b.rank_col,
                "rows": [cellify(r) for r in rows],
                "total": cellify(b.total, True) if (b.total is not None and not (region or q)) else None,
            })
        return out

    def dataset_query(conn, ds: Dataset, date: str, q: str, region: str, store: str, sort: str, desc: bool,
                      origin: str = "", scope=None, decision: str = "", in_scope_only: bool = False) -> tuple[str, list]:
        where, args = ["snapshot_date=?"], [date]
        if in_scope_only and ds.has_region:
            where.append("in_scope=1")
        ind = next((i for i in ex_mod.INDICATORS.values() if i.ds_key == ds.key), None)
        if decision and ind:   # 决定列筛选：done 已完成 / todo 未完成 / exempt 已豁免 / excluded 已剔除
            if decision == "done":
                where.append(f"COALESCE({ind.flag_col},'')=?")
                args.append(ind.hit)
            elif decision == "todo":
                where.append(f"COALESCE({ind.flag_col},'')<>?")
                args.append(ind.hit)
            elif decision == "exempt":
                where.append(f"upper(trim(vin)) IN ({ex_mod.active_vin_sql(ind.key)})")
                args += [ind.key, date, date]
            elif decision == "excluded":
                xs, xa = excl_mod.excluded_expr(conn, ind.table, date)
                where.append(xs)
                args += xa
        sw, sa = ex_mod.scope_where(ds, scope)   # 督导只看自己负责的门店
        if sw:
            where.append(sw)
            args += sa
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in ds.search_cols) + ")")
            args += [f"%{esc}%"] * len(ds.search_cols)
        if region and ds.has_region:
            where.append("region=?")
            args.append(region)
        elif region and ds.store_name_col:   # 没有 region 列的数据集（库存）：按门店表的归属区域
            where.append(f"lower({ds.store_name_col}) IN (SELECT lower(name) FROM stores WHERE active=1 AND region=?)")
            args.append(region)
        if store and ds.store_name_col:
            where.append(f"{ds.store_name_col}=?")
            args.append(store)
        if origin and ds.key == "renewal":
            where.append("origin=?")
            args.append(origin)
        cols = ["row_no"] + [c for c, _ in ds.columns] + (["region"] if ds.has_region else []) + (
            ["origin"] if ds.key == "renewal" else []) + (["part_key"] if False else [])
        order = sort if sort in cols else "row_no"
        sql_from = f" FROM {ds.table} WHERE " + " AND ".join(where)
        return sql_from + f" ORDER BY {order} {'DESC' if desc else 'ASC'}, row_no", args

    def labels_for(ds: Dataset) -> list[tuple[str, str]]:
        cols = list(ds.columns)
        if ds.has_region:
            cols.append(("region", "归属区域"))
        if ds.key == "renewal":
            cols.append(("origin", "来源"))
        return cols

    # ------------------------------------------------------------------ 页面
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, date: str | None = None, sort: str = "", region: str = "", conn=Depends(get_conn), user=Depends(login_required)):
        ctx = common(conn, request, date, user)
        sheet, computed_at, _ = load_sheet(conn, ctx["date"], "summary", None)
        home = home_table(sheet, sort, region)
        kpis = []
        quality = quality_report(conn, ctx["date"])
        return _TPL.TemplateResponse(request, "index.html", {**ctx, "region": region, "regions": regions(conn), "kpis": kpis, "spare_threshold": float(get_meta(conn, "spare_threshold", "0.8") or 0.8), "home": home, "indicator_cn": ex_mod.INDICATORS, "computed_at": computed_at,
                                                             "quality": quality, "authorized": user.full_detail,
                                                             "counts": json.loads(ctx["snap"]["row_counts"] or "{}")})

    @app.get("/tables/{slug}", response_class=HTMLResponse)
    def table_page(slug: str, request: Request, date: str | None = None, region: str = "", q: str = "",
                   mode: str | None = None, sort: str = "", conn=Depends(get_conn), user=Depends(login_required)):
        if slug in LEGACY_PAGES:
            qs = request.url.query
            return RedirectResponse(f"/tables/{LEGACY_PAGES[slug]}" + (f"?{qs}" if qs else ""), status_code=307)
        if slug not in TABLE_TITLES:
            raise HTTPException(404, "没有这张表")
        ctx = common(conn, request, date, user)
        sheet, computed_at, mode = load_sheet(conn, ctx["date"], CALC_SLUG.get(slug, slug), mode)
        if slug == "summary":
            return _TPL.TemplateResponse(request, "summary.html", {**ctx, "slug": slug, "title": TABLE_TITLES[slug][0],
                                         "desc": TABLE_TITLES[slug][1], "computed_at": computed_at, "home": home_table(sheet, sort, region),
                                         "region": region, "regions": regions(conn), "base_url": "/tables/summary", "region_mode": mode,
                                         "spare_threshold": float(get_meta(conn, "spare_threshold", "0.8") or 0.8),
                                         "export_url": f"/export/tables/summary.xlsx?date={ctx['date']}"})
        base = {"slug": slug, "title": TABLE_TITLES[slug][0], "desc": TABLE_TITLES[slug][1], "region": region, "q": q,
                "regions": regions(conn), "computed_at": computed_at, "region_mode": mode,
                "export_url": f"/export/tables-view/{slug}.xlsx?" + urlencode({k: v for k, v in {"date": ctx["date"], "region": region, "q": q}.items() if v})}
        if slug == "spare-parts":
            m = sheet.matrix or {}
            rows = [r for r in m.get("rows", [])
                    if (not region or (r["region"] or "").casefold() == region.casefold())
                    and (not q or q.casefold() in (r["name"] or "").casefold() or q.casefold() in (r["code"] or "").casefold())]
            cells = []
            for r in rows:
                cells.append({**r, "rate_t": fmt_value(r["rate"], "0%"), "ok": r["rate"] is not None and r["rate"] >= m["threshold"]})
            return _TPL.TemplateResponse(request, "spare.html", {**ctx, **base, "m": m, "rows": cells})
        blocks = render_blocks(sheet, region, q, store_regions(conn))
        return _TPL.TemplateResponse(request, "table.html", {**ctx, **base, "blocks": blocks})

    def _store_view(conn, date: str) -> dict:
        data = si_mod.compute(conn, date)
        return data

    def _fmt_store(st: dict) -> dict:
        for k in si_mod.ORDER:
            c = st["ind"][k]
            c["rate_t"] = fmt_value(c["rate"], PCT1_FMT)
            c["ex_total"] = c["ex_rule"] + c["ex_noregion"] + c["ex_vin"]
        st["spare"]["rate_t"] = fmt_value(st["spare"]["rate"], PCT1_FMT)
        return st

    @app.get("/stores", response_class=HTMLResponse)
    def stores_page(request: Request, date: str | None = None, region: str = "", q: str = "", sort: str = "region",
                    conn=Depends(get_conn), user=Depends(login_required)):
        ctx = common(conn, request, date, user)
        data = _store_view(conn, ctx["date"])
        rows = [_fmt_store(s) for s in data["stores"]
                if (not region or (s["region"] or "").casefold() == region.casefold())
                and (not q or q.casefold() in (s["name"] or "").casefold() or q.casefold() in (s["code"] or "").casefold())]
        if sort in si_mod.ORDER:
            rows.sort(key=lambda s: (s["ind"][sort]["rank"] is None, s["ind"][sort]["rank"] or 0, s["name"] or ""))
        elif sort == "spare":
            rows.sort(key=lambda s: (s["spare"]["rank"] is None, s["spare"]["rank"] or 0, s["name"] or ""))
        return _TPL.TemplateResponse(request, "stores.html", {
            **ctx, "rows": rows, "region": region, "q": q, "sort": sort, "regions": regions(conn), "heads": si_mod.HEADS,
            "order": si_mod.ORDER, "titles": {k: i.title for k, i in ex_mod.INDICATORS.items()}, "threshold": data["threshold"],
            "n_all": len(data["stores"])})

    @app.get("/stores/{code}", response_class=HTMLResponse)
    def store_detail(code: str, request: Request, date: str | None = None, conn=Depends(get_conn), user=Depends(login_required)):
        ctx = common(conn, request, date, user)
        data = _store_view(conn, ctx["date"])
        st = next((s for s in data["stores"] if (s["code"] or "").casefold() == code.casefold()), None)
        if not st:
            raise HTTPException(404, "没有这家门店")
        _fmt_store(st)
        inds = []
        for k in si_mod.ORDER:
            i = ex_mod.INDICATORS[k]
            c = st["ind"][k]
            inds.append({"key": k, "title": i.title, "ds_key": i.ds_key, "head": si_mod.HEADS[k], "c": c,
                         "url": f"/data/{i.ds_key}?" + urlencode({"date": ctx["date"], "store": st["name"]})})
        apps = []
        authorized = user.full_detail
        for r in ex_mod.list_exemptions(conn, store=st["name"], limit=200):
            if r["status"] in ("withdrawn",):
                continue
            r["vin_show"] = r["vin"] if authorized else mask_vin(r["vin"])
            r["ind_cn"] = "整车（所有指标）" if r["indicator"] == ex_mod.EXCLUDE_ALL else ex_mod.INDICATORS[r["indicator"]].title
            apps.append(r)
        return _TPL.TemplateResponse(request, "store_detail.html", {
            **ctx, "st": st, "inds": inds, "apps": apps, "status_cn": ex_mod.STATUS_CN, "kind_cn": ex_mod.KIND_CN,
            "spare_url": "/tables/spare-parts?" + urlencode({"date": ctx["date"], "q": st["name"]})})

    @app.get("/data/{key}", response_class=HTMLResponse)
    def data_page(key: str, request: Request, date: str | None = None, q: str = "", region: str = "", store: str = "",
                  origin: str = "", decision: str = "", scope_only: int = 0, page: int = 1, size: int = 50,
                  sort: str = "row_no", dir: str = "asc", conn=Depends(get_conn), user=Depends(login_required)):
        ds = BY_KEY.get(key)
        if not ds:
            raise HTTPException(404, "没有这个数据集")
        ctx = common(conn, request, date, user)
        authorized = user.full_detail
        size = max(10, min(size, 500))
        scope = ex_mod.scope_for(conn, user)   # 督导：只用于「能对哪些行申请」，不限制可见范围
        sql_from, args = dataset_query(conn, ds, ctx["date"], q, region, store, sort, dir == "desc", origin, None, decision, bool(scope_only))
        total = conn.execute("SELECT COUNT(*)" + sql_from.split(" ORDER BY")[0], args).fetchone()[0]
        pages = max(1, math.ceil(total / size))
        page = max(1, min(page, pages))
        cols = labels_for(ds)
        sel = ",".join(c for c, _ in cols)
        ind = next((i for i in ex_mod.INDICATORS.values() if i.ds_key == ds.key), None)
        flag_sql, flag_args = ex_mod.row_flag_select(conn, ind.key, ctx["date"]) if ind else ("0 AS _ex, 0 AS _xc, 0 AS _done", [])
        rows = [mask_row(ds, dict(r), authorized) for r in conn.execute(
            f"SELECT row_no,{sel},{flag_sql}" + sql_from + " LIMIT ? OFFSET ?", [*flag_args, *args, size, (page - 1) * size])]
        if scope is not None and ds.key != "stock":   # 标记哪些行属于本督导的门店（只有这些行能勾选申请）
            codes, names = scope["codes"], scope["names"]
            for r in rows:
                r["_mine"] = (str(r.get(ds.store_code_col) or "").casefold() in codes if ds.store_code_col else False) or \
                    str(r.get(ds.store_name_col) or "").casefold() in names
        else:
            for r in rows:
                r["_mine"] = True
        st = ex_mod.status_map(conn, ind.key, [ex_mod.vin_key(r.get("vin")) for r in rows]) if ind else {}
        for r in rows:
            r["_req"] = st.get(ex_mod.vin_key(r.get("vin")))
        reg_sql, reg_args = "", []
        if region:   # 门店下拉随区域联动
            reg_sql, reg_args = ((" AND region=?", [region]) if ds.has_region else
                                 (f" AND lower({ds.store_name_col}) IN (SELECT lower(name) FROM stores WHERE active=1 AND region=?)", [region]))
        stores_list = [x[0] for x in conn.execute(
            f"SELECT DISTINCT {ds.store_name_col} FROM {ds.table} WHERE snapshot_date=? AND {ds.store_name_col} IS NOT NULL{reg_sql} ORDER BY 1",
            (ctx["date"], *reg_args))] if ds.store_name_col else []
        params = {"date": ctx["date"], "q": q, "region": region, "store": store, "origin": origin, "size": size,
                  "decision": decision, "scope_only": scope_only or ""}

        def qs(**over):
            d = {**params, "sort": sort, "dir": dir, **over}
            return urlencode({k: v for k, v in d.items() if v not in ("", None)})

        resp = _TPL.TemplateResponse(request, "data.html", {
            **ctx, "ds": ds, "cols": cols, "rows": rows, "total": total, "page": page, "pages": pages, "size": size,
            "q": q, "region": region, "store": store, "origin": origin, "sort": sort, "dir": dir, "qs": qs,
            "my_rows": ex_mod.list_exemptions(conn, requester=user.username, indicator=ind.key, region=region, store=store, limit=100) if (ind and user.can_request) else [],
            "status_cn": ex_mod.STATUS_CN, "kind_cn": ex_mod.KIND_CN,
            "decision": decision, "scope_only": scope_only, "stores_list": stores_list, "filters_on": any([q, region, store, origin, decision, scope_only]),
            "regions": regions(conn), "authorized": authorized, "ind": ind, "can_request": bool(ind and user.can_request),
            "categories": ex_mod.CATEGORIES, "scoped": scope is not None, "scope_n": len(scope["names"]) if scope is not None else None, "ex_note": ex_mod.EXEMPTION_NOTE, "EXPLAIN": ex_mod.EXPLAIN,
            "export_url": "/export/data/%s.xlsx?%s" % (ds.key, urlencode({k: v for k, v in params.items() if v not in ("", None)}))})
        return resp

    # ------------------------------------------------------------------ 导出
    @app.get("/export/tables/{slug}.xlsx")
    def export_table(slug: str, date: str | None = None, mode: str | None = None, conn=Depends(get_conn), user=Depends(login_required)):
        date = pick_date(conn, date)
        if slug == "all":
            sheets = [replace(load_sheet(conn, date, s, mode)[0], name=EXPORT_NAMES[s]) for s in EXPORT_NAMES]
            name = f"区域各指标情况一览_{date}.xlsx"
        elif slug in TABLE_TITLES or slug in LEGACY_PAGES or slug in EXPORT_NAMES:
            slug = CALC_SLUG.get(LEGACY_PAGES.get(slug, slug), LEGACY_PAGES.get(slug, slug))
            sheets = [load_sheet(conn, date, slug, mode)[0]]
            name = f"{EXPORT_NAMES[slug]}_{date}.xlsx"
        else:
            raise HTTPException(404)
        return Response(sheets_to_xlsx(sheets), media_type=XLSX_MIME,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/export/data/{key}.xlsx")
    def export_data(key: str, request: Request, date: str | None = None, q: str = "", region: str = "", store: str = "",
                    origin: str = "", decision: str = "", scope_only: int = 0, conn=Depends(get_conn), user=Depends(login_required)):
        ds = BY_KEY.get(key)
        if not ds:
            raise HTTPException(404)
        date = pick_date(conn, date)
        authorized = user.full_detail
        sql_from, args = dataset_query(conn, ds, date, q, region, store, "row_no", False, origin, None, decision, bool(scope_only))
        cols = labels_for(ds)
        rows = (mask_row(ds, dict(r), authorized) for r in conn.execute(
            "SELECT " + ",".join(c for c, _ in cols) + sql_from + " LIMIT 200000", args))
        body = rows_to_xlsx(ds.title, [l for _, l in cols], ([r[c] for c, _ in cols] for r in rows))
        name = f"{ds.title}_{date}{'' if authorized else '_已脱敏'}.xlsx"
        return Response(body, media_type=XLSX_MIME,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    # ------------------------------------------------------------------ API
    @app.get("/api/health")
    def health():
        return {"status": "ok"}  # 免登录，仅供探活，不含任何数据

    @app.get("/api/snapshots")
    def api_snapshots(conn=Depends(get_conn), user=Depends(login_required)):
        out = snapshots(conn)
        for s in out:
            s["row_counts"] = json.loads(s["row_counts"] or "{}")
            s["files"] = json.loads(s["files"] or "{}")
        return out

    @app.get("/api/tables")
    def api_tables(user=Depends(login_required)):
        return [{"slug": k, "title": v[0], "description": v[1], "url": f"/tables/{k}", "api": f"/api/tables/{k}"}
                for k, v in TABLE_TITLES.items()]

    @app.get("/api/tables/{slug}")
    def api_table(slug: str, date: str | None = None, mode: str | None = None, conn=Depends(get_conn), user=Depends(login_required)):
        slug = CALC_SLUG.get(slug, slug)
        if slug not in calc.SLUG_ORDER:   # 含旧键 region-ranking / direct-ranking
            raise HTTPException(404)
        date = pick_date(conn, date)
        sheet, computed_at, mode = load_sheet(conn, date, slug, mode)
        d = sheet.to_dict()
        d.pop("cells", None)
        if d.get("matrix"):
            d["matrix"] = {**d["matrix"]}
        return {"snapshot_date": date, "computed_at": computed_at, "region_mode": mode, **d}

    @app.get("/api/stores")
    def api_stores(conn=Depends(get_conn), user=Depends(login_required)):
        return [dict(r) for r in conn.execute(
            "SELECT code,name,region,province,city,district,seq,direct_order,active,source,in_bitable,updated_at "
            "FROM stores ORDER BY seq")]

    @app.get("/api/quality")
    def api_quality(date: str | None = None, conn=Depends(get_conn), user=Depends(login_required)):
        return quality_report(conn, pick_date(conn, date))

    @app.get("/api/data/{key}")
    def api_data(key: str, request: Request, date: str | None = None, q: str = "", region: str = "", store: str = "",
                 origin: str = "", decision: str = "", scope_only: int = 0, page: int = 1, size: int = Query(50, ge=1, le=500),
                 sort: str = "row_no", dir: str = "asc", conn=Depends(get_conn), user=Depends(login_required)):
        ds = BY_KEY.get(key)
        if not ds:
            raise HTTPException(404)
        date = pick_date(conn, date)
        authorized = user.full_detail
        sql_from, args = dataset_query(conn, ds, date, q, region, store, sort, dir == "desc", origin, None, decision, bool(scope_only))
        total = conn.execute("SELECT COUNT(*)" + sql_from.split(" ORDER BY")[0], args).fetchone()[0]
        cols = labels_for(ds)
        page = max(1, page)
        rows = [mask_row(ds, dict(r), authorized) for r in conn.execute(
            "SELECT row_no," + ",".join(c for c, _ in cols) + sql_from + " LIMIT ? OFFSET ?", [*args, size, (page - 1) * size])]
        return {"snapshot_date": date, "total": total, "page": page, "size": size, "masked": not authorized,
                "columns": [{"key": c, "label": l} for c, l in cols], "rows": rows}

    def quality_report(conn, date: str) -> dict:
        """被剔除的明细：门店不在门店表（多维表格，启用中）内的行，不计入任何指标（区域、直营店、全国）。"""
        out = {}
        for ds in DATASETS:
            if not ds.has_region:
                continue
            tot = conn.execute(f"SELECT COUNT(*) FROM {ds.table} WHERE snapshot_date=?", (date,)).fetchone()[0]
            ex = conn.execute(f"SELECT COUNT(*) FROM {ds.table} WHERE snapshot_date=? AND in_scope=0", (date,)).fetchone()[0]
            stores = [{"store": r[0], "rows": r[1]} for r in conn.execute(
                f"SELECT COALESCE({ds.store_name_col},'(空)'), COUNT(*) c FROM {ds.table} WHERE snapshot_date=? AND in_scope=0 "
                f"GROUP BY 1 ORDER BY c DESC LIMIT 50", (date,))]
            out[ds.key] = {"title": ds.title, "rows": tot, "excluded_rows": ex, "in_scope_rows": tot - ex,
                           "excluded_stores": stores}
        stock_tot = conn.execute("SELECT COUNT(DISTINCT store_name) FROM stock_items WHERE snapshot_date=?", (date,)).fetchone()[0]
        stock_hit = conn.execute(
            "SELECT COUNT(DISTINCT s.store_name) FROM stock_items s JOIN stores t ON lower(t.name)=lower(s.store_name) AND t.active=1 "
            "WHERE s.snapshot_date=?", (date,)).fetchone()[0]
        out["exemptions"] = ex_mod.flipped_counts(conn, date)
        out["exclusions"] = excl_mod.excluded_counts(conn, date)
        out["exclusion_rules"] = excl_mod.list_rules(conn)
        out["stock"] = {"title": "门店备件库存", "stores_in_stock": stock_tot, "stores_matched_to_store_table": stock_hit,
                        "active_stores_without_stock": conn.execute(
                            "SELECT COUNT(*) FROM stores t WHERE t.active=1 AND NOT EXISTS (SELECT 1 FROM stock_items s "
                            "WHERE s.snapshot_date=? AND lower(s.store_name)=lower(t.name))", (date,)).fetchone()[0]}
        return out

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", conn=Depends(get_conn)):
        if service.load_session(conn, request.cookies.get(service.SESSION_COOKIE)):
            return RedirectResponse(safe_next(next), status_code=303)
        if not backend.supports_password_login:
            r = backend.login_redirect(request, safe_next(next))
            if r:
                resp = RedirectResponse(r[0], status_code=303)
                for k, v in r[1].items():
                    resp.set_cookie(k, v, httponly=True, samesite="lax", secure=secure_cookie(request), max_age=600, path="/")
                return resp
        import secrets as _s
        lc = _s.token_urlsafe(24)
        resp = _TPL.TemplateResponse(request, "login.html", {"next": safe_next(next), "csrf": lc, "error": "",
                                                              "password_login": backend.supports_password_login})
        resp.set_cookie("reportdb_lcsrf", lc, httponly=True, samesite="strict", secure=secure_cookie(request), max_age=1800, path="/login")
        return resp

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, username: str = Form(""), password: str = Form(""), csrf: str = Form(""),
                     next: str = Form("/"), conn=Depends(get_conn)):
        cookie = request.cookies.get("reportdb_lcsrf", "")
        if not cookie or not hmac.compare_digest(cookie.encode(), csrf.encode()):
            raise HTTPException(403, "表单已过期，请刷新登录页重试")
        if not backend.supports_password_login:
            raise HTTPException(400, "当前使用单点登录，不支持账号密码")
        try:
            ident = backend.authenticate_password(conn, username, password, client_ip(request))
        except AuthError as exc:
            import secrets as _s
            lc = _s.token_urlsafe(24)
            resp = _TPL.TemplateResponse(request, "login.html", {"next": safe_next(next), "csrf": lc, "error": str(exc),
                                                                  "username": username, "password_login": True},
                                         status_code=exc.status if exc.status in (401, 429) else 400)
            resp.set_cookie("reportdb_lcsrf", lc, httponly=True, samesite="strict", secure=secure_cookie(request), max_age=1800, path="/login")
            return resp
        row = service.get_user(conn, ident.username)
        resp = RedirectResponse(safe_next(next), status_code=303)
        resp.delete_cookie("reportdb_lcsrf", path="/login")
        return start_session(request, conn, row, resp)

    @app.get("/auth/oidc/login")
    def oidc_login(request: Request, next: str = "/"):
        r = backend.login_redirect(request, safe_next(next))
        if not r:
            raise HTTPException(404)
        resp = RedirectResponse(r[0], status_code=303)
        for k, v in r[1].items():
            resp.set_cookie(k, v, httponly=True, samesite="lax", secure=secure_cookie(request), max_age=600, path="/")
        return resp

    @app.get("/auth/oidc/logout")
    def oidc_logout_link(request: Request):
        """403 友好页的「退出登录」：结束统一登录会话后回到登录页（不需要本系统会话）。"""
        resp = RedirectResponse(backend.logout_redirect(request) or "/login", status_code=303)
        resp.delete_cookie(service.SESSION_COOKIE, path="/")
        return resp

    @app.get("/auth/oidc/callback")
    def oidc_callback(request: Request, conn=Depends(get_conn)):
        if not request.query_params.get("code") and not request.query_params.get("error") and not request.query_params.get("state"):
            return RedirectResponse("/login", status_code=303)      # back from the IdP logout
        try:
            ident, nxt = backend.complete_login(conn, request)
            row = service.provision_external(conn, ident)
        except AuthError as exc:
            if exc.status == 403:       # 账号已通过统一登录但没有开通本系统：友好页（返回黄页 / 退出登录），状态码仍为 403
                return HTMLResponse(render_no_access(exc.display_name, "区域报表", os.getenv("MHERO_AUTH_HUB_URL", "").strip() or DEFAULT_HUB_URL,
                                                     "/auth/oidc/logout", ds_dir=_DS_DIR), status_code=403, headers={"Cache-Control": "no-store"})
            return HTMLResponse(f"<meta name='viewport' content='width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover'><meta charset=utf-8><p>登录失败：{str(exc)[:200]}</p><p><a href='/login'>重新登录</a></p>", status_code=401)
        resp = RedirectResponse(safe_next(nxt), status_code=303)
        resp.delete_cookie(STATE_COOKIE, path="/")
        return start_session(request, conn, row, resp)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form(""), conn=Depends(get_conn), p: Principal = Depends(login_required)):
        check_csrf(p, csrf)
        service.destroy_session(conn, request.cookies.get(service.SESSION_COOKIE))
        resp = RedirectResponse(backend.logout_redirect(request) or "/login", status_code=303)
        resp.delete_cookie(service.SESSION_COOKIE, path="/")
        return resp

    # ---- 修改自己的密码
    @app.get("/account/password", response_class=HTMLResponse)
    def account_page(request: Request, msg: str = "", err: str = "", conn=Depends(get_conn), user=Depends(login_required)):
        return page(request, "account.html", conn, user, msg=msg, err=err, can_change=backend.manages_users and user.auth_source == "local")

    @app.post("/account/password")
    def account_change(request: Request, old: str = Form(""), new: str = Form(""), new2: str = Form(""), csrf: str = Form(""),
                       conn=Depends(get_conn), user=Depends(login_required)):
        check_csrf(user, csrf)
        row = service.get_user(conn, user.username)
        if user.auth_source != "local" or not service.verify_password(old, row["password_hash"]):
            return RedirectResponse("/account/password?" + urlencode({"err": "原密码不正确"}), status_code=303)
        if new != new2:
            return RedirectResponse("/account/password?" + urlencode({"err": "两次输入的新密码不一致"}), status_code=303)
        try:
            service.set_password(conn, user.username, new, user.username)
        except AuthError as exc:
            return RedirectResponse("/account/password?" + urlencode({"err": str(exc)}), status_code=303)
        resp = RedirectResponse("/login?" + urlencode({"next": "/"}), status_code=303)  # 改密码后所有会话失效，需重新登录
        resp.delete_cookie(service.SESSION_COOKIE, path="/")
        return resp

    # ---- 管理员：用户
    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request, msg: str = "", err: str = "", region: str = "", conn=Depends(get_conn), user=Depends(account_admin_required)):
        if backend.name == "oidc":      # 打开页面时自动从统一登录补齐账号（节流 60 秒；Casdoor 不可达时静默跳过）
            sso_sync.maybe_sync(conn_factory, account_bridge(), backend, min_interval=60)
        users = [dict(r) for r in conn.execute(
            "SELECT id,username,display_name,role,full_detail,disabled,auth_source,created_at,last_login,failed_count,locked_until,supervisor_name,global_admin FROM users ORDER BY id")]
        for u in users:
            u["locked"] = u["locked_until"] > time.time()
            if u["role"] == "supervisor":   # 可申请的门店：只按多维表格「督导」字段（督导手机号 = 账号名，或督导姓名）匹配
                sr = ex_mod.supervisor_stores(conn, u["username"], u["supervisor_name"] or "")
                u["scope_n"], u["scope_regions"] = len(sr), "、".join(sorted({r["region"] for r in sr if r["region"]}))
                u["_regions"] = {r["region"] for r in sr}
        if region:   # 区域筛选：只看在该区域有门店的督导
            users = [u for u in users if region in (u.get("_regions") or set())]
        return page(request, "admin_users.html", conn, user, region=region, regions=regions(conn), users=users, msg=msg, err=err,
                    manages=backend.manages_users, backend_name=backend.name, hub_accounts_url=os.getenv("MHERO_HUB_URL", "").rstrip("/") + "/admin/accounts" if os.getenv("MHERO_HUB_URL") else "",
                    audit=[dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 20")])

    def account_bridge():
        """统一账号服务（Casdoor 管理 API）；未配置返回 None。测试可通过 app.state.account_bridge 注入。"""
        if getattr(app.state, "account_bridge", None) is not None:
            return app.state.account_bridge
        if not hasattr(app.state, "_bridge_cache"):
            br = None
            try:
                from mhero_auth.accounts import AccountService
                from mhero_auth.casdoor_admin import CasdoorAdmin
                cl = CasdoorAdmin.from_env()
                if cl is not None:
                    br = AccountService(cl, audit_path=os.getenv("MHERO_ACCOUNTS_AUDIT_LOG", ""), revoke_path=os.getenv("MHERO_AUTH_REVOKE_FILE", ""))
            except Exception:  # noqa: BLE001
                br = None
            app.state._bridge_cache = br
        return app.state._bridge_cache

    @app.post("/admin/users/sync-sso")
    def admin_users_sync_sso(csrf: str = Form(""), conn=Depends(get_conn), user=Depends(account_admin_required)):
        """「从 SSO 同步」：把统一登录里能进入本系统的账号全部补进用户列表（默认不分配门店），已有账号同步显示名 / 身份。"""
        check_csrf(user, csrf)
        if backend.name != "oidc":
            return _back("/admin/users", err="当前不是单点登录模式，无需同步")
        br = account_bridge()
        if br is None:
            return _back("/admin/users", err="账号服务未配置，无法从 SSO 同步")
        try:
            snap = br.snapshot()
        except Exception as exc:  # noqa: BLE001
            return _back("/admin/users", err=f"读取统一登录账号失败：{str(exc)[:120]}")
        try:
            st = sso_sync.sync_users(conn, snap, backend, actor=user.username)
        except Exception as exc:  # noqa: BLE001
            return _back("/admin/users", err=f"从 SSO 同步失败：{str(exc)[:120]}")
        msg = (f"已从 SSO 同步：统一登录账号 {st['casdoor']} 个，可进入本系统 {st['eligible']} 个；"
               f"新增 {len(st['created'])} 个{('（' + '、'.join(st['created']) + '）') if st['created'] else ''}，"
               f"更新 {len(st['updated'])} 个，停用 {len(st['disabled'])} 个")
        if st["case_conflicts"]:
            msg += f"；{len(st['case_conflicts'])} 个账号与本地账号只差大小写，未合并（{'、'.join(st['case_conflicts'])}）"
        if st["local_without_access"]:
            msg += f"；另有 {len(st['local_without_access'])} 个本地账号在统一登录里已无本系统权限（未删除，登录会被拒）"
        return _back("/admin/users", msg=msg)

    def _sso_sync_loop():
        """后台定时同步（默认每小时；REPORTDB_SSO_SYNC_INTERVAL=0 关闭），保证新建账号无需管理员操作也会出现在用户列表。"""
        try:
            interval = float(os.getenv("REPORTDB_SSO_SYNC_INTERVAL", "3600") or 0)
        except ValueError:
            interval = 3600.0
        if interval <= 0:
            return
        time.sleep(min(30.0, interval))
        while True:
            try:
                sso_sync.maybe_sync(conn_factory, account_bridge(), backend, min_interval=0)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval)

    if backend.name == "oidc":
        import threading as _th
        _th.Thread(target=_sso_sync_loop, name="reportdb-sso-sync", daemon=True).start()

    @app.post("/admin/preview/{uid}/start")
    def preview_start(uid: int, csrf: str = Form(""), conn=Depends(get_conn), user=Depends(admin_required)):
        check_csrf(user, csrf)
        t = conn.execute("SELECT id,username,display_name FROM users WHERE id=? AND role='supervisor' AND disabled=0", (uid,)).fetchone()
        if not t:
            return _back("/admin/users", err="只能预览已启用的督导账号")
        service.audit(conn, user.username, "preview.start", t["username"])
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(PREVIEW_COOKIE, str(uid), httponly=True, samesite="lax", max_age=4 * 3600, path="/")
        return resp

    @app.post("/admin/preview/exit")
    def preview_exit(request: Request, csrf: str = Form(""), conn=Depends(get_conn), user=Depends(login_required)):
        check_csrf(user, csrf)
        if user.preview_admin:
            service.audit(conn, user.preview_admin, "preview.exit", user.username)
        resp = RedirectResponse("/admin/users", status_code=303)
        resp.delete_cookie(PREVIEW_COOKIE, path="/")
        return resp

    def _back(path, **q):
        return RedirectResponse(path + ("&" if "?" in path else "?") + urlencode(q), status_code=303)

    @app.post("/admin/users/create")
    def admin_user_create(username: str = Form(""), password: str = Form(""), role: str = Form("viewer"),
                          full_detail: str = Form(""), supervisor_name: str = Form(""), csrf: str = Form(""), conn=Depends(get_conn), user=Depends(account_admin_required)):
        check_csrf(user, csrf)
        if not backend.manages_users:
            raise HTTPException(400, "当前为单点登录，用户在 IdP 中管理")
        try:
            service.create_user(conn, username, password, role=role, full_detail=bool(full_detail), created_by=user.username,
                                supervisor_name=supervisor_name.strip(), display_name=supervisor_name.strip())
        except AuthError as exc:
            return _back("/admin/users", err=str(exc))
        return _back("/admin/users", msg=f"已创建用户 {username}")

    @app.post("/admin/users/{uid}/update")
    def admin_user_update(uid: int, role: str = Form(""), full_detail: str = Form(""), disabled: str = Form(""),
                          action: str = Form("save"), csrf: str = Form(""), conn=Depends(get_conn), user=Depends(account_admin_required)):
        check_csrf(user, csrf)
        if backend.name == "oidc" and action != "unlock":
            return _back("/admin/users", err="身份、系统角色和停用都在黄页「账号管理」里统一设置（只限全局超级管理员）")
        try:
            tgt = conn.execute("SELECT username,role,disabled FROM users WHERE id=?", (uid,)).fetchone()
            if backend.name == "oidc" and tgt and action in ("toggle", "save") and (action == "toggle" or (role and role != tgt["role"])):
                # 单点登录下，身份（管理员/督导）和停用以 Casdoor 为准：先改 Casdoor（含「不能给自己降权 / 不能动最后一名管理员」校验），再同步本地
                br = account_bridge()
                if br is None:
                    raise AuthError("账号服务未配置，请到首页「账号管理」修改身份 / 停用", 400)
                from mhero_auth.accounts import AccountError as _AE
                try:
                    if action == "toggle":
                        br.set_disabled(user.username, tgt["username"], not tgt["disabled"])
                    else:
                        if role not in ("admin", "supervisor"):
                            raise AuthError("单点登录下身份只能是 管理员 或 督导", 400)
                        br.set_level(user.username, tgt["username"], role)
                except _AE as exc:
                    raise AuthError(str(exc), 400)
            if action == "toggle":
                row = conn.execute("SELECT disabled FROM users WHERE id=?", (uid,)).fetchone()
                if uid == user.id:
                    raise AuthError("不能停用自己", 400)
                service.update_user(conn, uid, user.username, disabled=not (row and row["disabled"]))
            elif action == "unlock":
                with conn:
                    conn.execute("UPDATE users SET failed_count=0, locked_until=0 WHERE id=?", (uid,))
            else:
                if uid == user.id and role and role != "admin":
                    raise AuthError("不能降级自己", 400)
                service.update_user(conn, uid, user.username, role=role or None, full_detail=bool(full_detail))
        except AuthError as exc:
            return _back("/admin/users", err=str(exc))
        return _back("/admin/users", msg="已更新")

    @app.post("/admin/users/{uid}/password")
    def admin_user_password(uid: int, password: str = Form(""), csrf: str = Form(""), conn=Depends(get_conn), user=Depends(account_admin_required)):
        check_csrf(user, csrf)
        row = conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404)
        try:
            service.set_password(conn, row["username"], password, user.username)
        except AuthError as exc:
            return _back("/admin/users", err=str(exc))
        return _back("/admin/users", msg=f"已重置 {row['username']} 的密码，其旧会话已失效")

    # ------------------------------------------------------------------ 设置：常备件名单（管理员）
    def latest_snapshot(conn):
        r = conn.execute("SELECT MAX(snapshot_date) FROM snapshots").fetchone()
        return r[0] if r else None

    @app.get("/settings/spare-parts", response_class=HTMLResponse)
    def spare_settings(request: Request, msg: str = "", err: str = "", conn=Depends(get_conn), user=Depends(admin_view_required)):
        snap = latest_snapshot(conn)
        today = datetime.now().strftime("%Y-%m-%d")
        active = spare_mod.list_for_date(conn, today if not snap or today >= snap else snap)
        return page(request, "spare_settings.html", conn, user, active=active, history=spare_mod.history(conn), msg=msg,
                    err=err, today=today, snap=snap, regions=regions(conn))

    @app.get("/settings/spare-parts/template.xlsx")
    def spare_template(version: int | None = None, conn=Depends(get_conn), user=Depends(admin_view_required)):
        lst = spare_mod.get_list(conn, version) if version else spare_mod.list_for_date(conn, datetime.now().strftime("%Y-%m-%d"))
        name = f"常备件名单模板_v{lst['id']}.xlsx"
        return Response(spare_mod.template_xlsx(lst["items"]), media_type=XLSX_MIME,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.post("/settings/spare-parts/upload")
    def spare_upload(csrf: str = Form(""), file: UploadFile = File(...), conn=Depends(get_conn), user=Depends(admin_required)):
        check_csrf(user, csrf)
        raw = file.file.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            return _back("/settings/spare-parts", err="文件超过 5MB")
        try:
            parts = spare_mod.parse_upload(file.filename or "", raw)
            spare_mod.preview(conn, [dict(p) for p in parts], latest_snapshot(conn))  # 先校验（重复/全不计入等）
        except spare_mod.SpareListError as exc:
            return _back("/settings/spare-parts", err=str(exc))
        did = spare_mod.save_draft(conn, user.username, file.filename or "", parts)
        return RedirectResponse(f"/settings/spare-parts/preview/{did}", status_code=303)

    @app.get("/settings/spare-parts/preview/{draft_id}", response_class=HTMLResponse)
    def spare_preview(draft_id: int, request: Request, conn=Depends(get_conn), user=Depends(admin_view_required)):
        try:
            d = spare_mod.load_draft(conn, draft_id)
            pv = spare_mod.preview(conn, d["parts"], latest_snapshot(conn))
        except spare_mod.SpareListError as exc:
            return _back("/settings/spare-parts", err=str(exc))
        return page(request, "spare_preview.html", conn, user, pv=pv, draft=d, today=datetime.now().strftime("%Y-%m-%d"))

    @app.post("/settings/spare-parts/confirm")
    def spare_confirm(draft_id: int = Form(...), effective_from: str = Form(""), note: str = Form(""), csrf: str = Form(""),
                      conn=Depends(get_conn), user=Depends(admin_required)):
        check_csrf(user, csrf)
        try:
            d = spare_mod.load_draft(conn, draft_id)
            lid = spare_mod.create_version(conn, d["parts"], effective_from, user.username, note=note[:200],
                                           source=f"upload:{d['filename']}", filename=d["filename"])
        except spare_mod.SpareListError as exc:
            return _back("/settings/spare-parts", err=str(exc))
        done = spare_mod.recompute_from(conn, effective_from)
        service.audit(conn, user.username, "spare.create", f"v{lid} from {effective_from}; recomputed {len(done)}")
        return _back("/settings/spare-parts", msg=f"已生效：v{lid}（自 {effective_from} 起），重算了 {len(done)} 个快照")

    @app.post("/settings/spare-parts/rollback")
    def spare_rollback(list_id: int = Form(...), effective_from: str = Form(""), csrf: str = Form(""),
                       conn=Depends(get_conn), user=Depends(admin_required)):
        check_csrf(user, csrf)
        try:
            lid = spare_mod.rollback(conn, list_id, effective_from, user.username)
        except spare_mod.SpareListError as exc:
            return _back("/settings/spare-parts", err=str(exc))
        done = spare_mod.recompute_from(conn, effective_from)
        service.audit(conn, user.username, "spare.rollback", f"v{list_id} -> v{lid} from {effective_from}")
        return _back("/settings/spare-parts", msg=f"已回滚：v{list_id} 的内容生成新版本 v{lid}（自 {effective_from} 起），重算了 {len(done)} 个快照")

    # ------------------------------------------------------------------ 豁免：督导申请
    def requester_required(p: Principal = Depends(login_required)) -> Principal:
        if not p.can_request:
            raise HTTPException(403, "只有督导/管理员可以申请修正结果 / 剔除")
        return p

    @app.get("/exemptions/new", response_class=HTMLResponse)
    def exemption_new(request: Request, indicator: str = "first_service", kind: str = "exempt", date: str | None = None,
                      q: str = "", store: str = "", region: str = "", msg: str = "", err: str = "", row: str = "", ok: str = "", conn=Depends(get_conn), user=Depends(requester_required)):
        if indicator not in ex_mod.INDICATORS:
            raise HTTPException(404)
        ctx = common(conn, request, date, user)
        scope = ex_mod.scope_for(conn, user)
        if kind == "exclude":
            return RedirectResponse("/exclusions/new", status_code=303)
        cands = ex_mod.candidates(conn, indicator, ctx["date"], scope, q=q, store=store, limit=200, region=region)
        mine = ex_mod.list_exemptions(conn, requester=user.username, indicator=indicator, region=region, kind="exempt", limit=200)
        ds_ = BY_KEY[ex_mod.INDICATORS[indicator].ds_key]
        cols = labels_for(ds_)   # 与 /data/* 明细页同样的列、顺序、标签
        full = {}
        if cands:
            rn = [c["row_no"] for c in cands]
            for r in conn.execute(f"SELECT row_no,{','.join(c for c, _ in cols)} FROM {ds_.table} WHERE snapshot_date=? AND row_no IN ({','.join('?' * len(rn))})", (ctx["date"], *rn)):
                full[r["row_no"]] = mask_row(ds_, dict(r), user.full_detail)
        for c in cands:
            c["full"] = full.get(c["row_no"], {})
        return _TPL.TemplateResponse(request, "exemption_new.html", {
            **ctx, "region": region, "regions": regions(conn), "stores_list": stores_of(conn, region), "my_rows": mine, "req_kind": "exempt", "status_cn": ex_mod.STATUS_CN, "indicator": indicator, "kind": "exempt", "kind_cn": ex_mod.KIND_CN, "inds": ex_mod.INDICATORS, "ind": ex_mod.INDICATORS[indicator], "cands": cands, "q": q,
            "store": store, "categories": ex_mod.CATEGORIES, "msg": msg, "err": err, "scoped": scope is not None,
            "scope_n": len(scope["names"]) if scope is not None else None, "ex_note": ex_mod.EXEMPTION_NOTE, "EXPLAIN": ex_mod.EXPLAIN, "cols": cols,
            "back_url": "/exemptions/new?" + urlencode({k: v for k, v in dict(indicator=indicator, date=ctx["date"], region=region, store=store, q=q).items() if v}),
            "row": row, "ok": ok})

    @app.get("/exclusions/new", response_class=HTMLResponse)
    def exclusion_new(request: Request, date: str | None = None, q: str = "", store: str = "", region: str = "", msg: str = "", err: str = "", row: str = "", ok: str = "",
                      conn=Depends(get_conn), user=Depends(requester_required)):
        ctx = common(conn, request, date, user)
        scope = ex_mod.scope_for(conn, user)
        cands = ex_mod.vehicle_candidates(conn, ctx["date"], scope, q=q, store=store, region=region, limit=200)
        for c in cands:
            c["vin_show"] = mask_row(BY_KEY["task"], {"vin": c["vin"]}, user.full_detail)["vin"]
        mine = ex_mod.list_exemptions(conn, requester=user.username, region=region, kind="exclude", limit=200)
        return _TPL.TemplateResponse(request, "exclusion_new.html", {
            **ctx, "region": region, "regions": regions(conn), "stores_list": stores_of(conn, region), "my_rows": mine, "req_kind": "exclude",
            "status_cn": ex_mod.STATUS_CN, "kind_cn": ex_mod.KIND_CN, "inds": ex_mod.INDICATORS, "cands": cands, "q": q, "store": store,
            "msg": msg, "err": err, "scoped": scope is not None, "scope_n": len(scope["names"]) if scope is not None else None,
            "EXPLAIN_EXCLUDE": ex_mod.EXPLAIN_EXCLUDE,
            "back_url": "/exclusions/new?" + urlencode({k: v for k, v in dict(date=ctx["date"], region=region, store=store, q=q).items() if v}),
            "row": row, "ok": ok})

    ONE_ONLY = "每次只能提交 1 辆车的申请，不支持批量提交；请在对应车辆那一行点「提交申请」"

    def _safe_back(f, default):
        b = f.get("back") or ""
        return b if b.startswith("/") and not b.startswith("//") else default

    def _row_back(back, anchor, **q):
        base, _, frag = back.partition("#")
        return _back(base, **q) if not anchor else RedirectResponse(
            base + ("&" if "?" in base else "?") + urlencode(q) + "#" + anchor, status_code=303)

    @app.post("/exclusions/request")
    async def exclusion_request(request: Request, user=Depends(requester_required)):
        """整车剔除，单条提交：表单字段 xreason_<VIN>（该车理由，必填）。一次请求只允许 1 辆车，多于 1 辆整体拒绝。"""
        f = await request.form()
        check_csrf(user, f.get("csrf", ""))
        back = _safe_back(f, "/exclusions/new")
        keys = {k[8:] for k in f.keys() if k.startswith("xreason_")} | {k[6:] for k in f.keys() if k.startswith("xpick_")}
        conn = connect(path)
        try:
            try:
                if len(keys) > 1:
                    raise ex_mod.ExemptionError(ONE_ONLY)
                if not keys:
                    raise ex_mod.ExemptionError("没有可提交的车辆：请在要申请的车辆那一行填写理由并点「提交申请」")
                v = next(iter(keys))
                why = (f.get("xreason_" + v) or "").strip()
                if not why:
                    raise ex_mod.ExemptionError("请填写申请理由（必填）")
                snap = pick_date(conn, f.get("date"))
                res = ex_mod.request_exclusions(conn, user, snap, items=[(v, why)])
            except (ex_mod.ExemptionError, HTTPException) as exc:
                return _row_back(back, "r-" + (next(iter(keys)) if len(keys) == 1 else ""), err=str(getattr(exc, "detail", exc)),
                                 row=next(iter(keys)) if len(keys) == 1 else "")
            if not res["created"]:
                notes = [f"{v}（{w}）" for v, w in res["skipped"][:1]] + res["problems"][:1]
                return _row_back(back, "r-" + v, err="未提交：" + "；".join(notes), row=v)
            return _row_back(back, "r-" + v, msg="已提交 1 条剔除申请（整车），等待管理员审批；批准前不生效", row=v, ok="1")
        finally:
            conn.close()

    @app.post("/exemptions/request")
    async def exemption_request(request: Request, user=Depends(requester_required)):
        """修正结果，单条提交：表单字段 reason_<行号>（该车理由，必填）。一次请求只允许 1 辆车，多于 1 辆整体拒绝。
        （脚本用的 row_nos/vins + 共用 reason 也只接受 1 辆。）"""
        f = await request.form()
        check_csrf(user, f.get("csrf", ""))
        conn = connect(path)   # async 路由里自己开连接（依赖注入的连接属于线程池线程）
        try:
            return _do_exemption_request(f, conn, user)
        finally:
            conn.close()

    def _do_exemption_request(f, conn, user):
        indicator, kind = f.get("indicator", ""), "exempt"
        back = _safe_back(f, "/exemptions/new")
        row = ""
        try:
            snap = pick_date(conn, f.get("date"))
            rows = {k[7:] for k in f.keys() if k.startswith("reason_") and k[7:].isdigit()} | \
                   {k[5:] for k in f.keys() if k.startswith("pick_") and k[5:].isdigit()}
            legacy_rows = [x for x in f.getlist("row_nos") if str(x).strip()]
            legacy_vins = [x for x in re.split(r"[\s,;，；]+", (f.get("vins") or "").strip()) if x]
            if len(rows) + len(legacy_rows) + len(legacy_vins) > 1:
                raise ex_mod.ExemptionError(ONE_ONLY)
            if rows:
                row = next(iter(rows))
                why = (f.get("reason_" + row) or "").strip()
                if not why:
                    raise ex_mod.ExemptionError("请填写申请理由（必填）")
                res = ex_mod.request_exemptions(conn, user, indicator, snap, items=[(int(row), why)], kind=kind)
            elif legacy_rows or legacy_vins:
                row = legacy_rows[0] if legacy_rows else ""
                res = ex_mod.request_exemptions(
                    conn, user, indicator, snap, row_nos=[int(x) for x in legacy_rows], vins=legacy_vins,
                    category=f.get("category", ""), reason=f.get("reason", ""), kind=kind)
            else:
                raise ex_mod.ExemptionError("没有可提交的车辆：请在要申请的车辆那一行填写理由并点「提交申请」")
            if not res["created"]:
                notes = [f"{v}（{w}）" for v, w in res["skipped"][:1]] + res["problems"][:1]
                raise ex_mod.ExemptionError("未提交：" + "；".join(notes))
        except (ex_mod.ExemptionError, HTTPException) as exc:
            return _row_back(back, ("r-" + row) if row else "", err=str(getattr(exc, "detail", exc)), row=row)
        return _row_back(back, ("r-" + row) if row else "", msg="已提交 1 条修正结果申请，等待管理员审批", row=row, ok="1")

    @app.get("/exemptions/mine", response_class=HTMLResponse)
    def exemption_mine(request: Request, msg: str = "", err: str = "", status: str = "", indicator: str = "", kind: str = "",
                       q: str = "", region: str = "", store: str = "", date_from: str = "", date_to: str = "",
                       conn=Depends(get_conn), user=Depends(requester_required)):
        rows = ex_mod.list_exemptions(conn, requester=user.username, status=status, indicator=indicator, kind=kind, q=q,
                                      region=region, store=store, date_from=date_from, date_to=date_to)
        return page(request, "exemption_mine.html", conn, user, rows=rows, msg=msg, err=err, status_cn=ex_mod.STATUS_CN, inds=ex_mod.INDICATORS,
                    kind_cn=ex_mod.KIND_CN, status=status, indicator=indicator, kind=kind, q=q, date_from=date_from, date_to=date_to,
                    region=region, store=store, regions=regions(conn), stores_list=stores_of(conn, region))

    @app.post("/exemptions/{ex_id}/withdraw")
    def exemption_withdraw(ex_id: int, csrf: str = Form(""), conn=Depends(get_conn), user=Depends(requester_required)):
        check_csrf(user, csrf)
        try:
            ex_mod.withdraw(conn, user, ex_id)
        except ex_mod.ExemptionError as exc:
            return _back("/exemptions/mine", err=str(exc))
        return _back("/exemptions/mine", msg="已撤回")

    # ------------------------------------------------------------------ 豁免：管理员审批
    @app.get("/admin/exemptions", response_class=HTMLResponse)
    def admin_exemptions(request: Request, status: str = "pending", indicator: str = "", kind: str = "", q: str = "",
                         requester: str = "", store: str = "", region: str = "", date_from: str = "", date_to: str = "",
                         msg: str = "", err: str = "", conn=Depends(get_conn), user=Depends(admin_view_required)):
        rows = ex_mod.list_exemptions(conn, status=status, indicator=indicator, q=q, requester=requester, store=store, kind=kind,
                                      region=region, date_from=date_from, date_to=date_to)
        counts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM exemptions GROUP BY status")}
        return page(request, "admin_exemptions.html", conn, user, rows=rows, msg=msg, err=err, status=status, indicator=indicator, kind=kind, kind_cn=ex_mod.KIND_CN,
                    q=q, requester=requester, store=store, region=region, date_from=date_from, date_to=date_to, regions=regions(conn), stores_list=stores_of(conn, region),
                    counts=counts, status_cn=ex_mod.STATUS_CN, inds=ex_mod.INDICATORS,
                    export_qs=urlencode({k: v for k, v in dict(status=status, indicator=indicator, kind=kind, q=q, requester=requester, store=store, region=region, date_from=date_from, date_to=date_to).items() if v}))

    @app.get("/admin/exemptions.xlsx")
    def admin_exemptions_xlsx(status: str = "", indicator: str = "", kind: str = "", q: str = "", requester: str = "", store: str = "",
                              region: str = "", date_from: str = "", date_to: str = "", conn=Depends(get_conn), user=Depends(admin_view_required)):
        body = ex_mod.export_xlsx(ex_mod.list_exemptions(conn, status=status, indicator=indicator, kind=kind, q=q, requester=requester, store=store,
                                                         region=region, date_from=date_from, date_to=date_to, limit=100000))
        return Response(body, media_type=XLSX_MIME,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('修正结果与剔除清单.xlsx')}"})

    @app.post("/admin/exemptions/review")
    def admin_exemptions_review(action: str = Form(...), ids: list[int] = Form([]), comment: str = Form(""),
                                effective_from: str = Form(""), expires_at: str = Form(""), csrf: str = Form(""),
                                back: str = Form("/admin/exemptions"), conn=Depends(get_conn), user=Depends(admin_required)):
        check_csrf(user, csrf)
        try:
            res = ex_mod.review(conn, user, ids, action, comment.strip(), effective_from=effective_from, expires_at=expires_at)
        except ex_mod.ExemptionError as exc:
            return _back("/admin/exemptions", err=str(exc))
        word = {"approve": "批准", "reject": "驳回", "revoke": "撤销"}[action]
        msg = f"已{word} {len(res['changed'])} 条" + (f"，跳过 {len(res['skipped'])} 条（状态不符）" if res["skipped"] else "")
        if res["recomputed"]:
            msg += f"；已重算 {len(res['recomputed'])} 个快照（{res['recomputed'][0]} 起）"
        return _back("/admin/exemptions", msg=msg)

    @app.get("/admin/exemptions/{ex_id}", response_class=HTMLResponse)
    def admin_exemption_detail(ex_id: int, request: Request, conn=Depends(get_conn), user=Depends(admin_view_required)):
        r = conn.execute("SELECT * FROM exemptions WHERE id=?", (ex_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        return page(request, "admin_exemption_detail.html", conn, user, r=dict(r), log=ex_mod.history(conn, ex_id),
                    status_cn=ex_mod.STATUS_CN, kind_cn=ex_mod.KIND_CN, inds=ex_mod.INDICATORS)

    # ---- 管理员：给督导分配门店
    @app.get("/admin/users/{uid}/stores", response_class=HTMLResponse)
    def admin_user_stores(uid: int, request: Request, msg: str = "", region: str = "", conn=Depends(get_conn), user=Depends(account_admin_required)):
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404)
        matched = {r["code"] for r in ex_mod.supervisor_stores(conn, u["username"], u["supervisor_name"] or "")}
        stores = [dict(r) for r in conn.execute("SELECT code,name,region,supervisor,supervisor_phone FROM stores WHERE active=1" + (" AND region=?" if region else "") + " ORDER BY seq", (region,) if region else ())]
        return page(request, "admin_user_stores.html", conn, user, u=dict(u), stores=stores, mine=set(), by_name=matched, msg=msg, region=region, regions=regions(conn))

    @app.post("/admin/users/{uid}/stores")
    def admin_user_stores_save(uid: int, supervisor_name: str = Form(""), codes: list[str] = Form([]), region: str = Form(""), csrf: str = Form(""),
                               conn=Depends(get_conn), user=Depends(account_admin_required)):
        check_csrf(user, csrf)
        return _back(f"/admin/users/{uid}/stores", msg="督导门店只按飞书联络表的「督导」字段自动匹配，已不支持手工分配；请在飞书表里修改")
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404)
        valid = {r[0] for r in conn.execute("SELECT code FROM stores")}
        with conn:
            conn.execute("UPDATE users SET supervisor_name=? WHERE id=?", (supervisor_name.strip() or None, uid))
            if region:   # 按区域筛选时只改动该区域内的门店，其他区域已分配的保持不变
                conn.execute("DELETE FROM user_stores WHERE user_id=? AND store_code IN (SELECT code FROM stores WHERE region=?)", (uid, region))
            else:
                conn.execute("DELETE FROM user_stores WHERE user_id=?", (uid,))
            conn.executemany("INSERT INTO user_stores(user_id,store_code) VALUES (?,?)", [(uid, c) for c in set(codes) if c in valid])
        service.audit(conn, user.username, "user.stores", f"{u['username']} sup={supervisor_name} manual={len(codes)}")
        return _back(f"/admin/users/{uid}/stores", msg="已保存", **({"region": region} if region else {}))

    # ------------------------------------------------------------------ 日报导出（取代 Excel 日报）
    @app.get("/export/daily-report.xlsx")
    def export_daily_report(date: str | None = None, detail: int = 0, conn=Depends(get_conn), user=Depends(login_required)):
        date = pick_date(conn, date)
        body = daily_mod.build_daily_report(conn, date, include_detail=bool(detail), full_detail=user.full_detail)
        name = daily_mod.report_filename(date)
        return Response(body, media_type=XLSX_MIME, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/reports/daily", response_class=HTMLResponse)
    def daily_page(request: Request, date: str | None = None, region: str = "", conn=Depends(get_conn), user=Depends(login_required)):
        ctx = common(conn, request, date, user)
        hist = [{"date": s["snapshot_date"], "ingested_at": s["ingested_at"], "name": daily_mod.report_filename(s["snapshot_date"])}
                for s in ctx["snaps"]]
        return _TPL.TemplateResponse(request, "daily.html", {**ctx, "region": region, "regions": regions(conn), "hist": hist, "engine": os.getenv("MHERO_REPORT_ENGINE", "excel")})

    # ------------------------------------------------------------------ 设置：剔除规则（管理员）
    @app.get("/settings/exclusion-rules", response_class=HTMLResponse)
    def exclusion_rules_page(request: Request, msg: str = "", err: str = "", pv_dataset: str = "", pv_column: str = "",
                             pv_match: str = "equals", pv_value: str = "", region: str = "", conn=Depends(get_conn), user=Depends(admin_view_required)):
        snap = latest_snapshot(conn)
        reg_stores = {n.casefold() for n in stores_of(conn, region)} if region else None
        rules = excl_mod.list_rules(conn)
        for r in rules:
            try:
                r["pv"] = excl_mod.preview(conn, r["dataset_key"], r["column_name"], r["match_type"], r["value"], snap)
            except excl_mod.RuleError:
                r["pv"] = {"matched": None}
            if reg_stores is not None and r["pv"].get("matched"):   # 区域筛选：只列该区域门店的命中情况
                r["pv"] = {**r["pv"], "stores": [x for x in r["pv"].get("stores", []) if (x["store"] or "").casefold() in reg_stores]}
        pv = None
        if pv_value:
            try:
                pv = excl_mod.preview(conn, pv_dataset, pv_column, pv_match, pv_value, snap)
            except excl_mod.RuleError as exc:
                err = str(exc)
        cols = {k: [(c, l) for c, l in BY_KEY[k].columns] for k in excl_mod.RULE_DATASETS}
        return page(request, "exclusion_rules.html", conn, user, rules=rules, region=region, regions=regions(conn), pv=pv, msg=msg, err=err, snap=snap,
                    rule_datasets={k: BY_KEY[k].title for k in excl_mod.RULE_DATASETS}, cols=cols, match_types=excl_mod.MATCH_TYPES,
                    pv_in=dict(dataset=pv_dataset, column=pv_column, match=pv_match, value=pv_value),
                    counts=excl_mod.excluded_counts(conn, snap) if snap else {})

    def _rules_changed(conn, user, what: str):
        done = excl_mod.recompute_all(conn)
        service.audit(conn, user.username, "exclusion.rule", what)
        return len(done)

    @app.post("/settings/exclusion-rules/add")
    def exclusion_rule_add(dataset: str = Form(""), column: str = Form(""), match: str = Form("equals"), value: str = Form(""),
                           note: str = Form(""), csrf: str = Form(""), conn=Depends(get_conn), user=Depends(admin_required)):
        check_csrf(user, csrf)
        try:
            rid = excl_mod.add_rule(conn, user.username, dataset, column, match, value, note)
        except excl_mod.RuleError as exc:
            return _back("/settings/exclusion-rules", err=str(exc))
        n = _rules_changed(conn, user, f"add #{rid} {dataset}.{column} {match} {value}")
        return _back("/settings/exclusion-rules", msg=f"已添加规则 #{rid}，重算了 {n} 个快照")

    @app.post("/settings/exclusion-rules/{rid}/toggle")
    def exclusion_rule_toggle(rid: int, csrf: str = Form(""), conn=Depends(get_conn), user=Depends(admin_required)):
        check_csrf(user, csrf)
        row = conn.execute("SELECT enabled FROM exclusion_rules WHERE id=?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404)
        excl_mod.set_enabled(conn, rid, not row["enabled"])
        n = _rules_changed(conn, user, f"toggle #{rid} -> {int(not row['enabled'])}")
        return _back("/settings/exclusion-rules", msg=f"已{'停用' if row['enabled'] else '启用'}规则 #{rid}，重算了 {n} 个快照")

    @app.post("/settings/exclusion-rules/{rid}/delete")
    def exclusion_rule_delete(rid: int, csrf: str = Form(""), conn=Depends(get_conn), user=Depends(admin_required)):
        check_csrf(user, csrf)
        excl_mod.delete_rule(conn, rid)
        n = _rules_changed(conn, user, f"delete #{rid}")
        return _back("/settings/exclusion-rules", msg=f"已删除规则 #{rid}，重算了 {n} 个快照")

    # ------------------------------------------------------------------ 每个页面的「导出 Excel」（与页面同筛选、同脱敏、同权限）
    def xl_response(name: str, sheets) -> Response:
        return Response(multi_rows_to_xlsx(sheets), media_type=XLSX_MIME,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/export/home.xlsx")
    def export_home(date: str | None = None, region: str = "", sort: str = "", conn=Depends(get_conn), user=Depends(login_required)):
        """首页 / 总表页：全国 + 区域指标表（列同页面：名次、区域、各指标 率 / 分子 / 分母 / 名次）。"""
        date = pick_date(conn, date)
        home = home_table(load_sheet(conn, date, "summary", None)[0], sort, region)
        heads = ["名次", "区域"]
        for c in home["cols"]:
            a, b = c["sub"].split("/")
            heads += [f"{c['title']}完成率", f"{c['title']}·{a}", f"{c['title']}·{b}", f"{c['title']}名次"]
        rows = []
        for r in home["rows"]:
            line = ["" if r["total"] else (r["pos"] if r["pos"] is not None else ""), r["label"]]
            for c in r["cells"]:
                line += [c["raw"] if c["raw"] is not None else "", c["hit"], c["den"], c["rank"] or ""]
            rows.append(line)
        return xl_response(f"区域各指标情况一览_{date}{'_' + region if region else ''}.xlsx", [("区域各指标", heads, rows)])

    @app.get("/export/quality.xlsx")
    def export_quality(date: str | None = None, conn=Depends(get_conn), user=Depends(login_required)):
        """首页「数据质量与透明度」两张表：被剔除的明细、已剔除 / 已修正。"""
        date = pick_date(conn, date)
        qd = quality_report(conn, date)
        a = [[v["title"], v["rows"], v["in_scope_rows"], v["excluded_rows"], "、".join(f"{t['store']}（{t['rows']}）" for t in v["excluded_stores"])]
             for k, v in qd.items() if isinstance(v, dict) and "excluded_rows" in v]
        b = []
        for key, v in qd["exemptions"].items():
            e = qd["exclusions"][key]
            b.append([v["title"], e["by_rule"], e.get("no_region_rows", 0), e["by_vin_rows"], e["by_vin"], v["rows"], v["vins"]])
        return xl_response(f"数据质量与透明度_{date}.xlsx", [
            ("被剔除的明细", ["数据集", "总行数", "计入", "剔除", "主要被剔除门店（行数）"], a),
            ("已剔除与已修正", ["指标", "剔除：规则命中", "剔除：DMS 无区域", "剔除：整车申请（行）", "剔除：整车申请（VIN）", "修正结果：多计入分子的行", "修正结果：VIN 数"], b)])

    @app.get("/export/tables-view/{slug}.xlsx")
    def export_table_view(slug: str, date: str | None = None, region: str = "", q: str = "", conn=Depends(get_conn), user=Depends(login_required)):
        """区域指标 / 直营店指标 / 常备件备库率页：按页面当前的区域、门店关键字筛选后导出。"""
        if slug not in TABLE_TITLES or slug == "summary":
            raise HTTPException(404)
        date = pick_date(conn, date)
        sheet = load_sheet(conn, date, CALC_SLUG.get(slug, slug), None)[0]
        if slug == "spare-parts":
            m = sheet.matrix or {}
            rows = [r for r in m.get("rows", [])
                    if (not region or (r["region"] or "").casefold() == region.casefold())
                    and (not q or q.casefold() in (r["name"] or "").casefold() or q.casefold() in (r["code"] or "").casefold())]
            heads = ["门店", "编码", "区域", "备库率", "达标"] + [f"{c} {m['names'][i]}{'' if m['counted'][i] else '（不计入）'}" for i, c in enumerate(m["codes"])]
            body = [[r["name"], r["code"], r["region"], r["rate"], "" if r["rate"] is None else ("是" if r["rate"] >= m["threshold"] else "否"), *r["vals"]] for r in rows]
            sheets = [(TABLE_TITLES[slug][0], heads, body)]
        else:
            sheets = [(b["title"], b["headers"], [[c["v"] for c in row] for row in b["rows"]] + ([[c["v"] for c in b["total"]]] if b["total"] else []))
                      for b in render_blocks(sheet, region, q, store_regions(conn))]
        return xl_response(f"{EXPORT_NAMES[CALC_SLUG.get(slug, slug)]}_{date}{'_' + region if region else ''}.xlsx", sheets)

    @app.get("/export/stores.xlsx")
    def export_stores(date: str | None = None, region: str = "", q: str = "", sort: str = "region", conn=Depends(get_conn), user=Depends(login_required)):
        """门店指标列表：与页面同样的筛选、排序、列。"""
        date = pick_date(conn, date)
        data = _store_view(conn, date)
        rows = [_fmt_store(s) for s in data["stores"]
                if (not region or (s["region"] or "").casefold() == region.casefold())
                and (not q or q.casefold() in (s["name"] or "").casefold() or q.casefold() in (s["code"] or "").casefold())]
        if sort in si_mod.ORDER:
            rows.sort(key=lambda s: (s["ind"][sort]["rank"] is None, s["ind"][sort]["rank"] or 0, s["name"] or ""))
        elif sort == "spare":
            rows.sort(key=lambda s: (s["spare"]["rank"] is None, s["spare"]["rank"] or 0, s["name"] or ""))
        heads = ["#", "门店", "门店编码", "区域", "直营"]
        for k in si_mod.ORDER:
            t = ex_mod.INDICATORS[k].title
            heads += [f"{t}完成率", f"{t}·{si_mod.HEADS[k][1]}", f"{t}·{si_mod.HEADS[k][0]}", f"{t}全国名次"]
        heads += ["备库率", "备库率名次", "已修正（行）", "已剔除（行）", "待审批申请"]
        body = []
        for n, s in enumerate(rows, 1):
            line = [n, s["name"], s["code"], s["region"] or "", "是" if s["direct"] else "否"]
            for k in si_mod.ORDER:
                c = s["ind"][k]
                line += [c["rate"], c["num"], c["den"], c["rank"] or ""]
            fx = sum(s["ind"][k]["corrected"] for k in si_mod.ORDER)
            xx = sum(s["ind"][k]["ex_total"] for k in si_mod.ORDER)
            pend = sum(s["ind"][k].get("pending_fix", 0) for k in si_mod.ORDER) + (s.get("pending_exclude") or 0)
            line += [s["spare"]["rate"], s["spare"]["rank"] or "", fx, xx, pend]
            body.append(line)
        return xl_response(f"门店指标_{date}{'_' + region if region else ''}.xlsx", [("门店指标", heads, body)])

    @app.get("/export/stores/{code}.xlsx")
    def export_store_detail(code: str, date: str | None = None, conn=Depends(get_conn), user=Depends(login_required)):
        date = pick_date(conn, date)
        st = next((s for s in _store_view(conn, date)["stores"] if (s["code"] or "").casefold() == code.casefold()), None)
        if not st:
            raise HTTPException(404, "没有这家门店")
        _fmt_store(st)
        ind = []
        for k in si_mod.ORDER:
            c = st["ind"][k]
            ind.append([ex_mod.INDICATORS[k].title, c["den"], c["num"], c["rate"], f"第 {c['rank']} / {c['rank_of']}" if c["rank"] else "",
                        c["corrected"], c["ex_total"], c["ex_rule"], c["ex_noregion"], c["ex_vin"], c["pending_fix"]])
        ind.append(["常备件备库率", "", "达标" if st["spare"]["ok"] else "未达标", st["spare"]["rate"], f"第 {st['spare']['rank']}" if st["spare"]["rank"] else "", "", "", "", "", "", ""])
        apps = []
        for r in ex_mod.list_exemptions(conn, store=st["name"], limit=1000):
            if r["status"] == "withdrawn":
                continue
            apps.append([r["id"], ex_mod.KIND_CN.get(r["kind"], r["kind"]), ex_mod.indicator_title(r["indicator"]),
                         r["vin"] if user.full_detail else mask_vin(r["vin"]), r["reason"], r["requested_by"], r["requested_at"],
                         ex_mod.STATUS_CN.get(r["status"], r["status"]), r["review_comment"] or ""])
        return xl_response(f"门店指标_{st['name']}_{date}.xlsx", [
            ("指标明细", ["指标", "分母", "分子", "完成率", "全国名次", "已修正", "已剔除", "剔除：规则", "剔除：无区域", "剔除：整车", "待审批申请"], ind),
            ("本店申请", ["编号", "类型", "指标", "VIN", "理由", "申请人", "时间", "状态", "审批意见"], apps)])

    def _req_rows(rows, full: bool):
        return [[r["id"], ex_mod.KIND_CN.get(r["kind"], r["kind"]), r["vin"] if full else mask_vin(r["vin"]), ex_mod.indicator_title(r["indicator"]),
                 r["store_name"], r["region"] or "", r["reason"], r["requested_at"], ex_mod.STATUS_CN.get(r["status"], r["status"]),
                 r["review_comment"] or "", r["reviewed_by"] or "", r["reviewed_at"] or "", r["effective_from"] or "", r["expires_at"] or ""] for r in rows]
    REQ_HEADS = ["编号", "类型", "VIN", "指标", "门店", "区域", "理由", "申请时间", "状态", "审批意见", "审批人", "审批时间", "生效日期", "到期日期"]

    @app.get("/export/exemptions/mine.xlsx")
    def export_exemptions_mine(status: str = "", indicator: str = "", kind: str = "", q: str = "", region: str = "", store: str = "",
                               date_from: str = "", date_to: str = "", conn=Depends(get_conn), user=Depends(requester_required)):
        rows = ex_mod.list_exemptions(conn, requester=user.username, status=status, indicator=indicator, kind=kind, q=q,
                                      region=region, store=store, date_from=date_from, date_to=date_to, limit=100000)
        return xl_response(f"我的申请_{datetime.now():%Y-%m-%d}.xlsx", [("我的申请", REQ_HEADS, _req_rows(rows, user.full_detail))])

    @app.get("/export/exemptions/new.xlsx")
    def export_exemption_new(indicator: str = "first_service", date: str | None = None, q: str = "", store: str = "", region: str = "",
                             conn=Depends(get_conn), user=Depends(requester_required)):
        """申请修正结果页：当前指标可申请的车辆列表（列同页面，与 /data/* 同样脱敏）+ 我的申请明细。"""
        if indicator not in ex_mod.INDICATORS:
            raise HTTPException(404)
        date = pick_date(conn, date)
        scope = ex_mod.scope_for(conn, user)
        cands = ex_mod.candidates(conn, indicator, date, scope, q=q, store=store, limit=100000, region=region)
        ds_ = BY_KEY[ex_mod.INDICATORS[indicator].ds_key]
        cols = labels_for(ds_)
        full = {}
        rn = [c["row_no"] for c in cands]
        for i in range(0, len(rn), 500):
            part = rn[i:i + 500]
            for r in conn.execute(f"SELECT row_no,{','.join(c for c, _ in cols)} FROM {ds_.table} WHERE snapshot_date=? AND row_no IN ({','.join('?' * len(part))})", (date, *part)):
                full[r["row_no"]] = mask_row(ds_, dict(r), user.full_detail)
        body = [[n, *[full.get(c["row_no"], {}).get(k) for k, _ in cols], (ex_mod.KIND_CN[c["req"]["kind"]] + ex_mod.STATUS_CN[c["req"]["status"]]) if c["req"] else ""]
                for n, c in enumerate(cands, 1)]
        mine = ex_mod.list_exemptions(conn, requester=user.username, indicator=indicator, region=region, kind="exempt", limit=100000)
        t = ex_mod.INDICATORS[indicator].title
        return xl_response(f"申请修正结果_{t}_{date}.xlsx", [
            ("可申请车辆", ["#", *[l for _, l in cols], "已有申请"], body),
            ("我的申请明细", REQ_HEADS, _req_rows(mine, user.full_detail))])

    @app.get("/export/exclusions/new.xlsx")
    def export_exclusion_new(date: str | None = None, q: str = "", store: str = "", region: str = "", conn=Depends(get_conn), user=Depends(requester_required)):
        date = pick_date(conn, date)
        scope = ex_mod.scope_for(conn, user)
        cands = ex_mod.vehicle_candidates(conn, date, scope, q=q, store=store, region=region, limit=100000)
        body = [[n, mask_row(BY_KEY["task"], {"vin": c["vin"]}, user.full_detail)["vin"], c["store_code"] or "", c["store_name"] or "", c["region"] or "",
                 "、".join(c["inds"]), (ex_mod.KIND_CN[c["req"]["kind"]] + ex_mod.STATUS_CN[c["req"]["status"]]) if c["req"] else ""] for n, c in enumerate(cands, 1)]
        mine = ex_mod.list_exemptions(conn, requester=user.username, region=region, kind="exclude", limit=100000)
        return xl_response(f"申请剔除_{date}.xlsx", [
            ("可剔除车辆", ["#", "VIN", "门店编码", "门店名称", "归属区域", "涉及指标", "已有申请"], body),
            ("我的申请明细", REQ_HEADS, _req_rows(mine, user.full_detail))])

    @app.get("/export/admin/exemptions/{ex_id}.xlsx")
    def export_admin_exemption_detail(ex_id: int, conn=Depends(get_conn), user=Depends(admin_view_required)):
        r = conn.execute("SELECT * FROM exemptions WHERE id=?", (ex_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        return xl_response(f"申请{ex_id}.xlsx", [
            ("申请", REQ_HEADS, _req_rows([dict(r)], True)),
            ("审批记录", ["时间", "操作者", "动作", "状态变化", "意见"],
             [[h["at"], h["actor"], h["action"], f"{h['from_status'] or ''} → {h['to_status'] or ''}", h["comment"] or ""] for h in ex_mod.history(conn, ex_id)])])

    @app.get("/export/admin/users.xlsx")
    def export_admin_users(region: str = "", conn=Depends(get_conn), user=Depends(account_admin_required)):
        """账号列表（不含任何密码 / 令牌信息）。"""
        users = [dict(r) for r in conn.execute("SELECT id,username,display_name,role,disabled,auth_source,created_at,last_login,supervisor_name,global_admin FROM users ORDER BY id")]
        body = []
        for u in users:
            n, regs, rs = "", "", set()
            if u["role"] == "supervisor":   # 可申请门店：只按飞书联络表「督导」匹配（与页面、实际权限一致）
                sr = ex_mod.supervisor_stores(conn, u["username"], u["supervisor_name"] or "")
                rs = {r["region"] for r in sr if r["region"]}
                n, regs = len(sr), "、".join(sorted(rs))
            if region and region not in rs:
                continue
            label = {"admin": "超级管理员" if (u["global_admin"] or u["auth_source"] == "local") else "本系统超级管理员",
                     "readonly_admin": "只读管理员", "supervisor": "督导"}.get(u["role"], "用户")
            body.append([u["username"], u["display_name"] or "", label, u["supervisor_name"] or "",
                         n, regs, "已停用" if u["disabled"] else "正常", u["auth_source"], u["created_at"], u["last_login"] or ""])
        audit = [[a["at"], a["actor"], a["action"], a["detail"], a["ip"]] for a in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")]
        return xl_response(f"账号列表_{datetime.now():%Y-%m-%d}.xlsx", [
            ("账号", ["用户名", "姓名", "角色", "督导姓名", "可申请门店数（飞书匹配）", "所在区域", "状态", "来源", "创建时间", "最近登录"], body),
            ("最近操作记录", ["时间", "操作者", "动作", "详情", "IP"], audit)])

    @app.get("/export/admin/users/{uid}/stores.xlsx")
    def export_admin_user_stores(uid: int, region: str = "", conn=Depends(get_conn), user=Depends(account_admin_required)):
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404)
        matched = {r["code"] for r in ex_mod.supervisor_stores(conn, u["username"], u["supervisor_name"] or "")}
        rows = conn.execute("SELECT code,name,region,supervisor FROM stores WHERE active=1" + (" AND region=?" if region else "") + " ORDER BY seq", (region,) if region else ()).fetchall()
        body = [[r["name"], r["code"], r["region"] or "", r["supervisor"] or "", "是" if r["code"] in matched else "否"] for r in rows]
        return xl_response(f"可申请门店_{u['username']}.xlsx", [("可申请门店", ["门店", "门店编码", "区域", "飞书「督导」", "可申请"], body)])

    @app.get("/export/admin/exclusion-rules.xlsx")
    def export_exclusion_rules(region: str = "", conn=Depends(get_conn), user=Depends(admin_view_required)):
        snap = latest_snapshot(conn)
        reg_stores = {n.casefold() for n in stores_of(conn, region)} if region else None
        body, stores_rows = [], []
        for r in excl_mod.list_rules(conn):
            try:
                pv = excl_mod.preview(conn, r["dataset_key"], r["column_name"], r["match_type"], r["value"], snap)
            except excl_mod.RuleError:
                pv = {"matched": None}
            sts = pv.get("stores", []) or []
            if reg_stores is not None:
                sts = [x for x in sts if (x["store"] or "").casefold() in reg_stores]
            body.append([r["id"], r["dataset_key"], r["column_name"], excl_mod.MATCH_TYPES.get(r["match_type"], r["match_type"]),
                         r["value"], pv.get("matched"), pv.get("effective"), "启用" if r["enabled"] else "停用", r.get("note") or ""])
            stores_rows += [[r["id"], x["store"], x.get("rows")] for x in sts]
        return xl_response(f"剔除规则_{datetime.now():%Y-%m-%d}.xlsx", [
            ("剔除规则", ["#", "数据集", "列", "匹配", "值", "命中行", "其中计入范围内（实际剔除）", "状态", "备注"], body),
            ("命中门店", ["规则#", "门店", "行数"], stores_rows)])

    @app.get("/export/daily-history.xlsx")
    def export_daily_history(conn=Depends(get_conn), user=Depends(login_required)):
        body = [[s["snapshot_date"], s["ingested_at"] or "", daily_mod.report_filename(s["snapshot_date"])] for s in snapshots(conn)]
        return xl_response(f"历史日报清单_{datetime.now():%Y-%m-%d}.xlsx", [("历史日报", ["快照日期", "入库时间", "文件名"], body)])

    @app.get("/export/admin/spare-settings.xlsx")
    def export_spare_settings(conn=Depends(get_conn), user=Depends(admin_view_required)):
        snap = latest_snapshot(conn)
        today = datetime.now().strftime("%Y-%m-%d")
        active = spare_mod.list_for_date(conn, today if not snap or today >= snap else snap)
        items = [[it["sort_order"], it["code"], it["name"], "是" if it["counted"] else "否", it["std_qty"], it["group_label"] or ""] for it in active["items"]]
        hist = [[f"v{h['id']}", h["effective_from"], f"{h['created_at']} · {h['created_by']}", h["source"], h["n_items"], h["n_counted"], h["note"] or ""] for h in spare_mod.history(conn)]
        return xl_response(f"备件名单设置_{today}.xlsx", [
            (f"当前名单v{active['id']}", ["#", "备件号", "备件名称", "计入分母", "标准量", "分组"], items),
            ("版本历史", ["版本", "生效日期", "创建", "来源", "备件数", "计入数", "备注"], hist)])

    return app


def main() -> int:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="reportdb 只读 Web（FastAPI）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9013)
    ap.add_argument("--db", default="")
    args = ap.parse_args()
    uvicorn.run(create_app(args.db or None), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
