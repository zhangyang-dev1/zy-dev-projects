"""事件层：core 与所有 UI（TUI/Web/Desktop/CLI）之间的唯一通信契约。

设计说明
--------
* 核心引擎以 **同步迭代器** 的形式产出 :class:`AgentEvent`（见 ``engine.AgentEngine.ask``），
  UI 只需消费事件流即可渲染任意界面，core 不感知 UI 的存在。
* 事件是 **frozen dataclass**，不可变、可序列化（:meth:`AgentEvent.to_dict`），
  未来 Web 前端可直接经 SSE / WebSocket 推送，Desktop 前端可直接转为信号。

UI 渲染契约（所有前端必须遵守）
--------------------------------
=====================  ============================================================
事件                   UI 行为
=====================  ============================================================
``TurnStarted``        新建一轮对话上下文（可显示用户问题）
``AssistantDelta``     **追加** 到当前助手气泡（流式增量）
``AssistantMessage``   **设置** 当前助手气泡为该完整文本（幂等；非流式时仅有此事件）
``ToolCallStarted``    新建工具卡片，显示工具名与参数
``ToolCallFinished``   更新对应工具卡片的执行结果与状态
``ContextWarning``     显示上下文即将耗尽的警告（提示执行 /compact）
``CompactStarted``     上下文压缩开始（显示归档编号与"压缩中"状态）
``CompactFinished``    上下文压缩结束（ok=False 时原上下文保持不变）
``AgentError``         显示错误提示（不中断会话）
``TurnFinished``       本轮结束；UI 恢复输入、停止"思考中"状态
=====================  ============================================================

即：``Delta`` 追加、``Message`` 覆盖，二者配合可实现"流式打字 + 一次性输出"两种模式。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    """所有引擎事件的基类。"""

    @classmethod
    def kind(cls) -> str:
        """事件类型名（类名），用于序列化后的 ``type`` 字段。"""
        return cls.__name__

    @property
    def type(self) -> str:
        return self.kind()

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典（Web/Desktop 前端传输用）。"""
        data: dict[str, Any] = {"type": self.kind()}
        data.update(asdict(self))
        return data


@dataclass(frozen=True)
class TurnStarted(AgentEvent):
    """一轮提问开始。"""

    question: str


@dataclass(frozen=True)
class AssistantDelta(AgentEvent):
    """助手回答的流式增量文本（仅 stream 模式产生）。"""

    text: str


@dataclass(frozen=True)
class AssistantMessage(AgentEvent):
    """一条完整的助手消息（权威文本，UI 应"设置"而非"追加"）。"""

    text: str


@dataclass(frozen=True)
class ToolCallStarted(AgentEvent):
    """某个工具开始执行。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallFinished(AgentEvent):
    """某个工具执行结束。

    ``ok=False`` 表示工具返回了错误文本（错误即数据，交由模型自行纠正）；
    ``duration_ms`` 为执行耗时（毫秒），供前端展示。
    """

    id: str
    name: str
    ok: bool
    output: str
    duration_ms: float = 0.0


@dataclass(frozen=True)
class AgentError(AgentEvent):
    """传输层/引擎层错误（请求失败、轮次超限、用户取消等）。"""

    message: str


@dataclass(frozen=True)
class ContextWarning(AgentEvent):
    """上下文占用达到阈值（模型窗口的 80%）。

    UI 应醒目提示用户执行 ``/compact``；此后引擎会暂停接受普通提示词，
    直到上下文压缩成功完成。
    """

    message: str


@dataclass(frozen=True)
class CompactStarted(AgentEvent):
    """上下文压缩开始（此时已确定归档编号，原始上下文即将写入归档）。"""

    archive_id: str


@dataclass(frozen=True)
class CompactFinished(AgentEvent):
    """上下文压缩结束。

    ``ok=False`` 表示归档/摘要/替换失败，**原上下文保持不变**，可重新执行
    ``/compact``；``message`` 为给用户看的结果说明。
    """

    ok: bool
    archive_id: str
    message: str


@dataclass(frozen=True)
class TurnFinished(AgentEvent):
    """一轮结束。``answer`` 为最终回答（失败或取消时可能为空串）。

    **保证**：每次 ``ask()`` 迭代必然以其结束（正常完成 / 出错 / 取消），
    UI 可依赖它恢复状态。
    """

    answer: str


#: 引擎事件流类型别名
EventStream = Any  # Iterator[AgentEvent]
