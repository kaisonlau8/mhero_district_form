"""python -m reportdb.migrate  — 等价于 python -m reportdb.db"""
from .db import main

if __name__ == "__main__":
    raise SystemExit(main())
