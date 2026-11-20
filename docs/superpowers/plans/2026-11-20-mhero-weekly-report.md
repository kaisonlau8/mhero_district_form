# 猛士周报 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在现有 reportdb 导航中交付可切换周次的猛士周报：经营同环比、季度累计 KPI、不可静默覆盖的快照和空的督导原因。

**Architecture:** `weekly_parse.py` 只解析 xlsx 并做纯计算。`weekly_report.py` 负责校验元数据、写 SQLite 修订、组装页面模型。`weekly_import.py` 是 CLI。`webapp.py` 增加只读页、管理员导入和督导评语。不接入 `scripts/run_pipeline.py`。

**Tech Stack:** Python 3、FastAPI、Jinja2、openpyxl、SQLite 迁移 v9、pytest。

用户已要求同一会话按本计划直接实施，不另行选择子代理或再确认版式。

## Global Constraints

- 统计类型只接受「收款」，报表类型只接受「日报」；指标名用「收款工单数」。
- 车辆数只用汇总行；全国文件不拆区域；区域与全国对不平保留差额，不摊分。
- 金额与含税源表未注明，页面不写元、万元、含税。
- 缺快照显示「未导入」；分母 0 显示「不可比」，不计算涨幅，不把缺数写成 0。
- 环比是季度环比，同比是年度同比。未结束期间默认同进度（实现默认）。不再减 7 天或 364 天。
- KPI 达成率是分子/分母加权，禁止平均百分比。DMS 区域空为「无区域」，不查门店表。
- 门店+周期重复则拒绝加权。不把 774 写死为门店数。
- 相同文件幂等；不同文件默认拒绝覆盖。
- 督导原因初始为空，不生成文字。
- 不改 `config/crawl_settings.json` 的 `schedule`（保持 `08:30`），不让 `collect_weekly_sources` 去拉 DMS。
- 真实业务 xlsx 不进入 git。样本 2026-11-01 至 2026-11-19 不能绑为某一周。
- schema 只追加迁移，现有 v7→最新迁移测试改为期望 `SCHEMA_VERSION`，仍须断言 `store_code` 被补上。

---

### Task 1: 解析与同环比纯函数

**Files:**

- Create: `reportdb/weekly_parse.py`
- Test: `tests/test_reportdb_weekly.py`

**Interfaces:**

- Produces: `parse_operation_xlsx(blob) -> dict`，含 `scope`、`summary`（`orders`/`vehicles`/`output`/`labor`/`parts`/`other` 为 `Decimal`）、`detail_sums`、`business_types`、`quality`
- Produces: `parse_kpi_xlsx(blob) -> dict`，含 `row_count`、`unique_stores`、`duplicate_groups`、`blank_region_rows`、`periods`、`rollup_ok`、`national`、`regions`
- Produces: `change(current, base) -> {status, amount, rate}`，`status` 为 `missing` / `zero_base` / `ok`
- Produces: `quarter_compare(as_of)` 与 `year_compare(as_of)`，各含 `current` / `base` 起止日

- [x] **Step 1: Write the failing test**

覆盖：汇总行而不是车辆数相加、单区归一、混区拒绝、KPI 加权不等于百分比平均、续保分母含未新保、空区域不改名、重复门店拒绝汇总、分母 0、涨幅缺期与 0 分母、364 天窗口。

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reportdb_weekly.py -q --tb=no`

Expected: FAIL，`weekly_parse` 不存在。

- [x] **Step 3: Write minimal implementation**

按 spec 的表头与加权式实现。数量空单元格导致 `rollup_ok=False`。百分数列不参与加权。

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reportdb_weekly.py -q --tb=line`

Expected: 解析与窗口用例 PASS。

### Task 2: 快照、对账、页面模型

**Files:**

- Modify: `reportdb/db.py`（`_migrate_v9`，`MIGRATIONS` 只追加）
- Modify: `tests/test_stock_store_code.py`（版本号改为 `SCHEMA_VERSION`）
- Create: `reportdb/weekly_report.py`
- Test: `tests/test_reportdb_weekly.py`

**Interfaces:**

- Produces: `import_operation(conn, blob, *, week_start, period_start, period_end, exported_at, timezone="+08:00", stat_type="收款", report_type="日报", scope=None, filename="", actor="", allow_revision=False) -> dict`
- Produces: `import_kpi(conn, blob, *, week_start, as_of, exported_at, timezone="+08:00", year=None, quarter=None, filename="", actor="", allow_revision=False) -> dict`
- Produces: `build_weekly_page(conn, week=None, *, today=None) -> dict`
- Produces: `save_commentary(conn, week_start, region, body, actor) -> None`
- Produces: `collect_weekly_sources(**kwargs)` 抛出 `NotImplementedError`，文案含 `SHARED_DMS_BROWSER`
- 异常：`WeeklyImportError`、`SnapshotConflict`

- [x] **Step 1: Write the failing test**

覆盖：跨周区间拒绝、幂等、冲突、显式修订保留旧行、区域差额不摊分、无导入的当前周没有样本数字、三个期间的具体日期、0 分母涨幅、空区域不改挂到门店表区域、评语默认为空且可保存、采集函数拒绝。

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reportdb_weekly.py -q --tb=line`

Expected: FAIL，导入或建表函数缺失。

- [x] **Step 3: Write minimal implementation**

v9 表 `weekly_imports`、`weekly_commentary`。活跃部分唯一索引 `(kind, week_start, scope) WHERE status='active'`。

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reportdb_weekly.py tests/test_stock_store_code.py::test_migrate_v7_database_adds_store_code -q --tb=line`

Expected: PASS。

### Task 3: 导航、导入页、CLI、文档

**Files:**

- Modify: `reportdb/templates/base.html`、`reportdb/templates/index.html`、`reportdb/webapp.py`、`tests/test_reportdb_web.py`
- Create: `reportdb/templates/weekly.html`、`reportdb/templates/weekly_import.html`、`reportdb/weekly_import.py`、`docs/WEEKLY.md`
- Modify: `docs/REPORTDB.md`（短指针）

**Interfaces:**

- `GET /reports/weekly` 登录可见，查看者可见
- `GET/POST /reports/weekly/import` 仅管理员
- `POST /reports/weekly/commentary` 督导或管理员
- `GET /reports/weekly/imports/{id}/source.xlsx` 仅管理员下载原件
- CLI：`python -m reportdb.weekly_import operation|kpi`

- [x] **Step 1: Write the failing test**

页面含「猛士周报」「收款工单数」「未导入」「不是严格来厂台次」，不含「万元」「含税」。查看者 200、导入 403。管理员可导入。`PAGES` 增加 `/reports/weekly`。

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reportdb_weekly.py tests/test_reportdb_web.py::test_all_pages_and_api -q --tb=line`

Expected: FAIL，路由 404。

- [x] **Step 3: Write minimal implementation**

导航链到 `/reports/weekly`，不把周报绑进日报 `date`。导入说明写在页面和 `docs/WEEKLY.md`。

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reportdb_weekly.py tests/test_reportdb_web.py tests/test_stock_store_code.py::test_migrate_v7_database_adds_store_code -q --tb=line`

Expected: PASS。

### Task 4: 样本锁与回归

**Files:**

- Test: `tests/test_reportdb_weekly.py`（样本不在则 skip）

- [x] **Step 1: 样本断言**

若本机有附件：全国 6246 / 3135 / 6402712.72，华东 728 / 370 / 631854.40，KPI 774 行、774 个门店、0 组重复、596 行无区域、周期 `2026年Q4`。用 2026-11-01 至 2026-11-19 绑定 2026-11-16 必须失败，且当前周页面仍无 `6402712.72`。

- [x] **Step 2: 全量相关测试**

Run: `python3 -m pytest tests/test_reportdb_weekly.py tests/test_reportdb_web.py tests/test_reportdb_calc.py tests/test_reportdb_ingest.py tests/test_stock_store_code.py -q --tb=line`

Expected: PASS。`config/crawl_settings.json` 的 `schedule` 仍为 `08:30`。

## Self-review

- 规格里的两块版式、三类期间、汇总行、加权、红标、空原因、幂等、对账差额、样本隔离都有对应任务。
- 没有把「774 家店」写进生产代码。
- 未核验指标没有假映射。
- 计划不要求合并或部署。
