"""pet-hospital-mcp：Go 宠物医院 REST API 的 MCP 适配服务。

技术基线（与 README 保持一致）：

- 官方 Python SDK ``mcp==2.0.0``，高层服务器 ``mcp.server.MCPServer``
  （``mcp.server.fastmcp`` 在 2.x 已不存在，本项目不使用任何 1.x API）。
- MCP 协议版本 ``2026-07-28``：无握手、无会话、无 ``Mcp-Session-Id``，
  每个 HTTP POST 自描述（``_meta`` + ``MCP-*`` 请求头）。
- 传输：无状态 Streamable HTTP（``stateless_http=True``、
  ``json_response=True``），端点 ``/mcp``。

阶段一仅暴露一个工具 ``list_pets``，适配 Go 后端 ``GET /api/v1/pets``。
"""

__version__ = "0.1.0"

#: 实际使用的 MCP SDK 版本（固定依赖）。
MCP_SDK_VERSION = "2.0.0"

#: 实际使用的 MCP 协议版本（2026-07-28：无会话 Streamable HTTP）。
MCP_PROTOCOL_VERSION = "2026-07-28"
