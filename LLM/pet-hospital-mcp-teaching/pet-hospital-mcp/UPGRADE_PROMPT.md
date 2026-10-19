# UPGRADE_PROMPT：从 SDK 1.x FastMCP 升级到 SDK 2.x MCPServer 的任务约束

本文件记录本次重写（升级）的硬性约束，供教学对照与后续阶段延续。注意：本次交付是
**从零新建**的独立服务，不包含、不兼容、不迁移任何 SDK 1.x / FastMCP 代码。

## 技术栈（不可降级）

- Python 3.11+；
- 固定 `mcp==2.0.0`；
- MCP 协议版本 `2026-07-28`；
- 高层服务器只能是 `from mcp.server import MCPServer`；
- **禁止**导入或使用 `mcp.server.fastmcp.FastMCP`（2.x 已无该模块）；
- HTTP 框架为 SDK 装配出的 Starlette 应用，主机用 uvicorn。

## 传输模型：无状态 Streamable HTTP

- `streamable_http_app()` / `mcp.run(transport="streamable-http")` 必须带
  `stateless_http=True`、`json_response=True`；
- **不得**实现旧协议 `initialize` 握手；
- **不得**要求、生成或返回 `Mcp-Session-Id`；
- **不得**实现会话存储、会话过期、`max_sessions` 或有状态 SSE 恢复；
- 自定义路由（如 `GET /health`）必须在构建 HTTP 应用 / `run()` 之前注册；
- 启动 ASGI 应用时必须进入 SDK 的 session manager 生命周期
  （测试中为 `async with server.session_manager.run():`）。

## 与 Go 后端的边界

- Go 宠物医院服务是唯一业务后端，**不得修改**；
- MCP 服务是独立 Python 进程，只能通过 HTTP 调用 Go REST API；
- 上游地址来自 `PET_HOSPITAL_BASE_URL`（默认 `http://127.0.0.1:8080`）；
- 默认只监听 `127.0.0.1`，`MCP_HOST` / `MCP_PORT` 可配；
- 保留 `/health`；教学场景不实现认证、权限、CORS、Origin 校验。

## 工具与错误约定

- 阶段一只有一个工具：snake_case 的 `list_pets`，严格适配
  `GET /api/v1/pets` 及其 14 个查询参数；
- Pydantic 定义输入 / 成功输出 / 错误模型：真实枚举值、`page>=1`、
  `1<=pageSize<=500`、`min/max` 非负且 `min<=max`，拒绝未知字段、NaN、
  Infinity、布尔冒充数字与错误类型；
- 成功输出对应 Go 成功信封的 `data`：`items/total/page/pageSize/totalPages/totalCost`，
  并兼容 `records`、`charges` 为 `null` 或数组；
- 失败统一信封：`{"error": {"code", "message", "details"}}`，错误码至少包含
  `VALIDATION_ERROR`、`BACKEND_TIMEOUT`、`BACKEND_UNAVAILABLE`、
  `BACKEND_API_ERROR`、`BACKEND_INVALID_RESPONSE`、`INTERNAL_ERROR`；
- 后端调用必须有超时和有限重试；不向客户端暴露 httpx / Pydantic / SDK / Python 堆栈；
- 按 SDK 2.x 机制用 `CallToolResult(is_error=True)` 标记工具失败。

## 日志与测试

- 结构化 JSON 日志至少含 `timestamp`、`tool_name`、`params`、`status`、`duration_ms`；
- `ownerPhone`、`ownerAddr`、`chipNo`（含 snake_case 写法）递归脱敏；
- 测试用 pytest + pytest-asyncio + `httpx.MockTransport`，禁止访问真实 Go 服务；
- 测试命令固定为 `cd pet-hospital-mcp && pytest -q`，必须全部通过；
- 必须覆盖无状态连接流程：不发 `initialize`、无 `Mcp-Session-Id`、
  使用 2026-07-28 的发现（`server/discover`）与调用方式。

## 阶段范围

- **本次只实现 `list_pets`，不得提前实现阶段二工具。**
- 扩展方式：后续在 `src/pet_hospital_mcp/tools/` 增加模块并注册，
  复用 `rest_client`、`errors`、`logging_config`。
