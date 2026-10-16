"""MHERO_REPORT_ENGINE=reportdb 时 run_pipeline 使用：入库（含门店同步）→ 计算 → 导出日报 xlsx。

不发送任何消息；通知仍由 run_pipeline 沿用原来的飞书卡片逻辑，文件换成这里生成的。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import config
from .daily_report import write_daily_report
from .db import connect, migrate
from .ingest import _today, ingest_dir, prune
from .stores import sync_from_bitable


def engine() -> str:
    return "reportdb" if os.getenv("MHERO_REPORT_ENGINE", "excel").strip().lower() == "reportdb" else "excel"


def run(download_dir: Path, output_dir: Path, snap: str | None = None) -> dict:
    """失败抛异常（日报是这条路径的产出，不能静默失败）。"""
    snap = snap or _today()
    conn = connect()
    try:
        migrate(conn)
        info: dict = {"ok": True, "snapshot_date": snap}
        if os.getenv("REPORTDB_SYNC_STORES", "1").strip().lower() not in {"0", "false", "no", "off"}:
            try:  # 多维表格为准；失败/名单异常（安全闸）时沿用现有门店表
                info["stores"] = sync_from_bitable(conn)
            except Exception as exc:  # noqa: BLE001
                info["stores_error"] = str(exc)
        info.update(ingest_dir(conn, Path(download_dir), snap))
        keep = int(os.getenv("REPORTDB_KEEP_DAYS", "0") or 0)
        if keep:
            info["pruned"] = prune(conn, keep)
        detail = os.getenv("REPORTDB_DAILY_DETAIL", "").strip().lower() in {"1", "true", "yes", "on"}
        path = write_daily_report(conn, snap, Path(output_dir), include_detail=detail, full_detail=False)
        info["report"] = str(path)
        return info
    finally:
        conn.close()
