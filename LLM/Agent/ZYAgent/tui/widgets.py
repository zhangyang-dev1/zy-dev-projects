"""TUI 组件层：消息气泡与可折叠工具卡片（仅负责单条事件的可视化）。"""

from __future__ import annotations

import json
from typing import Any

from textual.containers import Vertical
from textual.widgets import Static

#: 参数摘要最大长度
_ARGS_MAX = 64


class MessageBubble(Vertical):
    """一条对话消息（role: user / assistant / error / info）。

    * 角色标签行（如 "你" / "ZYAgent"）+ 正文；
    * 契约实现：``append_text`` 追加流式增量，``set_text`` 幂等设置权威全文；
    * ``set_streaming`` 控制尾部光标 ``▌``。
    """

    def __init__(self, role: str, label: str, text: str = "", id: str | None = None) -> None:
        label_widget = Static(label, markup=False, classes="bubble-label")
        # 注意：不要用 shrink=True——它会让 auto 宽度容器把气泡收缩成标签宽度
        # （正文每行 1~2 字，呈"竖排"）。正文宽度由 CSS 确定（100%），天然折行。
        content_widget = Static("", markup=False, classes="bubble-text")
        super().__init__(label_widget, content_widget, id=id, classes=f"bubble {role}")
        self.role_name = role
        self._label_widget = label_widget
        self._content = content_widget
        self._raw = text
        self._streaming = False
        self._paint()

    # ------------------------------------------------------------ 属性
    @property
    def raw(self) -> str:
        return self._raw

    @property
    def streaming(self) -> bool:
        return self._streaming

    # ------------------------------------------------------------ 变更
    def set_text(self, text: str) -> None:
        """设置完整文本（幂等：与当前一致时跳过重绘）。"""
        if text == self._raw:
            return
        self._raw = text
        self._paint()

    def append_text(self, delta: str) -> None:
        """追加流式增量。"""
        if not delta:
            return
        self._raw += delta
        self._paint()

    def set_streaming(self, on: bool) -> None:
        """开始/结束流式（显示尾部光标）。"""
        on = bool(on)
        if on != self._streaming:
            self._streaming = on
            self._paint()

    def _paint(self) -> None:
        suffix = "▌" if self._streaming and self._raw else ""
        self._content.update(self._raw + suffix)


class ToolCard(Vertical):
    """一次工具调用的可折叠卡片。

    * 表头：状态标记 + ``工具名(参数摘要)`` + 耗时；
    * 点击卡片展开/收起：参数 JSON 与执行结果（悬停 tooltip 可看完整输出）；
    * 状态：运行中（⏳）→ ✅ 成功 / ❌ 失败，左边条颜色随之变化。
    """

    can_focus = True  # 允许点击/聚焦切换展开

    def __init__(
        self,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        args_max: int = _ARGS_MAX,
    ) -> None:
        head = Static("", markup=False, classes="tool-head")
        body = Static("", markup=False, classes="tool-body")
        super().__init__(head, body, classes="tool-card running")
        self.call_id = call_id
        self.tool_name = name
        self.arguments = arguments
        self.args_max = max(8, int(args_max))
        self.output = ""
        self.ok: bool | None = None  # None = 运行中
        self.duration_ms: float = 0.0
        self.expanded = False
        self._head = head
        self._body = body
        self._paint()

    # ------------------------------------------------------------ 状态
    @property
    def running(self) -> bool:
        return self.ok is None

    def finish(self, ok: bool, output: str, duration_ms: float = 0.0) -> None:
        """标记执行完成。"""
        self.ok = ok
        self.output = output or ""
        self.duration_ms = duration_ms
        self.remove_class("running")
        self.add_class("ok" if ok else "fail")
        self._paint()

    def toggle(self) -> None:
        """展开/收起详情。"""
        self.expanded = not self.expanded
        self._paint()

    def on_click(self) -> None:
        self.toggle()

    # ------------------------------------------------------------ 内部
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
        arrow = "▼" if self.expanded else "▶"
        if self.running:
            mark, timing = "⏳", " · 运行中"
            body = ""
            self.tooltip = f"{self.tool_name} 运行中…"
        else:
            mark = "✅" if self.ok else "❌"
            timing = self._duration()
            body = ""
            if self.expanded:
                sections = [f"参数\n{self._args_pretty()}"]
                if self.output:
                    sections.append(f"结果\n{self.output}")
                body = "\n\n" + "\n\n".join(sections)
            self.tooltip = self.output or self.tool_name
        self._head.update(f"{arrow} {mark} {self.tool_name}({self._args_summary()}){timing}")
        self._body.update(body)
        self._body.display = bool(body)
