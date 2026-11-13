"""门店备件库存：按表头解析 21/22 列导出，并优先用门店编码匹配备库率。"""
import io
import re
from pathlib import Path

from openpyxl import Workbook, load_workbook

from app.excel_values import WorkbookCalculator
from app.processor import build_report, load_rows_from_workbook, normalize_stock_rows
from reportdb import calc, ingest
from reportdb.datasets import BY_KEY
from reportdb.db import connect, migrate, schema_version
from reportdb.loaders import load_aligned, to_db_rows

NANNING = "猛士汽车南宁江南用户中心"
NANNING_CODE = "MSSGXZ004"
RENAMED = "零售中心"
PART = "22806000"

CANON = [
    "序号", "门店简称", "备件号", "备件名称", "适用车型", "仓库", "库位",
    "正常采购价", "现金成本价", "返利成本价", "成本价", "销售价", "实际可用",
    "计量单位", "账面库存", "理论可用", "不良品库存", "备件分类", "备件流速",
    "库存金额", "是否替换件",
]
HEADERS_22 = CANON[:1] + ["门店编码"] + CANON[1:]


def _row22(*, code=NANNING_CODE, name=RENAMED, part=PART, sale=999, available="7"):
    row = [None] * 22
    row[0] = 1
    row[1] = code
    row[2] = name
    row[3] = part
    row[4] = "机油滤清器"
    row[12] = sale
    row[13] = available
    row[21] = "否"
    return row


def _xlsx(sheets: list[tuple[str, list]]) -> bytes:
    wb = Workbook()
    first = True
    for title, rows in sheets:
        ws = wb.active if first else wb.create_sheet(title)
        if first:
            ws.title = title
            first = False
        for row in rows:
            ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _empty_sources(stock_bytes: bytes) -> list[tuple[str, bytes]]:
    dummy = _xlsx([("Sheet1", [["h"], ["x"]])])
    return [
        ("门店备件库存导出20261113_1.xlsx", stock_bytes),
        ("保养提醒任务列表.xlsx", dummy),
        ("首保实施率.xlsx", dummy),
        ("二保实施率.xlsx", dummy),
        ("新保投保率.xlsx", dummy),
        ("续保投保率.xlsx", dummy),
        ("去年同期交付未新保车辆.xlsx", dummy),
    ]


def test_header_parses_22_col_export_and_legacy_21_col():
    raw = _row22()
    aligned = normalize_stock_rows([raw], header=HEADERS_22)
    assert len(aligned) == 1
    row = aligned[0]
    assert row[1] == RENAMED
    assert row[2] == PART
    assert row[11] == 999  # 销售价留在 L 列，不能被当成实际可用
    assert row[12] == 7
    assert row[21] == NANNING_CODE

    # 表头在首行时同样能识别，不把表头当数据
    with_header = normalize_stock_rows([HEADERS_22, raw])
    assert len(with_header) == 1 and with_header[0][12] == 7 and with_header[0][21] == NANNING_CODE

    legacy = [None] * 21
    legacy[0] = 1
    legacy[1] = NANNING
    legacy[2] = PART
    legacy[11] = 12
    legacy[12] = "4"
    out = normalize_stock_rows([legacy], header=CANON)
    assert out[0][1] == NANNING and out[0][2] == PART and out[0][12] == 4 and out[0][21] is None
    # 无表头的旧 21 列行（测试夹具 / 已对齐数据）仍按位置读 M 列
    positional = normalize_stock_rows([legacy])
    assert positional[0][12] == 4 and positional[0][1] == NANNING and positional[0][21] is None

    spaced = _row22(code="  mscsds022  ")
    assert normalize_stock_rows([spaced], header=HEADERS_22)[0][21] == "mscsds022"


def test_stock_export_reads_named_sheet_not_active_decoy():
    decoy = [CANON, [1, "错表", "000", None, None, None, None, None, None, None, None, 1, "9"]]
    real = [HEADERS_22, _row22()]
    content = _xlsx([
        ("封面", decoy),
        ("网点备件库存查询20261113", real),
    ])
    rows = load_rows_from_workbook(content, "门店备件库存导出20261113_0800.xlsx")
    assert rows[0][1] == "门店编码"
    assert rows[1][2] == RENAMED and rows[1][13] == "7"


def test_excel_report_keeps_formula_columns_and_matches_by_store_code(tmp_path):
    content = _xlsx([
        ("封面", [CANON, [1, "错表", "000"]]),
        ("网点备件库存查询20261113", [HEADERS_22, _row22()]),
    ])
    template = Path("app/assets/report_template.xlsx").read_bytes()
    report, _name = build_report(_empty_sources(content), template)
    wb = load_workbook(io.BytesIO(report), data_only=False)
    ws = wb["备件库存明细"]
    assert [ws.cell(1, c).value for c in range(1, 22)] == CANON
    # B=模板里的经销商名称（编码对上后改写），C=备件号，M=实际可用；销售价不能跑到 M
    assert ws.cell(2, 2).value == NANNING
    assert ws.cell(2, 3).value == PART
    assert ws.cell(2, 12).value == 999
    assert ws.cell(2, 13).value == 7
    formula = wb["常备件备库率"].cell(19, 6).value
    assert "备件库存明细!$M:$M" in formula and "备件库存明细!$B:$B" in formula
    path = tmp_path / "report.xlsx"
    path.write_bytes(report)
    engine = WorkbookCalculator(path)
    engine._precompute_stock_sumifs()
    assert engine.cache[("常备件备库率", 19, 6)] == 7
    wb.close()


def test_spare_matrix_matches_renamed_store_by_code_only(conn):
    renamed = normalize_stock_rows([
        _row22(code="mssgxz004", available="7"),
        _row22(available="1", part="22802001"),
        _row22(code="NO-SUCH", name=NANNING, available="100"),
    ], header=HEADERS_22)
    legacy = normalize_stock_rows([[1, NANNING, PART] + [None] * 9 + ["3"]])
    data = {"stock": renamed + legacy, "task": [], "first-service": [], "second-service": [],
            "new-policy": [], "renewal": []}
    ingest.ingest_datasets(conn, "2026-11-13", data, source="t", recompute=False)
    saved = conn.execute(
        "SELECT store_name, store_code, part_no, available_qty FROM stock_items WHERE snapshot_date='2026-11-13' ORDER BY row_no"
    ).fetchall()
    assert saved[0]["store_name"] == RENAMED and saved[0]["store_code"] == "mssgxz004"
    assert saved[0]["part_no"] == PART and saved[0]["available_qty"] == 7
    assert saved[2]["store_code"] == "NO-SUCH" and saved[2]["available_qty"] == 100
    assert saved[3]["store_code"] is None and saved[3]["store_name"] == NANNING

    sp = calc.spare_matrix(conn, "2026-11-13")
    by_code = {r["code"]: r for r in sp["rows"]}
    codes = [p["code"] for p in sp["parts"]]
    nanning = by_code[NANNING_CODE]
    assert nanning["name"] == NANNING
    assert nanning["vals"][codes.index(PART)] == 10  # 编码命中的 7 + 无编码简称命中的 3；NO-SUCH 的 100 不计
    assert nanning["vals"][codes.index("22802001")] == 1
    assert all(v == 0 for v in by_code["MSCSDS001"]["vals"])

    sheets = calc.compute(conn, "2026-11-13")
    spare = next(b for b in sheets["direct-stores"].blocks if b.key == "spare")
    row = next(r for r in spare.rows if r[0] == NANNING)
    assert row[1] == 2 / 32  # 两件计入分母的备件有货


def test_ingest_export_file_and_historical_sheet(conn, tmp_path):
    stock_path = tmp_path / "门店备件库存导出20261113_1.xlsx"
    stock_path.write_bytes(_xlsx([
        ("封面", [["不是库存"]]),
        ("网点备件库存查询20261113", [HEADERS_22, _row22(available="8")]),
    ]))
    aligned = load_aligned("stock", stock_path)
    cols, rows = to_db_rows(BY_KEY["stock"], aligned)
    assert cols[-2:] == ["part_key", "store_code"]
    assert rows[0][cols.index("store_code")] == NANNING_CODE
    assert rows[0][cols.index("available_qty")] == 8
    assert rows[0][cols.index("part_no")] == PART

    report = tmp_path / "区域各指标情况一览1112.xlsx"
    wb = Workbook()
    headers = {
        "备件库存明细": CANON,
        "招揽实施率": ["序号"],
        "首保": ["VIN"],
        "二保": ["VIN"],
        "新保": ["VIN"],
        "续保": ["VIN"],
    }
    first = True
    for name, header in headers.items():
        ws = wb.active if first else wb.create_sheet(name)
        if first:
            ws.title = name
            first = False
        ws.append(header)
        if name == "备件库存明细":
            ws.append([1, NANNING, PART, "滤芯", None, None, None, None, None, None, None, 10, 6])
    wb.save(report)
    ingest.ingest_report(conn, report, "2026-11-12", recompute=False)
    row = conn.execute(
        "SELECT store_name, store_code, part_no, available_qty FROM stock_items WHERE snapshot_date='2026-11-12'"
    ).fetchone()
    assert row["store_name"] == NANNING and row["store_code"] is None
    assert row["part_no"] == PART and row["available_qty"] == 6
    sp = calc.spare_matrix(conn, "2026-11-12")
    hit = next(r for r in sp["rows"] if r["code"] == NANNING_CODE)
    assert hit["vals"][[p["code"] for p in sp["parts"]].index(PART)] == 6


def test_migrate_v7_database_adds_store_code(tmp_path):
    path = tmp_path / "v7.sqlite3"
    conn = connect(path)
    assert migrate(conn) == schema_version(conn) == 8
    conn.execute("DROP INDEX IF EXISTS idx_stock_items_code")
    conn.execute("ALTER TABLE stock_items DROP COLUMN store_code")
    conn.execute(
        "INSERT INTO stock_items(snapshot_date,row_no,seq,store_name,part_no,available_qty,part_key) "
        "VALUES ('2026-11-12',1,'1','交付服务中心','22806000',4,'22806000')"
    )
    conn.execute("PRAGMA user_version=7")
    conn.commit()
    assert "store_code" not in {r[1] for r in conn.execute("PRAGMA table_info(stock_items)")}
    assert schema_version(conn) == 7
    assert migrate(conn) == 8
    cols = [r[1] for r in conn.execute("PRAGMA table_info(stock_items)")]
    assert "store_code" in cols
    row = conn.execute("SELECT store_name, part_no, available_qty, store_code, part_key FROM stock_items").fetchone()
    assert (row["store_name"], row["part_no"], row["available_qty"], row["store_code"], row["part_key"]) == (
        "交付服务中心", "22806000", 4, None, "22806000")
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_stock_items_code'"
    ).fetchone()[0] == 1
    assert migrate(conn) == 8  # 幂等


def test_stock_detail_search_region_and_quality(conn, tmp_path):
    from fastapi.testclient import TestClient

    from reportdb.auth import service
    from reportdb.webapp import create_app

    renamed = normalize_stock_rows([_row22(available="7")], header=HEADERS_22)
    ingest.ingest_datasets(conn, "2026-11-13", {
        "stock": renamed, "task": [], "first-service": [], "second-service": [], "new-policy": [], "renewal": [],
    }, source="t", recompute=True)
    # 这份快照没有带大区的明细，入库会把门店区域记成「无区域」。区域筛选测的是库存按编码归到门店表，把区域设回种子值。
    conn.execute("UPDATE stores SET region='华南' WHERE code=?", (NANNING_CODE,))
    service.create_user(conn, "boss", "boss-password-1", role="admin")
    conn.commit()
    client = TestClient(create_app(Path(conn.execute("PRAGMA database_list").fetchone()[2])))
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert client.post("/login", data={"username": "boss", "password": "boss-password-1", "csrf": csrf, "next": "/"},
                       follow_redirects=False).status_code == 303

    by_name = client.get("/api/data/stock", params={"store": NANNING}).json()
    assert by_name["total"] == 1
    assert by_name["rows"][0]["store_name"] == RENAMED
    assert by_name["rows"][0]["store_code"] == NANNING_CODE
    assert "store_code" in [c["key"] for c in by_name["columns"]]

    by_code = client.get("/api/data/stock", params={"q": "mssgxz004"}).json()
    assert by_code["total"] == 1
    south = client.get("/api/data/stock", params={"region": "华南"}).json()
    assert south["total"] == 1
    north = client.get("/api/data/stock", params={"region": "华北"}).json()
    assert north["total"] == 0

    html = client.get("/data/stock", params={"region": "华南"}).text
    assert NANNING in html and RENAMED in html

    quality = client.get("/api/quality").json()["stock"]
    assert quality["stores_matched_to_store_table"] == 1
    active = conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0]
    assert quality["active_stores_without_stock"] == active - 1

    book = load_workbook(io.BytesIO(client.get("/export/data/stock.xlsx").content))
    header = [c.value for c in next(book.active.iter_rows(max_row=1))]
    assert "门店编码" in header and "实际可用" in header
    body = list(book.active.iter_rows(min_row=2, values_only=True))
    assert body and NANNING_CODE in body[0] and RENAMED in body[0]
