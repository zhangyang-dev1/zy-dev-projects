"""本地 MCP Server（真实联调用，非测试断言的一部分）。

用法::

    python tests/_local_mcp_server.py stdio   # stdio 协议（阻塞）
    python tests/_local_mcp_server.py http    # Streamable HTTP（127.0.0.1:8765/mcp）

工具覆盖：普通文本、结构化数据、抛错（isError 通道）、与 ZYAgent 本地工具
同名（getDate -> 验证服务器前缀映射）。
"""
from __future__ import annotations

import sys
from datetime import date
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer("zy-local-test", version="1.0")


@mcp.tool()
def echo(text: str) -> str:
    """把输入原样返回（联调用）。"""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """返回两数之和。"""
    return a + b


@mcp.tool()
def facts(city: str) -> dict[str, Any]:
    """返回某城市的结构化数据（验证 structuredContent 通道）。"""
    return {"city": city, "population": 12345678, "country": "CN"}


@mcp.tool()
def boom() -> str:
    """总是失败（验证 isError 错误回传通道）。"""
    raise ValueError("刻意失败：用于验证 MCP 错误通道")


@mcp.tool()
def getDate() -> str:
    """返回今天日期（与 ZYAgent 本地工具同名，验证服务器前缀映射）。"""
    return date.today().isoformat()


def main() -> int:
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "stdio":
        mcp.run(transport="stdio")
        return 0
    if transport == "http":
        import asyncio

        import uvicorn

        app = mcp.streamable_http_app(streamable_http_path="/mcp")
        config = uvicorn.Config(app, host="127.0.0.1", port=8765,
                                log_level="warning")
        print("local MCP (streamable http) listening on http://127.0.0.1:8765/mcp",
              flush=True)
        asyncio.run(uvicorn.Server(config).serve())
        return 0
    print(f"未知 transport: {transport}（支持 stdio / http）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
