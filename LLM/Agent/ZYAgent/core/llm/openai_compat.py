"""OpenAI 兼容协议的 HTTP 客户端（DeepSeek / OpenAI / 各类网关）。

层级：core / 资源层（适配器 Adapter）。
仅使用标准库 ``urllib.request``，零第三方依赖；支持普通请求与 SSE 流式请求。
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Sequence
from urllib import error, request

from .base import LLMClient, LLMError, LLMReply, Message, ReplyArrived, StreamPart, TextDelta, ToolSchema


class OpenAICompatClient(LLMClient):
    """``POST {base_url}/chat/completions`` 客户端。"""

    supports_stream = True

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 60) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    # ------------------------------------------------------------ 内部：传输
    def _endpoint(self) -> str:
        base = self._base_url
        return base if base.endswith("/chat/completions") else base + "/chat/completions"

    def _build_request(self, payload: dict[str, Any]) -> request.Request:
        return request.Request(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if payload.get("stream") else "application/json",
                "Authorization": "Bearer " + self._api_key,
            },
            method="POST",
        )

    def _payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "stream": stream,
        }
        if stream:
            # 让 OpenAI 兼容网关在流式响应末尾返回 token usage（用于上下文监控）；
            # 网关不支持时不会返回 usage，引擎会自动回退到本地估算。
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = list(tools)
        return payload

    @staticmethod
    def _open(req: request.Request, timeout: int):
        try:
            return request.urlopen(req, timeout=timeout)
        except error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # pragma: no cover - 读取错误体失败
                pass
            raise LLMError(f"HTTP {exc.code}: {body or exc.reason}") from exc
        except error.URLError as exc:
            raise LLMError(f"网络错误: {exc.reason}") from exc

    # ------------------------------------------------------------ 非流式
    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None = None,
    ) -> LLMReply:
        payload = self._payload(messages, tools, stream=False)
        with self._open(self._build_request(payload), self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_complete(data)

    # ------------------------------------------------------------ 流式
    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None = None,
    ) -> Iterator[StreamPart]:
        payload = self._payload(messages, tools, stream=True)
        acc = _ToolCallAccumulator()
        text_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None

        with self._open(self._build_request(payload), self._timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise LLMError(str(chunk["error"]))
                # include_usage 时，最后一个 chunk 的 choices 为空但带有 usage
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    text_parts.append(text)
                    yield TextDelta(text)
                if delta.get("tool_calls"):
                    acc.add(delta["tool_calls"])
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]

        message = acc.build()
        message["content"] = "".join(text_parts) or message["content"]
        yield ReplyArrived(message=message, finish_reason=finish_reason, usage=usage)


# ---------------------------------------------------------------------- 辅助
def _parse_complete(data: dict[str, Any]) -> LLMReply:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"响应缺少 choices: {json.dumps(data, ensure_ascii=False)[:300]}")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise LLMError("响应缺少 message")
    return LLMReply(
        message=message,
        finish_reason=choice.get("finish_reason"),
        usage=data.get("usage"),
    )


class _ToolCallAccumulator:
    """合并流式 ``delta.tool_calls`` 分片为完整 tool_calls 列表。"""

    #: index -> 结构体
    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}

    def add(self, deltas: Sequence[dict[str, Any]]) -> None:
        for item in deltas:
            index = int(item.get("index", 0))
            slot = self._calls.setdefault(
                index,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if item.get("id"):
                slot["id"] = item["id"]
            fn = item.get("function") or {}
            name = fn.get("name")
            if name:
                # 多数网关 name 只出现一次；个别网关重复发送完整 name，做去重保护
                current = slot["function"]["name"]
                slot["function"]["name"] = name if not current or current == name else current + name
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    def build(self) -> dict[str, Any]:
        """组装完整 assistant 消息（content 由调用方补全）。"""
        message: dict[str, Any] = {"role": "assistant", "content": None}
        if self._calls:
            message["tool_calls"] = [self._calls[i] for i in sorted(self._calls)]
        return message


class DeepSeekClient(OpenAICompatClient):  # 语义化别名，便于前端/配置引用
    """DeepSeek 专用别名（协议与 OpenAI 兼容）。"""
