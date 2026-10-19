"""Go 宠物医院 REST API 的异步 HTTP 客户端（MCP 服务访问后端的唯一通道）。

设计要点：

- 只通过 HTTP 调用 Go 后端，不共享任何代码 / 存储；
- 每次调用都有超时；对连接异常与 5xx 做**有限重试**（GET 安全幂等）；
- 一切失败映射为 :class:`RestClientError` 子类（统一 ``BACKEND_*`` 错误码），
  不向 MCP 客户端泄露 httpx 异常类型与堆栈；
- 支持注入 ``httpx`` transport（生产为默认网络传输，测试为
  ``httpx.MockTransport``），测试期间绝不访问真实 Go 服务。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import TracebackType
from typing import Any

import anyio
import httpx

from . import __version__
from .errors import (
    BackendAPIError,
    BackendInvalidResponseError,
    BackendTimeoutError,
    BackendUnavailableError,
)
from .logging_config import get_logger

logger = get_logger(__name__)

PETS_PATH = "/api/v1/pets"


class PetHospitalRestClient:
    """宠物医院 REST 客户端。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        backoff_factor: float = 0.2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries 必须 >= 0")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # -- 生命周期 ----------------------------------------------------------

    async def __aenter__(self) -> "PetHospitalRestClient":
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def open(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout),
                transport=self._transport,
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"pet-hospital-mcp/{__version__}",
                },
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            await self.open()
        assert self._client is not None
        return self._client

    # -- 公共 API ----------------------------------------------------------

    async def list_pets(self, params: Mapping[str, str]) -> dict[str, Any]:
        """调用 ``GET /api/v1/pets``，返回成功信封中的 ``data``（原始 dict）。

        输出数据的 Pydantic 校验由工具层完成；本方法只保证：
        200 + JSON 对象 + 存在 ``data`` 字段。
        """
        payload = await self._get_json(PETS_PATH, params=params)
        if not isinstance(payload, dict) or "data" not in payload:
            raise BackendInvalidResponseError(
                "后端成功响应缺少统一信封或 data 字段。",
                {"path": PETS_PATH},
            )
        return payload["data"]

    # -- 内部：带超时与有限重试的 GET --------------------------------------

    async def _get_json(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        attempt = 0
        while True:
            try:
                response = await client.get(path, params=dict(params or {}))
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise BackendTimeoutError(
                        details={"path": path, "attempts": attempt + 1}
                    ) from exc
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise BackendUnavailableError(
                        details={"path": path, "attempts": attempt + 1}
                    ) from exc
            else:
                if response.status_code < 400:
                    return self._parse_json_body(response, path)
                # 4xx 是确定性错误，重试无意义；5xx 视为可恢复，有限重试。
                if response.status_code < 500 or attempt >= self._max_retries:
                    exc = await self._api_error_from_response(response, path)
                    raise exc
                await response.aclose()

            attempt += 1
            if self._backoff_factor > 0:
                await anyio.sleep(self._backoff_factor * (2 ** (attempt - 1)))

    @staticmethod
    def _parse_json_body(response: httpx.Response, path: str) -> dict[str, Any]:
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise BackendInvalidResponseError(
                "后端响应不是合法 JSON。",
                {"path": path, "http_status": response.status_code},
            ) from exc
        if not isinstance(data, dict):
            raise BackendInvalidResponseError(
                "后端 JSON 响应不是对象。",
                {"path": path, "http_status": response.status_code},
            )
        return data

    @staticmethod
    async def _api_error_from_response(response: httpx.Response, path: str) -> BackendAPIError:
        upstream_message = ""
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                upstream_message = body["message"][:300]
        except (json.JSONDecodeError, ValueError):
            pass
        finally:
            await response.aclose()
        return BackendAPIError(
            f"后端 API 返回错误状态码 {response.status_code}。",
            {
                "path": path,
                "http_status": response.status_code,
                "upstream_message": upstream_message,
            },
        )
