from __future__ import annotations

from pathlib import Path
import sys


def bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    bundled_app_dir = bundle_root() / "app"
    if bundled_app_dir.exists():
        return bundled_app_dir
    return Path(__file__).resolve().parent


def static_dir() -> Path:
    return app_dir() / "static"


def assets_dir() -> Path:
    return app_dir() / "assets"
