"""指标修正申请，分两类（互不合并，各自独立申请 / 审批）：

1. 修正结果（kind='exempt'，原「豁免」）：按 VIN + 指标，把该车在该指标上的结果由「否」改成「是」（分子 +1，分母不变）；
   只适用于当前结果为「否」的车；管理员批准后跨快照持续生效，撤销/过期后失效。
2. 剔除（kind='exclude'）：针对整辆车（VIN），不分指标（indicator 固定为 'all'）。管理员批准后该车永久标记为「已剔除」，
   此后所有指标（分子、分母）、所有报表/首页/汇总/日报都不再统计它。待审批期间不生效；撤销才恢复。
Excel 路径不受影响，仅 reportdb 应用。

只改计算，不改明细源数据：明细里的「是否首保」等原值保持不变，页面用「已豁免」标记。
"""
from __future__ import annotations

import io
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .datasets import BY_KEY

ACTIVE_STATUSES = ("pending", "approved")
STATUS_CN = {"pending": "待审批", "approved": "已批准", "rejected": "已驳回", "revoked": "已撤销", "withdrawn": "已撤回"}
CATEGORIES = ("出口车挂店", "其他")


class ExemptionError(ValueError):
    pass


@dataclass(frozen=True)
class Indicator:
    key: str
    table: str
    ds_key: str
    flag_col: str      # 决定分子的列
    hit: str           # 该列等于此值才进分子
    title: str
    meaning: str       # 「豁免」在该指标上的含义（界面文案）

    @property
    def flag_label(self) -> str:
        col = dict(BY_KEY[self.ds_key].columns).get(self.flag_col, self.flag_col)
        return f"「{col}」=「{self.hit}」的行数"


INDICATORS: dict[str, Indicator] = {i.key: i for i in (
    Indicator("task", "task_items", "task", "task_status", "待处理", "招揽实施率",
              "该 VIN 的招揽任务按「任务状态=待处理」同样计入分子（即视为已招揽实施）"),
    Indicator("first_service", "first_service", "first-service", "done", "是", "首保实施率",
              "把该 VIN 的「是否首保」按「是」计入分子（视为已首保）"),
    Indicator("second_service", "second_service", "second-service", "done", "是", "二保实施率",
              "把该 VIN 的「是否二保」按「是」计入分子（视为已二保）"),
    Indicator("new_policy", "new_policy", "new-policy", "done", "是", "新保投保率",
              "把该 VIN 的「是否新保」按「是」计入分子（视为已新保）"),
    Indicator("renewal", "renewal", "renewal", "done", "是", "续保投保率",
              "把该 VIN 的「是否续保」按「是」计入分子（视为已续保；含去年同期交付未新保车辆）"),
)}
BY_TABLE = {i.table: i for i in INDICATORS.values()}
EXPLAIN = "修正结果：把这辆车在该指标上的结果由「否」改成「是」（分子 +1，分母不变），只对该指标有效。"
EXPLAIN_EXCLUDE = "剔除：针对整辆车，管理员批准后这辆车永久不再计入任何指标（分子分母都不计，例如 DMS 没标出口的出口车）；待审批期间不生效。"
EXEMPTION_NOTE = "修正结果只把该指标的分子视为「是」，分母不变，不改变源数据；不在门店表内的门店仍不计入。剔除请到「申请剔除」页（整车，不分指标），两类不能合并申请。"
EXCLUDE_ALL = "all"   # 剔除申请的 indicator 值：整车，不分指标


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def vin_key(v: Any) -> str:
    return re.sub(r"\s+", "", str(v or "")).upper()


# ----------------------------------------------------------------------------- 计算层用
def active_vin_sql(indicator: str, kind: str = "exempt") -> str:
    """有效豁免/剔除的 VIN 子查询，参数依次为 (indicator, snap, snap)。"""
    assert kind in ("exempt", "exclude")
    return (f"SELECT vin_key FROM exemptions WHERE indicator=? AND kind='{kind}' AND status='approved' AND effective_from<=? "
            "AND (expires_at IS NULL OR expires_at='' OR expires_at>=?)")


def hit_expr(table: str, flag_col: str, vcol: str = "vin") -> tuple[str, str]:
    """返回 (判断 SQL 片段, 说明)：flag=hit OR vin 在有效豁免内。参数顺序：hit, indicator, snap, snap。"""
    ind = BY_TABLE[table]
    return (f"({flag_col}=? OR upper(trim({vcol})) IN ({active_vin_sql(ind.key)}))", ind.key)


def flipped_counts(conn: sqlite3.Connection, snap: str) -> dict[str, dict[str, int]]:
    """每个指标：被豁免真正改变了分子的行数（计入范围内、原本不是 hit 的行）与涉及的 VIN 数。"""
    from .exclusions import not_excluded

    out = {}
    for ind in INDICATORS.values():
        xs, xa = not_excluded(conn, ind.table, snap)
        r = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT upper(trim(vin))) FROM {ind.table} WHERE snapshot_date=? AND in_scope=1 "
            f"AND {xs} AND COALESCE({ind.flag_col},'')<>? AND upper(trim(vin)) IN ({active_vin_sql(ind.key)})",
            (snap, *xa, ind.hit, ind.key, snap, snap)).fetchone()
        out[ind.key] = {"title": ind.title, "rows": r[0], "vins": r[1]}
    return out


def row_flag_select(conn, ind_key: str, snap: str) -> tuple[str, list]:
    """明细行附加列：_ex=有效修正结果，_xc=被剔除（规则或剔除申请），_done=原始状态已是 hit。"""
    from .exclusions import excluded_expr

    ind = INDICATORS[ind_key]
    xs, xa = excluded_expr(conn, ind.table, snap)
    return (f"CASE WHEN upper(trim(vin)) IN ({active_vin_sql(ind_key)}) THEN 1 ELSE 0 END AS _ex, "
            f"CASE WHEN {xs} THEN 1 ELSE 0 END AS _xc, "
            f"CASE WHEN {ind.flag_col}=? THEN 1 ELSE 0 END AS _done", [ind_key, snap, snap, *xa, ind.hit])


# ----------------------------------------------------------------------------- 日志
def _log(conn, ex_id: int, actor: str, action: str, frm: str | None, to: str | None, comment: str = "") -> None:
    conn.execute("INSERT INTO exemption_log(exemption_id,at,actor,action,from_status,to_status,comment) VALUES (?,?,?,?,?,?,?)",
                 (ex_id, _now(), actor, action, frm, to, comment))


def history(conn, ex_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM exemption_log WHERE exemption_id=? ORDER BY id", (ex_id,))]


# ----------------------------------------------------------------------------- 督导范围
SUPERVISOR_STORES_SQL = ("SELECT code,name,region FROM stores WHERE active=1 AND ("
                         "(?<>'' AND COALESCE(supervisor_phone,'')=?) OR (?<>'' AND lower(COALESCE(supervisor,''))=lower(?)))")


def supervisor_stores(conn, username: str, supervisor_name: str) -> list:
    """督导负责（可申请）的门店：只看多维表格联络表的「督导」字段——督导手机号 = 账号名（手机号），或督导姓名 = 账号的督导姓名。
    不再有手工分配（2026-10-19 决定）。"""
    phone = (username or "").strip() if re.fullmatch(r"1\d{10}", (username or "").strip()) else ""
    sup = (supervisor_name or "").strip()
    return conn.execute(SUPERVISOR_STORES_SQL, (phone, phone, sup, sup)).fetchall()


def scope_for(conn, user) -> dict[str, set[str]] | None:
    """None=不受限（管理员 / 只读管理员 / 用户——后两者本来就不能申请）；督导=多维表格「督导」字段匹配到的启用门店。
    查看不受此限制（督导看全国全部明细），只决定能对哪些行提交申请。"""
    if user.role != "supervisor":
        return None
    codes: set[str] = set()
    names: set[str] = set()
    for r in supervisor_stores(conn, user.username, getattr(user, "supervisor_name", "") or ""):
        codes.add(r["code"].casefold())
        names.add(r["name"].casefold())
    return {"codes": codes, "names": names}


def scope_where(ds, scope: dict[str, set[str]] | None) -> tuple[str, list]:
    if scope is None:
        return "", []
    parts, args = [], []
    if ds.store_code_col and scope["codes"]:
        parts.append(f"lower({ds.store_code_col}) IN ({','.join('?' * len(scope['codes']))})")
        args += sorted(scope["codes"])
    if ds.store_name_col and scope["names"]:
        parts.append(f"lower({ds.store_name_col}) IN ({','.join('?' * len(scope['names']))})")
        args += sorted(scope["names"])
    return ("(" + " OR ".join(parts) + ")", args) if parts else ("0", [])


# ----------------------------------------------------------------------------- 申请
def candidates(conn, ind_key: str, snap: str, scope, *, q: str = "", store: str = "", limit: int = 200,
               kind: str = "exempt", region: str = "") -> list[dict]:
    """修正结果申请列表：范围内、VIN 非空、未被剔除、且该指标结果当前为「否」（未命中）的分母行，带已有申请状态。
    kind 参数仅为兼容旧调用，不再有作用。"""
    from .exclusions import not_excluded

    ind = INDICATORS[ind_key]
    ds = BY_KEY[ind.ds_key]
    xs, xa = not_excluded(conn, ind.table, snap)
    where = ["snapshot_date=?", "in_scope=1", xs, "vin IS NOT NULL", "trim(vin)<>''",
             f"COALESCE({ind.flag_col},'')<>?"]
    args: list = [snap, *xa, ind.hit]
    sw, sa = scope_where(ds, scope)
    if sw:
        where.append(sw)
        args += sa
    if q:
        where.append("upper(vin) LIKE ?")
        args.append("%" + vin_key(q).replace("%", "").replace("_", "") + "%")
    if store:
        where.append(f"{ds.store_name_col}=?")
        args.append(store)
    if region:
        where.append("region=?")
        args.append(region)
    cols = ["row_no", "vin", ds.store_name_col + " AS store_name", "region", ind.flag_col + " AS flag"]
    rows = conn.execute(f"SELECT {','.join(cols)} FROM {ds.table} WHERE {' AND '.join(where)} ORDER BY {ds.store_name_col}, row_no LIMIT ?",
                        (*args, limit)).fetchall()
    out = [dict(r) for r in rows]
    st = status_map(conn, ind_key, [vin_key(r["vin"]) for r in out])
    for r in out:
        r["done"] = (r["flag"] or "") == ind.hit
        r["req"] = st.get(vin_key(r["vin"]))
    return out


KIND_CN = {"exempt": "修正结果", "exclude": "剔除"}


def indicator_title(key: str) -> str:
    return INDICATORS[key].title if key in INDICATORS else "全部指标（整车）"


MAX_PER_REQUEST = 1   # 申请只能逐辆提交，不允许批量
ONE_ONLY_MSG = "每次只能提交 1 辆车的申请，不支持批量提交"


def request_exemptions(conn, user, ind_key: str, snap: str, *, row_nos: Iterable[int] = (), vins: Iterable[str] = (),
                       category: str = "", reason: str = "", evidence: str = "", kind: str = "exempt",
                       items: Iterable[tuple[Any, str]] | None = None) -> dict[str, Any]:
    """申请「修正结果」（kind=exempt）：某指标上把「否」改成「是」。剔除请用 request_exclusions（整车，不分指标）。

    items=[(行号或 VIN, 该车自己的理由), ...]：每辆车各填各的理由（必填，空理由的跳过并说明）；
    旧用法 row_nos / vins + 共用 reason 保留给脚本/测试。当前结果已是「是」的车跳过（已完成无需修正）。"""
    if kind != "exempt":
        raise ExemptionError("剔除是整车的申请，请使用「申请剔除」")
    if ind_key not in INDICATORS:
        raise ExemptionError("指标不正确")
    if user.role not in ("supervisor", "admin"):
        raise ExemptionError("无权申请")
    category = category if category in CATEGORIES else "其他"
    reason = (reason or "").strip()
    if items is None and (not reason or len(reason) > 500):
        raise ExemptionError("请填写申请理由（500 字以内）")
    ind = INDICATORS[ind_key]
    ds = BY_KEY[ind.ds_key]
    scope = scope_for(conn, user)
    sw, sa = scope_where(ds, scope)
    wanted: dict[str, str] = {}    # vin_key → 该车的理由
    problems: list[str] = []
    ref_rows = [int(r) for r, _ in items if isinstance(r, int)] if items is not None else [int(x) for x in row_nos]
    found = {}
    if ref_rows:
        qs = ",".join("?" * len(ref_rows))
        found = {r["row_no"]: r["vin"] for r in conn.execute(
            f"SELECT row_no, vin FROM {ds.table} WHERE snapshot_date=? AND row_no IN ({qs})", (snap, *ref_rows))}
    pairs = list(items) if items is not None else [(n, reason) for n in row_nos] + [(v, reason) for v in vins]
    if len(pairs) > MAX_PER_REQUEST:
        raise ExemptionError(ONE_ONLY_MSG)
    for ref, why in pairs:
        why = (why or "").strip()
        v = vin_key(found.get(int(ref))) if isinstance(ref, int) else vin_key(ref)
        if not v:
            problems.append(f"第 {ref} 行没有 VIN" if isinstance(ref, int) else "VIN 为空")
            continue
        if not why:
            problems.append(f"{v} 未填写理由，已跳过")
            continue
        if len(why) > 500:
            problems.append(f"{v} 的理由超过 500 字，已跳过")
            continue
        wanted[v] = why
    if not wanted:
        raise ExemptionError("没有可提交的车辆（每辆车都必须填写理由）" + ("：" + "；".join(problems[:3]) if problems else ""))
    if len(wanted) > 500:
        raise ExemptionError("一次最多 500 辆")
    from .exclusions import not_excluded

    xs, xa = not_excluded(conn, ind.table, snap)
    created, skipped = [], []
    with conn:
        for v, reason in wanted.items():
            sql = (f"SELECT {','.join(filter(None, ['vin', ds.store_code_col, ds.store_name_col, 'region', ind.flag_col]))} "
                   f"FROM {ds.table} WHERE snapshot_date=? AND in_scope=1 AND {xs} AND upper(trim(vin))=?"
                   + (f" AND {sw}" if sw else ""))
            rows = conn.execute(sql, (snap, *xa, v, *sa)).fetchall()
            if not rows:
                skipped.append((v, "不在你负责的门店范围内、门店不在门店表内，或该车已被剔除" if scope is not None else "不在当日明细里、门店不在门店表内，或该车已被剔除"))
                continue
            todo = [r for r in rows if (r[ind.flag_col] or "") != ind.hit]
            if not todo:
                skipped.append((v, "结果已是「是」，无需修正"))
                continue
            if conn.execute("SELECT 1 FROM exemptions WHERE vin_key=? AND indicator=? AND kind='exempt' AND status IN ('pending','approved')",
                            (v, ind_key)).fetchone():
                skipped.append((v, "已有待审批/已生效的修正结果申请"))
                continue
            r0 = todo[0]
            cur = conn.execute(
                "INSERT INTO exemptions(vin,vin_key,indicator,store_code,store_name,region,reason_category,reason,evidence,"
                "requested_by,requested_at,request_snapshot,status,kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', 'exempt')",
                (r0["vin"], v, ind_key, r0[ds.store_code_col] if ds.store_code_col else None, r0[ds.store_name_col],
                 r0["region"], category, reason, (evidence or "").strip()[:1000], user.username, _now(), snap))
            _log(conn, cur.lastrowid, user.username, "request", None, "pending", f"[修正结果] {category}：{reason}")
            created.append(cur.lastrowid)
    return {"created": created, "skipped": skipped, "problems": problems}


def vehicle_candidates(conn, snap: str, scope, *, q: str = "", store: str = "", region: str = "", limit: int = 200) -> list[dict]:
    """剔除申请列表：所有指标明细里「计入范围、VIN 非空、尚未被剔除」的车辆，按 VIN 去重（整车，不分指标）。
    列：VIN、门店编码/名称、区域、涉及的指标、已有剔除申请状态。"""
    from .exclusions import not_excluded

    found: dict[str, dict] = {}
    for ind in INDICATORS.values():
        ds = BY_KEY[ind.ds_key]
        xs, xa = not_excluded(conn, ind.table, snap)
        where = ["snapshot_date=?", "in_scope=1", xs, "vin IS NOT NULL", "trim(vin)<>''"]
        args: list = [snap, *xa]
        sw, sa = scope_where(ds, scope)
        if sw:
            where.append(sw)
            args += sa
        if q:
            where.append("upper(vin) LIKE ?")
            args.append("%" + vin_key(q).replace("%", "").replace("_", "") + "%")
        if store:
            where.append(f"{ds.store_name_col}=?")
            args.append(store)
        if region:
            where.append("region=?")
            args.append(region)
        cols = ["vin", (ds.store_code_col + " AS store_code") if ds.store_code_col else "NULL AS store_code",
                ds.store_name_col + " AS store_name", "region"]
        for r in conn.execute(f"SELECT DISTINCT {','.join(cols)} FROM {ds.table} WHERE {' AND '.join(where)}", args):
            k = vin_key(r["vin"])
            e = found.setdefault(k, {"vin": r["vin"], "vin_key": k, "store_code": r["store_code"], "store_name": r["store_name"],
                                     "region": r["region"], "inds": []})
            if ind.title not in e["inds"]:
                e["inds"].append(ind.title)
    out = sorted(found.values(), key=lambda e: ((e["store_name"] or ""), e["vin_key"]))[:limit]
    st = exclusion_status(conn, [e["vin_key"] for e in out])
    for e in out:
        e["req"] = st.get(e["vin_key"])
    return out


def exclusion_status(conn, vin_keys: Iterable[str]) -> dict[str, dict]:
    vk = [v for v in set(vin_keys) if v]
    out: dict[str, dict] = {}
    for i in range(0, len(vk), 500):
        part = vk[i:i + 500]
        for r in conn.execute(f"SELECT id,vin_key,kind,status,reason FROM exemptions WHERE kind='exclude' AND status IN ('pending','approved') "
                              f"AND vin_key IN ({','.join('?' * len(part))}) ORDER BY id", part):
            out[r["vin_key"]] = {"id": r["id"], "kind": r["kind"], "status": r["status"], "reason": r["reason"]}
    return out


def request_exclusions(conn, user, snap: str, *, items: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """申请「剔除」整辆车（不分指标）。items=[(VIN, 该车理由)]。
    语义：待审批不生效；管理员批准后该车永久不计入任何指标；每个 VIN 只能有一条待审批/已生效的剔除申请。"""
    if user.role not in ("supervisor", "admin"):
        raise ExemptionError("无权申请")
    items = list(items)
    if len(items) > MAX_PER_REQUEST:
        raise ExemptionError(ONE_ONLY_MSG)
    wanted: dict[str, str] = {}
    problems: list[str] = []
    for ref, why in items:
        v, why = vin_key(ref), (why or "").strip()
        if not v:
            problems.append("VIN 为空")
        elif not why:
            problems.append(f"{v} 未填写理由，已跳过")
        elif len(why) > 500:
            problems.append(f"{v} 的理由超过 500 字，已跳过")
        else:
            wanted[v] = why
    if not wanted:
        raise ExemptionError("没有可提交的车辆（每辆车都必须填写理由）" + ("：" + "；".join(problems[:3]) if problems else ""))
    if len(wanted) > 500:
        raise ExemptionError("一次最多 500 辆")
    scope = scope_for(conn, user)
    cands = {}
    for v in wanted:
        for e in vehicle_candidates(conn, snap, scope, q=v, limit=50):
            if e["vin_key"] == v:
                cands[v] = e
    created, skipped = [], []
    with conn:
        for v, reason in wanted.items():
            e = cands.get(v)
            if not e:
                skipped.append((v, "不在你负责的门店范围内、门店不在门店表内，或该车已被剔除" if scope is not None else "不在当日明细里、门店不在门店表内，或该车已被剔除"))
                continue
            if conn.execute("SELECT 1 FROM exemptions WHERE vin_key=? AND kind='exclude' AND status IN ('pending','approved')", (v,)).fetchone():
                skipped.append((v, "已有待审批/已生效的剔除申请"))
                continue
            cur = conn.execute(
                "INSERT INTO exemptions(vin,vin_key,indicator,store_code,store_name,region,reason_category,reason,evidence,"
                "requested_by,requested_at,request_snapshot,status,kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', 'exclude')",
                (e["vin"], v, EXCLUDE_ALL, e["store_code"], e["store_name"], e["region"], "其他", reason, "",
                 user.username, _now(), snap))
            _log(conn, cur.lastrowid, user.username, "request", None, "pending", f"[剔除·整车] {reason}")
            created.append(cur.lastrowid)
    return {"created": created, "skipped": skipped, "problems": problems}


def withdraw(conn, user, ex_id: int) -> None:
    r = conn.execute("SELECT * FROM exemptions WHERE id=?", (ex_id,)).fetchone()
    if not r or r["requested_by"] != user.username:
        raise ExemptionError("只能撤回自己的申请")
    if r["status"] != "pending":
        raise ExemptionError("只有待审批的申请可以撤回")
    with conn:
        conn.execute("UPDATE exemptions SET status='withdrawn' WHERE id=?", (ex_id,))
        _log(conn, ex_id, user.username, "withdraw", "pending", "withdrawn")


# ----------------------------------------------------------------------------- 审批
def _date(v: str | None, label: str) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ExemptionError(f"{label}需为 YYYY-MM-DD")
    return v


def recompute_from(conn, effective_from: str) -> list[str]:
    from . import calc

    done = []
    for r in conn.execute("SELECT snapshot_date FROM snapshots WHERE snapshot_date>=? ORDER BY snapshot_date", (effective_from,)).fetchall():
        calc.compute_and_store(conn, r[0])
        done.append(r[0])
    return done


def review(conn, admin, ids: Iterable[int], action: str, comment: str = "", *, effective_from: str | None = None,
           expires_at: str | None = None) -> dict[str, Any]:
    """action: approve | reject | revoke。批准/撤销后重算 effective_from 起的快照。"""
    if admin.role != "admin":
        raise ExemptionError("需要管理员权限")
    if action not in ("approve", "reject", "revoke"):
        raise ExemptionError("操作不正确")
    eff, exp = _date(effective_from, "生效日期"), _date(expires_at, "到期日期")
    ids = [int(i) for i in ids]
    if not ids:
        raise ExemptionError("没有选中任何申请")
    if action in ("reject", "revoke") and not (comment or "").strip():
        raise ExemptionError("驳回/撤销需要填写意见")
    need, to = {"approve": ("pending", "approved"), "reject": ("pending", "rejected"), "revoke": ("approved", "revoked")}[action]
    changed, skipped, earliest = [], [], None
    with conn:
        for i in ids:
            r = conn.execute("SELECT * FROM exemptions WHERE id=?", (i,)).fetchone()
            if not r or r["status"] != need:
                skipped.append(i)
                continue
            if action == "approve":
                e = eff or r["request_snapshot"] or datetime.now().strftime("%Y-%m-%d")
                if r["kind"] == "exclude":
                    exp = None   # 剔除批准后永久有效（撤销才恢复）
                if exp and exp < e:
                    raise ExemptionError("到期日期不能早于生效日期")
                conn.execute("UPDATE exemptions SET status=?, reviewed_by=?, reviewed_at=?, review_comment=?, effective_from=?, expires_at=? WHERE id=?",
                             (to, admin.username, _now(), comment, e, exp, i))
                earliest = min(earliest or e, e)
            else:
                conn.execute("UPDATE exemptions SET status=?, reviewed_by=?, reviewed_at=?, review_comment=? WHERE id=?",
                             (to, admin.username, _now(), comment, i))
                if action == "revoke":
                    earliest = min(earliest or r["effective_from"], r["effective_from"])
            _log(conn, i, admin.username, action, need, to, comment)
            changed.append(i)
    recomputed = recompute_from(conn, earliest) if earliest else []
    return {"changed": changed, "skipped": skipped, "recomputed": recomputed}


# ----------------------------------------------------------------------------- 列表 / 导出
def list_exemptions(conn, *, status: str = "", indicator: str = "", q: str = "", requester: str = "", store: str = "",
                    limit: int = 500, kind: str = "", region: str = "", date_from: str = "", date_to: str = "") -> list[dict]:
    where, args = ["1=1"], []
    if region:
        where.append("region=?")
        args.append(region)
    if date_from:
        where.append("substr(requested_at,1,10)>=?")
        args.append(date_from)
    if date_to:
        where.append("substr(requested_at,1,10)<=?")
        args.append(date_to)
    for col, v in (("status", status), ("indicator", indicator), ("requested_by", requester), ("store_name", store), ("kind", kind)):
        if v:
            where.append(f"{col}=?")
            args.append(v)
    if q:
        where.append("(upper(vin) LIKE ? OR store_name LIKE ? OR reason LIKE ?)")
        args += ["%" + vin_key(q) + "%", f"%{q}%", f"%{q}%"]
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM exemptions WHERE {' AND '.join(where)} ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC LIMIT ?",
        (*args, limit))]


def status_map(conn, ind_key: str, vin_keys: Iterable[str]) -> dict[str, dict]:
    """修正结果页用：这些 VIN 在该指标上有效的（待审批/已批准）修正结果申请 → {vin_key: {kind, status, reason, id}}。"""
    vk = [v for v in set(vin_keys) if v]
    out: dict[str, dict] = {}
    for i in range(0, len(vk), 500):
        part = vk[i:i + 500]
        for r in conn.execute(f"SELECT id,vin_key,kind,status,reason FROM exemptions WHERE indicator=? AND kind='exempt' AND status IN ('pending','approved') "
                              f"AND vin_key IN ({','.join('?' * len(part))}) ORDER BY id", (ind_key, *part)):
            out[r["vin_key"]] = {"id": r["id"], "kind": r["kind"], "status": r["status"], "reason": r["reason"]}
    return out


def pending_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM exemptions WHERE status='pending'").fetchone()[0]


def export_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "修正结果与剔除清单"
    heads = ["编号", "类型", "VIN", "指标", "门店", "区域", "原因类别", "理由", "证据说明", "申请人", "申请时间", "状态", "审批人", "审批时间",
             "审批意见", "生效日期", "到期日期"]
    ws.append(heads)
    for r in rows:
        ws.append([r["id"], KIND_CN.get(r["kind"], r["kind"]), r["vin"], indicator_title(r["indicator"]), r["store_name"], r["region"], r["reason_category"], r["reason"],
                   r["evidence"], r["requested_by"], r["requested_at"], STATUS_CN.get(r["status"], r["status"]), r["reviewed_by"],
                   r["reviewed_at"], r["review_comment"], r["effective_from"], r["expires_at"]])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()
