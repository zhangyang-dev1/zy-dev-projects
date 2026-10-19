"""绕过 HTTP 协议，直接调用 MCPServer.call_tool() 的单元测试。

覆盖：输入参数校验失败、正常转发全参数、后端 4xx/5xx/超时/连接异常/非法 JSON/null records。
"""

from __future__ import annotations

import json

import httpx
import pytest

from pet_hospital_mcp.errors import (
    BACKEND_API_ERROR,
    BACKEND_INVALID_RESPONSE,
    BACKEND_TIMEOUT,
    BACKEND_UNAVAILABLE,
    VALIDATION_ERROR,
)

from .helpers import call_list_pets_direct, sample_pet, success_payload


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", False))


def _error_code(result) -> str:
    sc = getattr(result, "structured_content", None)
    assert isinstance(sc, dict), f"structured_content 应该是 dict，实际 {type(sc)}"
    return sc["error"]["code"]


def _error_details(result) -> dict:
    return getattr(result, "structured_content")["error"]["details"]


# ---------------------------------------------------------------------------
# 输入参数校验失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_unknown_field(server, mock_backend):
    result = await call_list_pets_direct(server, {"ownerName": "张", "bogus": 1})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR
    issues = _error_details(result)["issues"]
    fields = {i["field"] for i in issues}
    assert "bogus" in fields
    # 后端未被调用
    assert len(mock_backend.requests) == 0


@pytest.mark.asyncio
async def test_validation_page_zero(server, mock_backend):
    result = await call_list_pets_direct(server, {"page": 0})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR
    assert len(mock_backend.requests) == 0


@pytest.mark.asyncio
async def test_validation_page_negative(server, mock_backend):
    result = await call_list_pets_direct(server, {"page": -3})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_page_size_too_large(server, mock_backend):
    result = await call_list_pets_direct(server, {"pageSize": 501})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_page_size_zero(server, mock_backend):
    result = await call_list_pets_direct(server, {"pageSize": 0})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_page_bool(server, mock_backend):
    result = await call_list_pets_direct(server, {"page": True})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_page_float(server, mock_backend):
    result = await call_list_pets_direct(server, {"page": 1.5})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_species_invalid(server, mock_backend):
    result = await call_list_pets_direct(server, {"species": "狗"})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_status_invalid(server, mock_backend):
    result = await call_list_pets_direct(server, {"status": "已出院"})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_order_invalid(server, mock_backend):
    result = await call_list_pets_direct(server, {"order": "up"})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_sort_by_invalid(server, mock_backend):
    result = await call_list_pets_direct(server, {"sortBy": "foo"})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_min_negative(server, mock_backend):
    result = await call_list_pets_direct(server, {"min": -1.0})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_min_gt_max(server, mock_backend):
    result = await call_list_pets_direct(server, {"min": 100, "max": 50})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_min_nan(server, mock_backend):
    import math
    result = await call_list_pets_direct(server, {"min": float("nan")})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_min_infinity(server, mock_backend):
    result = await call_list_pets_direct(server, {"min": float("inf")})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


@pytest.mark.asyncio
async def test_validation_min_bool(server, mock_backend):
    result = await call_list_pets_direct(server, {"min": True})
    assert _is_error(result)
    assert _error_code(result) == VALIDATION_ERROR


# ---------------------------------------------------------------------------
# 正常转发全参数
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_all_params_forwarded(server, mock_backend):
    args = {
        "q": "感冒",
        "name": "旺财",
        "ownerName": "张",
        "ownerPhone": "1380",
        "species": "犬",
        "doctor": "李医生",
        "disease": "感染",
        "status": "住院中",
        "min": 50.5,
        "max": 500,
        "sortBy": "totalCost",
        "order": "desc",
        "page": 3,
        "pageSize": 15,
    }
    result = await call_list_pets_direct(server, args)
    assert not _is_error(result), f"不应该是错误: {getattr(result, 'structured_content', None)}"

    # 后端收到正确的路径
    assert mock_backend.last_path == "/api/v1/pets"

    # 全部参数正确转发
    q = mock_backend.last_query
    assert q["q"] == "感冒"
    assert q["name"] == "旺财"
    assert q["ownerName"] == "张"
    assert q["ownerPhone"] == "1380"
    assert q["species"] == "犬"
    assert q["doctor"] == "李医生"
    assert q["disease"] == "感染"
    assert q["status"] == "住院中"
    assert q["min"] == "50.5"
    assert q["max"] == "500"  # 整数浮点转整数串
    assert q["sortBy"] == "totalCost"
    assert q["order"] == "desc"
    assert q["page"] == "3"
    assert q["pageSize"] == "15"
    # 14 个参数全部被发送（无多余）
    assert len(q) == 14

    # 成功输出
    sc = result.structured_content
    assert sc["total"] == 1
    assert sc["page"] == 1
    assert sc["pageSize"] == 20
    assert sc["totalPages"] == 1
    assert sc["totalCost"] == 300.0
    assert isinstance(sc["items"], list)
    item = sc["items"][0]
    assert item["name"] == "旺财"
    # records / charges 是数组
    assert isinstance(item["records"], list)
    assert isinstance(item["charges"], list)


@pytest.mark.asyncio
async def test_success_minimal_args(server, mock_backend):
    """空参数：只发默认分页。"""
    result = await call_list_pets_direct(server, {})
    assert not _is_error(result)
    q = mock_backend.last_query
    assert q == {"page": "1", "pageSize": "20"}
    # 14 个参数里只有 page/pageSize 必发；其他不传
    assert len(q) == 2


# ---------------------------------------------------------------------------
# 后端 4xx / 5xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_400(server, mock_backend):
    async def _handler(_req):
        return httpx.Response(400, json={"code": 400, "message": "参数错误", "data": None})

    mock_backend.handler = _handler
    result = await call_list_pets_direct(server, {"q": "x"})
    assert _is_error(result)
    assert _error_code(result) == BACKEND_API_ERROR
    details = _error_details(result)
    assert details["http_status"] == 400
    assert details["path"] == "/api/v1/pets"


@pytest.mark.asyncio
async def test_backend_404(server, mock_backend):
    async def _handler(_req):
        return httpx.Response(404, json={"code": 404, "message": "not found"})

    mock_backend.handler = _handler
    result = await call_list_pets_direct(server, {})
    assert _is_error(result)
    assert _error_code(result) == BACKEND_API_ERROR


@pytest.mark.asyncio
async def test_backend_500(server, mock_backend):
    async def _handler(_req):
        return httpx.Response(500, json={"code": 500, "message": "server error"})

    mock_backend.handler = _handler
    result = await call_list_pets_direct(server, {})
    assert _is_error(result)
    assert _error_code(result) == BACKEND_API_ERROR
    details = _error_details(result)
    assert details["http_status"] == 500


# ---------------------------------------------------------------------------
# 超时 & 连接异常
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_timeout(server, mock_backend):
    async def slow_handler(_req):
        raise httpx.TimeoutException("timed out")

    mock_backend.handler = slow_handler
    result = await call_list_pets_direct(server, {})
    assert _is_error(result)
    assert _error_code(result) == BACKEND_TIMEOUT


@pytest.mark.asyncio
async def test_backend_unavailable(server, mock_backend):
    async def conn_err(_req):
        raise httpx.ConnectError("connection refused")

    mock_backend.handler = conn_err
    result = await call_list_pets_direct(server, {})
    assert _is_error(result)
    assert _error_code(result) == BACKEND_UNAVAILABLE


# ---------------------------------------------------------------------------
# 非法 JSON / 模型不符
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_invalid_json(server, mock_backend):
    async def bad_json(_req):
        return httpx.Response(200, content=b"not valid json", headers={"content-type": "application/json"})

    mock_backend.handler = bad_json
    result = await call_list_pets_direct(server, {})
    assert _is_error(result)
    assert _error_code(result) == BACKEND_INVALID_RESPONSE


@pytest.mark.asyncio
async def test_backend_missing_data_field(server, mock_backend):
    async def no_data(_req):
        return httpx.Response(200, json={"code": 200, "message": "ok"})

    mock_backend.handler = no_data
    result = await call_list_pets_direct(server, {})
    assert _is_error(result)
    assert _error_code(result) == BACKEND_INVALID_RESPONSE


@pytest.mark.asyncio
async def test_backend_data_model_mismatch(server, mock_backend):
    async def bad_shape(_req):
        # data 里 total 是字符串，违反 int 约束
        return httpx.Response(200, json={
            "code": 200, "message": "ok",
            "data": {"items": [], "total": "abc", "page": 1, "pageSize": 20, "totalPages": 1, "totalCost": 0},
        })

    mock_backend.handler = bad_shape
    result = await call_list_pets_direct(server, {})
    assert _is_error(result)
    assert _error_code(result) == BACKEND_INVALID_RESPONSE


# ---------------------------------------------------------------------------
# 兼容 records / charges 为 null（Go 真实表现）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_null_records_and_charges(server, mock_backend):
    pet = sample_pet(records=None, charges=None)

    async def _handler(_req):
        return httpx.Response(200, json=success_payload(items=[pet]))

    mock_backend.handler = _handler
    result = await call_list_pets_direct(server, {})
    assert not _is_error(result), f"{getattr(result, 'structured_content', None)}"
    item = result.structured_content["items"][0]
    # null 归一化为空数组
    assert item["records"] == []
    assert item["charges"] == []
    # chipNo / ownerPhone 原样保留（脱敏只在日志）
    assert item["chipNo"] == "CHIP-SECRET-0001"
    assert item["ownerPhone"] == "13800000000"


def _respond(status: int, body):
    return httpx.Response(status, json=body)
