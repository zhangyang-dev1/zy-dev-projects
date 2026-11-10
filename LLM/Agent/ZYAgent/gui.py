"""ZYAgent 图形界面（PySide6）。

单文件实现：浅色/深色主题切换（默认浅色）、消息气泡、可折叠工具卡片、
侧栏工具/MCP 状态、上下文占用条、斜杠命令补全。

运行::

    python main.py                  # 直接启动 GUI
    python main.py --ui gui "你好"  # 启动后自动发送首问
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTextBrowser,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

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

APP_NAME = "ZYAgent"


# ====================================================================== 主题
def _theme_colors(dark: bool) -> dict[str, str]:
    """返回主题色字典。"""
    if dark:
        return {
            "bg": "#0f172a",
            "surface": "#1e293b",
            "surface2": "#334155",
            "border": "#334155",
            "text": "#e2e8f0",
            "text_muted": "#94a3b8",
            "text_dim": "#64748b",
            "primary": "#3b82f6",
            "primary_hover": "#2563eb",
            "user_bubble": "#1e40af",
            "user_bubble_border": "#2563eb",
            "assistant_bubble": "#1e293b",
            "assistant_bubble_border": "#334155",
            "info_bubble": "#1e293b",
            "info_bubble_border": "#475569",
            "error_bubble": "#450a0a",
            "error_bubble_border": "#991b1b",
            "tool_bg": "#1e293b",
            "tool_border": "#334155",
            "link": "#7c9eff",
            "code_bg": "#0f172a",
        }
    return {
        "bg": "#f8fafc",
        "surface": "#ffffff",
        "surface2": "#e2e8f0",
        "border": "#e2e8f0",
        "text": "#1e293b",
        "text_muted": "#64748b",
        "text_dim": "#94a3b8",
        "primary": "#2563eb",
        "primary_hover": "#1d4ed8",
        "user_bubble": "#2563eb",
        "user_bubble_border": "#1d4ed8",
        "assistant_bubble": "#ffffff",
        "assistant_bubble_border": "#e2e8f0",
        "info_bubble": "#f1f5f9",
        "info_bubble_border": "#cbd5e1",
        "error_bubble": "#fef2f2",
        "error_bubble_border": "#fecaca",
        "tool_bg": "#ffffff",
        "tool_border": "#e2e8f0",
        "link": "#2563eb",
        "code_bg": "#f1f5f9",
    }


def _build_stylesheet(dark: bool) -> str:
    c = _theme_colors(dark)
    return f"""
QWidget {{
    background-color: {c['bg']};
    color: {c['text']};
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
}}
QMainWindow {{ background: {c['bg']}; }}

#main-toolbar {{
    background: {c['surface']};
    border: none;
    border-bottom: 1px solid {c['border']};
    padding: 4px 8px;
    spacing: 6px;
}}
#main-toolbar QToolButton {{
    background: transparent;
    color: {c['text']};
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid transparent;
}}
#main-toolbar QToolButton:hover {{
    background: {c['surface2']};
    border: 1px solid {c['border']};
}}
#toolbar-title {{
    color: {c['primary']};
    font-weight: 600;
    font-size: 14px;
}}

#chat-view {{ background: {c['bg']}; border: none; }}
#chat-container {{ background: {c['bg']}; }}

#bubble-user {{
    background: {c['user_bubble']};
    border-radius: 14px;
    border: 1px solid {c['user_bubble_border']};
    max-width: 78%;
    margin-left: 18%;
}}
#bubble-user #bubble-label {{ color: rgba(255,255,255,0.7); }}
#bubble-user #bubble-content {{ color: #ffffff; }}
#bubble-user #bubble-content a {{ color: #bfdbfe; }}

#bubble-assistant {{
    background: {c['assistant_bubble']};
    border-radius: 14px;
    border: 1px solid {c['assistant_bubble_border']};
    max-width: 82%;
}}
#bubble-info {{
    background: {c['info_bubble']};
    border-radius: 10px;
    border: 1px dashed {c['info_bubble_border']};
    max-width: 90%;
    margin: 0 5%;
}}
#bubble-error {{
    background: {c['error_bubble']};
    border-radius: 10px;
    border: 1px solid {c['error_bubble_border']};
    max-width: 90%;
    margin: 0 5%;
}}
#bubble-label {{
    color: {c['text_muted']};
    font-size: 11px;
    font-weight: 600;
    margin-bottom: 2px;
}}
#bubble-content {{
    background: transparent;
    color: {c['text']};
    border: none;
    font-size: 13px;
    line-height: 1.6;
}}
#bubble-content a {{ color: {c['link']}; }}
#bubble-content code {{
    background: {c['code_bg']};
    padding: 1px 5px;
    border-radius: 4px;
    font-family: "Consolas", "Cascadia Code", monospace;
    font-size: 12px;
}}
#bubble-content pre {{
    background: {c['code_bg']};
    padding: 10px;
    border-radius: 6px;
    border: 1px solid {c['border']};
}}
#bubble-content h1, #bubble-content h2, #bubble-content h3 {{
    color: {c['text']};
    margin: 8px 0 4px;
}}
#bubble-content blockquote {{
    border-left: 3px solid {c['border']};
    padding-left: 10px;
    color: {c['text_muted']};
    margin: 6px 0;
}}

#tool-card {{
    background: {c['tool_bg']};
    border-radius: 10px;
    border: 1px solid {c['tool_border']};
    border-left: 3px solid #f59e0b;
    max-width: 82%;
}}
#tool-card[state="ok"] {{ border-left: 3px solid #10b981; }}
#tool-card[state="fail"] {{ border-left: 3px solid #ef4444; }}
#tool-head {{
    color: {c['text']};
    font-family: "Consolas", "Cascadia Code", monospace;
    font-size: 12px;
    padding: 2px 0;
}}
#tool-body {{
    background: {c['bg']};
    border-radius: 6px;
    margin-top: 6px;
    padding: 8px;
    color: {c['text']};
    font-size: 12px;
    font-family: "Consolas", "Cascadia Code", monospace;
}}

#context-bar {{
    background: {c['surface']};
    padding: 5px 16px;
    border-top: 1px solid {c['border']};
    border-bottom: 1px solid {c['border']};
    font-size: 12px;
}}

#input-frame {{
    background: {c['surface']};
    border-top: 1px solid {c['border']};
}}
#prompt-input {{
    background: {c['bg']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 8px 10px;
    color: {c['text']};
    selection-background-color: {c['primary']};
}}
#prompt-input:focus {{ border: 1px solid {c['primary']}; }}

#send-btn {{
    background: {c['primary']};
    color: white;
    border: none;
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 600;
}}
#send-btn:hover {{ background: {c['primary_hover']}; }}
#send-btn:disabled {{ background: {c['surface2']}; color: {c['text_dim']}; }}

#sidebar {{
    background: {c['surface']};
    border-left: 1px solid {c['border']};
}}
#side-head {{
    color: {c['text']};
    font-size: 14px;
    font-weight: 700;
}}
#side-stats {{
    color: {c['text_muted']};
    font-size: 12px;
    line-height: 1.5;
}}
#side-section-title {{
    color: {c['primary']};
    font-size: 12px;
    font-weight: 600;
    border-top: 1px solid {c['border']};
    padding-top: 8px;
    margin-top: 4px;
}}
#mcp-area {{
    color: {c['text']};
    font-size: 11px;
    line-height: 1.5;
}}
#tool-item {{
    color: {c['text']};
    font-size: 12px;
    padding: 4px 6px;
    border-radius: 4px;
}}

#palette {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 6px;
}}
#palette-item {{
    color: {c['text']};
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 12px;
}}
#palette-item[selected="true"] {{
    background: {c['primary']};
    color: white;
}}

QStatusBar {{
    background: {c['surface']};
    border-top: 1px solid {c['border']};
    color: {c['text_muted']};
    font-size: 12px;
}}

QScrollBar:vertical {{
    background: {c['bg']};
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {c['surface2']};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {c['bg']};
    height: 8px;
}}
QScrollBar::handle:horizontal {{
    background: {c['surface2']};
    border-radius: 4px;
    min-width: 30px;
}}

QDialog {{ background: {c['surface']}; }}
QTextBrowser {{
    background: {c['surface']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 10px;
}}
"""


# ====================================================================== Markdown
def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _markdown_to_html(text: str) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    in_list = False

    def flush_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + _escape("\n".join(code_buf)) + "</code></pre>")
                code_buf = []
                in_code = False
            else:
                flush_list()
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue

        stripped = line.rstrip()
        if not stripped.strip():
            flush_list()
            out.append("<br>")
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_list()
            level = min(len(m.group(1)), 6)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue

        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + _inline(m.group(1)) + "</li>")
            continue

        m = re.match(r"^\d+\.\s+(.*)$", stripped)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + _inline(m.group(1)) + "</li>")
            continue

        if stripped.startswith(">"):
            flush_list()
            out.append(f"<blockquote>{_inline(stripped.lstrip('> ').strip())}</blockquote>")
            continue

        flush_list()
        out.append("<p>" + _inline(stripped) + "</p>")

    flush_list()
    if in_code:
        out.append("<pre><code>" + _escape("\n".join(code_buf)) + "</code></pre>")
    return "\n".join(out)


def _inline(text: str) -> str:
    text = _escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" style="color:#3b82f6">\1</a>',
        text,
    )
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", text)
    return text


# ====================================================================== 消息气泡
class MessageBubble(QFrame):
    def __init__(self, role: str, label: str, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role_name = role
        self._raw = text
        self._streaming = False
        self.setObjectName(f"bubble-{role}")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 10)
        layout.setSpacing(4)

        self._label = QLabel(label)
        self._label.setObjectName("bubble-label")
        layout.addWidget(self._label)

        self._content = QTextBrowser()
        self._content.setObjectName("bubble-content")
        self._content.setOpenExternalLinks(True)
        self._content.setFrameShape(QFrame.NoFrame)
        self._content.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._content.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._content.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self._content.document().setDocumentMargin(0)
        layout.addWidget(self._content)

        self._paint()

    @property
    def raw(self) -> str:
        return self._raw

    def set_text(self, text: str) -> None:
        if text == self._raw:
            return
        self._raw = text
        self._paint()

    def append_text(self, delta: str) -> None:
        if not delta:
            return
        self._raw += delta
        self._paint()

    def set_streaming(self, on: bool) -> None:
        on = bool(on)
        if on != self._streaming:
            self._streaming = on
            self._paint()

    def _paint(self) -> None:
        suffix = "▌" if self._streaming and self._raw else ""
        text = self._raw + suffix
        if self.role_name in ("assistant", "info", "error"):
            self._content.setHtml(_markdown_to_html(text))
        else:
            self._content.setPlainText(text)
        doc = self._content.document()
        doc.setTextWidth(self._content.viewport().width())
        height = doc.size().height() + 4
        self._content.setMinimumHeight(int(height))
        self._content.setMaximumHeight(int(height) + 20)


# ====================================================================== 工具卡片
class ToolCard(QFrame):
    def __init__(
        self,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        args_max: int = 64,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.call_id = call_id
        self.tool_name = name
        self.arguments = arguments
        self.args_max = max(8, int(args_max))
        self.output = ""
        self.ok: bool | None = None
        self.duration_ms: float = 0.0
        self._expanded = False
        self.setObjectName("tool-card")
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 8, 12, 8)
        self._layout.setSpacing(0)

        self._head = QLabel()
        self._head.setObjectName("tool-head")
        self._layout.addWidget(self._head)

        self._body = QTextBrowser()
        self._body.setObjectName("tool-body")
        self._body.setOpenExternalLinks(True)
        self._body.setFrameShape(QFrame.NoFrame)
        self._body.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body.setVisible(False)
        self._body.setMaximumHeight(240)
        self._layout.addWidget(self._body)

        self._paint()

    @property
    def running(self) -> bool:
        return self.ok is None

    def finish(self, ok: bool, output: str, duration_ms: float = 0.0) -> None:
        self.ok = ok
        self.output = output or ""
        self.duration_ms = duration_ms
        self._paint()

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._paint()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.toggle()
        super().mousePressEvent(event)

    def _args_summary(self) -> str:
        try:
            raw = json.dumps(self.arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(self.arguments)
        raw = raw.strip()
        limit = self.args_max
        return raw[: limit - 1] + "…" if len(raw) > limit else raw

    def _args_pretty(self) -> str:
        try:
            return json.dumps(self.arguments, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(self.arguments)

    def _duration(self) -> str:
        if self.duration_ms < 1000:
            return f" · {self.duration_ms:.0f} ms"
        return f" · {self.duration_ms / 1000:.1f} s"

    def _paint(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        if self.running:
            mark = "⏳"
            timing = " · 运行中"
            self.setProperty("state", "running")
        else:
            mark = "✅" if self.ok else "❌"
            timing = self._duration()
            self.setProperty("state", "ok" if self.ok else "fail")
        self._head.setText(f"{arrow} {mark} {self.tool_name}({self._args_summary()}){timing}")
        self.style().unpolish(self)
        self.style().polish(self)

        if not self.running and self._expanded:
            sections = [f"<b>参数</b><pre><code>{_escape(self._args_pretty())}</code></pre>"]
            if self.output:
                out = self.output
                if len(out) > 8000:
                    out = out[:8000] + f"\n…（已截断，原始长度 {len(self.output)} 字符）"
                sections.append(f"<b>结果</b><pre><code>{_escape(out)}</code></pre>")
            self._body.setHtml("\n".join(sections))
            self._body.setVisible(True)
        else:
            self._body.setVisible(False)


# ====================================================================== 聊天视图
class ChatView(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setObjectName("chat-view")

        self._container = QWidget()
        self._container.setObjectName("chat-container")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(16, 16, 16, 16)
        self._layout.setSpacing(10)
        self._layout.addStretch(1)
        self.setWidget(self._container)

        self._auto_scroll = True
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def _on_scroll(self) -> None:
        bar = self.verticalScrollBar()
        self._auto_scroll = bar.value() >= bar.maximum() - 4

    def append_widget(self, widget: QWidget) -> None:
        self._layout.insertWidget(self._layout.count() - 1, widget)

    def clear(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def scroll_to_bottom(self) -> None:
        if self._auto_scroll:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


# ====================================================================== 命令定义
@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    usage: str = ""


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("/new", "新建会话（自动保存当前会话）", "[标题]"),
    CommandSpec("/resume", "恢复历史会话"),
    CommandSpec("/save", "保存当前会话", "[标题]"),
    CommandSpec("/sessions", "列出已保存的历史会话"),
    CommandSpec("/compact", "压缩上下文（归档原始记录并生成摘要）"),
    CommandSpec("/clear", "清空界面（保留历史）"),
    CommandSpec("/tools", "列出已注册工具"),
    CommandSpec("/mcp", "MCP Server 连接状态"),
    CommandSpec("/model", "显示模型与接口"),
    CommandSpec("/status", "显示会话 / 上下文 / MCP 状态"),
    CommandSpec("/agents", "查看 AGENTS.md 注入状态"),
    CommandSpec("/cwd", "显示工作目录"),
    CommandSpec("/export", "导出当前会话为 Markdown", "[路径]"),
    CommandSpec("/help", "打开本帮助"),
    CommandSpec("/quit", "退出"),
)


# ====================================================================== 事件桥接线程
class EventWorker(QObject):
    event_ready = Signal(object)
    finished = Signal()

    def __init__(self, engine: AgentEngine, question: str, is_cancelled) -> None:
        super().__init__()
        self._engine = engine
        self._question = question
        self._is_cancelled = is_cancelled

    def run(self) -> None:
        try:
            for event in self._engine.ask(self._question, is_cancelled=self._is_cancelled):
                self.event_ready.emit(event)
        except Exception as exc:  # noqa: BLE001
            self.event_ready.emit(AgentError(f"内部错误: {type(exc).__name__}: {exc}"))
        finally:
            self.finished.emit()


class CompactWorker(QObject):
    event_ready = Signal(object)
    finished = Signal()

    def __init__(self, engine: AgentEngine, is_cancelled) -> None:
        super().__init__()
        self._engine = engine
        self._is_cancelled = is_cancelled

    def run(self) -> None:
        try:
            for event in self._engine.compact(is_cancelled=self._is_cancelled):
                self.event_ready.emit(event)
        except Exception as exc:  # noqa: BLE001
            self.event_ready.emit(AgentError(f"内部错误: {type(exc).__name__}: {exc}"))
        finally:
            self.finished.emit()


# ====================================================================== 主窗口
class MainWindow(QMainWindow):
    def __init__(self, engine: AgentEngine, initial_question: str | None = None) -> None:
        super().__init__()
        self.engine = engine
        self._initial_question = initial_question
        self._busy = False
        self._cancelled = False
        self._dark = False  # 默认浅色
        self._assistant_bubble: MessageBubble | None = None
        self._tool_cards: dict[str, ToolCard] = {}
        self._turns = 0
        self._model_calls = 0
        self._tool_calls_total = 0
        self._last_duration = 0.0
        self._turn_started = 0.0
        self._mcp_revision = -1
        self._palette_matches: list[CommandSpec] = []
        self._palette_index = 0

        self._setup_ui()
        self._setup_shortcuts()
        self._apply_theme(self._dark)
        self._refresh_context()
        self._refresh_sidebar(force=True)

        self._mcp_timer = QTimer(self)
        self._mcp_timer.timeout.connect(self._poll_mcp)
        self._mcp_timer.start(350)

        if self._initial_question:
            QTimer.singleShot(100, lambda: self._send(self._initial_question))

    # ------------------------------------------------------------ UI 构建
    def _setup_ui(self) -> None:
        self.setWindowTitle(f"{APP_NAME} — {self.engine.config.model}")
        self.resize(1100, 760)
        self.setMinimumSize(820, 540)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 工具栏
        self._toolbar = QToolBar()
        self._toolbar.setMovable(False)
        self._toolbar.setObjectName("main-toolbar")
        self.addToolBar(Qt.TopToolBarArea, self._toolbar)

        act_new = QAction("＋ 新会话", self)
        act_new.triggered.connect(lambda: self._new_session(None))
        act_save = QAction("💾 保存", self)
        act_save.triggered.connect(lambda: self._save_session(None))
        act_resume = QAction("📂 恢复", self)
        act_resume.triggered.connect(self._resume)
        act_compact = QAction("🗜 压缩", self)
        act_compact.triggered.connect(self._compact)
        act_clear = QAction("🗑 清屏", self)
        act_clear.triggered.connect(self._clear_view)
        act_help = QAction("❓ 帮助", self)
        act_help.triggered.connect(self._show_help)

        for act in (act_new, act_save, act_resume, act_compact, act_clear, act_help):
            self._toolbar.addAction(act)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._toolbar.addWidget(spacer)

        # 主题切换按钮
        self._theme_btn = QPushButton("🌙 深色")
        self._theme_btn.setObjectName("theme-btn")
        self._theme_btn.setCursor(Qt.PointingHandCursor)
        self._theme_btn.clicked.connect(self._toggle_theme)
        self._toolbar.addWidget(self._theme_btn)

        title_lbl = QLabel(f"  {APP_NAME}  ")
        title_lbl.setObjectName("toolbar-title")
        self._toolbar.addWidget(title_lbl)

        # 主区域
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        chat_widget = QWidget()
        chat_layout = QVBoxLayout(chat_widget)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)
        self.chat = ChatView()
        chat_layout.addWidget(self.chat, 1)

        self._context_bar = QLabel()
        self._context_bar.setObjectName("context-bar")
        chat_layout.addWidget(self._context_bar)

        input_frame = QFrame()
        input_frame.setObjectName("input-frame")
        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(12, 10, 12, 12)
        input_layout.setSpacing(8)

        self._input = QTextEdit()
        self._input.setObjectName("prompt-input")
        self._input.setPlaceholderText("输入问题后 Ctrl+Enter 发送；输入 / 查看命令…")
        self._input.setMaximumHeight(120)
        self._input.textChanged.connect(self._on_input_changed)
        input_layout.addWidget(self._input, 1)

        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("send-btn")
        self._send_btn.setMinimumWidth(72)
        self._send_btn.clicked.connect(self._on_submit)
        input_layout.addWidget(self._send_btn)

        chat_layout.addWidget(input_frame)
        splitter.addWidget(chat_widget)

        # 侧栏
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMaximumWidth(340)
        sidebar.setMinimumWidth(240)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(14, 14, 14, 14)
        side_layout.setSpacing(10)

        self._side_head = QLabel(f"{APP_NAME}")
        self._side_head.setObjectName("side-head")
        side_layout.addWidget(self._side_head)

        self._side_stats = QLabel()
        self._side_stats.setObjectName("side-stats")
        self._side_stats.setWordWrap(True)
        side_layout.addWidget(self._side_stats)

        self._side_mcp_title = QLabel("MCP")
        self._side_mcp_title.setObjectName("side-section-title")
        side_layout.addWidget(self._side_mcp_title)

        self._mcp_area = QLabel("加载中…")
        self._mcp_area.setObjectName("mcp-area")
        self._mcp_area.setWordWrap(True)
        self._mcp_area.setTextInteractionFlags(Qt.TextSelectableByMouse)
        side_layout.addWidget(self._mcp_area)

        self._side_tools_title = QLabel("工具")
        self._side_tools_title.setObjectName("side-section-title")
        side_layout.addWidget(self._side_tools_title)

        tools_scroll = QScrollArea()
        tools_scroll.setWidgetResizable(True)
        tools_scroll.setFrameShape(QFrame.NoFrame)
        tools_container = QWidget()
        self._tools_layout = QVBoxLayout(tools_container)
        self._tools_layout.setContentsMargins(0, 0, 0, 0)
        self._tools_layout.setSpacing(4)
        self._tools_layout.addStretch(1)
        tools_scroll.setWidget(tools_container)
        side_layout.addWidget(tools_scroll, 1)

        splitter.addWidget(sidebar)
        splitter.setSizes([760, 300])
        root.addWidget(splitter, 1)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_lbl = QLabel("就绪")
        self._status_bar.addWidget(self._status_lbl, 1)

        # 命令补全面板
        self._palette = QFrame(self)
        self._palette.setObjectName("palette")
        self._palette.setWindowFlags(Qt.Popup)
        self._palette_layout = QVBoxLayout(self._palette)
        self._palette_layout.setContentsMargins(6, 6, 6, 6)
        self._palette_layout.setSpacing(2)
        self._palette.hide()

    # ------------------------------------------------------------ 主题
    def _toggle_theme(self) -> None:
        self._dark = not self._dark
        self._apply_theme(self._dark)

    def _apply_theme(self, dark: bool) -> None:
        self._theme_btn.setText("☀ 浅色" if dark else "🌙 深色")
        self.setStyleSheet(_build_stylesheet(dark))
        # 刷新当前消息气泡的渲染
        for i in range(self.chat._layout.count()):
            item = self.chat._layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, MessageBubble):
                w._paint()

    # ------------------------------------------------------------ 快捷键
    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._on_submit)
        QShortcut(QKeySequence("Escape"), self, activated=self._on_escape)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._clear_view)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=lambda: self._new_session(None))
        QShortcut(QKeySequence("Ctrl+S"), self, activated=lambda: self._save_session(None))
        QShortcut(QKeySequence("F1"), self, activated=self._show_help)

    # ------------------------------------------------------------ 输入
    def _on_input_changed(self) -> None:
        self._refresh_palette()

    def _on_submit(self) -> None:
        if self._palette.isVisible() and self._palette_matches:
            spec = self._palette_matches[self._palette_index]
            self._hide_palette()
            text = spec.name
            if spec.usage:
                text += " "
            self._input.setPlainText(text)
            cursor = self._input.textCursor()
            cursor.movePosition(QTextCursor.End)
            self._input.setTextCursor(cursor)
            self._input.setFocus()
            return
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._input.clear()
        self._send(text)

    def _on_escape(self) -> None:
        if self._palette.isVisible():
            self._hide_palette()
            return
        if self._busy:
            self._cancelled = True
            self._set_status("停止中…", busy=True)

    # ------------------------------------------------------------ 发送/命令
    def _send(self, question: str) -> None:
        question = (question or "").strip()
        if not question:
            return
        self._hide_palette()
        if question.startswith("/"):
            self._run_command(question)
            return
        if self._busy:
            self._notify("上一轮还在生成中…（Esc 可中断）", "warning")
            return
        if self.engine.input_blocked:
            percent = int(self.engine.context_config.warning_ratio * 100)
            self._append_info(
                f"上下文占用已达 {percent}%，已暂停接收新的提示词。"
                "请点击「压缩」或输入 /compact 压缩上下文后继续。",
                error=True,
            )
            return
        self._append_user(question)
        self._cancelled = False
        self._set_busy(True)
        self._ask(question)

    def _run_command(self, text: str) -> None:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            self._show_help()
        elif cmd == "/clear":
            self._clear_view()
        elif cmd == "/new":
            self._new_session(arg or None)
        elif cmd == "/resume":
            self._resume()
        elif cmd == "/save":
            self._save_session(arg or None)
        elif cmd == "/sessions":
            self._list_sessions()
        elif cmd == "/compact":
            self._compact()
        elif cmd == "/quit":
            self.close()
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
                self._notify("生成中暂不能导出，可先按 Esc", "warning")
                return
            try:
                path = self.engine.export_markdown(arg or None)
            except Exception as exc:  # noqa: BLE001
                self._append_info(f"导出失败：{type(exc).__name__}: {exc}", error=True)
            else:
                self._append_info(f"会话已导出到\n{path}")
        else:
            self._append_info(f"未知命令 {cmd}，输入 /help 查看帮助", error=True)

    # ------------------------------------------------------------ 命令补全
    def _refresh_palette(self) -> None:
        text = self._input.toPlainText().strip()
        if not text.startswith("/") or " " in text:
            self._hide_palette()
            return
        matches = [s for s in COMMAND_SPECS if s.name.startswith(text.lower())]
        if not matches:
            self._hide_palette()
            return
        self._palette_matches = matches
        if self._palette_index >= len(matches):
            self._palette_index = 0
        while self._palette_layout.count():
            item = self._palette_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, spec in enumerate(matches[:8]):
            lbl = QLabel(f"{'▶' if i == self._palette_index else '  '} "
                         f"{spec.name}  {spec.description}")
            lbl.setObjectName("palette-item")
            lbl.setProperty("selected", i == self._palette_index)
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)
            self._palette_layout.addWidget(lbl)
        rect = self._input.geometry()
        pos = self._input.mapTo(self, rect.topLeft())
        self._palette.move(pos.x(), pos.y() - 4)
        self._palette.adjustSize()
        self._palette.show()

    def _hide_palette(self) -> None:
        self._palette.hide()
        self._palette_matches = []
        self._palette_index = 0

    # ------------------------------------------------------------ 核心桥接
    def _ask(self, question: str) -> None:
        self._worker_thread = QThread()
        self._worker = EventWorker(self.engine, question, lambda: self._cancelled)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.event_ready.connect(self._handle_event)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.finished.connect(self._on_ask_finished)
        self._worker_thread.start()

    def _on_ask_finished(self) -> None:
        self._set_busy(False)
        self._refresh_context()
        self._refresh_sidebar()

    def _compact(self) -> None:
        if self._busy:
            self._notify("上一轮还在生成中…（Esc 可中断）", "warning")
            return
        self._cancelled = False
        self._set_busy(True)
        self._compact_thread = QThread()
        self._compact_worker = CompactWorker(self.engine, lambda: self._cancelled)
        self._compact_worker.moveToThread(self._compact_thread)
        self._compact_thread.started.connect(self._compact_worker.run)
        self._compact_worker.event_ready.connect(self._handle_event)
        self._compact_worker.finished.connect(self._compact_thread.quit)
        self._compact_worker.finished.connect(self._compact_worker.deleteLater)
        self._compact_thread.finished.connect(self._compact_thread.deleteLater)
        self._compact_thread.finished.connect(self._on_compact_finished)
        self._compact_thread.start()

    def _on_compact_finished(self) -> None:
        self._set_busy(False)
        if self.engine.input_blocked:
            self._refresh_panels("需压缩")
        else:
            self._refresh_context()
            self._refresh_panels("就绪")

    # ------------------------------------------------------------ 事件渲染
    def _handle_event(self, event: AgentEvent) -> None:
        if isinstance(event, TurnStarted):
            self._turn_started = time.monotonic()
            self._set_status("思考中…", busy=True)

        elif isinstance(event, AssistantDelta):
            bubble = self._ensure_assistant_bubble()
            bubble.set_streaming(True)
            bubble.append_text(event.text)
            self.chat.scroll_to_bottom()

        elif isinstance(event, AssistantMessage):
            self._model_calls += 1
            if event.text:
                bubble = self._ensure_assistant_bubble()
                bubble.set_streaming(False)
                bubble.set_text(event.text)
            self.chat.scroll_to_bottom()
            self._refresh_panels("思考中…", busy=True)

        elif isinstance(event, ToolCallStarted):
            self._assistant_bubble = None
            card = ToolCard(
                event.id, event.name, event.arguments,
                args_max=self.engine.ui_config.tool_args_preview,
            )
            self._tool_cards[event.id] = card
            self.chat.append_widget(card)
            self._set_status(f"运行工具 {event.name} …", busy=True)
            self.chat.scroll_to_bottom()

        elif isinstance(event, ToolCallFinished):
            self._tool_calls_total += 1
            card = self._tool_cards.get(event.id)
            if card is not None:
                card.finish(event.ok, event.output, event.duration_ms)
            self._set_status("思考中…", busy=True)
            self.chat.scroll_to_bottom()

        elif isinstance(event, ContextWarning):
            self._append_info(f"⚠ {event.message}", error=True)
            self._refresh_context()

        elif isinstance(event, CompactStarted):
            self._assistant_bubble = None
            self._set_status(f"压缩上下文（归档 {event.archive_id}）…", busy=True)
            self._refresh_context()

        elif isinstance(event, CompactFinished):
            self._assistant_bubble = None
            self._append_info(event.message, error=not event.ok)
            if event.ok:
                self._refresh_panels("就绪")
            else:
                state = "需压缩" if self.engine.input_blocked else "压缩失败"
                self._refresh_panels(state)

        elif isinstance(event, AgentError):
            self._assistant_bubble = None
            self.chat.append_widget(
                MessageBubble("error", "错误", f"⚠ {event.message}")
            )
            self.chat.scroll_to_bottom()

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
        self.chat.append_widget(MessageBubble("user", "你", text))
        self.chat.scroll_to_bottom()

    def _append_info(self, text: str, error: bool = False) -> None:
        role = "error" if error else "info"
        label = "错误" if error else "提示"
        self.chat.append_widget(MessageBubble(role, label, text))
        self.chat.scroll_to_bottom()

    def _ensure_assistant_bubble(self) -> MessageBubble:
        if self._assistant_bubble is None:
            self._assistant_bubble = MessageBubble("assistant", APP_NAME)
            self.chat.append_widget(self._assistant_bubble)
        return self._assistant_bubble

    def _clear_view(self) -> None:
        self.chat.clear()
        self._assistant_bubble = None
        self._tool_cards.clear()
        self._set_status("就绪")

    def _render_history(self) -> None:
        self.chat.clear()
        turns = model_calls = tool_calls = 0
        for message in self.engine.session.messages:
            role = message.get("role")
            content = str(message.get("content") or "")
            if role == "system" or content.startswith(AGENTS_MD_PREFIX):
                continue
            if role == "user":
                if content.strip():
                    turns += 1
                    self.chat.append_widget(MessageBubble("user", "你", content))
            elif role == "assistant":
                model_calls += 1
                if content.strip():
                    self.chat.append_widget(MessageBubble("assistant", APP_NAME, content))
            elif role == "tool":
                tool_calls += 1
        self._turns, self._model_calls, self._tool_calls_total = turns, model_calls, tool_calls
        self._assistant_bubble = None
        self.chat.scroll_to_bottom()

    # ------------------------------------------------------------ 会话管理
    def _new_session(self, title: str | None) -> None:
        if self._busy:
            self._notify("生成中暂不能新建会话，可先按 Esc", "warning")
            return
        try:
            saved = self.engine.new_session(title)
        except Exception as exc:  # noqa: BLE001
            self._append_info(f"新建会话失败：{type(exc).__name__}: {exc}", error=True)
            return
        self.chat.clear()
        self._assistant_bubble = None
        self._tool_cards.clear()
        self._turns = self._model_calls = self._tool_calls_total = 0
        self._last_duration = 0.0
        self._refresh_panels("就绪")
        self._refresh_sidebar(force=True)
        self._notify(f"已保存旧会话：{saved.name}" if saved else "已新建会话")

    def _save_session(self, title: str | None) -> None:
        if self._busy:
            self._notify("生成中暂不能保存，可先按 Esc", "warning")
            return
        try:
            path = self.engine.save_session(title)
        except Exception as exc:  # noqa: BLE001
            self._append_info(f"保存失败：{type(exc).__name__}: {exc}", error=True)
            return
        self._notify(f"已保存会话：{path.name}")

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

    def _resume(self) -> None:
        if self._busy:
            self._notify("生成中暂不能恢复会话，可先按 Esc", "warning")
            return
        sessions = self.engine.list_sessions()
        if not sessions:
            self._append_info("暂无已保存会话。使用 /save 保存当前会话。")
            return
        items = [f"{s.title}  ·  {s.updated_at}  ·  {s.messages} 条消息  ·  {s.id}"
                 for s in sessions]
        item, ok = QInputDialog.getItem(
            self, "恢复历史会话", "选择要恢复的会话：", items, 0, False
        )
        if not ok or not item:
            return
        idx = items.index(item)
        try:
            info = self.engine.resume_session(sessions[idx].id)
        except Exception as exc:  # noqa: BLE001
            self._append_info(f"恢复会话失败：{type(exc).__name__}: {exc}", error=True)
            return
        self._render_history()
        self._refresh_panels("就绪")
        self._refresh_sidebar(force=True)
        self._notify(f"已恢复会话：{info.title}")

    # ------------------------------------------------------------ 状态/侧栏
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

    def _refresh_context(self) -> None:
        usage = self.engine.context_usage()
        used = format_tokens_k(usage.used)
        window = format_tokens_k(usage.window)
        pct = usage.percent
        src = "API" if usage.source == "api" else "估算"
        note = "（窗口未知，默认 128k）" if usage.window <= 0 else ""
        warn = pct >= 80
        color = "#ef4444" if warn else (self._theme_colors()["text_muted"])
        self._context_bar.setText(
            f'<span style="color:{color}">上下文 {used} / {window} · '
            f'已占用 {pct:.1f}%</span>'
            f'<span style="color:{self._theme_colors()["text_dim"]}">  ·  {src}{note}</span>'
        )

    def _theme_colors(self) -> dict[str, str]:
        return _theme_colors(self._dark)

    def _refresh_panels(self, state: str, busy: bool = False) -> None:
        self._set_status(state, busy=busy)
        self._refresh_context()
        self._refresh_sidebar()

    def _refresh_sidebar(self, force: bool = False) -> None:
        self._side_head.setText(f"{APP_NAME} · {len(self.engine.tools)} 个工具")
        if self._last_duration:
            stats = (
                f"轮次 {self._turns}  ·  模型调用 {self._model_calls}\n"
                f"工具调用 {self._tool_calls_total}  ·  上次耗时 {self._last_duration:.1f}s"
            )
        else:
            stats = (
                f"轮次 {self._turns}  ·  模型调用 {self._model_calls}  ·  "
                f"工具调用 {self._tool_calls_total}"
            )
        self._side_stats.setText(stats)

        if force:
            while self._tools_layout.count() > 1:
                item = self._tools_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for schema in self.engine.tools:
                fn = schema["function"]
                lbl = QLabel(
                    f'<b>{fn["name"]}</b><br>'
                    f'<span style="color:{self._theme_colors()["text_muted"]};font-size:11px">'
                    f'{fn["description"]}</span>'
                )
                lbl.setObjectName("tool-item")
                lbl.setWordWrap(True)
                self._tools_layout.insertWidget(self._tools_layout.count() - 1, lbl)

    def _poll_mcp(self) -> None:
        snapshot = self.engine.mcp.snapshot()
        if snapshot["revision"] == self._mcp_revision:
            return
        self._mcp_revision = snapshot["revision"]
        servers = snapshot["servers"]
        if not servers:
            if snapshot["state"] == "disabled":
                self._mcp_area.setText("未启用（[mcp] enabled=false）")
            elif not snapshot["started"]:
                self._mcp_area.setText("加载中…")
            else:
                self._mcp_area.setText("无 MCP Server（.mcp.json 未配置）")
        else:
            lines = []
            for srv in servers:
                tools = ", ".join(srv["tools"]) if srv["tools"] else "（无工具）"
                lines.append(
                    f'<b>{srv["name"]}</b> '
                    f'<span style="color:{self._theme_colors()["text_muted"]}">'
                    f'[{srv["kind"]}]</span><br>'
                    f'<span style="color:{self._theme_colors()["text_dim"]}">'
                    f'{srv["detail"]}</span><br>'
                    f'<span style="color:{self._theme_colors()["primary"]}">{tools}</span>'
                )
            self._mcp_area.setText("<br>".join(lines))

    def _set_status(self, text: str, busy: bool = False) -> None:
        dot = "🟡" if busy else "🟢"
        self._status_lbl.setText(f"{dot} {text}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send_btn.setEnabled(not busy)
        self._send_btn.setText("停止" if busy else "发送")

    def _notify(self, message: str, kind: str = "info") -> None:
        self._status_lbl.setText(message)
        if not self._busy:
            QTimer.singleShot(2500, lambda: not self._busy and self._set_status("就绪"))

    def _show_help(self) -> None:
        from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{APP_NAME} 帮助")
        dlg.resize(560, 480)
        layout = QVBoxLayout(dlg)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        lines = [
            f"<h2>{APP_NAME} 帮助</h2>",
            "<p><b>使用方式</b></p>",
            "<ul>",
            "<li>在底部输入框输入问题，<b>Ctrl+Enter</b> 发送。</li>",
            "<li>输入 <code>/</code> 弹出命令补全面板。</li>",
            "<li>点击工具栏 <b>🌙 深色</b> / <b>☀ 浅色</b> 切换主题。</li>",
            "</ul>",
            "<p><b>命令</b></p><table style='width:100%'>",
        ]
        for spec in COMMAND_SPECS:
            lines.append(
                f"<tr><td><code>{spec.name} {spec.usage}</code></td>"
                f"<td>{spec.description}</td></tr>"
            )
        lines.append("</table>")
        lines.append("<p><b>快捷键</b></p><ul>")
        for key, desc in (
            ("Ctrl+Enter", "发送问题"),
            ("Esc", "中断当前生成"),
            ("Ctrl+L", "清空界面（保留历史）"),
            ("Ctrl+N", "新建会话"),
            ("Ctrl+S", "保存当前会话"),
            ("F1", "帮助"),
        ):
            lines.append(f"<li><code>{key}</code> — {desc}</li>")
        lines.append("</ul>")
        browser.setHtml("\n".join(lines))
        layout.addWidget(browser)
        dlg.exec()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            self._cancelled = True
        event.accept()


# ====================================================================== 入口
def run_gui(engine: AgentEngine, initial_question: str | None = None) -> None:
    """启动图形界面（供 main.py 调用）。"""
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow(engine, initial_question=initial_question)
    window.show()
    sys.exit(app.exec())
