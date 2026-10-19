"""批量导入督导账号（来源：多维表格「参与考核特约店清单」的督导字段，经 directory_sync 落地的 store_directory.json）。

  python -m reportdb.import_supervisors --directory data/store_directory.json --db data/reportdb/reportdb.db            # 默认 dry-run，只列清单
  MHERO_BULK_PASSWORD=*** python -m reportdb.import_supervisors ... --apply --cred-file ~/m-hero-run/accounts-admin.cred --api-base http://127.0.0.1:8000

规则：用户名=手机号（11 位大陆手机号），显示名=督导姓名，角色 mhero-supervisor + mhero-app-report（其它系统权限以后另行授予）；
初始密码只从环境变量读取（不进命令行、日志、文档），Casdoor 里标记「首次登录必须改密」；同时在报表库建 users 行并把其门店写入 user_stores。
幂等：已存在的 Casdoor 账号不改密码、不改角色；user_stores 只增不删；报表库里已存在的用户不改角色。
dry-run 的输出里手机号一律打码（138****4381）。"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

PHONE_RE = re.compile(r"1[3-9]\d{9}")


def norm_phone(v) -> str:
    t = re.sub(r"[\s\-]", "", str(v or "")).replace("+86", "")
    if t.endswith(".0") and t[:-2].isdigit():
        t = t[:-2]
    return t


def mask(p: str) -> str:
    return p[:3] + "****" + p[-4:] if PHONE_RE.fullmatch(p or "") else (p[:2] + "…" if p else "")


def plan_from_directory(stores: list[dict]) -> dict:
    """-> {'people': [{name, phone, stores[], open_id}], 'problems': {...}}"""
    by_person: dict[tuple[str, str], dict] = {}
    no_phone, bad_phone, no_sup = [], [], []
    for s in stores:
        sup = s.get("supervisor")
        sup = sup if isinstance(sup, dict) else ({"name": sup} if sup else {})
        name = str(sup.get("name") or "").strip()
        code = s.get("code")
        if not name:
            no_sup.append(code)
            continue
        ph = norm_phone(sup.get("phone"))
        if not ph:
            no_phone.append((name, code))
            continue
        if not PHONE_RE.fullmatch(ph):
            bad_phone.append((name, ph, code))
            continue
        d = by_person.setdefault((name, ph), {"name": name, "phone": ph, "stores": [], "open_id": sup.get("open_id") or ""})
        d["stores"].append(code)
    people = sorted(by_person.values(), key=lambda x: (x["name"], x["phone"]))
    names = defaultdict(set); phones = defaultdict(set)
    for p in people:
        names[p["name"]].add(p["phone"]); phones[p["phone"]].add(p["name"])
    multi_phone = {n: sorted(v) for n, v in names.items() if len(v) > 1}            # same person, several phone numbers
    shared_phone = {ph: sorted(v) for ph, v in phones.items() if len(v) > 1}        # one number, several names -> cannot be a username
    return {"people": people, "no_supervisor": no_sup, "no_phone": no_phone, "bad_phone": bad_phone,
            "multi_phone": multi_phone, "shared_phone": shared_phone}


def importable(plan: dict) -> list[dict]:
    """Skip people whose phone is shared by several names (ambiguous). A name with several phones: only the phone covering the most
    stores is imported (others are reported) so one human = one account."""
    out, best = [], {}
    for p in plan["people"]:
        if p["phone"] in plan["shared_phone"]:
            continue
        cur = best.get(p["name"])
        if cur is None or len(p["stores"]) > len(cur["stores"]):
            best[p["name"]] = p
    return sorted(best.values(), key=lambda x: x["name"])


def render(plan: dict, existing: set[str] | None = None) -> str:
    imp = importable(plan)
    L = [f"督导账号导入清单（dry-run）  门店 {sum(len(p['stores']) for p in plan['people']) + len(plan['no_supervisor']) + len(plan['no_phone']) + len(plan['bad_phone'])} 家",
         f"可导入督导 {len(imp)} 人，覆盖门店 {sum(len(p['stores']) for p in imp)} 家", ""]
    L.append("姓名 | 手机号(打码) | 门店数" + (" | Casdoor 已存在" if existing is not None else ""))
    for p in imp:
        L.append(f"{p['name']} | {mask(p['phone'])} | {len(p['stores'])}" + ((" | 是" if p["phone"] in existing else " | 否") if existing is not None else ""))
    L += ["", f"没有填督导的门店：{len(plan['no_supervisor'])} 家 {plan['no_supervisor'][:10]}",
          f"督导没有手机号：{len(plan['no_phone'])} 家门店，涉及 {sorted({n for n, _ in plan['no_phone']})}",
          f"手机号格式不对：{[(n, mask(ph)) for n, ph, _ in plan['bad_phone']]}",
          f"同一督导多个手机号（只导入门店最多的那个）：{ {n: [mask(x) for x in v] for n, v in plan['multi_phone'].items()} }",
          f"同一手机号对应多个姓名（不导入，需人工确认）：{ {mask(k): v for k, v in plan['shared_phone'].items()} }"]
    return "\n".join(L)


def apply_reportdb(conn, people: list[dict], actor: str = "import") -> dict:
    """只建 users 行（已存在用户不改角色）。门店不再写 user_stores：督导门店只按多维表格「督导」字段匹配（2026-10-19）。"""
    from .auth import service
    new_users = linked = 0
    valid = {r[0] for r in conn.execute("SELECT code FROM stores")}
    for p in people:
        u = service.get_user(conn, p["phone"])
        with conn:
            if u is None:
                conn.execute("INSERT INTO users(username,display_name,role,full_detail,auth_source,created_at,created_by,supervisor_name,disabled)"
                             " VALUES (?,?,?,?,?,?,?,?,0)", (p["phone"], p["name"], "supervisor", 0, "oidc", service._now(), actor, p["name"]))
                new_users += 1
                u = service.get_user(conn, p["phone"])
            linked += sum(1 for c in p["stores"] if c in valid)    # 仅统计：这些门店会按多维表格「督导」自动匹配
    service.audit(conn, actor, "import.supervisors", f"users+{new_users} store_links+{linked}")
    return {"new_users": new_users, "store_links": linked}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--directory", required=True)
    ap.add_argument("--db", default="")
    ap.add_argument("--apply", action="store_true", help="真正创建（默认只 dry-run）")
    ap.add_argument("--cred-file", default=""); ap.add_argument("--api-base", default="")
    ap.add_argument("--password-env", default="MHERO_BULK_PASSWORD")
    ap.add_argument("--audit-log", default=os.getenv("MHERO_ACCOUNTS_AUDIT_LOG", ""))
    a = ap.parse_args(argv)
    stores = json.loads(Path(a.directory).read_text(encoding="utf-8")).get("stores") or []
    plan = plan_from_directory(stores)
    imp = importable(plan)
    existing = None
    cl = svc = None
    if a.cred_file and a.api_base:                      # read-only lookup of which accounts already exist
        from mhero_auth.accounts import AccountService
        from mhero_auth.casdoor_admin import CasdoorAdmin, load_credential
        cl = CasdoorAdmin(a.api_base, os.getenv("MHERO_AUTH_ORG", "mhero"), load_credential(a.cred_file))
        svc = AccountService(cl, audit_path=a.audit_log, revoke_path=os.getenv("MHERO_AUTH_REVOKE_FILE", ""))
        existing = {u["name"] for u in cl.list_users()}
    print(render(plan, existing))
    if not a.apply:
        print("\n(dry-run：没有创建任何账号，也没有改动报表库)")
        return 0
    pw = os.environ.get(a.password_env, "")
    if not pw or svc is None or not a.db:
        print("--apply 需要：环境变量 " + a.password_env + "、--cred-file、--api-base、--db", file=sys.stderr)
        return 2
    from .db import connect, migrate
    created = exist = 0
    for p in imp:
        r = svc.create_bulk("import", p["phone"], p["name"], pw, "supervisor", ["report"])
        created += r == "created"; exist += r == "exists"
    conn = connect(a.db); migrate(conn)
    res = apply_reportdb(conn, imp)
    print(f"\n完成：Casdoor 新建 {created}，已存在跳过 {exist}；报表库新增用户 {res['new_users']}，新增门店关联 {res['store_links']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
