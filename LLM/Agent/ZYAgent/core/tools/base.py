"""工具层基础：工具规格（Schema 契约）、注册表与执行路由。

层级：core / 资源层。

设计要点
--------
* **声明即契约**：``ToolSpec.to_schema()`` 产出的 JSON Schema 发送给 LLM，
  ``spec.name`` 同时是 :meth:`ToolRegistry.execute` 的路由键，两者天然一致（消除手工同步）。
* **错误即数据**：工具抛出的异常由注册表统一捕获并转成 ``ok=False`` 的文本结果，
  由模型自行纠正，绝不中断整个对话。
* **可扩展**：新增工具只需构造一个 :class:`ToolSpec` 并注册，UI 与引擎零改动。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

#: 工具函数签名：接收解析后的参数字典，返回给 LLM 的文本结果
ToolHandler = Callable[[dict[str, Any]], str]


class ToolError(Exception):
    """工具级业务错误（消息会原样回传给模型）。"""


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。"""

    ok: bool
    output: str


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的完整契约：名称 + 描述 + 参数 Schema + 实现。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}, "required": []})
    handler: ToolHandler = field(repr=False, default=lambda args: "")

    def to_schema(self) -> dict[str, Any]:
        """OpenAI function-calling 风格的工具声明。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """工具注册表：负责注册、声明导出、按名路由执行。

    线程安全：MCP 工具可能在后台线程异步注册，而引擎/UI 同时读取工具声明，
    因此注册与查询都用同一把可重入锁保护（执行处理器时不持锁）。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 注册
    def register(self, spec: ToolSpec) -> "ToolRegistry":
        """注册一个工具规格，返回自身（可链式调用）。"""
        if not spec.name:
            raise ValueError("工具名不能为空")
        with self._lock:
            self._tools[spec.name] = spec
        return self

    def register_many(self, specs: Sequence[ToolSpec]) -> "ToolRegistry":
        for spec in specs:
            self.register(spec)
        return self

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """装饰器式注册::

            @registry.tool("echo", "回显文本", {"type": "object",
                             "properties": {"text": {"type": "string"}}, "required": ["text"]})
            def echo(args):
                return args["text"]
        """

        def decorator(func: ToolHandler) -> ToolHandler:
            self.register(ToolSpec(name=name, description=description, handler=func,
                                   parameters=parameters or {"type": "object", "properties": {}, "required": []}))
            return func

        return decorator

    # ---------------------------------------------------------------- 查询
    @property
    def schemas(self) -> list[dict[str, Any]]:
        """随请求发送给 LLM 的工具声明列表。"""
        with self._lock:
            specs = list(self._tools.values())
        return [spec.to_schema() for spec in specs]

    @property
    def names(self) -> list[str]:
        with self._lock:
            return list(self._tools)

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    # ---------------------------------------------------------------- 执行
    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """按名执行工具；任何异常都转成 ``ok=False`` 的文本（错误即数据）。"""
        with self._lock:
            spec = self._tools.get(name)
        if spec is None:
            return ToolResult(ok=False, output=f"错误: 未知工具 {name}")
        args = arguments or {}
        try:
            output = spec.handler(args)
            return ToolResult(ok=True, output=output)
        except ToolError as exc:
            return ToolResult(ok=False, output=f"错误: {exc}")
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不让 UI/引擎崩溃
            return ToolResult(ok=False, output=f"错误: {type(exc).__name__}: {exc}")

    @staticmethod
    def parse_arguments(raw: str | bytes | None) -> dict[str, Any]:
        """解析 LLM 返回的 arguments JSON 字符串（失败返回空 dict 由执行阶段报告）。"""
        if not raw:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"__parse_error__": raw}
        return data if isinstance(data, dict) else {"__parse_error__": raw}
