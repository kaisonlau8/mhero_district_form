"""SQLite 连接 + 迁移（PRAGMA user_version）+ 从模板种子数据初始化。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config
from .datasets import DATASETS, ddl

CORE_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS regions (
    name TEXT PRIMARY KEY, sort_order INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stores (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT,
    province TEXT, city TEXT, district TEXT,
    seq INTEGER NOT NULL,              -- 常备件备库率表的序号/行序
    direct_order INTEGER,              -- 非空=直营店，数值=在「直营店指标」里的行序
    active INTEGER NOT NULL DEFAULT 1, -- 1=计入常备件备库率
    source TEXT NOT NULL DEFAULT 'template',  -- template | bitable
    in_bitable INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stores_name ON stores(name);
CREATE TABLE IF NOT EXISTS spare_parts (
    sort_order INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    std_qty REAL,
    group_label TEXT,
    counted INTEGER NOT NULL DEFAULT 1  -- 1=计入备库率分母/分子
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date TEXT PRIMARY KEY,
    ingested_at TEXT NOT NULL,
    source TEXT,
    files TEXT,
    row_counts TEXT,
    region_mode TEXT,
    calc_ms INTEGER
);
CREATE TABLE IF NOT EXISTS report_results (
    snapshot_date TEXT NOT NULL,
    table_key TEXT NOT NULL,
    region_mode TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    payload TEXT NOT NULL,             -- JSON: {"blocks": [...], "grid": [...]}
    PRIMARY KEY (snapshot_date, table_key, region_mode)
);
"""

V2_DDL = """
CREATE TABLE IF NOT EXISTS spare_lists (          -- 常备件名单版本（只增不改；回滚=复制旧版本为新版本）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from TEXT NOT NULL,                 -- 该版本起效的快照日期（含）
    created_at TEXT NOT NULL,
    created_by TEXT,
    note TEXT,
    source TEXT,                                  -- seed | upload:<文件名> | rollback:v<N>
    filename TEXT
);
CREATE TABLE IF NOT EXISTS spare_list_items (
    list_id INTEGER NOT NULL REFERENCES spare_lists(id),
    sort_order INTEGER NOT NULL,
    code TEXT NOT NULL,                           -- 备件号（原样展示）
    name TEXT,
    std_qty REAL,
    group_label TEXT,
    counted INTEGER NOT NULL DEFAULT 1,           -- 1=计入备库率分母/分子
    PRIMARY KEY (list_id, sort_order)
);
CREATE TABLE IF NOT EXISTS spare_drafts (         -- 上传后、确认前的预览草稿
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, created_by TEXT, filename TEXT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT,
    password_hash TEXT,                           -- 本地登录用；OIDC 用户为空
    role TEXT NOT NULL DEFAULT 'viewer',          -- admin | viewer
    full_detail INTEGER NOT NULL DEFAULT 0,       -- 1=明细页可看未脱敏的 VIN/姓名（admin 默认拥有）
    disabled INTEGER NOT NULL DEFAULT 0,
    auth_source TEXT NOT NULL DEFAULT 'local',    -- local | oidc
    created_at TEXT NOT NULL, created_by TEXT, last_login TEXT,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,                     -- sha256(cookie 值)
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, ip TEXT, ua TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, detail TEXT, ip TEXT
);
"""


def _migrate_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(V2_DDL)
    for d in DATASETS:  # 老库补 in_scope 列（新库的 DDL 里已经有）
        if not d.has_region:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({d.table})")}
        if "in_scope" not in cols:
            conn.execute(f"ALTER TABLE {d.table} ADD COLUMN in_scope INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"UPDATE {d.table} SET in_scope = CASE WHEN region IS NOT NULL THEN 1 ELSE 0 END")


V3_DDL = """
CREATE TABLE IF NOT EXISTS user_stores (            -- 已停用（2026-10-19）：督导门店只按多维表格「督导」匹配；表保留但不再读写
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store_code TEXT NOT NULL,
    PRIMARY KEY (user_id, store_code)
);
CREATE TABLE IF NOT EXISTS exemptions (              -- 豁免申请/决定：按 VIN + 指标，跨快照持续生效
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT NOT NULL,
    vin_key TEXT NOT NULL,                           -- upper(trim(vin))
    indicator TEXT NOT NULL,                         -- task | first_service | second_service | new_policy | renewal
    store_code TEXT, store_name TEXT, region TEXT,   -- 申请时的门店/区域
    reason_category TEXT NOT NULL,                   -- 出口车挂店 | 其他
    reason TEXT NOT NULL,
    evidence TEXT,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    request_snapshot TEXT,                           -- 申请时看的快照日期
    status TEXT NOT NULL DEFAULT 'pending',          -- pending | approved | rejected | revoked | withdrawn
    reviewed_by TEXT, reviewed_at TEXT, review_comment TEXT,
    effective_from TEXT,                             -- 批准后：从该快照日期起生效
    expires_at TEXT                                  -- 空=永久；否则到该日期（含）为止
);
CREATE INDEX IF NOT EXISTS idx_exemptions_active ON exemptions(indicator, status, vin_key);
CREATE TABLE IF NOT EXISTS exemption_log (           -- 只追加：触发器禁止修改/删除
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exemption_id INTEGER NOT NULL,
    at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    from_status TEXT, to_status TEXT, comment TEXT
);
CREATE TRIGGER IF NOT EXISTS exemption_log_no_update BEFORE UPDATE ON exemption_log
BEGIN SELECT RAISE(ABORT, 'exemption_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS exemption_log_no_delete BEFORE DELETE ON exemption_log
BEGIN SELECT RAISE(ABORT, 'exemption_log is append-only'); END;
"""


def _add_col(conn, table: str, col: str, decl: str) -> None:
    if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(V3_DDL)
    _add_col(conn, "stores", "supervisor", "TEXT")            # 多维表格「督导」
    _add_col(conn, "users", "supervisor_name", "TEXT")        # 督导账号对应的督导姓名（匹配 stores.supervisor）


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """剔除：分子分母都不计的车辆。规则表（按列匹配）+ 督导申请「剔除」（exemptions.kind='exclude'）。"""
    conn.executescript("""
CREATE TABLE IF NOT EXISTS exclusion_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_key TEXT NOT NULL,                  -- task | first-service | second-service | new-policy | renewal
    column_name TEXT NOT NULL,                  -- 明细表列名（datasets.py 里的 db 列名）
    match_type TEXT NOT NULL DEFAULT 'equals',  -- equals | contains（不分大小写）
    value TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    created_by TEXT, updated_at TEXT
);
""")
    _add_col(conn, "exemptions", "kind", "TEXT NOT NULL DEFAULT 'exempt'")   # exempt=豁免(只分子) | exclude=剔除(分子分母)
    if not conn.execute("SELECT COUNT(*) FROM exclusion_rules").fetchone()[0]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        seed = [(k, "store_name", "equals", "出口车专用", 1, "DMS 里归属出口的虚拟门店（实测 2026-10-09：首保 1252 行、续保 485 行、招揽 24 行）")
                for k in ("task", "first-service", "second-service", "new-policy", "renewal")]
        seed.append(("task", "owner_name", "contains", "进出口", 0, "候选（默认关闭）：车主为进出口公司。不稳定：不一定是出口车，开启前请先预览"))
        conn.executemany("INSERT INTO exclusion_rules(dataset_key,column_name,match_type,value,enabled,note,created_by,updated_at)"
                         " VALUES (?,?,?,?,?,?,'seed',?)", [(*r, now) for r in seed])


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """修正结果 / 剔除分家：
    * 已有 kind='exempt'（豁免）→ 就是「修正结果」，数据不动；
    * 已有 kind='exclude'（按指标的剔除）→ 变成整车剔除（indicator='all'）：同一 VIN 的多条有效申请合并成一条
      （有已批准的留最早的已批准，生效日期取最早；其余待审批的置为已撤回、多余已批准的置为已撤销，写日志），批准的剔除永久有效；
    * 迁移前先把整个库备份到 <库文件>.pre-v5.bak（内存库跳过）。幂等：重复运行不会再改动数据。"""
    import re as _re

    row = conn.execute("PRAGMA database_list").fetchone()
    dbfile = row[2] if row else ""
    if dbfile and Path(dbfile).exists():
        bak = Path(dbfile + ".pre-v5.bak")
        if not bak.exists():
            conn.commit()
            dst = sqlite3.connect(str(bak))
            try:
                conn.backup(dst)
            finally:
                dst.close()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log(i, action, frm, to, comment):
        conn.execute("INSERT INTO exemption_log(exemption_id,at,actor,action,from_status,to_status,comment) VALUES (?,?,?,?,?,?,?)",
                     (i, now, "migration", action, frm, to, comment))

    had_approved = False
    rows = conn.execute("SELECT * FROM exemptions WHERE kind='exclude' ORDER BY id").fetchall()
    by_vin: dict[str, list] = {}
    for r in rows:
        if r["status"] in ("pending", "approved"):
            by_vin.setdefault(r["vin_key"], []).append(r)
    for vk, grp in by_vin.items():
        approved = [r for r in grp if r["status"] == "approved"]
        had_approved = had_approved or bool(approved)
        keeper = (approved or grp)[0]
        if approved:
            eff = min(r["effective_from"] or "9999" for r in approved)
            conn.execute("UPDATE exemptions SET effective_from=? WHERE id=?", (eff, keeper["id"]))
        for r in grp:
            if r["id"] == keeper["id"]:
                continue
            to = "revoked" if r["status"] == "approved" else "withdrawn"
            conn.execute("UPDATE exemptions SET status=? WHERE id=?", (to, r["id"]))
            log(r["id"], "migrate-merge", r["status"], to, f"剔除改为整车（不分指标）：与 #{keeper['id']} 合并")
        if len(grp) >= 1:
            log(keeper["id"], "migrate", keeper["status"], keeper["status"], "剔除改为整车剔除（所有指标都不计）")
    conn.execute("UPDATE exemptions SET indicator='all' WHERE kind='exclude' AND indicator<>'all'")
    conn.execute("UPDATE exemptions SET expires_at=NULL WHERE kind='exclude' AND status='approved'")
    conn.commit()
    if had_approved:   # 按指标的剔除变成整车剔除 → 其它指标的数字会变，重算全部快照
        from . import calc

        for r in conn.execute("SELECT snapshot_date FROM snapshots ORDER BY snapshot_date").fetchall():
            calc.compute_and_store(conn, r[0])


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """区域口径：报表区域只取 DMS 导出（归一：华中1区→华中1）；多维表格区域只存 bitable_region 作信息，不参与归属。"""
    import re

    def norm(v):
        t = re.sub(r"\s+", "", v or "")
        return (t[:-1] if t.endswith("区") and len(t) > 1 else t) or None

    _add_col(conn, "stores", "bitable_region", "TEXT")
    for r in conn.execute("SELECT code,region FROM stores").fetchall():
        conn.execute("UPDATE stores SET region=?, bitable_region=? WHERE code=?", (norm(r[1]), norm(r[1]), r[0]))
    for r in conn.execute("SELECT name FROM regions").fetchall():
        if norm(r[0]) != r[0]:
            conn.execute("UPDATE regions SET name=? WHERE name=?", (norm(r[0]), r[0]))


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """权限一期：全局超级管理员标记；督导门店只按飞书联络表「督导」匹配，清掉手工分配。"""
    _add_col(conn, "users", "global_admin", "INTEGER NOT NULL DEFAULT 0")   # 统一登录全局超级管理员（唯一能管账号 / 权限）
    _add_col(conn, "stores", "supervisor_phone", "TEXT")      # 多维表格「督导手机号」（与统一登录账号名=手机号匹配）
    conn.execute("DELETE FROM user_stores")                    # 手工分配停用（部署前已备份数据库）


# 迁移列表：下标+1 == user_version。只追加，不改旧条目。元素可以是 SQL 脚本或 callable(conn)。
MIGRATIONS: list = [CORE_DDL + "\n" + "\n".join(ddl(d) for d in DATASETS), _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6, _migrate_v7]


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, *, seed: bool = True) -> int:
    ver = schema_version(conn)
    for i in range(ver, len(MIGRATIONS)):
        m = MIGRATIONS[i]
        conn.executescript(m) if isinstance(m, str) else m(conn)
        conn.execute(f"PRAGMA user_version={i + 1}")
        conn.commit()
    if seed:
        seed_if_empty(conn)
        ensure_default_spare_list(conn)
    return schema_version(conn)


def ensure_default_spare_list(conn: sqlite3.Connection) -> bool:
    """名单 v1 = 模板里的 33 个备件（817 保护套件 counted=0），自 2000-01-01 起生效。"""
    if conn.execute("SELECT COUNT(*) FROM spare_lists").fetchone()[0]:
        return False
    if not conn.execute("SELECT COUNT(*) FROM spare_parts").fetchone()[0]:
        return False
    with conn:
        cur = conn.execute(
            "INSERT INTO spare_lists(effective_from,created_at,created_by,note,source) VALUES ('2000-01-01',"
            "datetime('now','localtime'),'system','模板内置名单（种子）','seed')")
        lid = cur.lastrowid
        conn.execute(
            "INSERT INTO spare_list_items(list_id,sort_order,code,name,std_qty,group_label,counted) "
            "SELECT ?,sort_order,code,name,std_qty,group_label,counted FROM spare_parts", (lid,))
    return True


def seed_if_empty(conn: sqlite3.Connection, seed_path: Path | None = None) -> bool:
    """首次建库：把模板里写死的门店→区域、直营店、区域顺序、常备件清单搬进 DB 表。"""
    if conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0]:
        return False
    data = json.loads((seed_path or config.SEED_PATH).read_text(encoding="utf-8"))
    with conn:
        for i, name in enumerate(data["regions"], 1):
            conn.execute("INSERT INTO regions(name, sort_order) VALUES (?,?)", (name, i))
        direct = {n: i for i, n in enumerate(data["direct_stores"], 1)}
        for s in data["stores"]:
            conn.execute(
                "INSERT INTO stores(code,name,region,seq,direct_order,active,source,in_bitable,updated_at)"
                " VALUES (?,?,?,?,?,1,'template',0,datetime('now','localtime'))",
                (s["code"], s["name"], s["region"], s["seq"], direct.get(s["name"])),
            )
        for p in data["spare_parts"]:
            conn.execute(
                "INSERT INTO spare_parts(sort_order,code,name,std_qty,group_label,counted) VALUES (?,?,?,?,?,?)",
                (p["sort_order"], p["code"], p["name"], p["std_qty"], p["group_label"], p["counted"]),
            )
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('seeded_from', ?)", (data.get("source", ""),))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('spare_threshold', ?)",
                     (str(data.get("spare_threshold", 0.8)),))
    return True


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value))


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="创建/升级 reportdb 数据库并从模板种子初始化")
    ap.add_argument("--db", default="")
    ap.add_argument("--no-seed", action="store_true")
    args = ap.parse_args()
    conn = connect(args.db or None)
    ver = migrate(conn, seed=not args.no_seed)
    print(f"reportdb schema v{ver} @ {config.db_path() if not args.db else args.db}")
    for t in ("regions", "stores", "spare_parts", "snapshots"):
        print(f"  {t}: {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
