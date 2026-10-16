import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ.pop("MHERO_REPORTDB", None)
os.environ["REPORTDB_SYNC_STORES"] = "0"  # 测试不联网
os.environ["REPORTDB_PBKDF2_ITERS"] = "1000"
os.environ.pop("REPORTDB_REGION_MODE", None)


@pytest.fixture()
def conn(tmp_path):
    from reportdb.db import connect, migrate

    c = connect(tmp_path / "t.sqlite3")
    migrate(c)
    yield c
    c.close()


def dl_files(day="20261009"):
    d = ROOT / "download"
    return list(d.glob(f"*{day}_08*.xlsx"))


needs_real_data = pytest.mark.skipif(
    len(dl_files()) < 7 or not (ROOT / "output" / "区域各指标情况一览1009.xlsx").exists(),
    reason="需要 download/ 与 output/ 里 2026-10-09 的真实源文件（被 .gitignore 忽略）",
)
