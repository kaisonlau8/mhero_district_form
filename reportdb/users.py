"""用户管理 CLI：
  python -m reportdb.users create-admin [--username admin]   首个管理员；密码取自 REPORTDB_ADMIN_PASSWORD 或交互输入（不回显、输两次）
  python -m reportdb.users list
  python -m reportdb.users reset-password USERNAME
  python -m reportdb.users disable|enable USERNAME
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from .auth import AuthError, service
from .db import connect, migrate


def _password(prompt_user: str) -> str:
    env = os.getenv("REPORTDB_ADMIN_PASSWORD", "")
    if env:
        return env
    if not sys.stdin.isatty():
        raise AuthError("非交互环境请设置环境变量 REPORTDB_ADMIN_PASSWORD", 400)
    p1 = getpass.getpass(f"为 {prompt_user} 设置密码（至少 {service.MIN_PASSWORD} 位）：")
    if p1 != getpass.getpass("再输一次："):
        raise AuthError("两次输入不一致", 400)
    return p1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="reportdb 用户管理")
    ap.add_argument("--db", default="")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-admin"); c.add_argument("--username", default="admin")
    sub.add_parser("list")
    r = sub.add_parser("reset-password"); r.add_argument("username")
    d = sub.add_parser("disable"); d.add_argument("username")
    e = sub.add_parser("enable"); e.add_argument("username")
    args = ap.parse_args(argv)
    conn = connect(args.db or None)
    migrate(conn)
    try:
        if args.cmd == "create-admin":
            service.create_user(conn, args.username, _password(args.username), role="admin", full_detail=True, created_by="cli")
            print(f"已创建管理员 {args.username}")
        elif args.cmd == "list":
            for u in conn.execute("SELECT username,role,full_detail,disabled,auth_source,last_login FROM users ORDER BY id"):
                print(f"{u['username']:<20} {u['role']:<7} 完整明细={u['full_detail']} 停用={u['disabled']} {u['auth_source']} 最近登录={u['last_login'] or '-'}")
        elif args.cmd == "reset-password":
            service.set_password(conn, args.username, _password(args.username), "cli")
            print("密码已重置，该用户的旧会话已失效")
        else:
            u = service.get_user(conn, args.username)
            if not u:
                raise AuthError("用户不存在", 404)
            service.update_user(conn, u["id"], "cli", disabled=(args.cmd == "disable"))
            print("完成")
    except AuthError as exc:
        print("错误：", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
