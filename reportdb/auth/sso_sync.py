"""把统一登录（Casdoor）里的账号同步进报表库 users 表，管理员不必等对方先登录一次就能在「用户管理」里调整权限。

规则与登录时完全一致（复用 OidcBackend.map_identity）：
- 能进入本系统的账号（全局管理员，或 督导 + 开通了「区域报表」；按 REPORTDB_OIDC_* / MHERO_AUTH_REQUIRE_APP 判断）才建本地用户；
- 新建用户：身份 = Casdoor 身份（管理员 / 督导），明细权限按登录规则，不额外授予任何门店（user_stores 为空）；
  督导的「督导姓名」默认取显示名（与首次登录一致，之后由管理员调整）；
- 已有的 oidc 用户：只同步显示名与身份（登录时本来也会这样同步），不动门店分配；身份变化时明细权限按新身份重算并踢下线；
  Casdoor 里已停用的账号同步为停用并踢下线（只停不启，避免误把本地手工停用的账号放开）；
- 用户名只差大小写的不同账号不合并（计入 case_conflicts）；
- 本地有、但 Casdoor 里已不存在 / 已不能进入本系统的账号：不删除，只计数（登录时本来就会被拒）。
从不读取或写入密码。
"""
from __future__ import annotations

import sqlite3
import threading
import time

from .base import AuthError
from . import service

_LOCK = threading.Lock()
_LAST = {"at": 0.0}


def _info_from_snapshot(row: dict, prefix: str = "mhero-") -> dict:
    roles = []
    suffix = {"admin": "admin", "readonly_admin": "readonly-admin", "supervisor": "supervisor"}.get(row.get("level") or "")
    if suffix:
        roles.append(prefix + suffix)
    roles += [f"{prefix}app-{a}" for a in row.get("apps") or []]
    name = row["name"]
    return {"preferred_username": name, "name": name, "sub": name, "displayName": row.get("display") or name,
            "roles": [{"name": r} for r in roles]}


def sync_users(conn: sqlite3.Connection, snapshot: list[dict], backend, actor: str = "sso-sync") -> dict:
    """snapshot = AccountService.snapshot()；backend 需提供 map_identity（OidcBackend）。返回统计。"""
    import os
    claim = os.getenv("REPORTDB_OIDC_USERNAME_CLAIM", "preferred_username")
    if claim not in ("preferred_username", "name", "sub"):
        raise AuthError(f"REPORTDB_OIDC_USERNAME_CLAIM={claim} 与 Casdoor 账号名不对应，无法从 SSO 同步", 400)
    stats = {"casdoor": len(snapshot), "eligible": 0, "created": [], "updated": [], "disabled": [], "skipped_no_access": 0,
             "skipped_disabled": 0, "case_conflicts": [], "local_without_access": []}
    eligible: set[str] = set()
    with _LOCK, conn:
        for row in snapshot:
            name = row["name"]
            u = conn.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
            if u is not None and u["username"] != name:
                stats["case_conflicts"].append(name)          # 只差大小写的不同账号：不合并、不改动，交给管理员处理
                continue
            oidc_local = u is not None and u["auth_source"] == "oidc"
            if row.get("disabled") and oidc_local and not u["disabled"]:
                # Casdoor 已停用 → 本地停用并踢下线（无论是否仍有本系统权限）；只停不启
                conn.execute("UPDATE users SET disabled=1 WHERE id=?", (u["id"],))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
                stats["disabled"].append(name)
            try:
                ident = backend.map_identity(_info_from_snapshot(row))
            except AuthError:
                stats["skipped_no_access"] += 1
                continue
            eligible.add(name.casefold())
            stats["eligible"] += 1
            if u is None:
                if row.get("disabled"):
                    stats["skipped_disabled"] += 1
                    continue
                conn.execute("INSERT INTO users(username,display_name,role,full_detail,auth_source,created_at,created_by,supervisor_name,global_admin)"
                             " VALUES (?,?,?,?,?,?,?,?,?)",
                             (name, ident.display_name, ident.role, int(ident.full_detail), "oidc", service._now(), actor,
                              ident.display_name if ident.role == "supervisor" else None, int(ident.global_admin)))
                stats["created"].append(name)
                continue
            if not oidc_local:
                continue          # 本地账号（如 break-glass 管理员）不受 SSO 影响
            changes = {}
            if ident.display_name and ident.display_name != (u["display_name"] or ""):
                changes["display_name"] = ident.display_name
            if ident.role != u["role"]:
                changes["role"] = ident.role
                changes["full_detail"] = int(ident.full_detail)      # 与登录一致：身份变了，明细权限按新身份重算
            if int(ident.global_admin) != int(u["global_admin"] or 0):
                changes["global_admin"] = int(ident.global_admin)
            if ident.role == "supervisor" and not u["supervisor_name"]:
                changes["supervisor_name"] = ident.display_name
            if changes:
                conn.execute(f"UPDATE users SET {', '.join(k + '=?' for k in changes)} WHERE id=?", (*changes.values(), u["id"]))
                if "role" in changes or "global_admin" in changes:
                    conn.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))   # 权限变化后旧会话作废，重新登录生效
                stats["updated"].append(name)
        for (name,) in conn.execute("SELECT username FROM users WHERE auth_source='oidc' AND disabled=0"):
            if name.casefold() not in eligible:
                stats["local_without_access"].append(name)
        if stats["created"] or stats["updated"] or stats["disabled"]:
            conn.execute("INSERT INTO audit_log(at,actor,action,detail,ip) VALUES (?,?,?,?,?)",
                         (service._now(), actor, "sso.sync",
                          f"新增 {len(stats['created'])}：{','.join(stats['created'])}；更新 {len(stats['updated'])}："
                          f"{','.join(stats['updated'])}；停用 {len(stats['disabled'])}：{','.join(stats['disabled'])}", ""))
    _LAST["at"] = time.time()
    return stats


def maybe_sync(conn_factory, bridge, backend, *, min_interval: float = 60.0, actor: str = "sso-sync") -> dict | None:
    """节流的自动同步（打开用户管理页 / 后台定时调用）；失败返回 None，不影响页面。"""
    if bridge is None or not hasattr(backend, "map_identity") or time.time() - _LAST["at"] < min_interval:
        return None
    _LAST["at"] = time.time()      # 失败也节流，避免 Casdoor 故障时每次打开页面都卡住
    try:
        snap = bridge.snapshot()
        conn = conn_factory()
        try:
            return sync_users(conn, snap, backend, actor)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — 自动同步失败不影响页面，下次再试
        print(f"[reportdb] SSO 账号自动同步失败：{type(exc).__name__}: {str(exc)[:200]}", flush=True)
        return None
