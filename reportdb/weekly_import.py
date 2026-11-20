"""手动导入猛士周报 Excel。不登录 DMS，不改每日调度。

    python -m reportdb.weekly_import operation --file 经营.xlsx --week 2026-11-16 \\
        --span quarter --period-start 2026-10-01 --period-end 2026-11-20 \\
        --exported-at "2026-11-20 14:25:35"
指标不从这里导入。周报读取该周已有的 reportdb 总表。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import connect, migrate
from .weekly_parse import WeeklyParseError
from .weekly_report import SnapshotConflict, WeeklyImportError, import_operation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入猛士周报 Excel（不访问 DMS）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    operation = sub.add_parser("operation", help="服务经营报表：收款 + 日报")
    _common(operation)
    operation.add_argument("--span", required=True, choices=["quarter", "quarter_base", "year", "year_base"])
    operation.add_argument("--period-start", required=True)
    operation.add_argument("--period-end", required=True)
    operation.add_argument("--as-of", default="", help="对比期必填：本次更新的累计截止日，须落在 --week 那一周")
    operation.add_argument("--stat-type", default="收款")
    operation.add_argument("--report-type", default="日报")
    operation.add_argument("--scope", default="")

    args = parser.parse_args(argv)
    blob = Path(args.file).read_bytes()
    conn = connect()
    try:
        migrate(conn)
        result = import_operation(
            conn, blob, week_start=args.week, period_start=args.period_start, period_end=args.period_end,
            exported_at=args.exported_at, timezone=args.timezone, stat_type=args.stat_type,
            report_type=args.report_type, scope=args.scope or None, filename=args.file,
            actor=args.actor, allow_revision=args.revision, span=args.span, as_of=args.as_of or None,
        )
    except SnapshotConflict as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (WeeklyParseError, WeeklyImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", required=True)
    parser.add_argument("--week", required=True, help="周一，YYYY-MM-DD")
    parser.add_argument("--exported-at", required=True, help="实际导出时间，Excel 里没有这一列")
    parser.add_argument("--timezone", default="+08:00")
    parser.add_argument("--actor", default="cli")
    parser.add_argument("--revision", action="store_true", help="保留旧快照并写入新修订；默认拒绝覆盖")


if __name__ == "__main__":
    raise SystemExit(main())
