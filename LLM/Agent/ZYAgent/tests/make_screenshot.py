"""生成 TUI 截图（SVG）用于人工查看：python tests/make_screenshot.py"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import LLMConfig  # noqa: E402
from core.engine import AgentEngine  # noqa: E402
from core.llm.base import LLMClient, ReplyArrived, TextDelta  # noqa: E402
from tui.app import ZYAgentApp  # noqa: E402


class ScriptedClient(LLMClient):
    supports_stream = True

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield TextDelta("我先看看目录结构。")
            yield ReplyArrived(message={
                "role": "assistant",
                "content": "我先看看目录结构。",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "listdir", "arguments": '{"path": "."}'},
                }],
            })
        elif self.calls == 2:
            yield TextDelta("再读取 README 看看。")
            yield ReplyArrived(message={
                "role": "assistant",
                "content": "再读取 README 看看。",
                "tool_calls": [{
                    "id": "call_2", "type": "function",
                    "function": {"name": "readFile", "arguments": '{"path": "config.example.ini"}'},
                }],
            })
        else:
            text = ("当前目录是 ZYAgent 项目根目录，包含 core（核心业务层）、tui（终端界面）、"
                    "cli（控制台界面）、main.py（入口）等。README 文件描述了项目结构与用法。")
            for ch in text:
                yield TextDelta(ch)
            yield ReplyArrived(message={"role": "assistant", "content": text})

    def complete(self, messages, tools=None):  # pragma: no cover
        raise AssertionError


async def main() -> None:
    from textual.widgets import Input

    config = LLMConfig(api_key="x", base_url="http://invalid", model="deepseek-chat")
    # 截图不依赖网络：隔离 Skill/MCP 发现（避免启动时连接 .mcp.json 中的真实 Server）
    engine = AgentEngine(config, client=ScriptedClient(),
                         skills_root=Path(os.environ.get("TEMP", ".")) / "zy_shot_no_skills",
                         mcp_root=Path(os.environ.get("TEMP", ".")) / "zy_shot_no_mcp")
    app = ZYAgentApp(engine)
    async with app.run_test(size=(100, 34)) as pilot:
        app.query_one("#prompt", Input).value = "当前目录下有哪些文件？"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if "就绪" in str(app.query_one("#statusbar").content):
                break
        await pilot.pause(0.2)
        svg = app.export_screenshot()
        out = Path(__file__).parent / "tui_preview.svg"
        out.write_text(svg, encoding="utf-8")
        print("saved:", out)


if __name__ == "__main__":
    asyncio.run(main())
