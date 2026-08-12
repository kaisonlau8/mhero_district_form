"""Time utilities locked to UTC+8 / Beijing time (Asia/Shanghai)."""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

BEIJING_TZ_NAME = "Asia/Shanghai"
BEIJING_TZ = ZoneInfo(BEIJING_TZ_NAME)
_TZ_LOCKED = False


def ensure_beijing_tz() -> None:
    """进程级锁定：环境变量 TZ + tzset，避免跟随机器本地时区。"""
    global _TZ_LOCKED
    if _TZ_LOCKED and os.environ.get("TZ") == BEIJING_TZ_NAME:
        return
    os.environ["TZ"] = BEIJING_TZ_NAME
    # Unix: 让 time.localtime / strftime 跟随 TZ
    if hasattr(time, "tzset"):
        time.tzset()
    _TZ_LOCKED = True


ensure_beijing_tz()


def beijing_now() -> datetime:
    ensure_beijing_tz()
    return datetime.now(BEIJING_TZ)


def beijing_today() -> date:
    return beijing_now().date()


def beijing_strftime(fmt: str) -> str:
    return beijing_now().strftime(fmt)


def beijing_iso() -> str:
    """ISO8601，固定带 +08:00。"""
    return beijing_now().isoformat(timespec="seconds")


def from_timestamp_beijing(ts: float) -> datetime:
    ensure_beijing_tz()
    return datetime.fromtimestamp(ts, BEIJING_TZ)
