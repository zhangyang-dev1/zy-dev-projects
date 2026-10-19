# AnythingLLM MCP Server

MCP Server，让 AI 助手可以通过 MCP 协议与 AnythingLLM 工作区对话。

## 安装

```bash
cd E:/project/anythingllmMCP
pip install -e .
```

## 配置

环境变量（均可选，有默认值）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANYTHINGLLM_BASE` | `http://localhost:3001` | AnythingLLM 地址 |
| `ANYTHINGLLM_API_KEY` | `你的api_key` | API Key |
| `ANYTHINGLLM_WORKSPACE` | (空) | 默认工作区 slug |

## 运行

```bash
python server.py
```

MCP Server 启动在 `http://localhost:3000/mcp`

## 在 OpenCode 中使用

在 MCP 配置中添加：

```json
{
  "mcpServers": {
    "anythingllm": {
      "url": "http://localhost:3000/mcp"
    }
  }
}
```

## 提供的 Tool

### `list_workspaces`

列出所有可用的工作区，获取 slug。

### `ask_workspace`

向 AnythingLLM 工作区提问。

- `message` (string): 问题内容
- `workspace` (string, 可选): 工作区 slug，留空使用默认工作区
