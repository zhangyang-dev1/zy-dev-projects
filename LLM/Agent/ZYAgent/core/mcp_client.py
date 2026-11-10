"""MCP Client：连接外部 MCP Server，把其工具并入 ZYAgent 的 Function Calling 流程。

层级：core / 资源层。
依赖：标准库 + MCP Python SDK（``mcp``，**惰性导入**——SDK 缺失只影响 MCP 工具，
本地工具照常运行）。

配置：启动时读取**当前项目目录**（程序启动时的工作目录）下的 ``.mcp.json``::

    {
      "mcpServers": {
        "Bazi-MCP": {"type": "streamable_http", "url": "https://.../mcp"},
        "local":    {"type": "stdio", "command": "python", "args": ["-m", "server"]}
      }
    }

设计要点
--------
* **协议**：stdio 与 Streamable HTTP（MCP Python SDK 标准接口，兼容 1.x/2.x 命名）；
* **发现**：连接后 ``list_tools()`` 取 name / description / inputSchema，注册进现有
  :class:`~core.tools.base.ToolRegistry`——LLM 侧与本地工具无差别（声明即契约）；
* **映射**：内部维护 ``暴露名 -> (server, 原始工具名)`` 路由表；不同 Server 工具名冲突
  时自动加服务器名前缀（如 ``Bazi-MCP_tool``），并做非法字符清洗与去重；
* **调用**：MCP SDK 是 asyncio 接口，本模块用**后台线程 + 独立事件循环**桥接同步的
  工具处理器（``run_coroutine_threadsafe``），引擎与前端零改动；
* **隔离**：每个 Server 独立连接栈；单个 Server 连接失败或工具调用失败，只让**该
  Server 的工具**返回 ``ok=False`` 清晰文本，本地工具与其他 Server 不受牵连；
  ``/mcp`` 命令可查看各 Server 连接状态。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .tools.base import ToolError, ToolRegistry, ToolSpec

#: 默认连接初始化超时（秒）：initialize + list_tools
DEFAULT_INIT_TIMEOUT = 15.0

#: 默认单次工具调用超时（秒）
DEFAULT_CALL_TIMEOUT = 60.0

#: .mcp.json 文件名（位于当前项目根目录）
MCP_CONFIG_NAME = ".mcp.json"


# ====================================================================== 配置
@dataclass(frozen=True)
class MCPServerConfig:
    """单个 MCP Server 的连接配置。"""

    name: str
    type: str                      # "stdio" | "streamable_http"
    command: str = ""              # stdio：可执行文件
    args: tuple[str, ...] = ()     # stdio：参数列表
    env: dict[str, str] = field(default_factory=dict)  # stdio：附加环境变量
    url: str = ""                  # streamable_http：服务端点
    headers: dict[str, str] = field(default_factory=dict)  # streamable_http：请求头


def load_mcp_config(path: Path | str) -> tuple[list[MCPServerConfig], list[str]]:
    """读取 ``.mcp.json``，返回 ``(server 列表, 配置问题列表)``。

    * 文件不存在 -> ``([], [])``（未配置 MCP 是正常状态）；
    * JSON 非法 / 缺 ``mcpServers`` / 单项配置非法 -> 记入问题列表（不抛异常，
      保证坏配置不影响 Agent 启动与其他工具）。
    """
    cfg_path = Path(path)
    if not cfg_path.is_file():
        return [], []
    try:
        # utf-8-sig：兼容 Windows 编辑器写出的 BOM（Notepad/PowerShell 常见）
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], [f"{MCP_CONFIG_NAME} 解析失败: {type(exc).__name__}: {exc}"]
    if not isinstance(data, dict):
        return [], [f"{MCP_CONFIG_NAME} 顶层必须是 JSON 对象"]
    raw = data.get("mcpServers")
    if raw is None:
        return [], [f"{MCP_CONFIG_NAME} 缺少 mcpServers 字段"]
    if not isinstance(raw, dict):
        return [], ["mcpServers 必须是对象（{服务器名: 配置}）"]

    servers: list[MCPServerConfig] = []
    errors: list[str] = []
    for name, entry in raw.items():
        name = str(name).strip()
        if not name:
            errors.append("存在空的服务器名，请检查 mcpServers 键")
            continue
        if not isinstance(entry, dict):
            errors.append(f"server '{name}': 配置必须是对象")
            continue

        # ---- type 归一化（别名兼容）与推断
        raw_type = str(entry.get("type") or "").strip().lower().replace("-", "_")
        if raw_type in ("streamable_http", "streamablehttp", "http"):
            kind = "streamable_http"
        elif raw_type == "stdio":
            kind = "stdio"
        elif raw_type == "":
            if entry.get("url"):
                kind = "streamable_http"
            elif entry.get("command"):
                kind = "stdio"
            else:
                errors.append(f"server '{name}': 缺少 type（支持 stdio / streamable_http）")
                continue
        else:
            errors.append(
                f"server '{name}': 不支持的 type '{entry.get('type')}'（支持 stdio / streamable_http）"
            )
            continue

        if kind == "stdio":
            command = str(entry.get("command") or "").strip()
            if not command:
                errors.append(f"server '{name}': stdio 类型必须提供 command")
                continue
            raw_args = entry.get("args") or []
            if not isinstance(raw_args, list):
                errors.append(f"server '{name}': args 必须是数组")
                continue
            raw_env = entry.get("env") or {}
            if not isinstance(raw_env, dict):
                errors.append(f"server '{name}': env 必须是对象")
                continue
            servers.append(MCPServerConfig(
                name=name, type="stdio", command=command,
                args=tuple(str(a) for a in raw_args),
                env={str(k): str(v) for k, v in raw_env.items()},
            ))
        else:
            url = str(entry.get("url") or "").strip()
            if not url:
                errors.append(f"server '{name}': streamable_http 类型必须提供 url")
                continue
            scheme = url.split("://", 1)[0].lower() if "://" in url else ""
            if scheme not in ("http", "https"):
                errors.append(f"server '{name}': url 仅支持 http/https（收到 {url!r}）")
                continue
            raw_headers = entry.get("headers") or {}
            if not isinstance(raw_headers, dict):
                errors.append(f"server '{name}': headers 必须是对象")
                continue
            servers.append(MCPServerConfig(
                name=name, type="streamable_http", url=url,
                headers={str(k): str(v) for k, v in raw_headers.items()},
            ))
    return servers, errors


# ====================================================================== 名称映射
def expose_tool_name(server: str, tool: str, taken: Iterable[str]) -> str:
    """计算工具对 LLM 暴露的名字：清洗为 ``[A-Za-z0-9_-]``，冲突则加服务器名前缀并去重。

    ``taken`` 为已占用名集合（本地工具 + 已注册的 MCP 工具）。无论原名还是前缀名
    都会清洗，保证符合 Function Calling 的工具命名约束（``^[A-Za-z0-9_-]{1,64}$``）。
    """
    taken_set = set(taken)
    raw = (tool or "").strip()
    base = re.sub(r"[^A-Za-z0-9_-]", "_", raw) or "tool"
    if base not in taken_set:
        return base
    prefixed = re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}_{raw}") or "tool"
    if prefixed not in taken_set:
        return prefixed
    index = 2
    while f"{prefixed}_{index}" in taken_set:
        index += 1
    return f"{prefixed}_{index}"


# ====================================================================== 结果整理
def _dump_result(result: Any) -> str:
    """pydantic 结果的 JSON 兜底（无文本块时给模型可见的结构）。"""
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        try:
            data = dump(mode="json", by_alias=True)
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001 - 兜底
            return ""
    return ""


def format_call_result(result: Any) -> str:
    """把 MCP ``CallToolResult`` 整理成给 LLM 的文本。

    * 文本块按序拼接；图片/资源等非文本块给出占位说明；
    * ``structured_content`` 以 JSON 附加；
    * ``is_error`` -> 抛 :class:`ToolError`（注册表转 ``ok=False``，错误即数据）。
    """
    is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        block_type = getattr(block, "type", "") or ""
        if block_type == "text":
            parts.append(getattr(block, "text", "") or "")
        elif block_type == "image":
            mime = getattr(block, "mimeType", "") or ""
            parts.append(f"[图片 {mime}，内容省略]")
        elif block_type in ("resource", "resource_link"):
            resource = getattr(block, "resource", None)
            uri = str(getattr(resource, "uri", "") or getattr(block, "uri", "") or "")
            parts.append(f"[资源 {uri}]" if uri else "[资源内容省略]")
        else:
            parts.append(f"[{block_type or '未知'} 内容省略]")

    structured = (getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured:
        parts.append("结构化数据:\n" + json.dumps(structured, ensure_ascii=False, indent=2))

    text = "\n".join(p for p in parts if p).strip()
    if is_error:
        raise ToolError(text or _dump_result(result) or "MCP 工具执行错误（服务器未返回详情）")
    if text:
        return text
    return _dump_result(result) or "(MCP 工具执行成功，但没有返回内容)"


# ====================================================================== 管理器
class MCPManager:
    """MCP 客户端管理器：后台事件循环 + 连接生命周期 + 工具路由。

    线程模型：
    * :meth:`start` 在调用方线程**阻塞**完成发现与注册（测试/CLI 友好）；
    * :meth:`start_async` 把 :meth:`start` 放到后台线程执行，TUI 可以先进入界面，
      再通过 :meth:`snapshot` + ``revision`` 轮询动态刷新 MCP 区域；
    * 工具调用从任意线程经 ``run_coroutine_threadsafe`` 投递到**专属后台线程**的
      事件循环上执行，结果同步返回给工具处理器。
    """

    def __init__(
        self,
        init_timeout: float = DEFAULT_INIT_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        config_name: str = MCP_CONFIG_NAME,
    ) -> None:
        self.init_timeout = float(init_timeout)
        self.call_timeout = float(call_timeout)
        self.config_name = config_name or MCP_CONFIG_NAME
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._atexit_registered = False
        self._started = False
        self._lock = threading.Lock()
        self._routes: dict[str, tuple[str, str]] = {}          # 暴露名 -> (server, 原始名)
        self._sessions: dict[str, Any] = {}                    # server -> ClientSession
        self._stacks: dict[str, Any] = {}                      # server -> AsyncExitStack
        self.registered: dict[str, list[str]] = {}             # server -> 暴露名列表
        self.server_status: dict[str, tuple[str, str]] = {}    # server -> (ok|error|pending, 说明)
        self.config_errors: list[str] = []                     # .mcp.json 解析问题
        #: "idle"（未启动）| "loading"（后台连接中）| "ready"（加载完成）
        self.state: str = "idle"
        #: 每次状态变化 +1，前端据此判断是否需要刷新（避免无谓重绘）
        self.revision: int = 0

    def _bump(self) -> None:
        """状态变化：递增版本号，供 UI 轮询感知。"""
        with self._lock:
            self.revision += 1

    # ------------------------------------------------------------ 生命周期
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """启动（或复用）后台事件循环线程。"""
        if self._loop is not None and self._loop.is_running():
            return self._loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=_run, daemon=True, name="zyagent-mcp")
        thread.start()
        ready.wait(timeout=5.0)
        self._loop, self._thread = loop, thread
        if not self._atexit_registered:
            atexit.register(self.stop)  # 进程退出时关闭连接栈
            self._atexit_registered = True
        return loop

    def start(self, root: Path | str, registry: ToolRegistry) -> None:
        """读取 ``<root>/.mcp.json``，连接各 Server 并把工具注册进 ``registry``。

        单个 Server 失败只记录错误，绝不抛出——本地工具与其他 Server 不受影响。
        """
        self._started = True
        if self.state == "idle":
            self.state = "loading"
            self._bump()
        try:
            config_path = Path(root) / self.config_name
            servers, config_errors = load_mcp_config(config_path)
            if config_errors:
                with self._lock:
                    self.config_errors.extend(config_errors)
            if not servers:
                return

            # 先标记「连接中」，让 TUI 在后台加载期间就能展示每个 Server 的进度
            with self._lock:
                for cfg in servers:
                    self.server_status.setdefault(cfg.name, ("pending", "连接中…"))
            self._bump()

            loop = self._ensure_loop()
            for cfg in servers:
                future = asyncio.run_coroutine_threadsafe(self._connect(cfg), loop)
                try:
                    tools = future.result(timeout=self.init_timeout + 5.0)
                except TimeoutError:
                    future.cancel()
                    self._record_error(cfg.name, f"连接超时（>{self.init_timeout:.0f} 秒）")
                    continue
                except Exception as exc:  # noqa: BLE001 - 单 Server 失败不影响整体
                    self._record_error(cfg.name, f"{type(exc).__name__}: {exc}")
                    continue
                try:
                    self._register(cfg, tools, registry)
                except Exception as exc:  # noqa: BLE001 - 注册失败同样只影响该 Server
                    self._close_server(cfg.name)
                    self._record_error(cfg.name, f"工具注册失败: {type(exc).__name__}: {exc}")
                    continue
                with self._lock:
                    self.server_status[cfg.name] = (
                        "ok", f"已连接（{len(tools)} 个工具）"
                    )
                self._bump()
        finally:
            self.state = "ready"
            self._bump()

    def start_async(self, root: Path | str, registry: ToolRegistry) -> None:
        """后台加载 MCP（不阻塞调用方）：先进入 TUI，加载完再动态刷新。"""
        if self._started:
            return
        self._started = True
        self.state = "loading"
        self._bump()

        def _worker() -> None:
            try:
                self.start(root, registry)
            except Exception as exc:  # noqa: BLE001 - 异步路径同样绝不拖垮引擎
                with self._lock:
                    self.config_errors.append(
                        f"MCP 初始化失败: {type(exc).__name__}: {exc}"
                    )
                self.state = "ready"
                self._bump()

        threading.Thread(
            target=_worker, daemon=True, name="zyagent-mcp-init"
        ).start()

    def stop(self) -> None:
        """关闭全部连接并停止后台线程（幂等，可重复调用）。"""
        loop, thread = self._loop, self._thread
        if loop is None:
            return
        self._loop, self._thread = None, None
        try:
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self._close_all(), loop)
                with contextlib.suppress(Exception):
                    future.result(timeout=5.0)
                loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=5.0)
        except Exception:  # noqa: BLE001 - 清理失败不阻塞退出
            pass
        with contextlib.suppress(Exception):
            loop.close()

    # ------------------------------------------------------------ 连接
    async def _connect(self, cfg: MCPServerConfig) -> list[Any]:
        """连接单个 Server：initialize -> list_tools，返回工具列表。"""
        # MCP SDK 惰性导入：缺失/版本差异只影响该 Server
        from mcp import ClientSession, StdioServerParameters

        stack = contextlib.AsyncExitStack()
        try:
            if cfg.type == "stdio":
                from mcp.client.stdio import stdio_client

                # 合并当前环境（Windows 需要 SYSTEMROOT/PATH 等），再叠加配置项
                env = {**os.environ, **cfg.env}
                params = StdioServerParameters(
                    command=cfg.command, args=list(cfg.args), env=env,
                )
                streams = await stack.enter_async_context(stdio_client(params))
            else:
                streams = await self._enter_http(stack, cfg)
            read, write = streams[0], streams[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=self.init_timeout)
            result = await asyncio.wait_for(session.list_tools(), timeout=self.init_timeout)
        except BaseException:
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise

        self._stacks[cfg.name] = stack
        self._sessions[cfg.name] = session
        return list(getattr(result, "tools", None) or [])

    async def _enter_http(self, stack: contextlib.AsyncExitStack, cfg: MCPServerConfig):
        """进入 Streamable HTTP 传输（兼容 SDK 1.x/2.x 不同入口名）。"""
        try:  # SDK 2.x
            from mcp.client.streamable_http import streamable_http_client as http_fn
            is_v2 = True
        except ImportError:  # SDK 1.x
            from mcp.client.streamable_http import streamablehttp_client as http_fn
            is_v2 = False

        kwargs: dict[str, Any] = {}
        if cfg.headers:
            if is_v2:
                from mcp.client.streamable_http import create_mcp_http_client

                http_client = await stack.enter_async_context(
                    create_mcp_http_client(headers=cfg.headers)
                )
                kwargs["http_client"] = http_client
            else:
                kwargs["headers"] = cfg.headers
        return await stack.enter_async_context(http_fn(cfg.url, **kwargs))

    def _close_server(self, name: str) -> None:
        stack = self._stacks.pop(name, None)
        self._sessions.pop(name, None)
        if stack is None or self._loop is None or not self._loop.is_running():
            return
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(stack.aclose(), self._loop).result(timeout=5.0)

    async def _close_all(self) -> None:
        for name in list(self._stacks):
            stack = self._stacks.pop(name)
            with contextlib.suppress(Exception):
                await stack.aclose()
        self._sessions.clear()

    def _record_error(self, server: str, message: str) -> None:
        with self._lock:
            self.server_status[server] = ("error", message)
        self._bump()

    # ------------------------------------------------------------ 注册
    def _register(self, cfg: MCPServerConfig, tools: Iterable[Any], registry: ToolRegistry) -> None:
        """把 Server 的工具注册进注册表（冲突加前缀），并写入路由表。"""
        exposed_names: list[str] = []
        for tool in tools:
            original = str(getattr(tool, "name", "") or "").strip()
            if not original:
                continue
            exposed = expose_tool_name(cfg.name, original, registry.names)
            description = str(getattr(tool, "description", "") or "").strip()
            schema = (getattr(tool, "input_schema", None)
                      or getattr(tool, "inputSchema", None))
            if not isinstance(schema, dict) or not schema:
                schema = {"type": "object", "properties": {}, "required": []}

            registry.register(ToolSpec(
                name=exposed,
                description=description or f"MCP 工具 {original}（来自 Server {cfg.name}）",
                parameters=schema,
                handler=self._make_handler(exposed),
            ))
            self._routes[exposed] = (cfg.name, original)
            exposed_names.append(exposed)
        if not exposed_names:
            raise ValueError("Server 未提供任何可用工具")
        with self._lock:
            self.registered[cfg.name] = exposed_names

    def _make_handler(self, exposed: str):
        """构造同步工具处理器：LLM 的 function call -> MCP 异步调用。"""

        def handler(arguments: dict[str, Any]) -> str:
            return self.call_tool(exposed, arguments)

        return handler

    # ------------------------------------------------------------ 调用
    def call_tool(self, exposed: str, arguments: dict[str, Any]) -> str:
        """按路由表把工具调用转发到对应 Server，返回整理后的文本。"""
        route = self._routes.get(exposed)
        if route is None:
            raise ToolError(f"未知的 MCP 工具: {exposed}")
        server, tool_name = route
        session = self._sessions.get(server)
        if session is None:
            detail = self.server_status.get(server, ("", "未连接"))[1]
            raise ToolError(f"MCP Server '{server}' 未连接（{detail}）")
        loop = self._loop
        if loop is None or not loop.is_running():
            raise ToolError("MCP 客户端未运行，无法调用工具（请重启 ZYAgent）")

        future = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(session.call_tool(tool_name, arguments or {}),
                             timeout=self.call_timeout),
            loop,
        )
        try:
            result = future.result(timeout=self.call_timeout + 5.0)
        except TimeoutError:
            future.cancel()
            raise ToolError(
                f"MCP 工具调用超时（{self.call_timeout:.0f} 秒）: {server}.{tool_name}"
            ) from None
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - 传输中断/服务器错误 -> 清晰文本
            raise ToolError(
                f"MCP Server '{server}' 调用 {tool_name} 失败: {type(exc).__name__}: {exc}"
            ) from exc
        return format_call_result(result)

    # ------------------------------------------------------------ 状态
    def snapshot(self) -> dict[str, Any]:
        """结构化状态快照（供 TUI 侧栏动态渲染）。"""
        with self._lock:
            servers = [
                {
                    "name": name,
                    "kind": kind,
                    "detail": detail,
                    "tools": list(self.registered.get(name, [])),
                }
                for name, (kind, detail) in self.server_status.items()
            ]
            state = self.state
            revision = self.revision
            started = self._started
            errors = list(self.config_errors)
        return {
            "state": state,
            "revision": revision,
            "started": started,
            "servers": servers,
            "errors": errors,
            "config_name": self.config_name,
        }

    def status(self) -> str:
        """人类可读的连接状态（供 /mcp 命令与排障使用）。"""
        with self._lock:
            entries = list(self.server_status.items())
            errors = list(self.config_errors)
            state = self.state
            registered = {name: list(names) for name, names in self.registered.items()}
        lines: list[str] = []
        if not entries and not errors:
            if state == "loading":
                return f"MCP：正在加载 {self.config_name}…"
            return f"MCP：未配置 {self.config_name}（当前没有外部 MCP Server）"
        for name, (kind, detail) in entries:
            if kind == "ok":
                names = ", ".join(registered.get(name, []))
                lines.append(f"OK  {name}: {detail} — {names}")
            elif kind == "pending":
                lines.append(f"…   {name}: {detail}")
            else:
                lines.append(f"FAIL {name}: {detail}")
        for err in errors:
            lines.append(f"WARN {err}")
        suffix = "（加载中…）" if state == "loading" else ""
        return f"MCP Server 状态{suffix}:\n" + "\n".join(lines)
