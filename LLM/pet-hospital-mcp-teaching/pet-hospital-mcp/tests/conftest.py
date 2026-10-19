"""测试夹具。HTTP 端到端测试用 helpers.http_stack() 上下文管理器（绕过 fixture cancel scope）。"""

from __future__ import annotations

import httpx
import pytest

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalRestClient
from pet_hospital_mcp.server import build_server

from .helpers import MockBackend


@pytest.fixture
def settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8000,
        backend_base_url="http://pet-hospital.test",
        backend_timeout=5.0,
        backend_max_retries=0,
        backend_retry_backoff=0.0,
    )


@pytest.fixture
def mock_backend() -> MockBackend:
    return MockBackend()


@pytest.fixture
async def rest_client(settings: Settings, mock_backend: MockBackend) -> PetHospitalRestClient:
    client = PetHospitalRestClient(
        settings.backend_base_url,
        timeout=settings.backend_timeout,
        max_retries=settings.backend_max_retries,
        backoff_factor=settings.backend_retry_backoff,
        transport=httpx.MockTransport(mock_backend.handle),
    )
    await client.open()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def server(settings: Settings, rest_client: PetHospitalRestClient):
    return build_server(settings, rest_client=rest_client)
