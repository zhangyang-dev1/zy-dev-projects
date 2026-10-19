"""HTTP 端点端到端测试（无状态 2026-07-28 Streamable HTTP）。

关键：全部测试体用 ``http_stack()`` 上下文管理器包裹，让 AnyIO cancel scope
在**同一个** asyncio task 内 enter 和 exit（pytest-asyncio fixture 调度会拆 task）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from .helpers import (
    PROTOCOL_VERSION,
    http_stack,
    mcp_post,
    routing_headers,
    rpc_request,
)


# ---------------------------------------------------------------------------
# 工具注册 + JSON Schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_shows_list_pets():
    async with http_stack() as (client, _mb):
        resp = await mcp_post(client, "tools/list", name="tools")
        assert resp.status_code == 200
        body = resp.json()
        assert body["jsonrpc"] == "2.0"
        tools = body["result"]["tools"]
        assert [t["name"] for t in tools] == ["list_pets"]


@pytest.mark.asyncio
async def test_list_pets_json_schema_props():
    async with http_stack() as (client, _mb):
        resp = await mcp_post(client, "tools/list", name="tools")
        tool = resp.json()["result"]["tools"][0]
        schema = tool["inputSchema"]
        props = sorted(schema["properties"].keys())
        expected = sorted([
            "q", "name", "ownerName", "ownerPhone", "species", "doctor",
            "disease", "status", "min", "max", "sortBy", "order",
            "page", "pageSize",
        ])
        assert props == expected

        # 枚举限制（可选字段用 anyOf: [{enum...}, null] 表达）
        def _first_enum(prop):
            for sub in prop.get("anyOf", [prop]):
                if "enum" in sub:
                    return set(sub["enum"])
            if "enum" in prop:
                return set(prop["enum"])
            raise AssertionError(f"没找到 enum: {prop}")

        assert _first_enum(schema["properties"]["species"]) == {"犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"}
        assert _first_enum(schema["properties"]["status"]) == {"待就诊", "就诊中", "住院中", "已康复", "慢性病随访"}

        page_schema = schema["properties"]["page"]
        assert page_schema["minimum"] == 1
        page_size_schema = schema["properties"]["pageSize"]
        assert page_size_schema["minimum"] == 1
        assert page_size_schema["maximum"] == 500


# ---------------------------------------------------------------------------
# server/discover（2026-07-28 新协议发现机制）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_discover_returns_modern_version():
    async with http_stack() as (client, _mb):
        resp = await mcp_post(client, "server/discover")
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"]["supportedVersions"] == [PROTOCOL_VERSION]
        assert "capabilities" in body["result"]


# ---------------------------------------------------------------------------
# 无状态连接流程
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_initialize_method():
    """POST initialize → METHOD_NOT_FOUND（现代端点不做旧握手）。"""
    async with http_stack() as (client, _mb):
        resp = await client.post(
            "/mcp",
            json=rpc_request("initialize"),
            headers=routing_headers("initialize"),
        )
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_no_mcp_session_id_header():
    """响应头中**不存在** Mcp-Session-Id（无状态）。"""
    async with http_stack() as (client, _mb):
        resp = await mcp_post(client, "server/discover")
        assert "mcp-session-id" not in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_multiple_requests_independent():
    """先错后对：两次 tools/call 互不依赖。"""
    async with http_stack() as (client, mb):
        bad = await mcp_post(
            client, "tools/call",
            params={"name": "list_pets", "arguments": {"page": -1}},
            name="list_pets",
        )
        bad_result = bad.json()["result"]
        assert bad_result["isError"] is True

        good = await mcp_post(
            client, "tools/call",
            params={"name": "list_pets", "arguments": {"page": 1, "pageSize": 5}},
            name="list_pets",
        )
        good_result = good.json()["result"]
        assert good_result["isError"] is False
        # 关键：后端收到了我们的过滤/分页参数
        assert len(mb.requests) == 1  # only good call; bad one was rejected before reaching backend
        assert mb.last_query["pageSize"] == "5"
        assert mb.last_query["page"] == "1"


@pytest.mark.asyncio
async def test_three_discover_requests_no_session_id():
    """连续三次 discover 均不返回 Mcp-Session-Id。"""
    async with http_stack() as (client, _mb):
        for _ in range(3):
            resp = await mcp_post(client, "server/discover")
            assert "mcp-session-id" not in {k.lower() for k in resp.headers}


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint():
    async with http_stack() as (client, _mb):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["service"] == "pet-hospital-mcp"
        assert body["protocolVersion"] == PROTOCOL_VERSION
        assert "timestamp" in body
        assert body["version"]


# ---------------------------------------------------------------------------
# tools/call 通过 HTTP 端到端
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_success_via_http():
    async with http_stack() as (client, mb):
        params = {
            "name": "list_pets",
            "arguments": {
                "species": "犬",
                "status": "就诊中",
                "sortBy": "totalCost",
                "order": "desc",
                "page": 1,
                "pageSize": 10,
            },
        }
        resp = await mcp_post(client, "tools/call", params=params, name="list_pets")
        assert resp.status_code == 200
        call_result = resp.json()["result"]
        assert call_result["isError"] is False
        sc = call_result["structuredContent"]
        assert isinstance(sc["items"], list)
        assert sc["totalCost"] == 300.0  # mock default
        # 关键：后端收到全部过滤 / 排序 / 分页参数
        q = mb.last_query
        assert q["species"] == "犬"
        assert q["status"] == "就诊中"
        assert q["sortBy"] == "totalCost"
        assert q["order"] == "desc"
        assert q["page"] == "1"
        assert q["pageSize"] == "10"
        assert len(mb.requests) == 1


@pytest.mark.asyncio
async def test_tools_call_validation_error_via_http():
    async with http_stack() as (client, _mb):
        params = {
            "name": "list_pets",
            "arguments": {"species": "不是合法种类", "page": 999},
        }
        resp = await mcp_post(client, "tools/call", params=params, name="list_pets")
        call_result = resp.json()["result"]
        assert call_result["isError"] is True
        assert call_result["structuredContent"]["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_tools_call_backend_error_via_http():
    async with http_stack(backend_status=500, backend_json={"code": 500, "message": "boom"}) as (client, _mb):
        params = {"name": "list_pets", "arguments": {}}
        resp = await mcp_post(client, "tools/call", params=params, name="list_pets")
        call_result = resp.json()["result"]
        assert call_result["isError"] is True
        assert call_result["structuredContent"]["error"]["code"] == "BACKEND_API_ERROR"


# ---------------------------------------------------------------------------
# 现代端点路由头 & 响应格式
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_protocol_version_header_rejected():
    """缺少 MCP-Protocol-Version header → JSON-RPC error。"""
    async with http_stack() as (client, _mb):
        resp = await client.post(
            "/mcp",
            json=rpc_request("server/discover"),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        body = resp.json()
        assert "error" in body


@pytest.mark.asyncio
async def test_mcp_response_is_json():
    """MCP 端点返回 application/json（不是 SSE）。"""
    async with http_stack() as (client, _mb):
        resp = await mcp_post(client, "server/discover")
        assert "application/json" in resp.headers.get("content-type", "").lower()


# ---------------------------------------------------------------------------
# 响应不泄露内部错误栈（校验错误通过 HTTP / CallToolResult 不暴露 httpx/Pydantic/SDK 细节）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_error_does_not_expose_internal_stacks():
    async with http_stack() as (client, _mb):
        params = {"name": "list_pets", "arguments": {"page": "abc", "species": "狗"}}
        resp = await mcp_post(client, "tools/call", params=params, name="list_pets")
        body = resp.json()
        call_result = body["result"]
        envelope = call_result["structuredContent"]
        text = json.dumps(envelope, ensure_ascii=False).lower()
        for forbidden in ("httpx", "traceback", "pydantic/main.py", "file not found"):
            assert forbidden not in text, f"响应泄露了内部细节: {forbidden}"
