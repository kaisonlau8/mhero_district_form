#!/usr/bin/env python3
"""CLI wrapper around app.excel_values."""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from app.excel_values import flatten_formulas_to_values  # noqa: E402

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Flatten Excel formulas to values")
    parser.add_argument("source")
    parser.add_argument("-o", "--output", default="")
    args = parser.parse_args()
    out = flatten_formulas_to_values(
        Path(args.source),
        Path(args.output) if args.output else None,
    )
    print(out)
