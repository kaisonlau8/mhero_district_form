"""Helpers for Excel download compatibility (old Excel / Feishu / Windows)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from flask import Response, send_file

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def ascii_report_name(source: Path | str | None = None) -> str:
    """Stable ASCII filename so old clients keep the .xlsx extension."""
    stamp = ""
    if source is not None:
        name = Path(source).name
        m = re.search(r"(\d{4})", name)  # MMDD or HHMMSS fragment
        if m:
            stamp = m.group(1)
        else:
            m2 = re.search(r"(\d{8})", name)
            if m2:
                stamp = m2.group(1)
    if not stamp:
        from time_utils import beijing_strftime

        stamp = beijing_strftime("%m%d")
    return f"district-report-{stamp}.xlsx"


def write_ascii_sidecar(report_path: Path) -> Path:
    """Copy report to an ASCII-named sidecar next to it for easy sharing."""
    report_path = Path(report_path)
    side = report_path.parent / ascii_report_name(report_path)
    if side.resolve() != report_path.resolve():
        side.write_bytes(report_path.read_bytes())
    return side


def content_disposition(display_name: str, ascii_name: str | None = None) -> str:
    ascii_name = ascii_name or ascii_report_name(display_name)
    if not ascii_name.lower().endswith(".xlsx"):
        ascii_name = f"{ascii_name}.xlsx"
    # RFC 6266: ASCII fallback + UTF-8 filename*
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(display_name)}"
    )


def send_xlsx_file(path: Path, *, display_name: str | None = None) -> Response:
    path = Path(path)
    display_name = display_name or path.name
    if not display_name.lower().endswith(".xlsx"):
        display_name = f"{display_name}.xlsx"
    ascii_name = ascii_report_name(display_name)
    resp = send_file(
        path,
        mimetype=XLSX_MIME,
        as_attachment=True,
        download_name=ascii_name,
        conditional=True,
        max_age=0,
    )
    resp.headers["Content-Type"] = XLSX_MIME
    resp.headers["Content-Disposition"] = content_disposition(display_name, ascii_name)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # Help some proxies/clients preserve binary handling.
    resp.headers["Content-Transfer-Encoding"] = "binary"
    return resp
