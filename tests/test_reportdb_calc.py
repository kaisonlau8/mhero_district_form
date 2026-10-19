"""小数据集上手算校验：口径、排名、排序、归一化规则（不依赖真实文件）。"""
import pytest

from reportdb import calc
from reportdb.ingest import ingest_datasets
from reportdb.loaders import part_key
from reportdb.db import migrate

D1, D2, D3, D4 = ("猛士汽车南宁江南用户中心", "猛士汽车天津北辰用户中心", "猛士汽车郑州经开用户中心", "猛士汽车001武汉旗舰店")


def row(n, kw):
    r = [None] * n
    for k, v in kw.items():
        r[k] = v
    return r


def make_data(conn, extra_stock=()):
    stores = {r["name"]: r for r in conn.execute("SELECT * FROM stores")}
    code = lambda n: stores[n]["code"]
    # 首保（12 列）：B=大区(DMS) C=编码 D=名称 H=是否 I=实际日期（processor 规则：I 有值 => 是）
    first = [
        row(12, {1: "华南区", 2: code(D1), 3: D1, 7: "否", 8: "2026-01-01"}),   # I 有值 → 是
        row(12, {1: "华南区", 2: code(D1), 3: D1, 7: "是", 8: None}),          # I 空 → 否（H 原值被覆盖）
        row(12, {1: "华南区", 2: code(D1), 3: D1, 7: "否", 8: None}),
        row(12, {1: "华北区", 2: code(D2), 3: D2, 7: "是", 8: "2026-01-02"}),
        row(12, {1: "中区", 2: "NOSUCH", 3: "不在门店表的店", 7: "是", 8: "2026-01-02"}),  # 无归属 → 不计区域
    ]
    from app.processor import normalize_first_service_rows

    first = normalize_first_service_rows(first)
    task = [row(21, {1: "x", 2: code(D1), 3: D1, 6: "待处理"}), row(21, {1: "x", 2: code(D1), 3: D1, 6: "已完成"}),
            row(21, {1: "x", 2: code(D2), 3: D2, 6: "已完成"})]
    # 续保 + 去年同期交付未新保（缺「保险到期时间」，processor 在 E/F 间补空列）
    from app.processor import normalize_backlog_rows

    renewal = [row(13, {1: "x", 2: code(D1), 3: D1, 7: "是"}), row(13, {1: "x", 2: code(D1), 3: D1, 7: "否"})]
    backlog = normalize_backlog_rows([["V", "r", code(D1), D1, "保险属性", "2026-10", "是", "备注", "t", "r", "c", "n"]])
    assert len(backlog[0]) == 13 and backlog[0][5] is None and backlog[0][6] == "2026-10" and backlog[0][7] == "是"
    # 备件库存（21 列）：B 门店 C 备件号 M 实际可用
    from app.processor import normalize_stock_rows

    stock = normalize_stock_rows([
        row(21, {1: D1, 2: "22806000", 12: "5"}),        # 文本数字 → 数字
        row(21, {1: D1, 2: 22802001, 12: 0}),            # 0 不算已备
        row(21, {1: D1, 2: "55003001wc", 12: "2"}),      # 不分大小写匹配 55003001WC
        row(21, {1: D1, 2: "91401000", 12: "abc"}),      # 非数字忽略
        row(21, {1: D2, 2: "22806000", 12: "1"}),
        *extra_stock,
    ])
    return {"stock": stock, "task": task, "first-service": first, "second-service": [],
            "new-policy": [], "renewal": renewal + backlog}, ["renewal"] * 2 + ["backlog"]


def test_part_key():
    assert part_key("22806000") == part_key(22806000) == "22806000"
    assert part_key("55003001wc") == part_key("55003001WC")
    assert part_key("") is None and part_key(None) is None


def test_hand_computed(conn):
    data, origin = make_data(conn)
    ingest_datasets(conn, "2026-01-01", data, source="t", origin_renewal=origin, recompute=False)
    sheets = calc.compute(conn, "2026-01-01", "store")
    ds = sheets["direct-stores"]
    first = ds.blocks[0]
    # 南宁 3 应保 1 实绩；天津 1/1；郑州/武汉 0/0 → IFERROR=0
    assert first.rows[0][:4] == [D1, 3, 1, pytest.approx(1 / 3)]
    assert first.rows[1][:4] == [D2, 1, 1, 1.0]
    assert first.rows[2][1:4] == [0, 0, 0] and first.rows[3][1:4] == [0, 0, 0]
    assert [r[4] for r in first.rows] == [2, 1, 3, 3]  # RANK 降序，并列同名次
    assert first.total[:4] == ["合计", 4, 2, 0.5]
    # 区域层：南宁/天津按 DMS 区域（归一后）归属；"不在门店表的店" 不进任何区域
    summ = sheets["summary"].blocks[0]
    assert summ.total[1] == 4 and summ.total[2] == 2, "全国=各区域之和，不含未归属行"
    # 招揽：待处理 1 / 总 2（南宁）
    task = ds.blocks[5]
    assert task.rows[0][:4] == [D1, 2, 1, 0.5]
    # 续保：南宁 renewal 2 行 + backlog 1 行（均为 D1）：总 3，是 2
    ren = ds.blocks[3]
    assert ren.rows[0][:3] == [D1, 3, 2]


def test_spare_rules(conn):
    data, origin = make_data(conn)
    ingest_datasets(conn, "2026-01-01", data, source="t", origin_renewal=origin, recompute=False)
    sp = calc.spare_matrix(conn, "2026-01-01")
    by = {r["name"]: r for r in sp["rows"]}
    codes = [p["code"] for p in sp["parts"]]
    r1 = by[D1]["vals"]
    assert r1[codes.index("22806000")] == 5
    assert r1[codes.index("22802001")] == 0
    assert r1[codes.index("55003001WC")] == 2       # 大小写不敏感
    assert r1[codes.index("91401000")] == 0          # 文本忽略
    assert by[D1]["rate"] == 2 / 32 and by[D2]["rate"] == 1 / 32  # 分母 = 计入备库率的备件数 32
    assert sp["counted"] == 32 and len(codes) == 33  # AL 列（817 保护套件）计算但不计入


def test_sort_stable_and_none_last():
    from reportdb.tables import Block

    b = Block("t", 1, 1, ["a", "b", "c", "d", "e"], [["x", 0, 0, 0, 2], ["y", 0, 0, 0, 1], ["z", 0, 0, 0, None], ["w", 0, 0, 0, 1]])
    assert [r[0] for r in b.sorted_copy().rows] == ["y", "w", "x", "z"]


def test_region_mode_source_vs_store(conn):
    data, origin = make_data(conn)
    ingest_datasets(conn, "2026-01-01", data, source="t", origin_renewal=origin, recompute=False)
    src = calc.compute(conn, "2026-01-01", "source")["summary"].blocks[0]
    # source 模式沿用 DMS 大区原文：'华南区'/'华北区'/'中区' 都不等于模板区域名 → 全 0 且比率为空（与旧模板在新区域名下的行为一致）
    assert src.total[1] == 0 and all(r[3] is None for r in src.rows)


def test_zero_denominator_region(conn):
    data, origin = make_data(conn)
    ingest_datasets(conn, "2026-01-01", data, source="t", origin_renewal=origin, recompute=False)
    summ = calc.compute(conn, "2026-01-01", "store")["summary"].blocks[1]  # 二保：无数据
    assert all(r[1] == 0 and r[3] is None and r[4] is None for r in summ.rows)
    assert summ.total[3] is None


def test_formats():
    from reportdb.tables import fmt_value

    assert fmt_value(0.125, "0%") == "13%" and fmt_value(0.0625, "0.0%") == "6.3%" and fmt_value(None, "0%") == "-"
