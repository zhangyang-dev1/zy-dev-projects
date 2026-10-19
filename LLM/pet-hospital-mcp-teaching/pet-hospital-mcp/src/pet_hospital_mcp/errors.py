"""统一结构化错误约定。

所有工具失败（无效输入 / 上游异常 / 内部错误）都通过同一信封返回：

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "可读错误信息",
    "details": {}
  }
}
```

并以 MCP 2.x 的失败工具结果（``CallToolResult(is_error=True)``）承载，
``structured_content`` 为上面的信封，``content`` 为其 JSON 文本，
绝不向客户端暴露 httpx / Pydantic / SDK / Python 原始堆栈。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# 错误码（本服务对外契约，新增工具时复用）
# ---------------------------------------------------------------------------

VALIDATION_ERROR = "VALIDATION_ERROR"
"""工具输入未通过校验（枚举越界、分页非法、min>max、NaN、未知字段等）。"""

BACKEND_TIMEOUT = "BACKEND_TIMEOUT"
"""调用 Go REST API 超时（重试耗尽）。"""

BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
"""Go REST API 不可达（连接被拒绝、连接中断等）。"""

BACKEND_API_ERROR = "BACKEND_API_ERROR"
"""Go REST API 返回了非 2xx 响应。"""

BACKEND_INVALID_RESPONSE = "BACKEND_INVALID_RESPONSE"
"""Go REST API 返回 200，但响应体不是合法 JSON 或不符合数据模型。"""

INTERNAL_ERROR = "INTERNAL_ERROR"
"""工具自身发生未预期错误（细节仅进日志，不回传客户端）。"""


class ErrorDetail(BaseModel):
    """结构化错误详情。"""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """统一错误信封（对应工具结果的 structured_content）。"""

    error: ErrorDetail


def envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造并校验统一错误信封，返回可直接放入 structured_content 的 dict。"""
    return ErrorEnvelope(
        error=ErrorDetail(code=code, message=message, details=details or {})
    ).model_dump(mode="json")


def validation_envelope(details: dict[str, Any] | list[Any] | None = None) -> dict[str, Any]:
    """构造输入校验失败信封。"""
    return envelope(
        VALIDATION_ERROR,
        "工具输入参数校验失败，请按 details 中的字段提示修正后重试。",
        {"issues": details or []},
    )


def format_pydantic_issues(exc: ValidationError) -> list[dict[str, str]]:
    """把 Pydantic 校验错误转为白名单字段的安全列表（不泄露内部上下文）。"""
    issues: list[dict[str, str]] = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err.get("loc", ()))
        issues.append(
            {
                "field": field or "(root)",
                "type": str(err.get("type", "value_error")),
                "message": str(err.get("msg", "参数不合法")),
            }
        )
    return issues


def validation_envelope_from_pydantic(exc: ValidationError) -> dict[str, Any]:
    """从 Pydantic ValidationError 构造统一校验错误信封。"""
    return validation_envelope(format_pydantic_issues(exc))


def error_call_result(error_envelope: dict[str, Any]) -> CallToolResult:
    """把错误信封包装为 MCP 2.x 失败工具结果。

    - ``is_error=True``：2.x 官方规定的“工具执行失败”标记（模型可读、可纠错）；
    - ``structured_content``：统一错误结构，供客户端程序消费；
    - ``content``：同样内容的 JSON 文本，供模型阅读。
    """
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(error_envelope, ensure_ascii=False))
        ],
        structured_content=error_envelope,
        is_error=True,
    )


# ---------------------------------------------------------------------------
# REST 客户端异常（仅在服务内部流转，绝不直接暴露给 MCP 客户端）
# ---------------------------------------------------------------------------


class RestClientError(Exception):
    """调用 Go REST 后端失败的基类。

    :param code: :data:`VALIDATION_ERROR` 之外的 ``BACKEND_*`` 错误码
    :param message: 可读的、不含内部堆栈的错误信息
    :param details: 结构化附加信息（HTTP 状态码、URL 等，不含敏感数据）
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_envelope(self) -> dict[str, Any]:
        return envelope(self.code, self.message, self.details)


class BackendTimeoutError(RestClientError):
    def __init__(self, message: str = "调用宠物医院后端服务超时。", details: dict[str, Any] | None = None):
        super().__init__(BACKEND_TIMEOUT, message, details)


class BackendUnavailableError(RestClientError):
    def __init__(self, message: str = "无法连接宠物医院后端服务，请确认 Go REST API 已启动。",
                 details: dict[str, Any] | None = None):
        super().__init__(BACKEND_UNAVAILABLE, message, details)


class BackendAPIError(RestClientError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(BACKEND_API_ERROR, message, details)


class BackendInvalidResponseError(RestClientError):
    def __init__(self, message: str = "宠物医院后端返回了无法识别的响应。",
                 details: dict[str, Any] | None = None):
        super().__init__(BACKEND_INVALID_RESPONSE, message, details)
