"""Pick the file the public/Feishu download link serves (GET /api/report/latest).

engine=reportdb  -> ONLY the reportdb daily report: the file named by output/pipeline_manifest.json
                    (engine=="reportdb", status ok), or failing that the newest workbook that has the 5 summary
                    sheets and nothing else (no 首保/二保/新保/续保/备件库存明细… detail sheets) and is not a legacy variant.
engine=excel     -> legacy behaviour unchanged (newest 区域各指标情况一览*.xlsx, prefer *_公式版 sidecar).
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

SUMMARY_SHEETS = {"总表", "区域指标排序", "直营店指标", "直营店指标排序", "常备件备库率"}
LEGACY_MARKERS = ("_公式版", "新模板", "_legacy")
PATTERN = "区域各指标情况一览*.xlsx"


def sheet_names(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("xl/workbook.xml").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return []
    return [n.replace("&amp;", "&") for n in re.findall(r"<sheet\b[^>]*?\bname=\"([^\"]+)\"", xml)]


def is_summary_only(path: Path) -> bool:
    names = sheet_names(path)
    return bool(names) and set(names) <= SUMMARY_SHEETS


def latest_legacy(output_dir: Path) -> str:
    files = [p for p in output_dir.glob(PATTERN) if not p.name.startswith("~$")]
    if not files:
        return ""
    latest = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    if "_公式版" not in latest.name:
        companion = latest.with_name(f"{latest.stem}_公式版.xlsx")
        if companion.exists():
            return str(companion)
    return str(latest)


def latest_reportdb(output_dir: Path) -> str:
    manifest = output_dir / "pipeline_manifest.json"
    try:
        m = json.loads(manifest.read_text(encoding="utf-8"))
        if m.get("engine") == "reportdb" and m.get("status") == "ok" and m.get("report"):
            cand = output_dir / Path(str(m["report"])).name          # manifest may carry another host's absolute path
            if cand.is_file() and is_summary_only(cand):
                return str(cand)
    except (OSError, ValueError):
        pass
    files = [p for p in output_dir.glob(PATTERN)
             if not p.name.startswith("~$") and not any(k in p.name for k in LEGACY_MARKERS) and is_summary_only(p)]
    if not files:
        return ""
    return str(sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[0])


def latest_report(output_dir: Path, engine: str) -> str:
    if not output_dir.exists():
        return ""
    if (engine or "excel").strip().lower() == "reportdb":
        return latest_reportdb(output_dir)
    return latest_legacy(output_dir)
