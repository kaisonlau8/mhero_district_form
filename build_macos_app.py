from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
APP_NAME = "区域报表自动生成"
SPEC_PATH = ROOT / f"{APP_NAME}.spec"
ZIP_NAME = "mhero-district-form-macos-arm64.zip"


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def main() -> None:
    remove_path(SPEC_PATH)
    remove_path(BUILD_DIR)
    remove_path(DIST_DIR)

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--windowed",
            "--name",
            APP_NAME,
            "--add-data",
            "app/static:app/static",
            "--add-data",
            "app/assets:app/assets",
            "desktop_app.py",
        ]
    )
    run(
        [
            "ditto",
            "-c",
            "-k",
            "--sequesterRsrc",
            "--keepParent",
            str(DIST_DIR / f"{APP_NAME}.app"),
            str(DIST_DIR / ZIP_NAME),
        ]
    )
    remove_path(SPEC_PATH)


if __name__ == "__main__":
    main()
