"""Textual TUI 前端：消费 core 事件流渲染聊天界面。

依赖规则：只允许 ``import core.*``，不得被 core 反向依赖。

线程/并发模型：core 的 ``AgentEngine.ask()`` 是阻塞式同步迭代器，
本前端在异步 worker 中用 ``asyncio.to_thread`` **逐事件步进**，
既不阻塞 Textual 事件循环（界面始终可交互/可中断），又无需线程回调 UI。

MCP 采用异步加载：引擎先在后台线程连接 Server，本前端定时读取
``engine.mcp.snapshot()`` 的 ``revision``，变化时动态刷新侧栏 MCP 区域。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Group
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option

from core.context import format_tokens_k
from core.engine import AGENTS_MD_PREFIX, AgentEngine
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
from core.history import SessionInfo

from .widgets import MessageBubble, ToolCard

APP_NAME = "ZYAgent"


# ====================================================================== 命令定义
@dataclass(frozen=True)
class CommandSpec:
    """一条斜杠命令的元信息（帮助弹窗、补全面板、分发共用）。"""

    name: str
    description: str
    usage: str = ""
    takes_args: bool = False


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("/new", "新建会话（自动保存当前会话）", "[标题]", True),
    CommandSpec("/resume", "恢复历史会话（↑↓ 选择，Enter 确认，Esc 取消）"),
    CommandSpec("/save", "保存当前会话", "[标题]", True),
    CommandSpec("/sessions", "列出已保存的历史会话"),
    CommandSpec("/compact", "压缩上下文（归档原始记录并生成摘要）"),
    CommandSpec("/clear", "清空界面（保留历史）"),
    CommandSpec("/tools", "列出已注册工具"),
    CommandSpec("/mcp", "MCP Server 连接状态"),
    CommandSpec("/model", "显示模型与接口"),
    CommandSpec("/status", "显示会话 / 上下文 / MCP 状态"),
    CommandSpec("/agents", "查看 AGENTS.md 注入状态"),
    CommandSpec("/cwd", "显示工作目录"),
    CommandSpec("/export", "导出当前会话为 Markdown", "[路径]", True),
    CommandSpec("/help", "打开本帮助"),
    CommandSpec("/quit", "退出"),
)

#: 斜杠命令表（帮助弹窗与命令分发共用一份定义）
COMMANDS: dict[str, str] = {spec.name: spec.description for spec in COMMAND_SPECS}

#: 迭代结束哨兵
_SENTINEL = object()


# ====================================================================== 输入框
class PromptInput(Input):
    """输入框：斜杠命令补全面板可见时，优先处理 ↑↓ / Tab / Enter / Esc。"""

    async def _on_key(self, event: events.Key) -> None:
        handler = getattr(self.app, "handle_palette_key", None)
        if handler is not None and handler(event.key):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


# ====================================================================== 帮助弹窗
class HelpScreen(ModalScreen[None]):
    """帮助弹窗：使用方式 / 命令表 / 快捷键。Esc 或回车关闭。"""

    BINDINGS = [
        Binding("escape", "close", "关闭", show=False),
        Binding("enter", "close", "关闭", show=False),
    ]

    def compose(self) -> ComposeResult:
        table = Table(
            show_header=True, header_style="bold cyan",
            border_style="dim", box=None, pad_edge=False,
        )
        table.add_column("命令", style="bold yellow", no_wrap=True)
        table.add_column("作用")
        for cmd, desc in COMMANDS.items():
            table.add_row(cmd, desc)

        shortcuts = Table(border_style="dim", box=None, pad_edge=False)
        shortcuts.add_column("快捷键", style="bold cyan", no_wrap=True)
        shortcuts.add_column("作用")
        for key, desc in (
            ("Enter", "发送问题 / 执行选中的斜杠命令"),
            ("↑ / ↓", "在斜杠命令补全面板中选择"),
            ("Tab", "补全选中的斜杠命令"),
            ("Esc", "关闭补全 / 中断当前生成"),
            ("Ctrl+L", "清空界面（保留历史）"),
            ("Ctrl+N", "新建会话（自动保存当前会话）"),
            ("Ctrl+S", "保存当前会话"),
            ("Ctrl+B", "显示/隐藏侧栏"),
            ("Ctrl+D", "切换深色 / 浅色模式"),
            ("Ctrl+Q", "退出"),
        ):
            shortcuts.add_row(key, desc)

        usage = Text()
        usage.append("使用方式\n\n", style="bold underline cyan")
        for line in (
            "在底部输入框输入问题，Enter 发送。",
            "输入 / 会弹出命令补全面板：↑↓ 选择，Tab 补全，Enter 执行。",
            "模型需要查看/修改文件时会自动调用工具，工具卡片可点击展开。",
            "项目根存在 AGENTS.md 时，首次加载会注入会话并全程复用。",
        ):
            usage.append("• ", style="bold")
            usage.append(line + "\n")

        body = Group(
            Text(f"{APP_NAME} 帮助\n", style="bold"),
            usage,
            Text("命令\n", style="bold underline cyan"),
            table,
            Text("\n快捷键\n", style="bold underline cyan"),
            shortcuts,
        )
        with Vertical(id="help-box"):
            yield Static(body)
            yield Static("Esc / Enter 关闭", classes="hint", markup=True)

    def action_close(self) -> None:
        self.dismiss()


# ====================================================================== 恢复会话
class ResumeScreen(ModalScreen[str | None]):
    """历史会话列表面板：↑↓ 选择、Enter 恢复、Esc 取消。"""

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]

    def __init__(self, sessions: list[SessionInfo]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        options = [
            Option(
                f"{session.title}  ·  {session.updated_at}  ·  "
                f"{session.messages} 条消息  ·  {session.id}",
                id=session.id,
            )
            for session in self.sessions
        ]
        with Vertical(id="resume-box"):
            yield Static(
                "恢复历史会话\n↑↓ 选择 · Enter 恢复 · Esc 取消",
                classes="resume-title", markup=False,
            )
            yield OptionList(*options, id="session-list")

    def on_mount(self) -> None:
        options = self.query_one("#session-list", OptionList)
        options.focus()
        if options.option_count:
            options.highlighted = 0

    @on(OptionList.OptionSelected, "#session-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ====================================================================== 配置提示
class ConfigNoticeApp(App):
    """首次生成 config.ini 后的提示对话框（按 Enter / Esc 退出）。"""

    CSS = """
    Screen {
        align: center middle;
        background: $surface;
    }
    #config-box {
        width: 76;
        max-width: 92%;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $warning;
    }
    #config-box .hint {
        color: $text-muted;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "退出", show=False),
        Binding("enter", "dismiss", "退出", show=False),
    ]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.config_path = Path(path)

    def compose(self) -> ComposeResult:
        body = Text()
        body.append("已生成配置文件\n\n", style="bold underline yellow")
        body.append(f"{self.config_path}\n\n", style="bold")
        body.append(
            "当前文件里是 mock 配置，需要修改后才能正常工作：\n"
            "  1. 打开 config.ini；\n"
            "  2. 把 [llm] 的 api_key / base_url / model 改成真实值；\n"
            "  3. 重新启动 ZYAgent。\n\n"
            "其余参数（上下文阈值、MCP 超时、TUI 行为、会话保存等）"
            "也都在同一个文件里，可按需调整。",
        )
        with Vertical(id="config-box"):
            yield Static(body)
            yield Static("按 Enter / Esc 退出", classes="hint")

    def action_dismiss(self) -> None:
        self.exit()


def show_config_notice(path: str | Path) -> None:
    """显示「已生成 config.ini」对话框（供 main.py 调用）。"""
    ConfigNoticeApp(Path(path)).run()


# ====================================================================== 主应用
class ZYAgentApp(App):
    """ZYAgent 的 Textual 应用。"""

    CSS_PATH = "styles.tcss"
    TITLE = APP_NAME
    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", show=True),
        Binding("ctrl+l", "clear_view", "清屏", show=True),
        Binding("ctrl+n", "new_session", "新会话", show=True),
        Binding("ctrl+s", "save_session", "保存", show=True),
        Binding("ctrl+b", "toggle_sidebar", "侧栏", show=True),
        Binding("ctrl+d", "toggle_dark", "深浅模式", show=True),
        Binding("f1", "help", "帮助", show=True),
        Binding("escape", "interrupt", "中断", show=True),
    ]

    def __init__(self, engine: AgentEngine, initial_question: str | None = None) -> None:
        super().__init__()
        self.engine = engine
        self._initial_question = initial_question
        self.dark = False  # 默认浅色模式
        self._busy = False
        self._cancelled = False
        #: 当前正在接收流式文本的助手气泡（None 表示下一条消息开新气泡）
        self._assistant_bubble: MessageBubble | None = None
        self._tool_cards: dict[str, ToolCard] = {}
        self._last_scroll = 0.0
        # 会话统计（仅展示用，不进 core）
        self._turns = 0
        self._model_calls = 0
        self._tool_calls_total = 0
        self._last_duration = 0.0
        self._turn_started = 0.0
        # 斜杠命令补全状态
        self._palette_matches: list[CommandSpec] = []
        self._palette_index = 0
        self._palette_query = ""
        self._palette_visible = False
        # 侧栏动态刷新（MCP 异步加载）
        self._mcp_revision = -1
        self._tool_names = [s["function"]["name"] for s in self.engine.tools]

    # ------------------------------------------------------------ 视图组装
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield VerticalScroll(id="chat")
            with VerticalScroll(id="sidebar"):
                yield Static(f"{APP_NAME} · {len(self.engine.registry)} 个工具",
                             id="side-head", markup=False)
                yield Static(self._sidebar_stats("就绪"), id="side-stats")
                yield Static("MCP", id="side-mcp-title", markup=False)
                yield Static("加载中…", id="mcp-area", markup=True)
                yield Static("工具", id="side-tools-title", markup=False)
                with Vertical(id="tool-list"):
                    for schema in self.engine.tools:
                        fn = schema["function"]
                        yield Static(
                            f"[bold]{escape(fn['name'])}[/]  [dim]{escape(fn['description'])}[/]",
                            classes="tool-item", markup=True,
                        )
        yield Static("", id="palette", markup=True)
        yield Static(self._context_text(), id="contextbar", markup=True)
        yield PromptInput(
            placeholder="输入问题后 Enter 发送；输入 / 查看命令补全", id="prompt"
        )
        yield Static(self._status_text("就绪"), id="statusbar", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.engine.config.model} · {os.getcwd()}"
        try:
            self.query_one("#sidebar").styles.width = self.engine.ui_config.sidebar_width
            self.query_one("#palette").styles.max_height = self.engine.ui_config.palette_max_rows
        except Exception:  # noqa: BLE001 - 样式设置失败不影响功能
            pass
        self.query_one("#prompt", Input).focus()
        self._refresh_context()
        self._refresh_sidebar_dynamic(force=True)
        self.set_interval(0.35, self._poll_mcp)
        if self._initial_question:
            self._send(self._initial_question)

    # ------------------------------------------------------------ 交互入口
    @on(Input.Submitted, "#prompt")
    def _on_submit(self, event: Input.Submitted) -> None:
        self._send(event.value)

    @on(Input.Changed, "#prompt")
    def _on_prompt_changed(self, event: Input.Changed) -> None:
        self._refresh_palette(event.value)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_interrupt(self) -> None:
        """Esc：优先关闭补全面板；否则请求停止当前生成。"""
        if self._palette_visible:
            self._hide_palette()
            return
        if not self._busy:
            return
        self._cancelled = True
        self._set_status("停止中…", busy=True)

    def action_toggle_sidebar(self) -> None:
        self.query_one("#sidebar").toggle_class("hidden")

    def action_toggle_dark(self) -> None:
        """Ctrl+D：切换深色 / 浅色主题。"""
        self.dark = not self.dark
        mode = "深色" if self.dark else "浅色"
        self._set_status(f"已切换 {mode} 模式", busy=False)

    def action_clear_view(self) -> None:
        """Ctrl+L / /clear：只清空界面，保留会话历史。"""
        self._hide_palette()
        self._clear_view()
        self._set_status("就绪")

    def action_new_session(self) -> None:
        """Ctrl+N：新建会话（先自动保存当前会话）。"""
        self._new_session(None)

    def action_save_session(self) -> None:
        """Ctrl+S：保存当前会话。"""
        self._save_session(None)

    def action_resume(self) -> None:
        """打开历史会话列表面板。"""
        if self._busy:
            self.notify("生成中暂不能恢复会话，可先按 Esc", severity="warning")
            return
        sessions = self.engine.list_sessions()
        if not sessions:
            self._append_info("暂无已保存会话。使用 /save 保存当前会话。")
            return

        def _restore(session_id: str | None) -> None:
            if not session_id:
                return
            try:
                info = self.engine.resume_session(session_id)
            except Exception as exc:  # noqa: BLE001 - 损坏文件等
                self._append_info(
                    f"恢复会话失败：{type(exc).__name__}: {exc}", error=True
                )
                return
            self._render_history()
            self._refresh_panels("就绪")
            self._refresh_sidebar_dynamic(force=True)
            self.notify(f"已恢复会话：{info.title}")

        self.push_screen(ResumeScreen(sessions), _restore)

    def _send(self, question: str) -> None:
        question = (question or "").strip()
        if not question:
            return
        self._hide_palette()
        self.query_one("#prompt", Input).value = ""
        if question.startswith("/"):
            self._run_command(question)
            return
        if self._busy:
            self.notify("上一轮还在生成中…（Esc 可中断）", severity="warning")
            return
        if self.engine.input_blocked:
            percent = int(self.engine.context_config.warning_ratio * 100)
            self._append_info(
                f"上下文占用已达 {percent}%，已暂停接收新的提示词。"
                "请输入 /compact 压缩上下文后继续。",
                error=True,
            )
            self.notify(f"上下文已达 {percent}%，请输入 /compact", severity="warning")
            return
        self._append_user(question)
        self._cancelled = False
        self._set_busy(True)
        self._ask(question)

    # ------------------------------------------------------------ 斜杠命令
    def _run_command(self, text: str) -> None:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            self.action_help()
        elif cmd == "/clear":
            self.action_clear_view()
        elif cmd == "/new":
            self._new_session(arg or None)
        elif cmd == "/resume":
            self.action_resume()
        elif cmd == "/save":
            self._save_session(arg or None)
        elif cmd == "/sessions":
            self._list_sessions()
        elif cmd == "/compact":
            self._compact()
        elif cmd == "/quit":
            self.exit()
        elif cmd == "/cwd":
            self._append_info(f"工作目录\n{os.getcwd()}")
        elif cmd == "/model":
            cfg = self.engine.config
            self._append_info(f"模型 {cfg.model}\n接口 {cfg.base_url}")
        elif cmd == "/status":
            self._append_info(self._status_report())
        elif cmd == "/agents":
            self._append_info(self.engine.agents_md_status())
        elif cmd == "/tools":
            lines = [f"{s['function']['name']} — {s['function']['description']}"
                     for s in self.engine.tools]
            self._append_info("已注册工具（%d 个）\n%s" % (len(lines), "\n".join(lines)))
        elif cmd == "/mcp":
            self._append_info(self.engine.mcp.status())
        elif cmd == "/export":
            if self._busy:
                self.notify("生成中暂不能导出，可先按 Esc", severity="warning")
                return
            try:
                path = self.engine.export_markdown(arg or None)
            except Exception as exc:  # noqa: BLE001
                self._append_info(f"导出失败：{type(exc).__name__}: {exc}", error=True)
            else:
                self.notify(f"已导出：{path}")
                self._append_info(f"会话已导出到\n{path}")
        else:
            self._append_info(f"未知命令 {cmd}，输入 /help 查看帮助", error=True)

    def _new_session(self, title: str | None) -> None:
        if self._busy:
            self.notify("生成中暂不能新建会话，可先按 Esc", severity="warning")
            return
        try:
            saved = self.engine.new_session(title)
        except Exception as exc:  # noqa: BLE001
            self._append_info(f"新建会话失败：{type(exc).__name__}: {exc}", error=True)
            return
        self._clear_view()
        self._turns = self._model_calls = self._tool_calls_total = 0
        self._last_duration = 0.0
        self._refresh_panels("就绪")
        self._refresh_sidebar_dynamic(force=True)
        self.notify(f"已保存旧会话：{saved.name}" if saved else "已新建会话")

    def _save_session(self, title: str | None) -> None:
        if self._busy:
            self.notify("生成中暂不能保存，可先按 Esc", severity="warning")
            return
        try:
            path = self.engine.save_session(title)
        except Exception as exc:  # noqa: BLE001
            self._append_info(f"保存失败：{type(exc).__name__}: {exc}", error=True)
            return
        self.notify(f"已保存会话：{path.name}")

    def _list_sessions(self) -> None:
        sessions = self.engine.list_sessions()
        if not sessions:
            self._append_info("暂无已保存会话。使用 /save 保存当前会话。")
            return
        lines = [f"{index}. {session.describe()}"
                 for index, session in enumerate(sessions, 1)]
        self._append_info(
            "已保存会话（%d 个，新 → 旧）\n%s\n\n使用 /resume 选择恢复。"
            % (len(sessions), "\n".join(lines))
        )

    def _status_report(self) -> str:
        cfg = self.engine.config
        usage = self.engine.context_usage()
        snapshot = self.engine.mcp.snapshot()
        return (
            f"会话  {self.engine.session_title or '未命名'}（{self.engine.session_id}）\n"
            f"模型  {cfg.model}\n"
            f"接口  {cfg.base_url}\n"
            f"上下文  {usage.describe()}（{'API' if usage.source == 'api' else '估算'}）\n"
            f"阈值  {int(self.engine.context_config.warning_ratio * 100)}%\n"
            f"MCP  {snapshot['state']} · {len(snapshot['servers'])} 个 Server\n"
            f"工具  {len(self.engine.tools)} 个\n"
            f"目录  {os.getcwd()}"
        )

    # ------------------------------------------------------------ 命令补全
    def _refresh_palette(self, value: str | None = None) -> None:
        """根据输入框内容显示/更新斜杠命令补全面板。"""
        try:
            palette = self.query_one("#palette", Static)
            prompt = self.query_one("#prompt", Input)
        except Exception:  # noqa: BLE001 - compose 阶段控件可能尚未挂载
            return
        text = prompt.value if value is None else value
        query = (text or "").strip()
        if not query.startswith("/") or " " in query:
            self._hide_palette()
            return

        matches = [spec for spec in COMMAND_SPECS if spec.name.startswith(query.lower())]
        if query != self._palette_query:
            self._palette_query = query
            self._palette_index = 0
        self._palette_matches = matches
        if not matches:
            self._hide_palette()
            return
        if self._palette_index >= len(matches):
            self._palette_index = 0

        lines: list[str] = []
        limit = max(1, self.engine.ui_config.palette_max_rows)
        for index, spec in enumerate(matches[:limit]):
            marker = "▶" if index == self._palette_index else " "
            name = escape(spec.name)
            desc = escape(spec.description)
            usage = f" [dim]{escape(spec.usage)}[/]" if spec.usage else ""
            if index == self._palette_index:
                lines.append(f"[bold reverse]{marker} {name}[/] [dim]{desc}[/]{usage}")
            else:
                lines.append(f"{marker} [bold]{name}[/] [dim]{desc}[/]{usage}")
        palette.update("\n".join(lines))
        palette.display = True
        self._palette_visible = True

    def _hide_palette(self) -> None:
        self._palette_matches = []
        self._palette_index = 0
        self._palette_query = ""
        self._palette_visible = False
        try:
            self.query_one("#palette", Static).display = False
        except Exception:  # noqa: BLE001
            pass

    def _apply_palette(self, run: bool) -> None:
        if not self._palette_matches:
            return
        spec = self._palette_matches[self._palette_index]
        if run:
            self._hide_palette()
            self.query_one("#prompt", Input).value = ""
            self._send(spec.name)
            return
        prompt = self.query_one("#prompt", Input)
        prompt.value = spec.name + (" " if spec.takes_args else "")
        prompt.cursor_position = len(prompt.value)
        self._refresh_palette(prompt.value)

    def handle_palette_key(self, key: str) -> bool:
        """输入框按键回调：返回 True 表示已被补全面板消费。"""
        if not self._palette_visible or not self._palette_matches:
            return False
        if key in ("up", "down"):
            count = len(self._palette_matches)
            step = 1 if key == "down" else -1
            self._palette_index = (self._palette_index + step) % count
            self._refresh_palette()
            return True
        if key == "tab":
            self._apply_palette(run=False)
            return True
        if key == "enter":
            self._apply_palette(run=True)
            return True
        if key == "escape":
            self._hide_palette()
            return True
        return False

    # ------------------------------------------------------------ 核心桥接
    @work(exclusive=True, group="ask")
    async def _ask(self, question: str) -> None:
        """在后台逐步消费 core 事件流，事件回到 UI 线程渲染。"""
        stream = self.engine.ask(question, is_cancelled=lambda: self._cancelled)
        try:
            while True:
                event: AgentEvent | object = await asyncio.to_thread(next, stream, _SENTINEL)
                if event is _SENTINEL:
                    break
                self._handle_event(event)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - 前端兜底，绝不白屏
            self._handle_event(AgentError(f"内部错误: {type(exc).__name__}: {exc}"))
        finally:
            self._set_busy(False)
            self._refresh_context()
            self._refresh_sidebar_dynamic()

    @work(exclusive=True, group="ask")
    async def _compact(self) -> None:
        """在后台消费 engine.compact() 事件流（/compact）。"""
        if self._busy:
            self.notify("上一轮还在生成中…（Esc 可中断）", severity="warning")
            return
        self._cancelled = False
        self._set_busy(True)
        stream = self.engine.compact(is_cancelled=lambda: self._cancelled)
        try:
            while True:
                event: AgentEvent | object = await asyncio.to_thread(next, stream, _SENTINEL)
                if event is _SENTINEL:
                    break
                self._handle_event(event)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - 前端兜底，绝不白屏
            self._handle_event(AgentError(f"内部错误: {type(exc).__name__}: {exc}"))
        finally:
            self._set_busy(False)
            if self.engine.input_blocked:
                self._refresh_panels("需压缩")
            else:
                self._refresh_context()

    # ------------------------------------------------------------ 事件渲染
    def _handle_event(self, event: AgentEvent) -> None:
        """按 core/events.py 的渲染契约分发事件。"""
        if isinstance(event, TurnStarted):
            self._turn_started = time.monotonic()
            self._set_status("思考中…", busy=True)

        elif isinstance(event, AssistantDelta):
            bubble = self._ensure_assistant_bubble()
            bubble.set_streaming(True)
            bubble.append_text(event.text)
            self._scroll_chat()

        elif isinstance(event, AssistantMessage):
            self._model_calls += 1
            if event.text:
                bubble = self._ensure_assistant_bubble()
                bubble.set_streaming(False)
                bubble.set_text(event.text)
            self._scroll_chat(force=True)
            self._refresh_panels("思考中…", busy=True)

        elif isinstance(event, ToolCallStarted):
            self._assistant_bubble = None  # 之后的助手消息另起气泡
            card = ToolCard(
                event.id, event.name, event.arguments,
                args_max=self.engine.ui_config.tool_args_preview,
            )
            self._tool_cards[event.id] = card
            self.query_one("#chat", VerticalScroll).mount(card)
            self._set_status(f"运行工具 {event.name} …", busy=True)
            self._scroll_chat(force=True)

        elif isinstance(event, ToolCallFinished):
            self._tool_calls_total += 1
            card = self._tool_cards.get(event.id)
            if card is not None:
                card.finish(event.ok, event.output, event.duration_ms)
            self._set_status("思考中…", busy=True)
            self._scroll_chat(force=True)

        elif isinstance(event, ContextWarning):
            self._append_info(f"⚠ {event.message}", error=True)
            self.notify("上下文已达阈值，请执行 /compact", severity="warning", timeout=8)
            self._refresh_context()

        elif isinstance(event, CompactStarted):
            self._assistant_bubble = None
            self._set_status(f"压缩上下文（归档 {event.archive_id}）…", busy=True)
            self._refresh_context()

        elif isinstance(event, CompactFinished):
            self._assistant_bubble = None
            self._append_info(event.message, error=not event.ok)
            if event.ok:
                state = "就绪"
            else:
                state = "需压缩" if self.engine.input_blocked else "压缩失败"
            self._refresh_panels(state)

        elif isinstance(event, AgentError):
            self._assistant_bubble = None
            self.query_one("#chat", VerticalScroll).mount(
                MessageBubble("error", "错误", f"⚠ {event.message}")
            )
            self._scroll_chat(force=True)

        elif isinstance(event, TurnFinished):
            self._assistant_bubble = None
            self._turns += 1
            if self._turn_started:
                self._last_duration = time.monotonic() - self._turn_started
            state = "已停止" if self._cancelled else (
                "需压缩" if self.engine.input_blocked else "就绪"
            )
            self._refresh_panels(state)

    # ------------------------------------------------------------ 视图辅助
    def _append_user(self, text: str) -> None:
        row = Horizontal(MessageBubble("user", "你", text), classes="msg-row user")
        self.query_one("#chat", VerticalScroll).mount(row)
        self._scroll_chat(force=True)

    def _append_info(self, text: str, error: bool = False) -> None:
        role = "error" if error else "info"
        label = "错误" if error else "提示"
        self.query_one("#chat", VerticalScroll).mount(MessageBubble(role, label, text))
        self._scroll_chat(force=True)

    def _ensure_assistant_bubble(self) -> MessageBubble:
        if self._assistant_bubble is None:
            self._assistant_bubble = MessageBubble("assistant", APP_NAME)
            self.query_one("#chat", VerticalScroll).mount(self._assistant_bubble)
        return self._assistant_bubble

    def _clear_view(self) -> None:
        self.query_one("#chat", VerticalScroll).remove_children()
        self._assistant_bubble = None
        self._tool_cards.clear()

    def _render_history(self) -> None:
        """把恢复的会话历史渲染到聊天区（跳过 system / AGENTS.md / 工具细节）。"""
        self._clear_view()
        turns = model_calls = tool_calls = 0
        chat = self.query_one("#chat", VerticalScroll)
        for message in self.engine.session.messages:
            role = message.get("role")
            content = str(message.get("content") or "")
            if role == "system" or content.startswith(AGENTS_MD_PREFIX):
                continue
            if role == "user":
                if content.strip():
                    turns += 1
                    chat.mount(Horizontal(
                        MessageBubble("user", "你", content), classes="msg-row user"
                    ))
            elif role == "assistant":
                model_calls += 1
                if content.strip():
                    chat.mount(MessageBubble("assistant", APP_NAME, content))
            elif role == "tool":
                tool_calls += 1
        self._turns, self._model_calls, self._tool_calls_total = (
            turns, model_calls, tool_calls
        )
        self._assistant_bubble = None
        self._scroll_chat(force=True)

    def _scroll_chat(self, force: bool = False) -> None:
        """自动跟随底部（用户向上翻阅时不打扰）。"""
        chat = self.query_one("#chat", VerticalScroll)
        now = time.monotonic()
        near_bottom = chat.scroll_y >= chat.max_scroll_y - 8
        if (force or now - self._last_scroll >= self.engine.ui_config.scroll_throttle) and near_bottom:
            self._last_scroll = now
            chat.scroll_end(animate=False, immediate=True, x_axis=False)

    # ------------------------------------------------------------ 侧栏动态刷新
    def _poll_mcp(self) -> None:
        self._refresh_sidebar_dynamic()

    def _refresh_sidebar_dynamic(self, force: bool = False) -> None:
        """MCP 异步加载完成后刷新 MCP 区域与工具清单（revision 未变则跳过）。"""
        revision = self.engine.mcp.revision
        if not force and revision == self._mcp_revision:
            return
        self._mcp_revision = revision
        try:
            self.query_one("#mcp-area", Static).update(self._mcp_text())
            self.query_one("#side-head", Static).update(
                f"{APP_NAME} · {len(self.engine.registry)} 个工具"
            )
        except Exception:  # noqa: BLE001 - compose 阶段控件可能尚未挂载
            return

        names = [schema["function"]["name"] for schema in self.engine.tools]
        if names == self._tool_names:
            return
        self._tool_names = names
        try:
            tool_list = self.query_one("#tool-list", Vertical)
            tool_list.remove_children()
            tool_list.mount_all([
                Static(
                    f"[bold]{escape(schema['function']['name'])}[/]  "
                    f"[dim]{escape(schema['function']['description'])}[/]",
                    classes="tool-item", markup=True,
                )
                for schema in self.engine.tools
            ])
        except Exception:  # noqa: BLE001
            pass

    def _mcp_text(self) -> str:
        snapshot = self.engine.mcp.snapshot()
        servers = snapshot["servers"]
        if not snapshot["started"]:
            return "[dim]未启用[/]"
        if snapshot["state"] == "loading" and not servers:
            return "[yellow]加载中…[/]"
        if not servers and not snapshot["errors"]:
            return f"[dim]未配置 {escape(snapshot['config_name'])}[/]"

        lines: list[str] = []
        for server in servers:
            name = escape(str(server["name"]))
            detail = escape(str(server["detail"]))
            if server["kind"] == "ok":
                lines.append(f"[green]●[/] [bold]{name}[/] [dim]{detail}[/]")
                for tool in server["tools"]:
                    lines.append(f"  [dim]· {escape(str(tool))}[/]")
            elif server["kind"] == "pending":
                lines.append(f"[yellow]○[/] [bold]{name}[/] [dim]{detail}[/]")
            else:
                lines.append(f"[red]✗[/] [bold]{name}[/] [red]{detail}[/]")
        for error in snapshot["errors"]:
            lines.append(f"[yellow]⚠ {escape(str(error))}[/]")
        return "\n".join(lines)

    # ------------------------------------------------------------ 状态面板
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if not busy:
            self._cancelled = False
            self.query_one("#prompt", Input).focus()

    def _refresh_panels(self, state: str, busy: bool = False) -> None:
        self._set_status(state, busy=busy)
        self.query_one("#side-stats", Static).update(self._sidebar_stats(state))
        self._refresh_context()

    def _set_status(self, state: str, busy: bool = False) -> None:
        self.query_one("#statusbar", Static).update(self._status_text(state, busy))

    def _refresh_context(self) -> None:
        """刷新上下文占用条与输入框提示（压缩/阈值状态变化时调用）。"""
        try:
            self.query_one("#contextbar", Static).update(self._context_text())
            prompt = self.query_one("#prompt", Input)
        except Exception:  # noqa: BLE001 - compose 阶段控件可能尚未挂载
            return
        percent = int(self.engine.context_config.warning_ratio * 100)
        if self.engine.compacting:
            prompt.placeholder = "正在压缩上下文，请稍候…"
        elif self.engine.input_blocked:
            prompt.placeholder = f"上下文已达 {percent}%，请输入 /compact 压缩后继续"
        else:
            prompt.placeholder = "输入问题后 Enter 发送；输入 / 查看命令补全"

    def _context_text(self) -> str:
        """上下文占用条文本：``上下文 12.3k / 128k · 已占用 9.6%``。"""
        usage = self.engine.context_usage()
        cfg = self.engine.config
        source = "API" if usage.source == "api" else "估算"
        default_note = "" if cfg.context_window_configured else "，窗口未知，默认 128k"
        body = (
            f"上下文 {format_tokens_k(usage.used)} / {format_tokens_k(usage.window)}"
            f" · 已占用 {usage.percent:.1f}%（{source}{default_note}）"
        )
        if self.engine.input_blocked or usage.blocked:
            return f"[bold red]⚠ {body} · 请执行 /compact 压缩[/]"
        if usage.percent >= 60:
            return f"[yellow]{body}[/]"
        return f"[dim]{body}[/]"

    def _status_text(self, state: str, busy: bool = False) -> str:
        dot = "[yellow]●[/]" if busy else "[green]●[/]"
        elapsed = ""
        if busy and self._turn_started:
            elapsed = f" · [dim]{time.monotonic() - self._turn_started:.1f}s[/]"
        elif self._last_duration:
            elapsed = f" · [dim]{self._last_duration:.1f}s[/]"
        return (
            f"{dot} [bold]{escape(state)}[/]"
            f" · [dim]{self._turns} 轮[/]"
            f" · {escape(self.engine.config.model)}"
            f" · [dim]工具 {self._tool_calls_total} 次[/]"
            f"{elapsed}"
            f" · [dim]Esc 中断 · F1 帮助[/]"
        )

    def _sidebar_stats(self, state: str) -> str:
        cfg = self.engine.config
        usage = self.engine.context_usage()
        window_note = "" if cfg.context_window_configured else "（默认128k）"
        return (
            f"状态  {escape(state)}\n"
            f"轮次  {self._turns}\n"
            f"模型调用  {self._model_calls}\n"
            f"工具调用  {self._tool_calls_total}\n"
            f"上次耗时  {self._last_duration:.1f} s\n"
            f"上下文  {usage.describe()}{window_note}\n"
            f"会话  {escape(self.engine.session_title or '未命名')}\n"
            f"模型  {escape(cfg.model)}\n"
            f"接口  {escape(cfg.base_url)}\n"
            f"目录  {escape(os.getcwd())}"
        )


def run_app(engine: AgentEngine, initial_question: str | None = None) -> None:
    """启动 TUI（供 main.py 调用）。"""
    ZYAgentApp(engine, initial_question=initial_question).run()
