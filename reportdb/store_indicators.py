"""门店指标：每家店的 招揽/首保/二保/新保/续保 + 备库率，与首页「总表」同一口径、同一批 SQL 条件。

数据来源与 calc._agg 完全一致：in_scope=1、去掉剔除（规则 / DMS 无区域 / 已批准的整车剔除）、
分子 = 原始状态命中 OR 该 VIN 有生效的修正结果。因此 同一区域内所有门店分子/分母之和 == 首页该区域那一行。
另外给出透明度字段：已修正（真正把否改成是的行数）、已剔除（分别按规则 / DMS 无区域 / 整车剔除）、待审批申请数。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from . import calc
from .db import get_meta
from .datasets import BY_KEY
from .exclusions import NO_REGION_SQL, not_excluded, rule_match_sql, vin_excl_in_sql
from .exemptions import EXCLUDE_ALL, INDICATORS, active_vin_sql, hit_expr

ORDER = ["task", "first_service", "second_service", "new_policy", "renewal"]   # 与首页列顺序一致（备库率另算）
HEADS = {"task": ("应招揽", "已招揽"), "first_service": ("应保", "实绩"), "second_service": ("应保", "实绩"),
         "new_policy": ("新车交付", "已投保"), "renewal": ("到期", "已续保")}


def _rate(num: int, den: int) -> float | None:
    return None if not den else num / den


def _store_index(conn) -> tuple[dict[str, dict], dict[str, dict]]:
    by_code, by_name = {}, {}
    for r in conn.execute("SELECT * FROM stores WHERE active=1 ORDER BY seq, code"):
        d = dict(r)
        by_code[(d["code"] or "").casefold()] = d
        by_name.setdefault((d["name"] or "").casefold(), d)
    return by_code, by_name


def _resolve(by_code, by_name, code, name) -> dict | None:
    return by_code.get((code or "").casefold()) or by_name.get((name or "").casefold())


def compute(conn: sqlite3.Connection, snap: str) -> dict[str, Any]:
    """返回 {"stores": [...], "cells": {(store_code, region, ind): [den, num]}, "regions": [...]}。"""
    by_code, by_name = _store_index(conn)
    stores: dict[str, dict] = {}
    for s in by_code.values():
        stores[s["code"]] = {"code": s["code"], "name": s["name"], "region": s["region"], "direct": s["direct_order"] is not None,
                             "supervisor": s.get("supervisor") if "supervisor" in s else None, "ind": {}, "by_region": defaultdict(dict)}
    for st in stores.values():
        for k in ORDER:
            st["ind"][k] = {"den": 0, "hit": 0, "corrected": 0, "ex_rule": 0, "ex_noregion": 0, "ex_vin": 0, "pending_fix": 0}
        st["pending_exclude"] = 0

    for key in ORDER:
        ind = INDICATORS[key]
        ds = BY_KEY[ind.ds_key]
        t = ind.table
        sc, sn = ds.store_code_col, ds.store_name_col
        expr, ikey = hit_expr(t, ind.flag_col)
        xsql, xargs = not_excluded(conn, t, snap)
        # 与 calc._agg 同一 WHERE，只是多分一层门店/区域
        sql = (f"SELECT {sc} AS c, {sn} AS n, region AS rg, COUNT(*) AS den, SUM(CASE WHEN {expr} THEN 1 ELSE 0 END) AS h, "
               f"SUM(CASE WHEN COALESCE({ind.flag_col},'')<>? AND upper(trim(vin)) IN ({active_vin_sql(key)}) THEN 1 ELSE 0 END) AS fx "
               f"FROM {t} WHERE snapshot_date=? AND {sn} IS NOT NULL AND in_scope=1 AND {xsql} GROUP BY {sc}, {sn}, region")
        params = (ind.hit, ikey, snap, snap, ind.hit, key, snap, snap, snap, *xargs)
        for r in conn.execute(sql, params):
            s = _resolve(by_code, by_name, r["c"], r["n"])
            if not s:
                continue
            cell = stores[s["code"]]["ind"][key]
            cell["den"] += r["den"]
            cell["hit"] += r["h"] or 0
            cell["corrected"] += r["fx"] or 0
            br = stores[s["code"]]["by_region"][(r["rg"] or "").casefold()]
            b = br.setdefault(key, [0, 0])
            b[0] += r["den"]
            b[1] += r["h"] or 0
        # 被剔除的行
        m, ma = rule_match_sql(conn, ds.key)
        sql = (f"SELECT {sc} AS c, {sn} AS n, SUM(CASE WHEN {m} THEN 1 ELSE 0 END) AS rule, "
               f"SUM(CASE WHEN NOT {m} AND {NO_REGION_SQL} THEN 1 ELSE 0 END) AS nr, "
               f"SUM(CASE WHEN NOT {m} AND NOT {NO_REGION_SQL} AND {vin_excl_in_sql()} THEN 1 ELSE 0 END) AS vn "
               f"FROM {t} WHERE snapshot_date=? AND {sn} IS NOT NULL AND in_scope=1 GROUP BY {sc}, {sn}")
        for r in conn.execute(sql, (*ma, *ma, *ma, snap, snap, snap)):
            s = _resolve(by_code, by_name, r["c"], r["n"])
            if s:
                cell = stores[s["code"]]["ind"][key]
                cell["ex_rule"] += r["rule"] or 0
                cell["ex_noregion"] += r["nr"] or 0
                cell["ex_vin"] += r["vn"] or 0
    # 待审批申请
    for r in conn.execute("SELECT indicator, store_code, store_name FROM exemptions WHERE status='pending'"):
        s = _resolve(by_code, by_name, r["store_code"], r["store_name"])
        if not s:
            continue
        if r["indicator"] == EXCLUDE_ALL:
            stores[s["code"]]["pending_exclude"] += 1
        elif r["indicator"] in ORDER:
            stores[s["code"]]["ind"][r["indicator"]]["pending_fix"] += 1
    # 备库率
    thr = float(get_meta(conn, "spare_threshold", "0.8") or 0.8)
    sp = {}
    for r in calc.spare_matrix(conn, snap)["rows"]:
        sp.setdefault(r["code"], r["rate"])
    out = list(stores.values())
    for st in out:
        for k in ORDER:
            c = st["ind"][k]
            c["num"] = c["den"] - c["hit"] if k == "task" else c["hit"]   # 招揽：首页「已招揽」= 应招揽 − 未招揽(待处理)
            c["rate"] = _rate(c["num"], c["den"])
        r_ = sp.get(st["code"])
        st["spare"] = {"rate": r_, "ok": bool(r_ is not None and r_ >= thr), "threshold": thr}
        st["by_region"] = {rg: v for rg, v in st["by_region"].items()}
    # 全国名次（有分母的门店之间；并列同名次）
    for k in ORDER:
        for st, rk in zip(out, calc.rank_desc([st["ind"][k]["rate"] if st["ind"][k]["den"] else None for st in out])):
            st["ind"][k]["rank"] = rk
        st_n = sum(1 for st in out if st["ind"][k]["den"])
        for st in out:
            st["ind"][k]["rank_of"] = st_n
    for st, rk in zip(out, calc.rank_desc([st["spare"]["rate"] for st in out])):
        st["spare"]["rank"] = rk
    out.sort(key=lambda s: ((s["region"] or "~"), s["name"] or ""))
    return {"stores": out, "threshold": thr}


def region_totals(data: dict[str, Any]) -> dict[str, dict[str, list[int]]]:
    """按行的区域（region，取自 DMS 导出）汇总各门店的 [分母, 分子]，用于和首页区域行核对。"""
    tot: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for st in data["stores"]:
        for rg, d in st["by_region"].items():
            for k, (den, hit) in d.items():
                tot[rg][k][0] += den
                tot[rg][k][1] += hit
    return tot
