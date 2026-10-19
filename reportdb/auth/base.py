"""认证后端接口。Web 应用只依赖这里的抽象，不关心登录方式；
本地账号密码（LocalBackend）与 OIDC 单点登录（OidcBackend，Casdoor 等）可通过环境变量切换。"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class AuthError(Exception):
    """登录失败（消息可直接展示给用户）。"""

    def __init__(self, message: str, status: int = 401, display_name: str = ""):
        super().__init__(message)
        self.status = status
        self.display_name = display_name        # 仅 403「未开通本系统」友好页展示


@dataclass
class Identity:
    username: str
    display_name: str = ""
    role: str = "viewer"          # admin | readonly_admin | supervisor | viewer(=用户，只看汇总)
    full_detail: bool = False
    auth_source: str = "local"    # local | oidc
    global_admin: bool = False    # 统一登录的全局超级管理员（mhero-admin）：唯一能管账号 / 权限


class AuthBackend(ABC):
    """扩展点：
    * supports_password_login —— 是否展示账号/密码表单（本地登录为 True）。
    * authenticate_password   —— 校验账号密码，返回 Identity（失败抛 AuthError）。
    * login_redirect          —— 外部 IdP 登录：返回要跳转的 URL（并可在 response cookie 里留 state）；本地登录返回 None。
    * complete_login          —— 外部 IdP 回调：用 query 参数换取身份。
    * logout_redirect         —— 登出后要跳转的外部地址（如 IdP 的 end_session），无则 None。
    * manages_users           —— True 表示用户由本系统管理（/admin/users 可建号/禁用/重置密码）；
                                 OIDC 下用户和密码在 IdP 里管理，本系统只保留角色映射。
    会话（cookie、过期、CSRF）统一由 reportdb.auth.service 处理，与后端无关。"""

    name: str = "base"
    supports_password_login: bool = False
    manages_users: bool = False

    def authenticate_password(self, conn: sqlite3.Connection, username: str, password: str, ip: str) -> Identity:
        raise AuthError("该登录方式不支持账号密码")

    def login_redirect(self, request: Any, next_url: str) -> tuple[str, dict[str, str]] | None:
        """返回 (跳转 URL, 需要写入的临时 cookie) 或 None。"""
        return None

    def complete_login(self, conn: sqlite3.Connection, request: Any) -> tuple[Identity, str]:
        """返回 (Identity, 登录后跳转的 next)。"""
        raise AuthError("该登录方式不支持回调")

    def logout_redirect(self, request: Any) -> str | None:
        return None
