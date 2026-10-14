"""剔除（exclude）：分子、分母都不计算的车辆。与「修正结果」（只改分子）不同；剔除优先于修正结果。

两个来源：
1. 规则表 exclusion_rules：某数据集的某列等于/包含某值（如门店名称=「出口车专用」），管理员在 /settings/exclusion-rules 维护；
2. 督导申请「剔除」并经管理员批准的 VIN（exemptions.kind='exclude'，indicator='all'）：整车，对所有指标都不计，永久生效，待审批不生效。
Excel 路径不受影响。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .datasets import BY_KEY, BY_TABLE as DS_BY_TABLE

MATCH_TYPES = {"equals": "等于", "contains": "包含"}
RULE_DATASETS = ("task", "first-service", "second-service", "new-policy", "renewal")


class RuleError(ValueError):
    pass


def list_rules(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    q = "SELECT * FROM exclusion_rules" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(q)]


def _cond(rule: dict) -> tuple[str, list]:
    col = rule["column_name"]
    if rule["match_type"] == "equals":
        return f"lower(COALESCE({col},''))=lower(?)", [rule["value"]]
    return f"instr(lower(COALESCE({col},'')), lower(?))>0", [rule["value"]]


def rule_match_sql(conn, ds_key: str) -> tuple[str, list]:
    """该数据集启用中的规则的「命中」条件；无规则返回 ('0', [])。"""
    conds, args = [], []
    for r in list_rules(conn, enabled_only=True):
        if r["dataset_key"] == ds_key:
            c, a = _cond(r)
            conds.append(c)
            args += a
    return ("(" + " OR ".join(conds) + ")", args) if conds else ("0", [])


NO_REGION_LABEL = "DMS 无区域"
NO_REGION_SQL = "trim(COALESCE(src_region,''))=''"   # 自动规则：DMS 导出的区域列为空 → 剔除（分子分母都不计）


def vin_excl_in_sql() -> str:
    return ("upper(trim(vin)) IN (SELECT vin_key FROM exemptions WHERE kind='exclude' AND status='approved' "
            "AND effective_from<=? AND (expires_at IS NULL OR expires_at='' OR expires_at>=?))")


def excluded_expr(conn, table: str, snap: str) -> tuple[str, list]:
    """行被剔除的 SQL 条件（规则命中 OR VIN 在有效剔除申请内）及参数。"""
    ds = DS_BY_TABLE[table]
    m, ma = rule_match_sql(conn, ds.key)
    return f"({m} OR {NO_REGION_SQL} OR {vin_excl_in_sql()})", [*ma, snap, snap]   # 剔除按整车（VIN），对所有指标一视同仁


def not_excluded(conn, table: str, snap: str) -> tuple[str, list]:
    e, a = excluded_expr(conn, table, snap)
    return f"NOT {e}", a


def excluded_counts(conn, snap: str) -> dict[str, dict[str, Any]]:
    """每个指标：被剔除的行数（门店在门店表内的行里：规则命中 / 因剔除申请）。"""
    from .exemptions import INDICATORS

    out = {}
    for ind in INDICATORS.values():
        ds = BY_KEY[ind.ds_key]
        m, ma = rule_match_sql(conn, ds.key)
        by_rule = conn.execute(f"SELECT COUNT(*) FROM {ind.table} WHERE snapshot_date=? AND in_scope=1 AND {m}", (snap, *ma)).fetchone()[0]
        nr = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT upper(trim(vin))) FROM {ind.table} WHERE snapshot_date=? AND in_scope=1 AND NOT {m} AND {NO_REGION_SQL}",
            (snap, *ma)).fetchone()
        nr_stores = [{"store": r[0], "rows": r[1]} for r in conn.execute(
            f"SELECT COALESCE({ds.store_name_col},'(空)'), COUNT(*) FROM {ind.table} WHERE snapshot_date=? AND in_scope=1 AND NOT {m} AND {NO_REGION_SQL} GROUP BY 1 ORDER BY 2 DESC",
            (snap, *ma))]
        by_vin = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT upper(trim(vin))) FROM {ind.table} WHERE snapshot_date=? AND in_scope=1 AND NOT {m} AND NOT {NO_REGION_SQL} AND {vin_excl_in_sql()}",
            (snap, *ma, snap, snap)).fetchone()
        out[ind.key] = {"title": ind.title, "by_rule": by_rule, "by_vin_rows": by_vin[0], "by_vin": by_vin[1],
                        "no_region_rows": nr[0], "no_region_vins": nr[1], "no_region_stores": nr_stores}
    return out


# ----------------------------------------------------------------------------- 规则维护
def validate(dataset_key: str, column: str, match: str, value: str) -> tuple[str, str, str, str]:
    value = (value or "").strip()
    if dataset_key not in RULE_DATASETS:
        raise RuleError("数据集不正确")
    if column not in {c for c, _ in BY_KEY[dataset_key].columns}:
        raise RuleError("列不属于该数据集")
    if match not in MATCH_TYPES:
        raise RuleError("匹配方式只能是 等于/包含")
    if not value or len(value) > 100:
        raise RuleError("匹配值不能为空（100 字以内）")
    return dataset_key, column, match, value


def preview(conn, dataset_key: str, column: str, match: str, value: str, snap: str | None) -> dict[str, Any]:
    dataset_key, column, match, value = validate(dataset_key, column, match, value)
    ds = BY_KEY[dataset_key]
    if not snap:
        return {"matched": None}
    c, a = _cond({"column_name": column, "match_type": match, "value": value})
    base = f"FROM {ds.table} WHERE snapshot_date=? AND {c}"
    total = conn.execute(f"SELECT COUNT(*) FROM {ds.table} WHERE snapshot_date=?", (snap,)).fetchone()[0]
    matched = conn.execute(f"SELECT COUNT(*) {base}", (snap, *a)).fetchone()[0]
    eff = conn.execute(f"SELECT COUNT(*) {base} AND in_scope=1", (snap, *a)).fetchone()[0]
    stores = [{"store": r[0], "rows": r[1], "in_scope": r[2]} for r in conn.execute(
        f"SELECT COALESCE({ds.store_name_col},'(空)'), COUNT(*), MAX(in_scope) {base} GROUP BY 1 ORDER BY 2 DESC LIMIT 10", (snap, *a))]
    return {"snapshot_date": snap, "total": total, "matched": matched, "effective": eff, "stores": stores}


def add_rule(conn, actor: str, dataset_key: str, column: str, match: str, value: str, note: str = "") -> int:
    dataset_key, column, match, value = validate(dataset_key, column, match, value)
    with conn:
        cur = conn.execute("INSERT INTO exclusion_rules(dataset_key,column_name,match_type,value,enabled,note,created_by,updated_at)"
                           " VALUES (?,?,?,?,1,?,?,?)", (dataset_key, column, match, value, note[:200], actor, _now()))
    return cur.lastrowid


def set_enabled(conn, rule_id: int, enabled: bool) -> None:
    with conn:
        conn.execute("UPDATE exclusion_rules SET enabled=?, updated_at=? WHERE id=?", (int(enabled), _now(), rule_id))


def delete_rule(conn, rule_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM exclusion_rules WHERE id=?", (rule_id,))


def recompute_all(conn) -> list[str]:
    from . import calc

    done = []
    for r in conn.execute("SELECT snapshot_date FROM snapshots ORDER BY snapshot_date").fetchall():
        calc.compute_and_store(conn, r[0])
        done.append(r[0])
    return done


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
