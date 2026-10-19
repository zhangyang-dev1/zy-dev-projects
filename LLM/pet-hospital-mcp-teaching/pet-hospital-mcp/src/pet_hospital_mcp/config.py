"""运行配置：全部来自环境变量，带教学场景下合理的本地默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Go 宠物医院 REST API 的默认地址（唯一业务后端）。
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

#: MCP 服务默认仅监听本机回环地址（规范建议，教学场景不做 Origin 校验）。
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: MCP 端点路径。
DEFAULT_MCP_PATH = "/mcp"

#: 上游 REST 调用默认超时（秒）与有限重试次数。
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 2
#: 重试退避基数（秒），仅在连接异常 / 5xx 时退避；测试可置 0。
DEFAULT_RETRY_BACKOFF = 0.2


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """进程级配置。

    环境变量：

    - ``MCP_HOST`` / ``MCP_PORT``：MCP HTTP 监听地址（默认 127.0.0.1:8000）
    - ``MCP_PATH``：MCP 端点路径（默认 ``/mcp``）
    - ``PET_HOSPITAL_BASE_URL``：Go REST API 地址
    - ``PET_HOSPITAL_TIMEOUT``：单次上游 HTTP 超时（秒）
    - ``PET_HOSPITAL_MAX_RETRIES``：上游失败时的有限重试次数
    - ``PET_HOSPITAL_RETRY_BACKOFF``：重试退避基数（秒）
    - ``LOG_LEVEL``：日志级别
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    mcp_path: str = DEFAULT_MCP_PATH
    backend_base_url: str = DEFAULT_BASE_URL
    backend_timeout: float = DEFAULT_TIMEOUT_SECONDS
    backend_max_retries: int = DEFAULT_MAX_RETRIES
    backend_retry_backoff: float = DEFAULT_RETRY_BACKOFF
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        port = _env_int("MCP_PORT", DEFAULT_PORT)
        if not 1 <= port <= 65535:
            port = DEFAULT_PORT
        path = _env_str("MCP_PATH", DEFAULT_MCP_PATH)
        if not path.startswith("/"):
            path = "/" + path
        timeout = _env_float("PET_HOSPITAL_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        if timeout <= 0:
            timeout = DEFAULT_TIMEOUT_SECONDS
        retries = _env_int("PET_HOSPITAL_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        if retries < 0:
            retries = DEFAULT_MAX_RETRIES
        backoff = _env_float("PET_HOSPITAL_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF)
        if backoff < 0:
            backoff = DEFAULT_RETRY_BACKOFF
        return cls(
            host=_env_str("MCP_HOST", DEFAULT_HOST),
            port=port,
            mcp_path=path,
            backend_base_url=_env_str("PET_HOSPITAL_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            backend_timeout=timeout,
            backend_max_retries=retries,
            backend_retry_backoff=backoff,
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        )
