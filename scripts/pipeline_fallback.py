"""Decide when a district crawl should recover and retry, without a live browser.

Feishu is intentionally not sent for an attempt that will be retried. The VIP
auto-login module is optional: this repo never stores DMS credentials.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Callable, Optional

MAX_ATTEMPTS_CAP = 3
_AUTO_LOGIN_MODULES: dict[str, Any] = {}

# Substrings of Playwright / CDP / login failures seen from crawl().
_RECOVERABLE_MARKERS = (
    "has been closed",
    "target closed",
    "targetclosed",
    "browser closed",
    "page closed",
    "context closed",
    "execution context was destroyed",
    "connection closed",
    "websocket",
    "econnrefused",
    "econnreset",
    "protocol error",
    "failed to connect to chrome",
    "over cdp",
    "cdp port",
    "cdp",
    "not responding",
    "net::err_",
    "login page",
    "log in first",
    "not on dms",
    "logged_out",
    "logged out",
    "session closed",
    "browser disconnected",
    "most likely the page has been closed",
)


def redact_text(text: str) -> str:
    """Drop a leaked DMS password. Empty values are ignored."""
    secret = os.environ.get("DMS_LOGIN_PASSWORD") or ""
    if len(secret) >= 4 and secret in text:
        text = text.replace(secret, "***")
    return text


def format_crawl_error(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")[:500]


def is_recoverable_crawl_failure(crawl: dict | None) -> bool:
    """True for a crashed page, a dead CDP session, or a login redirect.

    ``partial`` still builds a report (existing behavior). A fatal crawl with
    no error text is treated as recoverable so a swallowed crash is retried once.
    """
    if not isinstance(crawl, dict):
        return False
    status = str(crawl.get("status") or "")
    if status in {"ok", "partial", "dry_run"}:
        return False
    if status != "fatal_error":
        return False
    error = str(crawl.get("error") or "").strip()
    if not error:
        return True
    blob = error.lower()
    return any(marker in blob for marker in _RECOVERABLE_MARKERS)


def will_retry_crawl(
    *,
    attempt: int,
    max_attempts: int,
    crawl: dict | None,
    dry_run: bool = False,
    skip_crawl: bool = False,
) -> bool:
    if dry_run or skip_crawl:
        return False
    if attempt >= max_attempts:
        return False
    return is_recoverable_crawl_failure(crawl)


def should_notify_pipeline(*, dry_run: bool, skip_notify: bool, will_retry: bool) -> bool:
    """Final success or final failure only. A retry must not ping Feishu."""
    if dry_run or skip_notify or will_retry:
        return False
    return True


def resolve_max_attempts(value: int | None = None) -> int:
    """1 = no retry. Default 2 (one retry). Hard cap avoids overlapping the 09:00 VIP crawl."""
    if value is None:
        raw = os.getenv("DISTRICT_CRAWL_MAX_ATTEMPTS", "2").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 2
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 2
    return max(1, min(number, MAX_ATTEMPTS_CAP))


def resolve_backoff_seconds(value: float | None = None) -> float:
    if value is None:
        raw = os.getenv("DISTRICT_CRAWL_RETRY_BACKOFF_SECONDS", "8").strip()
        try:
            value = float(raw)
        except ValueError:
            value = 8.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 8.0
    if number < 0:
        number = 0.0
    return min(number, 120.0)


def auto_login_candidates(plugin_root: Path) -> list[Path]:
    """VIP ``dms_auto_login.py`` locations. First existing file wins."""
    paths: list[Path] = []
    env_script = (os.environ.get("DMS_AUTO_LOGIN_SCRIPT") or "").strip()
    if env_script:
        paths.append(Path(env_script).expanduser())
    vip_root = (os.environ.get("MHERO_VIP_ROOT") or "").strip()
    if vip_root:
        paths.append(Path(vip_root).expanduser() / "scripts" / "dms_auto_login.py")
    home = Path.home()
    plugin_root = Path(plugin_root)
    paths.extend(
        [
            plugin_root.parent / "m-hero-vip-custom-alert" / "scripts" / "dms_auto_login.py",
            home / "myCode" / "m-hero" / "m-hero-vip-custom-alert" / "scripts" / "dms_auto_login.py",
            home / "m-hero-prod" / "m-hero-vip-custom-alert" / "scripts" / "dms_auto_login.py",
            Path("/home/ubuntu/m-hero-prod/m-hero-vip-custom-alert/scripts/dms_auto_login.py"),
            Path("/home/ubuntu/myCode/m-hero/m-hero-vip-custom-alert/scripts/dms_auto_login.py"),
            Path("/Users/i/myCode/m-hero/m-hero-vip-custom-alert/scripts/dms_auto_login.py"),
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def find_auto_login_script(plugin_root: Path) -> Optional[Path]:
    for path in auto_login_candidates(plugin_root):
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def load_auto_login_module(plugin_root: Path) -> Any:
    path = find_auto_login_script(plugin_root)
    if path is None:
        return None
    key = str(path)
    cached = _AUTO_LOGIN_MODULES.get(key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("vip_dms_auto_login", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        print(f"[pipeline] auto-login module failed to load: {redact_text(f'{type(exc).__name__}: {exc}')[:200]}")
        return None
    _AUTO_LOGIN_MODULES[key] = module
    print(f"[pipeline] auto-login module: {path}")
    return module


def call_auto_login(plugin_root: Path, port: int, reason: str) -> str:
    """Call VIP ``ensure_logged_in`` if the file is on disk. Never invents credentials."""
    module = load_auto_login_module(plugin_root)
    fn = getattr(module, "ensure_logged_in", None) if module is not None else None
    if not callable(fn):
        return "auto_login:unavailable"
    try:
        result = fn(Path(plugin_root), int(port), reason=reason)
    except Exception as exc:
        return redact_text(f"auto_login:error:{type(exc).__name__}: {exc}")[:300]
    return redact_text(str(result))[:300]


def default_recovery_deps(plugin_root: Path) -> dict[str, Callable]:
    from dfmc_browser_utils import (
        ensure_cdp_browser_running,
        get_default_state_file,
        hard_refresh_dms,
        inspect_dms_login,
    )

    root = Path(plugin_root)

    def ensure_port() -> int:
        # Do not launch a second Chromium. Keepalive owns the shared process;
        # a hung or dead browser must fail this crawl instead of stealing the profile.
        state = get_default_state_file(root)
        return int(ensure_cdp_browser_running(state, plugin_root=root))

    def inspect(port: int) -> dict:
        return inspect_dms_login(int(port))

    def refresh(port: int) -> str:
        return hard_refresh_dms(root, int(port), label="pipeline-retry")

    def auto_login(port: int, reason: str) -> str:
        return call_auto_login(root, int(port), reason)

    return {
        "ensure_port": ensure_port,
        "inspect": inspect,
        "refresh": refresh,
        "auto_login": auto_login,
    }


def recover_dms_session(plugin_root: Path | str, deps: dict | None = None) -> dict[str, Any]:
    """Inspect the shared DMS tab, then auto-login or reopen the dashboard.

    Returns ``{ok, action, status, detail}``. ``ok`` is true only when the
    session is ``logged_in`` afterwards. Does not raise.
    """
    root = Path(plugin_root)
    if deps is None:
        deps = default_recovery_deps(root)

    try:
        port = int(deps["ensure_port"]() or 0)
    except Exception as exc:
        return {
            "ok": False,
            "action": "ensure_browser",
            "status": "offline",
            "detail": redact_text(f"{type(exc).__name__}: {exc}")[:300],
        }
    if port <= 0:
        return {"ok": False, "action": "ensure_browser", "status": "offline", "detail": "no cdp port"}

    def inspect() -> dict:
        try:
            snap = deps["inspect"](port) or {}
        except Exception as exc:
            return {"status": "offline", "detail": redact_text(f"{type(exc).__name__}: {exc}")[:200]}
        return snap if isinstance(snap, dict) else {"status": "other"}

    snap = inspect()
    status = str(snap.get("status") or "other")
    print(f"[pipeline] session status={status}")

    if status == "logged_out":
        try:
            detail = str(deps["auto_login"](port, "district pipeline retry"))
        except Exception as exc:
            detail = f"auto_login:error:{type(exc).__name__}: {exc}"
        detail = redact_text(detail)[:300]
        status2 = str(inspect().get("status") or "other")
        if status2 == "logged_in":
            return {"ok": True, "action": "auto_login", "status": status2, "detail": detail}
        if not detail.startswith("auto_login:unavailable"):
            return {"ok": False, "action": "auto_login", "status": status2, "detail": detail}
        status = status2

    try:
        detail = redact_text(str(deps["refresh"](port)))[:300]
    except Exception as exc:
        detail = redact_text(f"{type(exc).__name__}: {exc}")[:300]
        return {"ok": False, "action": "refresh_dashboard", "status": status, "detail": detail}

    status3 = str(inspect().get("status") or "other")
    return {
        "ok": status3 == "logged_in",
        "action": "refresh_dashboard",
        "status": status3,
        "detail": detail,
    }
