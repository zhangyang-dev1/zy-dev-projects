"""测试辅助函数（夹具在 conftest.py，这里是可复用的普通函数）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
from contextlib import asynccontextmanager

from mcp.server.mcpserver import Context

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalRestClient
from pet_hospital_mcp.server import build_app, build_server

PROTOCOL_VERSION = "2026-07-28"

META: dict[str, Any] = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "pytest-client", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def sample_pet(*, records="__default__", charges="__default__") -> dict[str, Any]:
    """构造与 Go model.Pet JSON 表现一致的档案。

    传 ``None`` 表示显式 JSON null（Go API 真实表现）；不传时用默认数组。
    """
    records_list = [
        {"id": "r-001", "visitDate": "2026-07-20", "doctor": "李医生",
         "diagnosis": "感冒", "symptoms": "咳嗽", "treatment": "对症治疗",
         "prescription": ["药品A"], "weightKg": 18.5, "temperature": 38.7,
         "followUp": "三天后复诊", "charge": 300.0, "createdAt": "2026-07-20T10:00:00Z"},
    ]
    charges_list = [
        {"id": "c-001", "item": "血常规", "category": "检查", "amount": 300.0,
         "doctor": "李医生", "date": "2026-07-20", "note": ""},
    ]
    return {
        "id": "p-001", "name": "旺财", "species": "犬", "breed": "金毛",
        "gender": "公", "ageMonths": 24, "color": "金黄", "chipNo": "CHIP-SECRET-0001",
        "ownerName": "张三", "ownerPhone": "13800000000", "ownerAddr": "保密地址 88 号",
        "doctor": "李医生", "disease": "上呼吸道感染", "status": "就诊中",
        "allergy": "青霉素", "note": "",
        "records": records_list if records == "__default__" else records,
        "charges": charges_list if charges == "__default__" else charges,
        "totalCost": 300.0, "visitCount": 1,
        "createdAt": "2026-07-20T10:00:00Z", "updatedAt": "2026-07-20T10:00:00Z",
    }


def success_payload(
    *, items: Any = None, total: int = 1, page: int = 1, page_size: int = 20,
    total_pages: int = 1, total_cost: float = 300.0,
) -> dict[str, Any]:
    return {
        "code": 200, "message": "ok",
        "data": {
            "items": items if items is not None else [sample_pet()],
            "total": total, "page": page, "pageSize": page_size,
            "totalPages": total_pages, "totalCost": total_cost,
        },
        "time": "2026-07-28T08:00:00Z",
    }


class MockBackend:
    """可编排的假 Go 后端，记录全部请求。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def default_handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=success_payload())

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = getattr(self, "handler", self.default_handler)
        return await handler(request)

    @property
    def last_query(self) -> dict[str, str]:
        assert self.requests
        return dict(self.requests[-1].url.params.items())

    @property
    def last_path(self) -> str:
        assert self.requests
        return self.requests[-1].url.path


def make_context(raw_arguments: dict[str, Any]) -> Context:
    return Context(
        request_context=SimpleNamespace(
            params={"arguments": raw_arguments},
            protocol_version=PROTOCOL_VERSION,
        )
    )


def rpc_request(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
    body_params: dict[str, Any] = {"_meta": META}
    if params:
        body_params.update(params)
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": body_params}


def routing_headers(method: str, name: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


async def mcp_post(client: httpx.AsyncClient, method: str, *,
                   params: dict[str, Any] | None = None,
                   name: str | None = None,
                   headers: dict[str, str] | None = None) -> httpx.Response:
    merged = routing_headers(method, name)
    if headers:
        merged.update(headers)
    return await client.post("/mcp", json=rpc_request(method, params), headers=merged)


async def call_list_pets_direct(server, raw: dict[str, Any]):
    return await server.call_tool("list_pets", raw, context=make_context(raw))


@asynccontextmanager
async def http_stack(
    *,
    settings: Settings | None = None,
    backend_status: int | None = None,
    backend_json: Any = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, MockBackend]]:
    """HTTP 端到端测试的完整栈：设置 → mock 后端 → server → app → session_manager → httpx client。

    全部在**同一个** asyncio task 内运行（pytest-asyncio 的 fixture 调度可能拆 task，
    导致 AnyIO cancel scope 跨 task；直接在测试内用这个上下文管理器是最稳的做法）。
    """
    settings = settings or Settings(
        host="127.0.0.1",
        port=8000,
        backend_base_url="http://pet-hospital.test",
        backend_timeout=5.0,
        backend_max_retries=0,
        backend_retry_backoff=0.0,
    )
    mb = MockBackend()

    if backend_status is not None or backend_json is not None:
        code = backend_status or 200
        body = backend_json if backend_json is not None else success_payload()
        async def fixed_handler(_req):
            return httpx.Response(code, json=body)
        mb.handler = fixed_handler

    rc = PetHospitalRestClient(
        settings.backend_base_url,
        timeout=settings.backend_timeout,
        max_retries=settings.backend_max_retries,
        backoff_factor=settings.backend_retry_backoff,
        transport=httpx.MockTransport(mb.handle),
    )
    await rc.open()
    try:
        server = build_server(settings, rest_client=rc)
        app = build_app(server, settings)

        async with server.session_manager.run():
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 8000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client:
                yield client, mb
    finally:
        await rc.aclose()
