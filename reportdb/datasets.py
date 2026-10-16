"""7 份源报表 → 6 张明细表（续保 + 去年同期交付未新保车辆合并为 renewal，origin 区分）。

列顺序 = processor.py 归一化之后写入模板对应 sheet 的列顺序（A,B,C...），因此
下标与模板公式里的列字母一一对应（B=大区, D=门店名称, G/H=是否...）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Dataset:
    key: str  # URL / API 标识
    table: str  # SQLite 表名
    title: str  # 中文名
    source_prefixes: tuple[str, ...]  # processor.SOURCE_SPECS 的 key（renewal 含两个）
    template_sheet: str
    columns: tuple[tuple[str, str], ...]  # (db列名, 中文列名)，与模板列 A.. 对齐
    store_code_col: str | None = None
    store_name_col: str | None = None
    search_cols: tuple[str, ...] = ()
    mask_vin: tuple[str, ...] = ()
    mask_name: tuple[str, ...] = ()
    mask_phone: tuple[str, ...] = ()
    has_region: bool = True
    default_sort: str = "row_no"
    notes: str = ""


DATASETS: tuple[Dataset, ...] = (
    Dataset(
        key="stock",
        table="stock_items",
        title="门店备件库存",
        source_prefixes=("stock",),
        template_sheet="备件库存明细",
        columns=(
            ("seq", "序号"), ("store_name", "门店简称"), ("part_no", "备件号"), ("part_name", "备件名称"),
            ("model", "适用车型"), ("warehouse", "仓库"), ("location", "库位"), ("purchase_price", "正常采购价"),
            ("cash_cost", "现金成本价"), ("rebate_cost", "返利成本价"), ("cost_price", "成本价"),
            ("sale_price", "销售价"), ("available_qty", "实际可用"), ("unit", "计量单位"),
            ("book_qty", "账面库存"), ("theory_qty", "理论可用"), ("defective_qty", "不良品库存"),
            ("category", "备件分类"), ("velocity", "备件流速"), ("stock_amount", "库存金额"),
            ("is_replacement", "是否替换件"),
        ),
        store_name_col="store_name",
        search_cols=("store_name", "part_no", "part_name"),
        has_region=False,
        notes="M 列（实际可用）归一化为数字；非数字保留原文，SUMIFS 忽略。",
    ),
    Dataset(
        key="task",
        table="task_items",
        title="保养提醒任务（招揽）",
        source_prefixes=("task",),
        template_sheet="招揽实施率",
        columns=(
            ("seq", "序号"), ("src_region", "区域"), ("store_code", "门店编码"), ("store_name", "门店名称"),
            ("task_code", "任务编码"), ("task_type", "任务类型"), ("task_status", "任务状态"),
            ("series", "售后车系"), ("vin", "VIN"), ("user_name", "用车人名称"), ("user_phone", "用车人电话"),
            ("owner_name", "车主名称"), ("owner_phone", "车主电话"), ("next_appt_time", "下次预约时间"),
            ("appt_no", "预约单号"), ("created_at", "创建日期"), ("due_date", "到期日期"),
            ("first_visit_date", "首次回访日期"), ("first_visitor", "首次回访人"), ("status", "状态"),
            ("closed_at", "关闭时间"),
        ),
        store_code_col="store_code", store_name_col="store_name",
        search_cols=("store_name", "store_code", "vin", "task_code"),
        mask_vin=("vin",), mask_name=("user_name", "owner_name"), mask_phone=("user_phone", "owner_phone"),
    ),
    Dataset(
        key="first-service",
        table="first_service",
        title="首保实施率明细",
        source_prefixes=("first_service",),
        template_sheet="首保",
        columns=(
            ("vin", "VIN"), ("src_region", "大区"), ("store_code", "交付门店编码"), ("store_name", "交付门店名称"),
            ("deliver_date", "交付日期"), ("due_date", "首保到期日期"), ("assess_month", "归属考核年月"),
            ("done", "是否首保"), ("actual_date", "实际首保日期"), ("actual_store_code", "实际首保门店编码"),
            ("actual_store_name", "实际首保门店名称"), ("work_order_no", "首保工单号"),
        ),
        store_code_col="store_code", store_name_col="store_name",
        search_cols=("store_name", "store_code", "vin"), mask_vin=("vin",),
        notes="「是否首保」按 processor.py 规则重写：实际首保日期有值=是，否则=否。",
    ),
    Dataset(
        key="second-service",
        table="second_service",
        title="二保实施率明细",
        source_prefixes=("second_service",),
        template_sheet="二保",
        columns=(
            ("vin", "VIN"), ("src_region", "大区"), ("store_code", "首保门店编码"), ("store_name", "首保门店名称"),
            ("first_date", "首保日期"), ("due_date", "二保到期日期"), ("assess_month", "归属考核年月"),
            ("done", "是否二保"), ("actual_date", "实际二保日期"), ("actual_store_code", "实际二保门店编码"),
            ("actual_store_name", "实际二保门店名称"), ("work_order_no", "二保工单号"),
        ),
        store_code_col="store_code", store_name_col="store_name",
        search_cols=("store_name", "store_code", "vin"), mask_vin=("vin",),
    ),
    Dataset(
        key="new-policy",
        table="new_policy",
        title="新保投保率明细",
        source_prefixes=("new_policy",),
        template_sheet="新保",
        columns=(
            ("vin", "VIN"), ("src_region", "交付门店大区"), ("store_code", "交付门店编码"),
            ("store_name", "交付门店名称"), ("deliver_date", "交付日期"), ("assess_month", "归属考核年月"),
            ("done", "是否新保"), ("remark", "投保备注"), ("effective_time", "生效时间"),
            ("insure_region", "投保门店大区"), ("insure_store_code", "新报投保门店编码"),
            ("insure_store_name", "新报投保门店名称"), ("insurance_attr", "保险属性"),
        ),
        store_code_col="store_code", store_name_col="store_name",
        search_cols=("store_name", "store_code", "vin", "insure_store_name"), mask_vin=("vin",),
    ),
    Dataset(
        key="renewal",
        table="renewal",
        title="续保投保率明细（含去年同期交付未新保）",
        source_prefixes=("renewal", "renewal_backlog"),
        template_sheet="续保",
        columns=(
            ("vin", "VIN"), ("src_region", "投保门店大区"), ("store_code", "投保门店编码"),
            ("store_name", "投保门店名称"), ("insurance_attr", "保险属性"), ("expire_time", "保险到期时间"),
            ("assess_month", "归属考核年月"), ("done", "是否续保"), ("remark", "投保备注"),
            ("effective_time", "续保生效时间"), ("renew_region", "续保门店大区"),
            ("renew_store_code", "续保门店编码"), ("renew_store_name", "续保门店名称"),
        ),
        store_code_col="store_code", store_name_col="store_name",
        search_cols=("store_name", "store_code", "vin", "renew_store_name"), mask_vin=("vin",),
        notes="去年同期交付未新保车辆在 E/F 之间补空列后并入（origin=backlog），其门店列为交付门店。",
    ),
)

BY_KEY = {d.key: d for d in DATASETS}
BY_TABLE = {d.table: d for d in DATASETS}
# processor.SOURCE_SPECS 的 key → Dataset
SOURCE_ROLE_TO_DATASET = {role: d for d in DATASETS for role in d.source_prefixes}


def column_names(ds: Dataset) -> list[str]:
    return [c for c, _ in ds.columns]


def ddl(ds: Dataset) -> str:
    """生成明细表 DDL。除 stock.available_qty 外全部 TEXT；available_qty 不指定亲和性以保留数字/文本区别。"""
    cols = []
    for name, _ in ds.columns:
        if ds.key == "stock" and name == "available_qty":
            cols.append(f"{name}")  # 无类型 => BLOB 亲和性，保留 int/real/text
        else:
            cols.append(f"{name} TEXT")
    extra = []
    if ds.key == "stock":
        extra.append("part_key TEXT")
    if ds.has_region:
        extra.append("region TEXT")
        extra.append("in_scope INTEGER NOT NULL DEFAULT 0")  # 1=门店在门店表（多维表格）内，计入指标
    if ds.key == "renewal":
        extra.append("origin TEXT")
    body = ",\n    ".join(["snapshot_date TEXT NOT NULL", "row_no INTEGER NOT NULL", *cols, *extra,
                           "PRIMARY KEY (snapshot_date, row_no)"])
    idx = [f"CREATE INDEX IF NOT EXISTS idx_{ds.table}_store ON {ds.table}(snapshot_date, {ds.store_name_col or 'row_no'});"]
    if ds.has_region:
        idx.append(f"CREATE INDEX IF NOT EXISTS idx_{ds.table}_region ON {ds.table}(snapshot_date, region);")
    if ds.key == "stock":
        idx.append("CREATE INDEX IF NOT EXISTS idx_stock_items_part ON stock_items(snapshot_date, part_key);")
    return f"CREATE TABLE IF NOT EXISTS {ds.table} (\n    {body}\n) WITHOUT ROWID;\n" + "\n".join(idx)
