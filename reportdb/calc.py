"""计算层：用 SQLite 聚合 + Python 排名/排序，复现模板 5 张汇总 sheet（不依赖 Excel / LibreOffice）。

口径见 docs/REPORTDB.md。所有行列坐标与模板一致，便于逐格对比。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from . import config
from .db import get_meta
from .loaders import part_key
from .tables import PCT0, PCT1, Block, Sheet

SLUGS = {
    "direct-stores": "直营店指标",
    "region-ranking": "区域指标排序",
    "direct-ranking": "直营店指标排序",
    "summary": "总表",
    "spare-parts": "常备件备库率",
}
SLUG_ORDER = ["summary", "region-ranking", "direct-stores", "direct-ranking", "spare-parts"]


def ratio(a: float, b: float) -> float | None:
    """Excel: a/b，b=0 时为 #DIV/0!（这里记 None）。"""
    return None if not b else a / b


def ratio0(a: float, b: float) -> float | int:
    """Excel: IFERROR(a/b,0)。"""
    return 0 if not b else a / b


def rank_desc(values: list[float | None]) -> list[int | None]:
    """Excel RANK(v, range)（降序，并列同名次）；空值/错误值不参与、其本身排名为空。"""
    nums = [v for v in values if v is not None]
    return [None if v is None else 1 + sum(1 for x in nums if x > v) for v in values]


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def _agg(conn: sqlite3.Connection, table: str, snap: str, key_col: str, flag_col: str, flag_val: str,
         scoped: bool = True) -> dict[str, list[int]]:
    """COUNTIF / COUNTIFS 等价：按 key（不分大小写）统计总数与 flag=值 的个数。"""
    out: dict[str, list[int]] = {}
    # 分子 = 原始状态命中 OR 该 VIN 在该指标上有生效的修正结果（exemptions.py）；分母不变
    from .exemptions import BY_TABLE, hit_expr

    if table in BY_TABLE:
        from .exclusions import not_excluded

        expr, ind_key = hit_expr(table, flag_col)
        xsql, xargs = not_excluded(conn, table, snap) if scoped else ("1", [])  # 剔除（分子分母都不计）先于修正结果
        sql = (f"SELECT {key_col} AS k, COUNT(*) AS n, SUM(CASE WHEN {expr} THEN 1 ELSE 0 END) AS h "
               f"FROM {table} WHERE snapshot_date=? AND {key_col} IS NOT NULL{' AND in_scope=1' if scoped else ''} AND {xsql} GROUP BY {key_col}")
        params = (flag_val, ind_key, snap, snap, snap, *xargs)
    else:
        sql = (f"SELECT {key_col} AS k, COUNT(*) AS n, SUM(CASE WHEN {flag_col}=? THEN 1 ELSE 0 END) AS h "
               f"FROM {table} WHERE snapshot_date=? AND {key_col} IS NOT NULL{' AND in_scope=1' if scoped else ''} GROUP BY {key_col}")
        params = (flag_val, snap)
    for r in conn.execute(sql, params):
        cur = out.setdefault(r["k"].casefold(), [0, 0])
        cur[0] += r["n"]
        cur[1] += r["h"] or 0
    return out


def _get(d: dict[str, list[int]], key: str) -> tuple[int, int]:
    v = d.get(key.casefold())
    return (v[0], v[1]) if v else (0, 0)


def spare_matrix(conn: sqlite3.Connection, snap: str, parts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """门店×备件矩阵。parts 缺省取快照当天生效的名单版本（spare_lists）；传入则用于上传预览试算。"""
    list_meta = None
    if parts is None:
        from .spare import list_for_date

        lst = list_for_date(conn, snap)
        list_meta = {k: lst[k] for k in ("id", "effective_from", "created_at", "created_by", "note", "source")}
        parts = [dict(it, sort_order=it["sort_order"]) for it in lst["items"]]
    else:
        parts = [dict(p, sort_order=i) for i, p in enumerate(parts, 1)]
    stores = [dict(r) for r in conn.execute("SELECT * FROM stores WHERE active=1 ORDER BY seq, code")]
    sums: dict[tuple[str, str], float] = defaultdict(float)
    for r in conn.execute(
        "SELECT store_name, part_key, SUM(CASE WHEN typeof(available_qty) IN ('integer','real') THEN available_qty "
        "ELSE 0 END) AS q FROM stock_items WHERE snapshot_date=? AND store_name IS NOT NULL AND part_key IS NOT NULL "
        "GROUP BY store_name, part_key", (snap,)):
        sums[(r["store_name"].casefold(), r["part_key"])] += r["q"] or 0
    pkeys = [part_key(p["code"]) for p in parts]
    counted_idx = [i for i, p in enumerate(parts) if p["counted"]]
    rows = []
    for seq, s in enumerate(stores, 1):
        vals = []
        for pk in pkeys:
            v = sums.get(((s["name"] or "").casefold(), pk), 0)
            vals.append(int(v) if float(v) == int(v) else v)
        pos = sum(1 for i in counted_idx if vals[i] > 0)
        rate = pos / len(counted_idx) if counted_idx else None
        rows.append({"seq": seq, "code": s["code"], "name": s["name"], "region": s["region"], "rate": rate, "vals": vals})
    return {"parts": parts, "rows": rows, "counted": len(counted_idx), "list": list_meta}


# ---------------------------------------------------------------------------
# 区域级 / 门店级 指标块
# ---------------------------------------------------------------------------

def _band(sheet_slug: str, level: str, labels: list[str], d: dict[str, dict], spare: dict[str, Any],
          fmt: str, base_row: int, ifzero: bool, thr: float) -> list[Block]:
    """生成「总表」/「直营店指标」两层×3块 共6个块。level: 'region' | 'store'。"""
    n = len(labels)
    div = ratio0 if ifzero else ratio
    first_label = "区域" if level == "region" else "门店"
    blocks: list[Block] = []

    def make_rate_block(title, key, col, row, headers, vals, rate_fn, total_label):
        rates = [rate_fn(b, a) for a, b in vals]  # vals=(应保/总数, 实绩)；比率=实绩/应保
        ranks = rank_desc(rates)
        rows = [[lbl, a, b, r, k] for lbl, (a, b), r, k in zip(labels, vals, rates, ranks)]
        ta, tb = sum(a for a, _ in vals), sum(b for _, b in vals)
        total = [total_label, ta, tb, rate_fn(tb, ta), None]
        return Block(title=title, row=row, col=col, headers=headers, rows=rows, total=total, fmts=[None, None, None, fmt, None], rank_col=4, key=key)

    r1, r2 = base_row, base_row + n + 3
    f = first_label
    # 首保 / 二保 / 新保
    fs = [_get(d["first"], l) for l in labels]
    ss = [_get(d["second"], l) for l in labels]
    ns = [_get(d["new"], l) for l in labels]
    rn = [_get(d["renewal"], l) for l in labels]
    tk = [_get(d["task"], l) for l in labels]
    blocks.append(make_rate_block("首保实施率", "first", 1, r1, [f, "应保", "实绩", "首保实施率", "排名"], fs, div, "合计" if level == "store" else "全国"))
    blocks.append(make_rate_block("二保实施率", "second", 6, r1, [f, "应保", "实绩", "二保实施率", "排名"], ss, div, "合计" if level == "store" else "全国"))
    blocks.append(make_rate_block("新保投保率", "new", 11, r1, [f, "新车交付量", "新车投保", "新保投保率", "排名"], ns, div, "合计" if level == "store" else "全国"))
    blocks.append(make_rate_block("续保投保率", "renewal", 1, r2, [f, "续保到期", "实绩续保", "续保率", "排名"], rn, div, "合计" if level == "store" else "全国"))
    # 招揽：L=总数 M=未招揽（待处理）；实施率=(L-M)/L
    tkv = [(a, h) for a, h in tk]
    rates = [div(a - h, a) for a, h in tkv]
    ranks = rank_desc(rates)
    rows = [[l, a, h, r, k] for l, (a, h), r, k in zip(labels, tkv, rates, ranks)]
    ta, th = sum(a for a, _ in tkv), sum(h for _, h in tkv)
    tk_block = Block(title="招揽实施率", row=r2, col=11, headers=[f, "应招揽数", "未招揽数", "实施率", "排名"], rows=rows,
                     total=["合计" if level == "store" else "全国", ta, th, div(ta - th, ta), None],
                     fmts=[None, None, None, fmt, None], rank_col=4, key="task")
    # 常备件
    if level == "region":
        stores = spare["rows"]
        should = [sum(1 for s in stores if (s["region"] or "").casefold() == l.casefold()) for l in labels]
        actual = [sum(1 for s in stores if (s["region"] or "").casefold() == l.casefold()
                      and s["rate"] is not None and s["rate"] >= thr) for l in labels]
        rates = [div(a, b) for a, b in zip(actual, should)]
        ranks = rank_desc(rates)
        rows = [[l, a, b, r, k] for l, a, b, r, k in zip(labels, should, actual, rates, ranks)]
        ta, tb = sum(should), sum(actual)
        sp_block = Block(title="常备件备库率", row=r2, col=6, headers=[f, "应备店数", "实备店数", "达标率", "排名"], rows=rows,
                         total=["全国", ta, tb, div(tb, ta), None], fmts=[None, None, None, fmt, None], rank_col=4, key="spare")
    else:
        first_by_name: dict[str, float | None] = {}
        for s in spare["rows"]:
            first_by_name.setdefault((s["name"] or "").casefold(), s["rate"])
        g = [first_by_name.get(l.casefold()) or 0 for l in labels]
        ok = [1 if v >= thr else 0 for v in g]
        ranks = rank_desc(list(map(float, g)))
        rows = [[l, v, "是" if o else "否", o, k] for l, v, o, k in zip(labels, g, ok, ranks)]
        n_ok = sum(ok)
        avg = sum(g) / len(g) if g else None
        sp_block = Block(title="常备件备库率", row=r2, col=6, headers=[f, "备库率", "是否达标", "达标率", "排名"], rows=rows,
                         total=["合计", avg, f"{n_ok}/{len(labels)}", n_ok / len(labels) if labels else None, None],
                         fmts=[None, PCT1, None, PCT1, None], rank_col=4, key="spare")
    # 块顺序：首保 二保 新保 | 续保 常备件 招揽
    order = blocks[:3] + [blocks[3], sp_block, tk_block]
    return order


def compute(conn: sqlite3.Connection, snapshot_date: str, region_mode: str | None = None) -> dict[str, Sheet]:
    mode = region_mode or config.region_mode()
    rcol = "region" if mode == "store" else "src_region"
    thr = float(get_meta(conn, "spare_threshold", "0.8") or 0.8)
    regions = [r["name"] for r in conn.execute("SELECT name FROM regions ORDER BY sort_order")]
    directs = [r["name"] for r in conn.execute(
        "SELECT name FROM stores WHERE direct_order IS NOT NULL AND active=1 ORDER BY direct_order")]
    spare = spare_matrix(conn, snapshot_date)

    scoped = mode == "store"  # source 模式（隐藏的诊断开关）不剔除任何行，等同旧模板字面行为

    def pair(table, key_col, flag_col, flag):
        return _agg(conn, table, snapshot_date, key_col, flag_col, flag, scoped)

    out: dict[str, Sheet] = {}
    # 区域层（总表）
    dreg = {
        "first": pair("first_service", rcol, "done", "是"),
        "second": pair("second_service", rcol, "done", "是"),
        "new": pair("new_policy", rcol, "done", "是"),
        "renewal": pair("renewal", rcol, "done", "是"),
        "task": pair("task_items", rcol, "task_status", "待处理"),
    }
    summary = Sheet("summary", "总表", _band("summary", "region", regions, dreg, spare, PCT0, 1, False, thr))
    # 门店层（直营店指标）
    dst = {
        "first": pair("first_service", "store_name", "done", "是"),
        "second": pair("second_service", "store_name", "done", "是"),
        "new": pair("new_policy", "store_name", "done", "是"),
        "renewal": pair("renewal", "store_name", "done", "是"),
        "task": pair("task_items", "store_name", "task_status", "待处理"),
    }
    direct = Sheet("direct-stores", "直营店指标", _band("direct-stores", "store", directs, dst, spare, PCT1, 1, True, thr))
    # 排序页：块内按排名列（第5列）升序稳定排序；合计行引用未排序页（仅前 4 列）
    reg_rank = Sheet("region-ranking", "区域指标排序", [b.sorted_copy() for b in summary.blocks])
    reg_rank_fmt = PCT0
    dir_rank = Sheet("direct-ranking", "直营店指标排序", [b.sorted_copy() for b in direct.blocks])
    for sh in (reg_rank, dir_rank):
        for b in sh.blocks:
            if b.total is not None and len(b.total) > 4:
                b.total[4] = None
    # 常备件备库率 宽表
    sp = Sheet("spare-parts", "常备件备库率")
    parts, rows = spare["parts"], spare["rows"]
    heads = ["序号", "经销商编码", "经销商名称", "区域", "备库率"]
    codes = [int(p["code"]) if str(p["code"]).isdigit() else p["code"] for p in parts]
    cells: dict[tuple[int, int], Any] = {}
    fm: dict[tuple[int, int], str] = {}
    for j, h in enumerate(heads, 1):
        cells[(1, j)] = h
    for j, p in enumerate(parts):
        c = 6 + j
        cells[(1, c)] = codes[j]
        cells[(2, c)] = p["name"]
        if p["std_qty"] is not None:
            q = p["std_qty"]
            cells[(3, c)] = int(q) if float(q) == int(q) else q
        if p["group_label"]:
            cells[(4, c)] = p["group_label"]
    for i, s in enumerate(rows):
        r = 5 + i
        cells[(r, 1)], cells[(r, 2)], cells[(r, 3)], cells[(r, 4)], cells[(r, 5)] = s["seq"], s["code"], s["name"], s["region"], s["rate"]
        fm[(r, 5)] = PCT0
        for j, v in enumerate(s["vals"]):
            cells[(r, 6 + j)] = v
    sp.extra_cells, sp.extra_fmts = cells, fm
    sp.matrix = {
        "headers": heads, "codes": codes, "names": [p["name"] for p in parts],
        "std": [p["std_qty"] for p in parts], "groups": [p["group_label"] for p in parts],
        "counted": [p["counted"] for p in parts], "rows": rows, "counted_n": spare["counted"], "threshold": thr,
        "list": spare["list"],
    }
    out.update({"summary": summary, "region-ranking": reg_rank, "direct-stores": direct,
                "direct-ranking": dir_rank, "spare-parts": sp})
    return out


def compute_and_store(conn: sqlite3.Connection, snapshot_date: str, region_mode: str | None = None) -> int:
    """计算并把 5 张表的结果落库（report_results），返回耗时毫秒。"""
    mode = region_mode or config.region_mode()
    t0 = time.perf_counter()
    sheets = compute(conn, snapshot_date, mode)
    ms = int((time.perf_counter() - t0) * 1000)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        for slug, sh in sheets.items():
            conn.execute(
                "INSERT OR REPLACE INTO report_results(snapshot_date,table_key,region_mode,computed_at,payload)"
                " VALUES (?,?,?,?,?)", (snapshot_date, slug, mode, now, json.dumps(sh.to_dict(), ensure_ascii=False)))
        conn.execute("UPDATE snapshots SET calc_ms=?, region_mode=? WHERE snapshot_date=?", (ms, mode, snapshot_date))
    return ms
