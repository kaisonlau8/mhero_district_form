"""报表结构：Block（一个 5 列/4 列指标块）与 Sheet（与模板单元格坐标一一对应的网格）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

PCT0 = "0%"
PCT1 = "0.0%"


def col_letter(idx: int) -> str:
    s = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def fmt_value(value: Any, fmt: str | None) -> str:
    """按 Excel 数字格式显示：0% / 0.0%（四舍五入同 Excel：半数向远离 0 取整）。"""
    if value is None:
        return "-"
    if fmt in (PCT0, PCT1) and isinstance(value, (int, float)) and not isinstance(value, bool):
        places = 0 if fmt == PCT0 else 1
        q = Decimal(repr(float(value))) * 100
        q = q.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        return f"{q:.{places}f}%"
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else f"{value:.4g}"
    return str(value)


@dataclass
class Block:
    """一个指标块：标题行 + 表头行 + 数据行 + 可选合计行，起始于 (row, col)（1 基，同模板坐标）。"""

    title: str
    row: int
    col: int
    headers: list[str]
    rows: list[list[Any]]
    total: list[Any] | None = None
    fmts: list[str | None] = field(default_factory=list)
    rank_col: int | None = None  # 排名列在块内的下标（SORT 用）
    key: str = ""

    @property
    def width(self) -> int:
        return len(self.headers)

    def sorted_copy(self) -> "Block":
        """Excel: SORT(range, 5, 1) —— 按块内第 5 列升序，稳定排序，空值（错误值）排最后。"""
        ri = self.rank_col if self.rank_col is not None else 4
        rows = sorted(self.rows, key=lambda r: (r[ri] is None, r[ri] if r[ri] is not None else 0))
        return Block(self.title, self.row, self.col, list(self.headers), [list(r) for r in rows],
                     list(self.total) if self.total else None, list(self.fmts), self.rank_col, self.key)


@dataclass
class Sheet:
    slug: str
    name: str  # 模板 sheet 名
    blocks: list[Block] = field(default_factory=list)
    extra_cells: dict[tuple[int, int], Any] = field(default_factory=dict)
    extra_fmts: dict[tuple[int, int], str] = field(default_factory=dict)
    matrix: dict[str, Any] | None = None  # 常备件备库率：宽表（多行表头 + 门店×备件矩阵）

    def grid(self) -> dict[tuple[int, int], Any]:
        g: dict[tuple[int, int], Any] = {}
        for b in self.blocks:
            g[(b.row, b.col)] = b.title
            for j, h in enumerate(b.headers):
                g[(b.row + 1, b.col + j)] = h
            r = b.row + 2
            for row in b.rows:
                for j, v in enumerate(row):
                    if v is not None:
                        g[(r, b.col + j)] = v
                r += 1
            if b.total is not None:
                for j, v in enumerate(b.total):
                    if v is not None:
                        g[(r, b.col + j)] = v
        g.update({k: v for k, v in self.extra_cells.items() if v is not None})
        return g

    def formats(self) -> dict[tuple[int, int], str]:
        f: dict[tuple[int, int], str] = {}
        for b in self.blocks:
            n = len(b.rows) + (1 if b.total is not None else 0)
            for i in range(n):
                for j, fm in enumerate(b.fmts):
                    if fm:
                        f[(b.row + 2 + i, b.col + j)] = fm
        f.update(self.extra_fmts)
        return f

    def cells(self) -> list[list[Any]]:
        """[[row, col, value, fmt], ...]，用于落库与导出。"""
        fm = self.formats()
        return [[r, c, v, fm.get((r, c))] for (r, c), v in sorted(self.grid().items())]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Sheet":
        sh = cls(d["slug"], d["name"])
        sh.blocks = [Block(b["title"], b["row"], b["col"], b["headers"], b["rows"], b["total"], b["fmts"],
                           b["rank_col"], b["key"]) for b in d["blocks"]]
        sh.matrix = d.get("matrix")
        if sh.matrix is not None:  # 宽表：从 cells 还原
            for r, c, v, fmt in d["cells"]:
                sh.extra_cells[(r, c)] = v
                if fmt:
                    sh.extra_fmts[(r, c)] = fmt
        return sh

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug, "name": self.name, "matrix": self.matrix, "cells": self.cells(),
            "blocks": [
                {"title": b.title, "key": b.key, "headers": b.headers, "rows": b.rows, "total": b.total,
                 "fmts": b.fmts, "rank_col": b.rank_col, "row": b.row, "col": b.col}
                for b in self.blocks
            ],
        }


def block_cells(block: Block) -> list[tuple[int, int, Any, str | None]]:
    out = []
    for i, row in enumerate(block.rows + ([block.total] if block.total is not None else [])):
        for j, v in enumerate(row):
            out.append((block.row + 2 + i, block.col + j, v, block.fmts[j] if j < len(block.fmts) else None))
    return out
