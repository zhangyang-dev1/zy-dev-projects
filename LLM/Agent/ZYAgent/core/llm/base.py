"""LLM 客户端抽象：定义 core 对任意模型网关的最小依赖面。

层级：core / 资源层（端口 Port）。
任何符合本协议的客户端（DeepSeek、OpenAI、本地 Ollama、Mock 测试桩）都可注入引擎。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

Message = dict[str, Any]
ToolSchema = dict[str, Any]


@dataclass(frozen=True)
class LLMReply:
    """非流式一次请求的回复。"""

    message: Message                       # choices[0].message
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class TextDelta:
    """流式文本增量。"""

    text: str


@dataclass(frozen=True)
class ReplyArrived:
    """流式结束时汇总出的完整 assistant 消息。"""

    message: Message = field(default_factory=dict)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None   # API 返回的 token 用量（可能为 None）


StreamPart = TextDelta | ReplyArrived


class LLMClient(ABC):
    """LLM 客户端端口（Port）。

    * :meth:`complete` —— 必须实现；
    * :meth:`stream` —— 可选能力，不支持时设置 ``supports_stream = False``，
      引擎会自动回退到 :meth:`complete`。
    """

    supports_stream: bool = False

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None = None,
    ) -> LLMReply:
        """同步发起一次非流式请求。"""

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None = None,
    ) -> Iterator[StreamPart]:
        """流式请求：先产出若干 :class:`TextDelta`，最后产出一个 :class:`ReplyArrived`。"""
        raise NotImplementedError(f"{type(self).__name__} 不支持流式请求")


class LLMError(RuntimeError):
    """请求失败（网络、HTTP 状态、响应结构非法）。"""
