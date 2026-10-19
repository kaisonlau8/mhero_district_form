"""路径与环境变量。全部可用环境变量覆盖，默认值不影响现有生产流程。"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
SEED_PATH = PACKAGE_DIR / "seed_data.json"
TEMPLATE_PATH = PROJECT_ROOT / "app" / "assets" / "report_template.xlsx"


def db_path() -> Path:
    """SQLite 单文件。默认 data/reportdb/reportdb.sqlite3（data/ 下的内容已被 .gitignore 之外的 reportdb 子目录单独忽略）。"""
    raw = os.getenv("REPORTDB_PATH", "").strip()
    return Path(raw) if raw else PROJECT_ROOT / "data" / "reportdb" / "reportdb.sqlite3"


def enabled() -> bool:
    """run_pipeline 集成开关，默认关闭。"""
    return os.getenv("MHERO_REPORTDB", "").strip().lower() in {"1", "true", "yes", "on"}


def region_mode() -> str:
    """store=门店表（多维表格）决定参与考核范围，区域只取 DMS 导出（为空记「无区域」；默认，正式口径）；
    source=沿用 DMS「大区」列（等同旧模板字面行为）——仅作隐藏的诊断/对账开关，页面与文档不推荐。"""
    mode = os.getenv("REPORTDB_REGION_MODE", "store").strip().lower()
    return mode if mode in {"store", "source"} else "store"


def stores_authoritative() -> bool:
    """多维表格是否为门店清单唯一来源（表里没有的门店置为停用）。默认 True；设 REPORTDB_STORES_AUTHORITATIVE=0 才关闭。
    安全闸：拉回的名单为空或少于当前启用门店的 50% 时整次同步中止（见 stores.StoreSyncAborted）。"""
    return os.getenv("REPORTDB_STORES_AUTHORITATIVE", "1").strip().lower() not in {"0", "false", "no", "off"}


STORE_SYNC_MIN_RATIO = 0.5
