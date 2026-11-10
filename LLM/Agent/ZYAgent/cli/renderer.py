"""控制台前端实现：把 core 事件流渲染为逐行文本。

渲染契约同 ``core/events.py``：Delta 追加、Message 设置（非流式时全文输出）。
"""

from __future__ import annotations

import sys
from typing import Callable, TextIO

from core.engine import AgentEngine
from core.events import (
    AgentError,
    AgentEvent,
    AssistantDelta,
    AssistantMessage,
    CompactFinished,
    CompactStarted,
    ContextWarning,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)

_HELP = """\
命令:
  /help            显示本帮助
  /new [标题]      新建会话（自动保存当前会话）
  /resume <编号>   恢复指定历史会话
  /sessions        列出已保存的历史会话
  /save [标题]     保存当前会话
  /export [路径]   导出当前会话为 Markdown
  /tools           列出可用工具
  /mcp             MCP Server 连接状态
  /agents          查看 AGENTS.md 注入状态
  /status          会话 / 上下文 / MCP 状态
  /compact         压缩上下文（归档原始记录并生成摘要）
  /reset           清空会话上下文
  /quit            退出（也可直接 Ctrl+C）
直接输入问题即可与模型对话。"""


class ConsoleRenderer:
    """把单轮 ``ask()`` 的事件流渲染到终端。"""

    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out or sys.stdout
        # Windows GBK 控制台遇到 emoji 等字符时不崩溃（替换为占位符）
        try:
            self._out.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass
        self._msg_open = False   # 是否正在输出未换行的助手正文
        self._thinking_shown = False  # 是否已显示"正在思考…"

    def _print(self, text: str = "", end: str = "\n", flush: bool = False) -> None:
        print(text, end=end, file=self._out, flush=flush)

    def _close_line(self) -> None:
        if self._msg_open:
            self._print()
            self._msg_open = False

    def handle(self, event: AgentEvent) -> None:
        if isinstance(event, TurnStarted):
            # 生成内容开始时提示"正在思考…"（横排，不换行）
            self._print("正在思考…", end="", flush=True)
            self._thinking_shown = True
            return

        if isinstance(event, AssistantDelta):
            # 首个增量到达：清除"正在思考…"并换行，输出"助手: " + 增量（横排）
            if self._thinking_shown:
                self._print()  # 换行，结束"正在思考…"
                self._thinking_shown = False
            if not self._msg_open:
                self._print("助手: ", end="", flush=True)
                self._msg_open = True
            self._print(event.text, end="", flush=True)
            return

        if isinstance(event, AssistantMessage):
            if self._thinking_shown:
                self._print()  # 换行，结束"正在思考…"
                self._thinking_shown = False
            if self._msg_open:
                self._close_line()
            elif event.text:
                self._print(f"助手: {event.text}")
            return

        if isinstance(event, ToolCallStarted):
            self._close_line()
            self._print(f"[工具 {event.name}] 正在调用…", end="", flush=True)
            self._msg_open = True
            return

        if isinstance(event, ToolCallFinished):
            self._close_line()
            first_line = (event.output or "").splitlines()
            mark = "✓" if event.ok else "✗"
            detail = first_line[0] if first_line else ""
            self._print(f"[{mark} 工具 {event.name}] {detail}")
            return

        if isinstance(event, ContextWarning):
            self._close_line()
            self._print(f"[警告] {event.message}")
            return

        if isinstance(event, CompactStarted):
            self._close_line()
            self._print(f"[压缩] 正在归档原始上下文：{event.archive_id}.jsonl")
            return

        if isinstance(event, CompactFinished):
            self._close_line()
            mark = "✓" if event.ok else "✗"
            self._print(f"[{mark} 压缩] {event.message}")
            return

        if isinstance(event, AgentError):
            self._close_line()
            self._print(f"[错误] {event.message}")
            return

        if isinstance(event, TurnFinished):
            self._close_line()
            return

    def render_turn(self, engine: AgentEngine, question: str) -> None:
        """渲染完整一轮对话。"""
        try:
            for event in engine.ask(question):
                self.handle(event)
        except KeyboardInterrupt:
            self._close_line()
            self._print("[已中断]")


def run_console(engine: AgentEngine, question: str | None = None,
                out: TextIO | None = None) -> int:
    """运行控制台前端：带问题则单轮作答后返回；否则进入交互循环。

    :returns: 进程退出码（0 正常 / 1 配置或运行错误）
    """
    renderer = ConsoleRenderer(out)
    if question:
        renderer.render_turn(engine, question)
        return 0

    print(f"ZYAgent 控制台 · 模型 {engine.config.model} · 工具 {len(engine.registry)} 个")
    print("输入 /help 查看命令，/quit 退出\n")
    while True:
        try:
            line = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "quit", "exit"):
            break
        if line == "/help":
            print(_HELP)
            continue
        if line.startswith("/new"):
            title = line[len("/new"):].strip() or None
            saved = engine.new_session(title)
            print(f"已保存旧会话: {saved}" if saved else "已新建会话")
            continue
        if line.startswith("/save"):
            title = line[len("/save"):].strip() or None
            try:
                print(f"已保存会话: {engine.save_session(title)}")
            except Exception as exc:  # noqa: BLE001
                print(f"保存失败: {type(exc).__name__}: {exc}")
            continue
        if line == "/sessions":
            sessions = engine.list_sessions()
            if not sessions:
                print("暂无已保存会话")
            for index, session in enumerate(sessions, 1):
                print(f"{index}. {session.describe()}")
            continue
        if line.startswith("/resume"):
            sid = line[len("/resume"):].strip()
            if not sid:
                print("用法: /resume <会话编号>（先用 /sessions 查看）")
                continue
            try:
                info = engine.resume_session(sid)
            except Exception as exc:  # noqa: BLE001
                print(f"恢复失败: {type(exc).__name__}: {exc}")
            else:
                print(f"已恢复会话: {info.title}（{info.messages} 条消息）")
            continue
        if line == "/agents":
            print(engine.agents_md_status())
            continue
        if line == "/status":
            usage = engine.context_usage()
            print(f"会话: {engine.session_title or '未命名'}（{engine.session_id}）")
            print(f"上下文: {usage.describe()}（{'API' if usage.source == 'api' else '估算'}）")
            print(f"工具: {len(engine.tools)} 个 · MCP: {engine.mcp.snapshot()['state']}")
            continue
        if line.startswith("/export"):
            target = line[len("/export"):].strip() or None
            try:
                print(f"已导出: {engine.export_markdown(target)}")
            except Exception as exc:  # noqa: BLE001
                print(f"导出失败: {type(exc).__name__}: {exc}")
            continue
        if line == "/tools":
            print("、".join(engine.registry.names))
            continue
        if line == "/mcp":
            print(engine.mcp.status())
            continue
        if line == "/compact":
            for event in engine.compact():
                renderer.handle(event)
            continue
        if line == "/reset":
            engine.reset()
            print("已清空会话上下文")
            continue
        renderer.render_turn(engine, line)
    return 0
