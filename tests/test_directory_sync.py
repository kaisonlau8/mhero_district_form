"""门店联系人同步：1..4 号售后负责人字段解析（不联网，直接喂假 records）。"""
import json

import pytest

import directory_sync


@pytest.fixture()
def fake_records():
    return [
        {
            "店代码": "D001", "店名称": "一号店", "区域": "华中1区",
            "门店省": "湖北省", "门店市": "武汉市", "门店区": "江汉区",
            "督导": [{"id": "sup1", "name": "督导甲"}],
            "督导手机号": "135-0000-0001",
            "门店售后业务负责人 1": [{"id": "ou1", "name": "张三"}],
            "售后业务负责人手机号 1": "138-0000-0001",
            "门店售后业务负责人2": [{"id": "ou2", "name": "李四"}],
            "售后业务负责人手机号2": "+86 13800000002",
            "门店售后业务负责人 3": [{"id": "ou1", "name": "张三"}],   # 与 1 号同 open_id，去重
            "售后业务负责人手机号 3": "13800000003",
            "门店售后业务负责人 4": [{"name": "王五"}],
            "售后业务负责人手机号 4": "",
        },
        {
            "店代码": "D002", "店名称": "旧字段店",
            "门店售后业务负责人": [{"id": "ou9", "name": "旧负责人"}],   # 无编号旧字段 = 1 号
            "售后业务负责人手机号": "139-0000-0009",
        },
        {
            "店代码": "D003", "店名称": "只有二号店",
            "门店售后业务负责人 1": [],
            "门店售后业务负责人 2": [{"id": "ou3", "name": "赵六"}],
            "售后业务负责人手机号 2": "13700000003",
        },
        {
            "店代码": "D004", "店名称": "没有负责人店",
            "门店售后业务负责人 1": [],
            "售后业务负责人手机号 2": "",
        },
        {
            "店代码": "D005", "店名称": "只有电话店",
            "门店售后业务负责人 4": [],
            "售后业务负责人手机号 4": "136-0000-0005",   # 有电话也算有负责人
        },
    ]


def _sync(tmp_path, monkeypatch, records):
    monkeypatch.setattr(directory_sync, "_fetch_records", lambda: records)
    monkeypatch.setattr(directory_sync, "DIRECTORY_PATH", tmp_path / "store_directory.json")
    monkeypatch.setattr(directory_sync, "SYNC_STATE_PATH", tmp_path / ".runtime" / "state.json")
    return directory_sync.sync_directory()


def test_sync_multi_slot_managers(tmp_path, monkeypatch, fake_records):
    res = _sync(tmp_path, monkeypatch, fake_records)
    assert res["store_count"] == 5 and res["missing_manager_count"] == 1

    data = json.loads((tmp_path / "store_directory.json").read_text(encoding="utf-8"))
    by_code = {s["code"]: s for s in data["stores"]}

    managers = by_code["D001"]["service_managers"]
    assert [m["name"] for m in managers] == ["张三", "李四", "王五"]   # 按 1..4 顺序；3 号同 open_id 去重
    assert [m["phone"] for m in managers] == ["13800000001", "13800000002", ""]
    assert [m["open_id"] for m in managers] == ["ou1", "ou2", ""]
    assert all(set(m) == {"name", "phone", "open_id"} for m in managers)
    assert by_code["D001"]["service_manager"] == managers[0]          # primary = 第一个非空槽位

    legacy = by_code["D002"]["service_managers"]
    assert legacy == [{"name": "旧负责人", "phone": "13900000009", "open_id": "ou9"}]
    assert by_code["D002"]["service_manager"] == legacy[0]

    slot2 = by_code["D003"]["service_managers"]
    assert slot2 == [{"name": "赵六", "phone": "13700000003", "open_id": "ou3"}]
    assert by_code["D003"]["service_manager"] == slot2[0]              # 1 号为空时 primary = 2 号

    assert by_code["D004"]["service_managers"] == []
    assert by_code["D004"]["service_manager"] == {"name": "", "phone": "", "open_id": ""}
    assert by_code["D005"]["service_managers"][0]["phone"] == "13600000005"


def test_manager_slots_legacy_priority_and_name_dedupe():
    fields = {
        "门店售后业务负责人": [{"id": "legacy", "name": "旧负责人"}],
        "门店售后业务负责人 1": [{"id": "new", "name": "新负责人"}],
        "售后业务负责人手机号": "111",
        "售后业务负责人手机号 1": "222",
        "门店售后业务负责人2": [{"name": "同名"}],
        "门店售后业务负责人 3": [{"name": "同名"}],
    }
    managers = directory_sync._manager_slots(fields)
    assert managers == [
        {"name": "新负责人", "phone": "222", "open_id": "new"},
        {"name": "同名", "phone": "", "open_id": ""},
    ]


def test_manager_slot1_numbered_wins_and_dup_merges_phone():
    import directory_sync as ds
    fields = {
        "门店售后业务负责人 1": [{"name": "新经理", "id": "ou_new"}],
        "门店售后业务负责人": [{"name": "旧经理", "id": "ou_old"}],
        "售后业务负责人手机号": "13900000000",
        "门店售后业务负责人 2": [{"name": "新经理", "id": "ou_new"}],
        "售后业务负责人手机号 2": "13800000000",
        "门店售后业务负责人3": None,
    }
    assert ds._manager_slots(fields) == [{"name": "新经理", "phone": "13800000000", "open_id": "ou_new"}]
