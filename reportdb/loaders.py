"""读取 7 份源 xlsx（或已生成的日报 xlsx 里的源数据 sheet），复用 app/processor.py 的归一化规则。"""
from __future__ import annotations

import re
import sys
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from . import config

if str(config.PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(config.PROJECT_ROOT))

from app.processor import (  # noqa: E402  复用，保证与 Excel 路径同一套规则
    SOURCE_SPECS,
    detect_file_role,
    load_rows_from_workbook,
    normalize_backlog_rows,
    normalize_first_service_rows,
    normalize_stock_rows,
    parse_numeric_text,
)

from .datasets import BY_KEY, DATASETS, Dataset

_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


_STRIP_RE = re.compile(r"[\s\-_\u3000]+")


def part_key(value: Any) -> str | None:
    """备件号的比较键：去空格/横线/下划线、转大写；数字与数字样文本等同（Excel SUMIFS 的行为：22806000 == "22806000" == "022806000"）。"""
    if value is None:
        return None
    if isinstance(value, float) and value == int(value):
        value = int(value)
    text = str(value)
    text = _STRIP_RE.sub("", text).upper()
    if text == "":
        return None
    if _NUM_RE.fullmatch(text):
        try:
            f = float(text)
            return str(int(f)) if f == int(f) else repr(f)
        except ValueError:
            pass
    return text


def clean(value: Any) -> Any:
    """空串 → None（Excel 写出再读回同样是空单元格）；其余原样。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value if value != "" else None
    if hasattr(value, "isoformat") and not isinstance(value, (int, float)):
        return value.isoformat(sep=" ") if hasattr(value, "hour") else value.isoformat()
    return value


def _pad(row: Iterable[Any], n: int) -> list[Any]:
    cur = list(row)
    if len(cur) < n:
        cur.extend([None] * (n - len(cur)))
    return cur[:n]


def find_source_files(directory: Path) -> dict[str, Path]:
    """与 run_pipeline.collect_source_files + build_report 同一选择规则：按文件名排序，同角色后者覆盖前者。"""
    found: dict[str, Path] = {}
    for path in sorted(Path(directory).glob("*.xlsx")):
        if path.name.startswith("~$"):
            continue
        role = detect_file_role(path.name)
        if role in (None, "template"):
            continue
        found[role] = path
    return found


def load_aligned(role: str, path: Path) -> list[list[Any]]:
    """role → 去掉表头、做完归一化、与模板列对齐的行。"""
    rows = load_rows_from_workbook(Path(path).read_bytes(), Path(path).name)[1:]
    if role == "stock":
        return normalize_stock_rows(rows)
    if role == "first_service":
        return normalize_first_service_rows(rows)
    if role == "renewal_backlog":
        return normalize_backlog_rows(rows)
    return [list(r) for r in rows]


def load_directory_sources(directory: Path) -> tuple[dict[str, list[list[Any]]], dict[str, str]]:
    """返回 ({dataset_key: aligned_rows}, {role: filename})。续保 = 续保 + backlog（origin 另记）。"""
    files = find_source_files(directory)
    missing = [s.prefix for s in SOURCE_SPECS if s.key not in files]
    if missing:
        raise ValueError("缺少以下文件：" + "、".join(missing))
    role_key = {"stock": "stock", "task": "task", "first_service": "first-service",
                "second_service": "second-service", "new_policy": "new-policy",
                "renewal": "renewal", "renewal_backlog": "renewal"}
    out: dict[str, list[list[Any]]] = {}
    origin: list[str] = []
    for spec in SOURCE_SPECS:
        rows = load_aligned(spec.key, files[spec.key])
        out.setdefault(role_key[spec.key], []).extend(rows)
        if role_key[spec.key] == "renewal":
            origin.extend(["backlog" if spec.key == "renewal_backlog" else "renewal"] * len(rows))
    out["_origin_renewal"] = origin  # type: ignore[assignment]
    return out, {k: v.name for k, v in files.items()}


def load_report_workbook(path: Path) -> dict[str, list[list[Any]]]:
    """从已生成的日报 xlsx 取出 6 个源 sheet（已是归一化+对齐后的行），用于历史回灌。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=False)
    out: dict[str, list[list[Any]]] = {}
    try:
        for ds in DATASETS:
            if ds.template_sheet not in wb.sheetnames:
                raise ValueError(f"{path.name} 缺少工作表：{ds.template_sheet}")
            ws = wb[ds.template_sheet]
            try:
                ws.reset_dimensions()
            except Exception:
                pass
            rows = [list(r) for r in ws.iter_rows(values_only=True)][1:]
            # 去掉模板残留的全空行
            out[ds.key] = [r for r in rows if any(v is not None and v != "" for v in r)]
    finally:
        wb.close()
    return out


def to_db_rows(ds: Dataset, aligned: list[list[Any]], *, region_lookup=None, origins: list[str] | None = None):
    """aligned 行 → (列名列表, 元组列表)。附加 region / part_key / origin 列。"""
    ncols = len(ds.columns)
    names = [c for c, _ in ds.columns]
    extra: list[str] = []
    if ds.key == "stock":
        extra.append("part_key")
    if ds.has_region:
        extra.extend(["region", "in_scope"])
    if ds.key == "renewal":
        extra.append("origin")
    code_i = names.index(ds.store_code_col) if ds.store_code_col else None
    name_i = names.index(ds.store_name_col) if ds.store_name_col else None
    part_i = names.index("part_no") if ds.key == "stock" else None
    out = []
    for row_no, raw in enumerate(aligned, 1):
        r = [clean(v) for v in _pad(raw, ncols)]
        if ds.key == "stock":
            r[names.index("available_qty")] = parse_numeric_text(r[names.index("available_qty")])
            r = [v if not isinstance(v, (int, float)) or isinstance(v, bool) else v for v in r]
        tail: list[Any] = []
        if ds.key == "stock":
            tail.append(part_key(r[part_i]))
        if ds.has_region:
            reg, scope = region_lookup(r[code_i], r[name_i], r[names.index("src_region")]) if region_lookup else (None, 0)
            tail.extend([reg, scope])
        if ds.key == "renewal":
            tail.append(origins[row_no - 1] if origins else None)
        # 非 TEXT 的统一转成字符串，避免 TEXT 亲和性把 int 变成 '1' 之外的差异（SQLite 会自动转，这里显式一致）
        vals = []
        for n, v in zip(names, r):
            if ds.key == "stock" and n == "available_qty":
                vals.append(v)
            else:
                vals.append(None if v is None else (v if isinstance(v, str) else str(v)))
        out.append((row_no, *vals, *tail))
    return ["row_no", *names, *extra], out
