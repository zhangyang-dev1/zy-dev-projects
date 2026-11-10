"""LLM 客户端子包：抽象端口 + 具体适配器。"""

from .base import (
    LLMClient,
    LLMError,
    LLMReply,
    Message,
    ReplyArrived,
    StreamPart,
    TextDelta,
    ToolSchema,
)
from .openai_compat import DeepSeekClient, OpenAICompatClient

__all__ = [
    "DeepSeekClient",
    "LLMClient",
    "LLMError",
    "LLMReply",
    "Message",
    "OpenAICompatClient",
    "ReplyArrived",
    "StreamPart",
    "TextDelta",
    "ToolSchema",
]
