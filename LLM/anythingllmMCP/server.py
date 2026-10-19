import os
import httpx
from mcp.server import MCPServer

mcp = MCPServer("anythingllm")

ANYTHINGLLM_BASE = os.getenv("ANYTHINGLLM_BASE", "http://localhost:3001/api/v1")
ANYTHINGLLM_API_KEY = os.getenv("ANYTHINGLLM_API_KEY", "你的api_key")
DEFAULT_WORKSPACE = os.getenv("ANYTHINGLLM_WORKSPACE", "310d670a-98c4-47e1-bbfa-e1a8c095ef92")


def _headers():
    return {
        "Authorization": f"Bearer {ANYTHINGLLM_API_KEY}",
        "Content-Type": "application/json",
    }


@mcp.tool()
async def list_workspaces() -> str:
    """列出所有可用的 AnythingLLM 工作区，返回 slug 列表。"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{ANYTHINGLLM_BASE}/workspaces", headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    workspaces = data.get("workspaces", [])
    if not workspaces:
        return "没有找到任何工作区"
    lines = [f"- {w.get('name')} (slug: {w.get('slug')})" for w in workspaces]
    return "可用工作区:\n" + "\n".join(lines)


@mcp.tool()
async def ask_workspace(message: str, workspace: str = "") -> str:
    """向 AnythingLLM 工作区提问并获取 AI 回答。

    Args:
        message: 要问的问题
        workspace: 工作区 slug（留空则使用默认工作区）
    """
    slug = workspace or DEFAULT_WORKSPACE
    if not slug:
        return "错误：未指定工作区 slug，请先调用 list_workspaces 获取，或设置环境变量 ANYTHINGLLM_WORKSPACE"

    url = f"{ANYTHINGLLM_BASE}/workspace/{slug}/chat"
    payload = {"message": message, "mode": "chat"}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    return data.get("textResponse", str(data))


def main():
    mcp.run(transport="streamable-http", host="0.0.0.0", port=3000)


if __name__ == "__main__":
    main()
