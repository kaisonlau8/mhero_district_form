"""摄取入口：python -m reportdb.ingest --dir <download dir> --date YYYY-MM-DD

把 7 份源报表（或历史日报 xlsx 里的源数据 sheet）按 snapshot_date 写入 SQLite，并重算 5 张汇总表。
同一天重复摄取 = 覆盖该天（幂等）。不访问 DMS，不发送任何飞书消息，不写多维表格。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import calc, config
from .datasets import DATASETS
from .db import connect, migrate
from .loaders import load_directory_sources, load_report_workbook, to_db_rows
from .stores import StoreSyncAborted, apply_dms_regions, region_lookup_factory, sync_from_bitable, sync_from_directory_json


def _today() -> str:
    try:  # 与项目其余部分一致使用北京时间
        sys.path.insert(0, str(config.PROJECT_ROOT / "scripts"))
        from time_utils import beijing_strftime  # type: ignore

        return beijing_strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _write_dataset(conn: sqlite3.Connection, ds, snap: str, aligned, origins, lookup) -> int:
    cols, rows = to_db_rows(ds, aligned, region_lookup=lookup, origins=origins)
    conn.execute(f"DELETE FROM {ds.table} WHERE snapshot_date=?", (snap,))
    sql = f"INSERT INTO {ds.table}(snapshot_date,{','.join(cols)}) VALUES (?,{','.join('?' * len(cols))})"
    conn.executemany(sql, [(snap, *r) for r in rows])
    return len(rows)


def ingest_datasets(conn: sqlite3.Connection, snap: str, data: dict, *, source: str, files: dict | None = None,
                    origin_renewal: list | None = None, recompute: bool = True, region_mode: str | None = None) -> dict:
    """data: {dataset_key: 已对齐/归一化的行}。"""
    lookup = region_lookup_factory(conn)
    counts: dict[str, int] = {}
    t0 = time.perf_counter()
    with conn:
        for ds in DATASETS:
            counts[ds.key] = _write_dataset(
                conn, ds, snap, data[ds.key], origin_renewal if ds.key == "renewal" else None, lookup)
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(snapshot_date,ingested_at,source,files,row_counts,region_mode,calc_ms)"
            " VALUES (?,?,?,?,?,?,NULL)",
            (snap, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source, json.dumps(files or {}, ensure_ascii=False),
             json.dumps(counts, ensure_ascii=False), region_mode or config.region_mode()))
    region_info = apply_dms_regions(conn, snap)
    load_ms = int((time.perf_counter() - t0) * 1000)
    calc_ms = calc.compute_and_store(conn, snap, region_mode) if recompute else None
    return {"snapshot_date": snap, "rows": counts, "load_ms": load_ms, "calc_ms": calc_ms,
            "region_fallback_rows": region_info.get("fallback_rows"), "region_fallback_stores": region_info.get("fallback_stores")}


def ingest_dir(conn: sqlite3.Connection, directory: Path, snap: str, **kw) -> dict:
    data, files = load_directory_sources(Path(directory))
    origin = data.pop("_origin_renewal")
    return ingest_datasets(conn, snap, data, source=f"dir:{directory}", files=files, origin_renewal=origin, **kw)


def ingest_report(conn: sqlite3.Connection, report: Path, snap: str, **kw) -> dict:
    """历史回灌：从已生成的 区域各指标情况一览MMDD.xlsx 里取源 sheet。"""
    data = load_report_workbook(Path(report))
    return ingest_datasets(conn, snap, data, source=f"report:{Path(report).name}", **kw)


def recompute(conn: sqlite3.Connection, snap: str, region_mode: str | None = None, remap: bool = False) -> int:
    """重算某一天。remap=True：按当前门店表重新归属各明细行的 in_scope，并按 DMS 区域重算 region。"""
    if remap:
        lookup = region_lookup_factory(conn)
        with conn:
            for ds in DATASETS:
                if not ds.has_region:
                    continue
                rows = conn.execute(
                    f"SELECT row_no,{ds.store_code_col},{ds.store_name_col},src_region FROM {ds.table} WHERE snapshot_date=?", (snap,)).fetchall()
                conn.executemany(
                    f"UPDATE {ds.table} SET region=?, in_scope=? WHERE snapshot_date=? AND row_no=?",
                    [(*lookup(r[1], r[2], r[3]), snap, r[0]) for r in rows])
        apply_dms_regions(conn, snap)
    return calc.compute_and_store(conn, snap, region_mode)


def prune(conn: sqlite3.Connection, keep_days: int) -> list[str]:
    """删除早于 keep_days 天的快照明细与结果（0=不清理）。"""
    if keep_days <= 0:
        return []
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    old = [r[0] for r in conn.execute("SELECT snapshot_date FROM snapshots WHERE snapshot_date<?", (cutoff,))]
    with conn:
        for snap in old:
            for ds in DATASETS:
                conn.execute(f"DELETE FROM {ds.table} WHERE snapshot_date=?", (snap,))
            conn.execute("DELETE FROM report_results WHERE snapshot_date=?", (snap,))
            conn.execute("DELETE FROM snapshots WHERE snapshot_date=?", (snap,))
    return old


def ingest_after_pipeline(download_dir: Path, snap: str | None = None) -> dict:
    """run_pipeline 的可选钩子：MHERO_REPORTDB=1 时调用。绝不抛异常（失败只记录，不影响 Excel/推送）。"""
    try:
        conn = connect()
        migrate(conn)
        info: dict = {"ok": True}
        if config.os.getenv("REPORTDB_SYNC_STORES", "1").strip().lower() not in {"0", "false", "no", "off"}:
            try:  # 默认先从多维表格同步门店（权威）；失败或名单异常（安全闸）时沿用现有门店表，不中断入库
                info["stores"] = sync_from_bitable(conn)
            except Exception as exc:
                info["stores_error"] = str(exc)
        info.update(ingest_dir(conn, download_dir, snap or _today()))
        keep = int(config.os.getenv("REPORTDB_KEEP_DAYS", "0") or 0)
        if keep:
            info["pruned"] = prune(conn, keep)
        conn.close()
        return info
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 7 份源报表载入 reportdb 并重算（不发飞书、不爬 DMS）")
    ap.add_argument("--dir", help="源报表目录（如 download/）")
    ap.add_argument("--report", help="历史回灌：已生成的 区域各指标情况一览MMDD.xlsx")
    ap.add_argument("--date", default="", help="快照日期 YYYY-MM-DD，默认今天（北京时间）")
    ap.add_argument("--db", default="")
    ap.add_argument("--region-mode", choices=["store", "source"], default=None, help=argparse.SUPPRESS)  # 诊断用，默认 store
    ap.add_argument("--sync-stores", choices=["bitable", "json"], default=None,
                    help="先同步门店表：bitable=读多维表格（只读）；json=读 data/store_directory.json 缓存")
    ap.add_argument("--no-calc", action="store_true")
    ap.add_argument("--keep-days", type=int, default=0, help="清理 N 天前的快照（0=不清理）")
    ap.add_argument("--recompute", action="store_true", help="只重算 --date 指定的快照，不重新载入")
    ap.add_argument("--remap", action="store_true", help="与 --recompute 合用：按当前门店表重新归属区域")
    args = ap.parse_args(argv)

    snap = args.date or _today()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", snap):
        ap.error("--date 需为 YYYY-MM-DD")
    conn = connect(args.db or None)
    migrate(conn)
    try:
        if args.sync_stores == "bitable":
            print("stores:", sync_from_bitable(conn))
        elif args.sync_stores == "json":
            print("stores:", sync_from_directory_json(conn))
    except StoreSyncAborted as exc:
        print(f"[WARN] 门店同步已中止（保留现有门店表）：{exc}")
    if args.recompute:
        ms = recompute(conn, snap, args.region_mode, remap=args.remap)
        print(f"recomputed {snap} in {ms} ms")
    else:
        if bool(args.dir) == bool(args.report):
            ap.error("需要且只能指定 --dir 或 --report 之一")
        if args.dir:
            res = ingest_dir(conn, Path(args.dir), snap, recompute=not args.no_calc, region_mode=args.region_mode)
        else:
            res = ingest_report(conn, Path(args.report), snap, recompute=not args.no_calc, region_mode=args.region_mode)
        print(json.dumps(res, ensure_ascii=False))
    if args.keep_days:
        print("pruned:", prune(conn, args.keep_days))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
