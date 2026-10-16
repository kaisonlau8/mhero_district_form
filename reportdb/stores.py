"""门店主数据：模板种子 + 多维表格（参与考核特约店清单）同步。

多维表格只读：这里调用 scripts/directory_sync.py 里的 _fetch_records()（只发 GET），
结果只写 DB，不写 data/store_directory.json，也不触碰任何飞书写接口。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import config
from .db import set_meta


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sup_name(v: Any) -> str | None:
    """督导可能是字符串，或 directory_sync 的 {name, open_id, phone} 结构。"""
    if isinstance(v, dict):
        v = v.get("name")
    v = str(v).strip() if v else ""
    return v or None


def norm_region(v: Any) -> str | None:
    """区域名归一：去空白和结尾的「区」。华中1区 / 华中 1 → 华中1；华北区 → 华北。"""
    t = re.sub(r"\s+", "", str(v)) if v else ""
    if t.endswith("区") and len(t) > 1:
        t = t[:-1]
    return t or None


class StoreSyncAborted(RuntimeError):
    """多维表格返回的门店名单为空或明显偏少：放弃本次同步，保留现有门店表。"""


def upsert_stores(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    authoritative: bool | None = None,
    source: str = "bitable",
    min_ratio: float | None = None,
) -> dict[str, int]:
    """rows: [{code,name,region,province,city,district}]。
    - 已有门店：更新名称/区域/省市区，标记 in_bitable=1。
    - 新门店：追加到 seq 末尾（active=1）。
    - authoritative=True（默认，多维表格为唯一来源）：不在清单里的门店 active=0，in_bitable=0。
    - 安全闸：清单为空，或有效门店数 < 当前启用门店数 × min_ratio（默认 50%）→ 抛 StoreSyncAborted，什么都不改。
    """
    if authoritative is None:
        authoritative = config.stores_authoritative()
    if min_ratio is None:
        min_ratio = config.STORE_SYNC_MIN_RATIO
    incoming = {str(r.get("code") or "").strip() for r in rows} - {""}
    current_active = conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0]
    if not incoming:
        raise StoreSyncAborted("多维表格返回的门店名单为空，已放弃同步，保留现有门店表")
    if authoritative and current_active and len(incoming) < current_active * min_ratio:
        raise StoreSyncAborted(
            f"多维表格只返回 {len(incoming)} 家门店，少于当前启用门店 {current_active} 家的 {int(min_ratio*100)}%，已放弃同步，保留现有门店表")
    added = updated = deactivated = 0
    seen: set[str] = set()
    with conn:
        max_seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM stores").fetchone()[0]
        for r in rows:
            code = str(r.get("code") or "").strip()
            if not code:
                continue
            seen.add(code)
            r = dict(r, region=norm_region(r.get("region")))
            cur = conn.execute("SELECT code,name,region FROM stores WHERE code=?", (code,)).fetchone()
            if cur:
                conn.execute(
                    "UPDATE stores SET name=?, region=?, bitable_region=?, province=?, city=?, district=?, in_bitable=1,"
                    " active=1, source=?, updated_at=? WHERE code=?",
                    (r.get("name") or cur["name"], r.get("region") or cur["region"], r.get("region"), r.get("province"),
                     r.get("city"), r.get("district"), source, _now(), code),
                )
                if "supervisor" in r:
                    conn.execute("UPDATE stores SET supervisor=? WHERE code=?", (_sup_name(r.get("supervisor")), code))
                updated += 1
            else:
                max_seq += 1
                conn.execute(
                    "INSERT INTO stores(code,name,region,bitable_region,province,city,district,seq,active,source,in_bitable,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,1,?,1,?)",
                    (code, r.get("name") or code, r.get("region"), r.get("region"), r.get("province"), r.get("city"),
                     r.get("district"), max_seq, source, _now()),
                )
                if "supervisor" in r:
                    conn.execute("UPDATE stores SET supervisor=? WHERE code=?", (_sup_name(r.get("supervisor")), code))
                added += 1
        if authoritative and seen:
            cur = conn.execute(
                f"UPDATE stores SET active=0, in_bitable=0 WHERE active=1 AND code NOT IN ({','.join('?' * len(seen))})",
                tuple(seen),
            )
            deactivated = cur.rowcount
        missing = conn.execute("SELECT COUNT(*) FROM stores WHERE in_bitable=0 AND active=1").fetchone()[0]
    set_meta(conn, "stores_synced_at", _now())
    set_meta(conn, "stores_source", source)
    return {"added": added, "updated": updated, "deactivated": deactivated, "active_not_in_bitable": missing,
            "total": conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0]}


def sync_from_directory_json(conn: sqlite3.Connection, path: Path | None = None, **kw) -> dict[str, int]:
    """用本地缓存 data/store_directory.json（directory_sync 写出的文件）更新门店表。离线可用。"""
    p = Path(path) if path else config.PROJECT_ROOT / "data" / "store_directory.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    res = upsert_stores(conn, data.get("stores") or [], source="directory_json", **kw)
    set_meta(conn, "stores_directory_synced_at", str(data.get("synced_at") or ""))
    return res


def sync_from_bitable(conn: sqlite3.Connection, **kw) -> dict[str, int]:
    """直接读多维表格（只读 GET），只写 DB。需要 .env 里的 APP_ID/APP_SECRET/DIRECTORY_*。"""
    scripts = str(config.PROJECT_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import directory_sync as ds  # noqa: WPS433

    stores = []
    for fields in ds._fetch_records():
        code = ds._field_text(fields.get("店代码") or fields.get("门店编码"))
        if not code:
            continue
        stores.append({
            "code": code,
            "name": ds._field_text(fields.get("店名称") or fields.get("门店名称")),
            "region": ds._field_text(fields.get("区域")),
            "province": ds._field_text(fields.get("门店省")),
            "city": ds._field_text(fields.get("门店市")),
            "district": ds._field_text(fields.get("门店区")),
            "supervisor": (ds._parse_user(fields.get("督导")) or (None,))[0],
        })
    if not stores:
        raise StoreSyncAborted("参与考核特约店清单未返回有效门店，已放弃同步，保留现有门店表")
    res = upsert_stores(conn, stores, source="bitable", **kw)
    set_meta(conn, "stores_directory_synced_at", _now())
    return res


def region_lookup_factory(conn: sqlite3.Connection):
    """返回 f(code, name, src_region=None)->(region|None, in_scope)。
    是否参与考核（in_scope）只认启用中的门店（多维表格清单）；不在清单内 → (None, 0)。
    区域：以 DMS 导出的区域列（归一后）为准；DMS 为空/不是已知区域时，退回该门店的多维表格区域。"""
    known = {(r[0] or "").casefold() for r in conn.execute("SELECT name FROM regions")}
    by_code: dict[str, str | None] = {}
    by_name: dict[str, str | None] = {}
    has_b = "bitable_region" in {r[1] for r in conn.execute("PRAGMA table_info(stores)")}
    rcol = "COALESCE(bitable_region,region)" if has_b else "region"
    for r in conn.execute(f"SELECT code,name,{rcol} AS region FROM stores WHERE active=1 ORDER BY seq"):
        by_code[(r["code"] or "").casefold()] = r["region"]
        by_name.setdefault((r["name"] or "").casefold(), r["region"])

    def lookup(code: Any, name: Any, src_region: Any = None) -> tuple[str | None, int]:
        if code and str(code).casefold() in by_code:
            fb = by_code[str(code).casefold()]
        elif name and str(name).casefold() in by_name:
            fb = by_name[str(name).casefold()]
        else:
            return None, 0
        d = norm_region(src_region)
        if d and d.casefold() in known:
            return d, 1
        return fb, 1

    return lookup


def apply_dms_regions(conn: sqlite3.Connection, snap: str) -> dict[str, Any]:
    """把门店表的 region 设为该快照 DMS 导出里该店出现最多的区域（归一后）；DMS 里没有 → 退回多维表格区域。
    只在 snap 是最新快照时执行（门店表只反映最新状态）。返回 {changed:[...], fallback_rows:{ds:n}, fallback_stores:[...]}。"""
    from .datasets import DATASETS
    if "bitable_region" not in {r[1] for r in conn.execute("PRAGMA table_info(stores)")}:
        return {"skipped": True}
    latest = conn.execute("SELECT MAX(snapshot_date) FROM snapshots").fetchone()[0]
    if latest and snap < latest:
        return {"skipped": True}
    votes: dict[str, dict[str, int]] = {}
    fb_rows: dict[str, int] = {}
    fb_stores: set[str] = set()
    for ds in DATASETS:
        if not ds.has_region:
            continue
        for r in conn.execute(
            f"SELECT {ds.store_code_col} c, region, src_region FROM {ds.table} WHERE snapshot_date=? AND in_scope=1", (snap,)):
            if norm_region(r["src_region"]) and r["region"]:
                votes.setdefault((r["c"] or "").casefold(), {}).setdefault(r["region"], 0)
                votes[(r["c"] or "").casefold()][r["region"]] += 1
            else:
                fb_rows[ds.key] = fb_rows.get(ds.key, 0) + 1
                fb_stores.add(r["c"] or "")
    changed = []
    with conn:
        for s in conn.execute("SELECT code,name,region,bitable_region FROM stores WHERE active=1").fetchall():
            v = votes.get((s["code"] or "").casefold())
            new = max(v.items(), key=lambda kv: kv[1])[0] if v else s["bitable_region"]
            if new and new != s["region"]:
                conn.execute("UPDATE stores SET region=? WHERE code=?", (new, s["code"]))
                changed.append((s["code"], s["name"], s["region"], new))
    return {"changed": changed, "fallback_rows": fb_rows, "fallback_stores": sorted(fb_stores)}
