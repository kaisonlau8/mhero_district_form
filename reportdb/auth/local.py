from __future__ import annotations

from .base import AuthBackend, Identity
from . import service


class LocalBackend(AuthBackend):
    """本地账号密码（默认）。用户存在 users 表，由 /admin/users 管理。"""

    name = "local"
    supports_password_login = True
    manages_users = True

    def authenticate_password(self, conn, username, password, ip) -> Identity:
        return service.authenticate_local(conn, username, password, ip)
