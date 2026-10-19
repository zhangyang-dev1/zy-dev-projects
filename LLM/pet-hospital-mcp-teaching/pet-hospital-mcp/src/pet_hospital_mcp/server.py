"""MCP 服务器装配（SDK 2.x ``MCPServer`` / 无状态 Streamable HTTP）。

要点：

- 使用 ``mcp.server.MCPServer``（2.x 高层 API），**不使用**
  ``mcp.server.fastmcp``（该模块在 2.x 已移除）；
- 传输为无状态 Streamable HTTP：``stateless_http=True`` +
  ``json_response=True``，没有 initialize 握手、没有 ``Mcp-Session-Id``、
  没有会话存储 / 过期 / SSE 恢复；
- 子类化 :class:`MCPServer` 仅做一件事：把 SDK 层（如入参 schema 校验）
- 与未预期异常统一转换为约定的结构化错误信封，绝不把 Pydantic / httpx /
  Python 原始信息直接抛给 MCP 客户端。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from mcp import MCPError
from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import MCP_PROTOCOL_VERSION, __version__
from .config import Settings
from .errors import (
    INTERNAL_ERROR,
    VALIDATION_ERROR,
    envelope,
    error_call_result,
    validation_envelope_from_pydantic,
)
from .logging_config import get_logger
from .rest_client import PetHospitalRestClient
from .tools import register_all

logger = get_logger(__name__)

SERVER_NAME = "pet-hospital-mcp"
SERVER_INSTRUCTIONS = (
    "宠物医院只读查询服务（Go REST API 的 MCP 适配层，协议 2026-07-28，无状态）。"
    "需要查询宠物档案、主人、医生、疾病、状态与花费区间时使用 list_pets。"
)


class PetHospitalMCPServer(MCPServer):
    """携带 REST 客户端并统一兜底工具错误的 MCPServer。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: 由 build_server 注入；工具通过 server 实例访问。
        self.rest_client: PetHospitalRestClient | None = None

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
    ) -> CallToolResult:
        try:
            return await super().call_tool(name, arguments, context)
        except MCPError:
            # 协议级错误（如未知方法 / 参数版本问题）按 JSON-RPC 错误原样透传。
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底边界，统一错误信封
            return error_call_result(_map_unexpected_exception(exc))


def _map_unexpected_exception(exc: BaseException) -> dict[str, Any]:
    """把逃出工具的异常映射为统一信封（不泄露内部堆栈）。"""
    # SDK 入参 schema 校验失败会被工具绑定层包成 ToolError，__cause__ 是
    # pydantic.ValidationError；工具内主动抛出的校验错误同理。
    cause = exc.__cause__ if isinstance(exc.__cause__, BaseException) else exc
    if isinstance(cause, PydanticValidationError):
        env = validation_envelope_from_pydantic(cause)
        logger.warning(
            "tool input rejected by SDK validation",
            extra={"event": "sdk_validation_error", "error_code": VALIDATION_ERROR},
        )
        return env
    logger.exception("unexpected error while executing tool")
    return envelope(
        INTERNAL_ERROR,
        "MCP 服务内部错误，请求未能完成，请查看服务端日志。",
    )


@asynccontextmanager
async def _lifespan(server: PetHospitalMCPServer):
    """随会话管理器启动 / 关闭一次 REST 客户端（2.x：生命周期只运行一次）。"""
    if server.rest_client is not None:
        await server.rest_client.open()
    try:
        yield
    finally:
        if server.rest_client is not None:
            await server.rest_client.aclose()


def register_health_route(server: PetHospitalMCPServer) -> None:
    """注册与 MCP 无关的纯 HTTP 健康检查端点（自定义路由不走鉴权）。"""

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:  # noqa: ARG001
        return JSONResponse(
            {
                "status": "healthy",
                "service": SERVER_NAME,
                "version": __version__,
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )


def build_server(
    settings: Settings | None = None,
    *,
    rest_client: PetHospitalRestClient | None = None,
) -> PetHospitalMCPServer:
    """构造（但不启动）MCP 服务器，完成工具、健康检查与 REST 客户端装配。"""
    settings = settings or Settings.from_env()
    server = PetHospitalMCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=_lifespan,
    )
    server.rest_client = rest_client or PetHospitalRestClient(
        settings.backend_base_url,
        timeout=settings.backend_timeout,
        max_retries=settings.backend_max_retries,
        backoff_factor=settings.backend_retry_backoff,
    )
    register_all(server)
    register_health_route(server)
    return server


def build_app(server: PetHospitalMCPServer, settings: Settings):
    """构建无状态 Streamable HTTP ASGI 应用（端点默认 /mcp）。"""
    return server.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
        host=settings.host,
    )
