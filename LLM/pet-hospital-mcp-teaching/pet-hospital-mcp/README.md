# pet-hospital-mcp

把**现有 Go 宠物医院 REST API** 暴露给 AI Agent 的独立 **MCP（Model Context Protocol）服务**。

本服务是教学项目阶段一的交付物：**只有一个只读工具 `list_pets`**，对应 Go 后端的
`GET /api/v1/pets`。它不实现、不兼容、不迁移任何 SDK 1.x / FastMCP 服务。

| 项目 | 实际值 |
| --- | --- |
| Python | 3.11+（开发验证使用 3.13） |
| MCP Python SDK | **`mcp==2.0.0`**（固定） |
| MCP 协议版本 | **`2026-07-28`** |
| 服务器 API | SDK 2.x 的 **`mcp.server.MCPServer`**（**不使用** `mcp.server.fastmcp`） |
| 传输 | **无状态 Streamable HTTP**（`stateless_http=True`，`json_response=True`） |
| MCP 端点 | `POST http://127.0.0.1:8000/mcp` |
| 健康检查 | `GET http://127.0.0.1:8000/health` |
| 业务后端 | Go REST API（唯一数据来源，默认 `http://127.0.0.1:8080`） |

无状态的含义：不进行旧版 `initialize` 握手、不要求也不返回 `Mcp-Session-Id`、
没有会话存储 / 会话过期 / `max_sessions` / 有状态 SSE 恢复。每个请求自包含。

---

## 1. 先启动 Go REST API（必须）

MCP 服务本身**不包含任何业务数据**，只会通过 HTTP 调用 Go 后端。请先在仓库根目录启动
Go 宠物医院服务（默认监听 `127.0.0.1:8080`），并确认：

```bash
curl http://127.0.0.1:8080/api/v1/pets
```

能返回统一信封：

```json
{ "code": 200, "message": "ok", "data": { "items": [], "total": 0, "...": "..." } }
```

> MCP 服务不得与 Go 服务共享数据库或代码；它只能通过 HTTP 访问 Go REST API。

## 2. 安装

```bash
cd pet-hospital-mcp
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

pip install -e ".[dev]"
```

## 3. 配置（环境变量）

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCP_HOST` | `127.0.0.1` | MCP HTTP 监听地址（默认只监听本机回环） |
| `MCP_PORT` | `8000` | MCP HTTP 监听端口 |
| `MCP_PATH` | `/mcp` | MCP Streamable HTTP 端点路径 |
| `PET_HOSPITAL_BASE_URL` | `http://127.0.0.1:8080` | Go REST API 基础地址 |
| `PET_HOSPITAL_TIMEOUT` | `10` | 单次上游调用超时（秒） |
| `PET_HOSPITAL_MAX_RETRIES` | `2` | 连接异常 / 5xx 的有限重试次数（4xx 不重试） |
| `PET_HOSPITAL_RETRY_BACKOFF` | `0.2` | 重试退避基数（秒，指数退避） |
| `LOG_LEVEL` | `INFO` | 日志级别（stderr 单行 JSON） |

教学场景**不实现**认证、权限、CORS 与 Origin 校验。

## 4. 启动 MCP 服务

```bash
# 任选其一（二者等价）
python -m pet_hospital_mcp
pet-hospital-mcp
```

自定义地址示例：

```powershell
$env:MCP_HOST="127.0.0.1"; $env:MCP_PORT="9000"
$env:PET_HOSPITAL_BASE_URL="http://127.0.0.1:8080"
pet-hospital-mcp
```

启动后：

- MCP Streamable HTTP 端点：`http://127.0.0.1:8000/mcp`
- 健康检查：`http://127.0.0.1:8000/health`

## 5. 健康检查

```bash
curl http://127.0.0.1:8000/health
```

响应示例：

```json
{
  "status": "healthy",
  "service": "pet-hospital-mcp",
  "version": "0.1.0",
  "protocolVersion": "2026-07-28",
  "timestamp": "2026-07-28T08:00:00.000000+00:00"
}
```

---

## 6. 唯一工具：`list_pets`

代理 `GET /api/v1/pets`，支持且仅支持与后端一致的 14 个查询参数：

| 参数 | 类型 / 允许值 | 说明 |
| --- | --- | --- |
| `q` | string | 全文检索（跨字段，空格分词 AND） |
| `name` | string | 宠物姓名（模糊） |
| `ownerName` | string | 主人姓名（模糊） |
| `ownerPhone` | string | 主人电话（模糊） |
| `species` | `犬` `猫` `兔` `鸟` `仓鼠` `爬宠` `其他` | 种类精确筛选 |
| `doctor` | string | 主治医生（模糊） |
| `disease` | string | 疾病 / 主要诊断（模糊） |
| `status` | `待就诊` `就诊中` `住院中` `已康复` `慢性病随访` | 状态精确筛选 |
| `min` / `max` | number，非负，且 `min <= max` | 总花费闭区间 |
| `sortBy` | `id` `name` `ownerName` `species` `doctor` `disease` `status` `totalCost` `visitCount` `createdAt` `updatedAt` | 排序字段 |
| `order` | `asc` / `desc` | 排序方向（后端默认 desc） |
| `page` | integer >= 1（默认 1） | 页码 |
| `pageSize` | integer 1–500（默认 20） | 每页条数 |

未知字段、`NaN` / `Infinity`、布尔冒充数字、错误类型一律拒绝（`VALIDATION_ERROR`）。

成功输出对应 Go 成功信封中的 `data`：
`items`、`total`、`page`、`pageSize`、`totalPages`、`totalCost`；
`items[].records` 与 `items[].charges` 兼容后端为 `null` 或数组两种 JSON 表现
（`null` 归一化为空数组）。

### 使用 MCP Inspector 验证

```bash
# 另开终端，在已安装 dev 依赖的 venv 中
npx @modelcontextprotocol/inspector
```

在 Inspector 中选择 **Streamable HTTP** 传输，URL 填：

```
http://127.0.0.1:8000/mcp
```

连接后应能看到：

- 协议版本 `2026-07-28`；
- **没有**会话 ID（无状态）；
- 工具列表中只有 `list_pets`，可查看其 JSON Schema；
- 调用 `list_pets`，例如参数 `{ "status": "住院中", "sortBy": "totalCost", "order": "desc", "page": 1, "pageSize": 10 }`，返回 `items/total/...`。

### 使用 SDK 2.x 客户端（进程内）验证

```python
import asyncio
from mcp import Client
from pet_hospital_mcp.server import build_app, build_server
from pet_hospital_mcp.config import Settings
from httpx import ASGITransport, AsyncClient

async def main():
    settings = Settings()
    server = build_server(settings)
    app = build_app(server, settings)
    transport = ASGITransport(app=app, client=("127.0.0.1", 8000))
    async with server.session_manager.run(), Client(server) as client:
        print(client.protocol_version)          # 2026-07-28
        tools = await client.list_tools()
        print([t.name for t in tools.tools])   # ['list_pets']
        result = await client.call_tool(
            "list_pets", {"q": "张", "page": 1, "pageSize": 5}
        )
        print(result.structured_content)       # {items, total, page, ...}

asyncio.run(main())
```

### 使用原始 HTTP（无状态请求，无需 initialize / 无 Session-Id）

```bash
curl -i http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/call" \
  -H "Mcp-Name: list_pets" \
  -d '{
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
          "name": "list_pets",
          "arguments": { "status": "就诊中", "page": 1, "pageSize": 5 }
        }
      }'
```

要点：

- 响应头中**不存在** `mcp-session-id`；
- 对 `initialize` 的 POST 返回 JSON-RPC 方法不存在错误（现代无状态端点不做旧握手）；
- 发现支持的协议版本：使用 `server/discover`，返回 `supportedVersions` 含 `2026-07-28`。

### 统一错误结构

任何失败都返回 `is_error=true` 的工具结果，`structured_content` 为：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "工具输入参数校验失败，请按 details 中的字段提示修正后重试。",
    "details": { "issues": [ { "field": "page", "type": "greater_than_equal", "message": "Input should be greater than or equal to 1" } ] }
  }
}
```

错误码：`VALIDATION_ERROR`、`BACKEND_TIMEOUT`、`BACKEND_UNAVAILABLE`、
`BACKEND_API_ERROR`、`BACKEND_INVALID_RESPONSE`、`INTERNAL_ERROR`。
不会向客户端暴露 httpx / Pydantic / SDK / Python 的原始堆栈。

---

## 7. 单元测试

测试全部基于 `httpx.MockTransport`，**不会访问真实 Go 服务**。

```bash
cd pet-hospital-mcp
pytest -q
```

预期：全部用例通过（覆盖正常转发全参数、输入校验失败、4xx/5xx、超时、连接异常、
非法 JSON / 数据模型不符、工具注册与 JSON Schema、无状态连接流程、`server/discover`、
`/health`、HTTP 端点发现与调用、日志脱敏）。

---

## 8. 目录结构

```
pet-hospital-mcp/
├── pyproject.toml
├── README.md
├── UPGRADE_PROMPT.md
├── src/pet_hospital_mcp/
│   ├── __init__.py
│   ├── __main__.py        # python -m pet_hospital_mcp
│   ├── config.py          # 环境变量配置
│   ├── server.py          # MCPServer 装配、统一错误兜底、/health、无状态 HTTP
│   ├── rest_client.py     # Go REST 客户端：超时 + 有限重试 + 错误映射
│   ├── errors.py          # 错误码 / 错误信封 / CallToolResult 失败结果
│   ├── logging_config.py  # JSON 日志 + 敏感字段递归脱敏
│   └── tools/
│       ├── __init__.py
│       └── list_pets.py   # 阶段一唯一工具
└── tests/
```

后续阶段新增工具时，只需在 `tools/` 增加模块并在 `tools.register_all` 注册，
复用 REST 客户端、错误约定与日志约定，无需改动传输层。

> **阶段声明：本项目仅实现阶段一工具 `list_pets`，未实现任何阶段二工具。**
