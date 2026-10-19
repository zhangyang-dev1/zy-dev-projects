"""日志脱敏单测。"""

from pet_hospital_mcp.logging_config import mask_sensitive


def test_mask_sensitive_top_level_camel_case():
    data = {"ownerPhone": "13800000000", "ownerAddr": "保密地址 88 号", "chipNo": "CHIP-SECRET"}
    result = mask_sensitive(data)
    assert result == {
        "ownerPhone": "***",
        "ownerAddr": "***",
        "chipNo": "***",
    }


def test_mask_sensitive_top_level_snake_case():
    data = {"owner_phone": "13800000000", "owner_addr": "保密地址 88", "chip_no": "CHIP-SECRET"}
    result = mask_sensitive(data)
    assert result == {"owner_phone": "***", "owner_addr": "***", "chip_no": "***"}


def test_mask_sensitive_recursive_dict():
    data = {
        "items": [
            {"ownerPhone": "1380", "name": "旺财"},
        ],
        "nested": {"deep": {"chipNo": "ABC", "ok": 123}},
        "x": None,
        "y": [{"owner_addr": "a", "not_sensitive": "b"}],
    }
    result = mask_sensitive(data)
    assert result["items"][0]["ownerPhone"] == "***"
    assert result["items"][0]["name"] == "旺财"
    assert result["nested"]["deep"]["chipNo"] == "***"
    assert result["nested"]["deep"]["ok"] == 123
    assert result["y"][0]["owner_addr"] == "***"
    assert result["y"][0]["not_sensitive"] == "b"
    assert result["x"] is None


def test_mask_sensitive_does_not_mutate_original():
    original = {"ownerPhone": "1380", "nested": {"chipNo": "CHIP"}}
    import copy
    clone = copy.deepcopy(original)
    mask_sensitive(original)
    assert original == clone


def test_mask_sensitive_truncates_long_strings():
    long_value = "x" * 500
    data = {"msg": long_value}
    result = mask_sensitive(data)
    assert result["msg"].endswith("...(truncated)")
    assert len(result["msg"]) < len(long_value)


def test_mask_sensitive_preserves_numbers_bools_none():
    data = {"count": 3, "active": True, "nothing": None, "rate": 0.5}
    result = mask_sensitive(data)
    assert result == data
