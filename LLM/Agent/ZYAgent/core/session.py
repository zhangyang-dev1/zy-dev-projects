"""会话层：维护 OpenAI 风格的 messages 历史。

层级：core / 领域层；与 UI、HTTP 客户端均无耦合。
"""

from __future__ import annotations

from typing import Any, Sequence

Message = dict[str, Any]


class ConversationSession:
    """一次对话的消息历史（多轮用户提问共享同一份历史）。

    线程模型：**单会话单线程写入**。TUI 用一个后台 worker 顺序消费引擎事件，
    因此不会出现并发写；Web 前端如需并发请为每个连接创建独立实例。
    """

    def __init__(self, system_prompt: str = "") -> None:
        self._messages: list[Message] = []
        if system_prompt:
            self._messages.append({"role": "system", "content": system_prompt})

    # ---------------------------------------------------------------- 查询
    @property
    def messages(self) -> list[Message]:
        """历史消息的浅拷贝（防止调用方意外修改内部状态）。"""
        return [dict(m) for m in self._messages]

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self):
        return iter(self._messages)

    # ---------------------------------------------------------------- 变更
    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_assistant(self, message: Message) -> None:
        """追加模型返回的 assistant 消息（可能包含 tool_calls）。"""
        self._messages.append(dict(message))

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self._messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )

    def reset(self, system_prompt: str = "") -> None:
        """清空历史（可保留系统提示词）。"""
        self._messages.clear()
        if system_prompt:
            self._messages.append({"role": "system", "content": system_prompt})

    def replace(self, messages: Sequence[Message]) -> None:
        """用新的消息列表整体替换历史（上下文压缩后使用）。"""
        self._messages = [dict(m) for m in messages]

    def copy(self) -> "ConversationSession":
        """复制一份独立会话（用于导出/快照）。"""
        new = ConversationSession()
        new._messages = [dict(m) for m in self._messages]
        return new
