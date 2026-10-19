"""JSON 结构化日志与敏感字段脱敏。

每条工具调用至少记录：``timestamp``、``tool_name``、``params``、
``status``、``duration_ms``。

以下字段（camelCase 与 snake_case 两种写法）在写入日志前递归脱敏：
``ownerPhone``/``owner_phone``、``ownerAddr``/``owner_addr``、
``chipNo``/``chip_no``。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

#: 需要递归脱敏的字段名（camelCase 与 snake_case）。
SENSITIVE_KEYS = frozenset(
    {
        "ownerPhone",
        "owner_phone",
        "ownerAddr",
        "owner_addr",
        "chipNo",
        "chip_no",
    }
)

MASK = "***"

#: 脱敏后单值最大长度，避免把完整敏感数据写进日志。
MAX_LOG_VALUE_LEN = 200


def mask_sensitive(value: Any) -> Any:
    """递归返回脱敏后的结构（不修改原对象）。"""
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and key in SENSITIVE_KEYS:
                masked[key] = MASK
            else:
                masked[key] = mask_sensitive(val)
        return masked
    if isinstance(value, (list, tuple)):
        return [mask_sensitive(item) for item in value]
    if isinstance(value, str) and len(value) > MAX_LOG_VALUE_LEN:
        return value[:MAX_LOG_VALUE_LEN] + "...(truncated)"
    return value


class JsonFormatter(logging.Formatter):
    """把 LogRecord 输出为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # 业务结构化字段（tool_name/params/status/duration_ms 等）。
        for key in ("tool_name", "params", "status", "duration_ms", "event", "error_code", "detail"):
            if hasattr(record, key):
                val = getattr(record, key)
                payload[key] = mask_sensitive(val) if key == "params" else val
        if record.exc_info:
            # 仅记录异常类型与消息，绝不输出完整堆栈到对客户端可见处；
            # 堆栈本身也不写入日志，避免 httpx/Python 内部路径扩散（教学服务）。
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """配置 stderr JSON 日志（幂等，重复调用不会叠加 handler）。"""
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    # 替换而非追加，避免 reload / 测试场景重复打印。
    root.handlers = [handler]
    root.setLevel(level.upper() if isinstance(level, str) else level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_tool_call(
    logger: logging.Logger,
    *,
    tool_name: str,
    params: Any,
    status: str,
    duration_ms: float,
    error_code: str | None = None,
) -> None:
    """以统一字段记录一次工具调用（params 递归脱敏）。"""
    logger.info(
        "tool call finished",
        extra={
            "tool_name": tool_name,
            "params": mask_sensitive(params),
            "status": status,
            "duration_ms": round(float(duration_ms), 3),
            "error_code": error_code,
        },
    )
