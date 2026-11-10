"""引擎层：编排"提问 -> LLM -> 工具调用 -> 最终回答"的完整循环。

层级：core / 编排层（应用服务）。

* 对外唯一入口 :meth:`AgentEngine.ask`，返回 **事件流迭代器**（契约见 ``events.py``）；
* 依赖倒置：LLM 客户端与工具注册表均可注入，core 不依赖任何 UI；
* 线程模型：一次 ``ask()`` 顺序执行、不并行；前端在后台逐步消费即可
  （TUI 用 ``asyncio.to_thread`` 逐事件步进，Web 可包成 async 生成器）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import (
    ContextConfig,
    LLMConfig,
    MCPConfig,
    SearchConfig,
    SessionConfig,
    UIConfig,
)
from .context import (
    ContextUsage,
    build_compact_request,
    build_compacted_messages,
    context_directory,
    estimate_messages_tokens,
    estimate_tokens,
    new_archive_id,
    save_archive,
    usage_total_tokens,
)
from .events import (
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
from .history import SessionInfo, SessionStore, new_session_id
from .llm.base import LLMClient, LLMError, ReplyArrived, TextDelta
from .llm.openai_compat import OpenAICompatClient
from .mcp_client import MCPManager
from .session import ConversationSession
from .skills import Skill, build_skills_prompt, discover_skills
from .tools import ToolRegistry, ToolResult, build_default_registry
from .tools.recall import RECALL_TOOL_NAME, build_recall_tool
from .tools.skill import (
    CREATE_SKILL_TOOL_NAME,
    SKILL_TOOL_NAME,
    build_create_skill_tool,
    build_load_skill_tool,
)

#: 项目说明文件（位于项目根，与 .agents/skills 同根）
AGENTS_MD_NAME = "AGENTS.md"

#: AGENTS.md 注入消息的前缀（用于去重与压缩后保留）
AGENTS_MD_PREFIX = "【项目上下文 · AGENTS.md】"


@dataclass(frozen=True)
class _Final:
    """内部标记：一次请求完成，携带权威 assistant 消息与 API 用量。"""

    message: dict[str, Any]
    usage: dict[str, Any] | None = None


class _Cancelled(Exception):
    """内部信号：用户请求停止（流式传输中途生效）。"""


@dataclass(frozen=True)
class _ToolCallPair:
    """内部载体：单个工具调用的起止事件。"""

    started: ToolCallStarted
    finished: ToolCallFinished


class AgentEngine:
    """与界面无关的 Agent 引擎（所有前端共用同一个类）。

    用法（任意前端通用）::

        engine = AgentEngine(config)
        for event in engine.ask("当前目录下有哪些文件"):
            ...  # 按 events.py 契约渲染
    """

    def __init__(
        self,
        config: LLMConfig,
        registry: ToolRegistry | None = None,
        client: LLMClient | None = None,
        session: ConversationSession | None = None,
        search: SearchConfig | None = None,
        skills_root: Path | str | None = None,
        mcp_root: Path | str | None = None,
        context_config: ContextConfig | None = None,
        mcp_config: MCPConfig | None = None,
        session_config: SessionConfig | None = None,
        ui_config: UIConfig | None = None,
        history: SessionStore | None = None,
        mcp_async: bool | None = None,
    ) -> None:
        self.config = config
        self.search = search if search is not None else SearchConfig()
        self.context_config = context_config or ContextConfig()
        self.mcp_config = mcp_config or MCPConfig()
        self.session_config = session_config or SessionConfig()
        self.ui_config = ui_config or UIConfig()

        # ---- Skill 动态加载：初始化仅发现 name/description（不读取正文）
        self.skills_root = Path(skills_root) if skills_root is not None else Path.cwd()
        self.skills: tuple[Skill, ...] = tuple(discover_skills(self.skills_root))
        self._system_prompt = self._compose_system_prompt()
        #: AGENTS.md 正文（不存在时为 None）；「一次注入，全程复用」
        self._agents_md_text = self._read_agents_md()

        self.registry = registry if registry is not None else build_default_registry(
            self.search
        )
        # createSkill 恒注册（即使尚无技能也可从零创建）；loadSkill 仅在发现技能后注册
        if CREATE_SKILL_TOOL_NAME not in self.registry:
            self.registry.register(build_create_skill_tool(self.skills_root))
        if self.skills and SKILL_TOOL_NAME not in self.registry:
            self.registry.register(build_load_skill_tool(self.skills, root=self.skills_root))
        # recallContext 恒注册：检索 .agents/context/ 下的历史上下文归档
        if RECALL_TOOL_NAME not in self.registry:
            self.registry.register(
                build_recall_tool(self.skills_root, self.context_config)
            )

        # ---- MCP：读取当前项目目录 .mcp.json，连接 Server 并注册其工具
        # （默认异步：先进入 TUI，后台加载完成后由界面轮询 snapshot() 动态刷新）
        self.mcp_root = Path(mcp_root) if mcp_root is not None else self.skills_root
        self.mcp = MCPManager(
            init_timeout=self.mcp_config.init_timeout,
            call_timeout=self.mcp_config.call_timeout,
            config_name=self.mcp_config.config_file,
        )
        if self.mcp_config.enabled:
            use_async = self.mcp_config.async_load if mcp_async is None else mcp_async
            try:
                if use_async:
                    self.mcp.start_async(self.mcp_root, self.registry)
                else:
                    self.mcp.start(self.mcp_root, self.registry)
            except Exception as exc:  # noqa: BLE001 - MCP 绝不阻断本地工具
                self.mcp.config_errors.append(
                    f"MCP 初始化失败: {type(exc).__name__}: {exc}"
                )
                self.mcp.state = "ready"

        self.client = client if client is not None else OpenAICompatClient(
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            timeout=config.timeout,
        )
        self.session = session if session is not None else ConversationSession(
            system_prompt=self._system_prompt
        )

        # ---- 上下文压缩状态（/compact 与阈值监控）
        self.context_dir = context_directory(
            self.skills_root, self.context_config.archive_dir
        )
        self._compacting = False
        self._blocked = False            # 上下文达到阈值后暂停普通提问
        self._last_api_tokens: int | None = None  # 最近一次 API 返回的上下文用量

        # ---- 会话持久化（/save、/new、/resume）
        self.history = history or SessionStore(
            self.skills_root,
            directory=self.session_config.dir,
            max_listed=self.session_config.max_listed,
        )
        self.session_id = new_session_id()
        self.session_title = ""
        self._ensure_agents_md()

    # ---------------------------------------------------------------- 内部构造
    def _compose_system_prompt(self) -> str:
        """原 System Prompt + 可用 Skill 清单（无 Skill 时保持原样）。"""
        fragment = build_skills_prompt(self.skills)
        base = (self.config.system_prompt or "").strip()
        if not fragment:
            return base
        return f"{base}\n\n{fragment}" if base else fragment

    # ---------------------------------------------------------------- AGENTS.md
    @property
    def agents_md_path(self) -> Path:
        return self.skills_root / AGENTS_MD_NAME

    @property
    def agents_md_injected(self) -> bool:
        """AGENTS.md 是否已经在当前会话历史中（只注入一次）。"""
        return any(self._is_agents_message(m) for m in self.session.messages)

    def _read_agents_md(self) -> str | None:
        """读取项目根 AGENTS.md（UTF-8，兼容 BOM）；不存在/为空返回 None。"""
        path = self.agents_md_path
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return text or None

    @staticmethod
    def _is_agents_message(message: dict[str, Any]) -> bool:
        return (
            message.get("role") == "user"
            and str(message.get("content") or "").startswith(AGENTS_MD_PREFIX)
        )

    def _ensure_agents_md(self) -> bool:
        """首次加载会话时把 AGENTS.md 作为一条消息插入历史（已存在则跳过）。

        采用「一次注入，全程复用」：消息进入聊天历史后会随会话一起持久化，
        后续轮次直接复用同一前缀，保证 Prompt Cache 命中；压缩时也会保留。
        位置固定在 System Prompt 之后（无 System Prompt 时为首条），保证前缀稳定。
        """
        if not self._agents_md_text or self.agents_md_injected:
            return False
        content = f"{AGENTS_MD_PREFIX}\n\n{self._agents_md_text}"
        messages = self.session.messages
        index = 1 if messages and messages[0].get("role") == "system" else 0
        messages.insert(index, {"role": "user", "content": content})
        self.session.replace(messages)
        return True

    def agents_md_status(self) -> str:
        """``/agents`` 命令的人类可读状态。"""
        path = self.agents_md_path
        if not self._agents_md_text:
            return f"未发现 {AGENTS_MD_NAME}（查找位置：{path}）"
        injected = "已注入" if self.agents_md_injected else "未注入"
        return (
            f"已加载 {path}\n"
            f"注入状态：{injected}（一次注入，全程复用；压缩后仍保留）\n"
            f"内容长度：{len(self._agents_md_text)} 字符"
        )

    # ---------------------------------------------------------------- 状态
    @property
    def tools(self) -> list[dict[str, Any]]:
        """发送给 LLM 的工具声明（前端状态栏可展示）。"""
        return self.registry.schemas

    @property
    def compacting(self) -> bool:
        """是否正在压缩上下文（压缩期间不处理任何普通任务/工具调用）。"""
        return self._compacting

    @property
    def input_blocked(self) -> bool:
        """上下文是否已达到阈值（暂停接受普通提示词，等待 /compact）。"""
        return self._blocked

    def context_usage(self) -> ContextUsage:
        """当前会话的上下文占用快照。

        优先使用模型 API 返回的 token 用量；拿不到时按本地规则估算
        （System Prompt + 对话历史 + 工具调用消息 + 工具声明）。
        """
        threshold = self.context_config.warning_ratio
        if self._last_api_tokens is not None:
            return ContextUsage(
                used=self._last_api_tokens,
                window=self.config.context_window,
                source="api",
                threshold=threshold,
            )
        used = estimate_messages_tokens(
            self.session.messages,
            self.context_config.ascii_chars_per_token,
            self.context_config.message_overhead_tokens,
        )
        if self.registry.schemas:
            used += estimate_tokens(
                json.dumps(self.registry.schemas, ensure_ascii=False),
                self.context_config.ascii_chars_per_token,
            )
        return ContextUsage(
            used=used,
            window=self.config.context_window,
            source="estimate",
            threshold=threshold,
        )

    def reset(self) -> None:
        """清空会话历史（保留 System Prompt），并重新注入 AGENTS.md。"""
        self.session.reset(system_prompt=self._system_prompt)
        self.session_id = new_session_id()
        self.session_title = ""
        self._blocked = False
        self._last_api_tokens = None
        self._ensure_agents_md()

    # ---------------------------------------------------------------- 会话持久化
    def _has_content(self) -> bool:
        """会话是否有值得保存的对话（排除 system 与 AGENTS.md 注入消息）。"""
        return any(
            m.get("role") != "system" and not self._is_agents_message(m)
            for m in self.session.messages
        )

    def save_session(self, title: str | None = None) -> Path:
        """保存当前会话为 JSONL 事件日志，返回文件路径。"""
        if title and title.strip():
            self.session_title = title.strip()
        return self.history.save(
            self.session_id,
            self.session.messages,
            title=self.session_title,
            model=self.config.model,
        )

    def new_session(self, title: str | None = None) -> Path | None:
        """新建会话：先自动保存当前会话（如有内容），再重置历史。

        :returns: 已保存的旧会话路径；没有可保存内容时为 None。
        """
        saved: Path | None = None
        if self.session_config.enabled and self._has_content():
            try:
                saved = self.save_session(title)
            except (OSError, ValueError):
                saved = None
        self.reset()
        return saved

    def list_sessions(self) -> list[SessionInfo]:
        """列出已保存会话（新 → 旧）。"""
        return self.history.list()

    def resume_session(self, session_id: str) -> SessionInfo:
        """恢复一个已保存会话（替换当前历史，并重新注入缺失的 AGENTS.md）。"""
        info, messages = self.history.load(session_id)
        restored = [dict(m) for m in messages]

        # System Prompt 使用当前配置（Skill 清单可能已变化），保证工具/技能声明一致
        if self._system_prompt:
            if restored and restored[0].get("role") == "system":
                restored[0] = {"role": "system", "content": self._system_prompt}
            else:
                restored.insert(0, {"role": "system", "content": self._system_prompt})

        self.session.replace(restored)
        self.session_id = info.id
        self.session_title = info.title
        self._blocked = False
        self._last_api_tokens = None
        self._ensure_agents_md()
        return info

    def _autosave(self) -> None:
        """每轮结束后的自动保存（失败不影响对话）。"""
        if not (self.session_config.enabled and self.session_config.auto_save):
            return
        if not self._has_content():
            return
        try:
            self.save_session()
        except (OSError, ValueError):
            pass

    def export_markdown(self, path: str | Path | None = None) -> Path:
        """把当前会话导出为 Markdown，返回文件路径。"""
        target = Path(path).expanduser() if path else (
            self.history.directory.parent / "exports" / f"{self.session_id}.md"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        title = self.session_title or self.session_id
        lines = [
            f"# {title}",
            "",
            f"- 会话编号：{self.session_id}",
            f"- 模型：{self.config.model}",
            f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        for message in self.session.messages:
            role = message.get("role")
            content = str(message.get("content") or "").strip()
            if role == "system" or self._is_agents_message(message):
                continue
            if role == "user":
                if content:
                    lines.extend(["## 用户", "", content, ""])
            elif role == "assistant":
                if content:
                    lines.extend(["## 助手", "", content, ""])
                for call in message.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    lines.append(
                        f"> 调用工具 `{fn.get('name')}`：`{fn.get('arguments')}`"
                    )
                    lines.append("")
            elif role == "tool":
                lines.extend(["### 工具结果", "", "```", content, "```", ""])
        target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return target

    # ---------------------------------------------------------------- 主循环
    def ask(
        self,
        question: str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[AgentEvent]:
        """执行一次提问，产出事件流。

        事件顺序::

            TurnStarted
            [AssistantDelta] * n                    (流式增量)
            AssistantMessage                        (权威全文)
            [ToolCallStarted, ToolCallFinished] * n (如有工具调用，则继续下一轮请求)
            ...
            TurnFinished                            (正常/出错/停止均以其收尾)

        :param question: 用户问题
        :param is_cancelled: 取消回调；返回 True 时引擎在安全点停止并以
                             ``TurnFinished`` 收尾（不会中断进行中的 HTTP 请求）
        """
        question = (question or "").strip()
        if not question:
            yield AgentError("问题不能为空")
            yield TurnFinished(answer="")
            return

        # ---- 压缩期间不接受任何普通任务（避免与摘要生成相互干扰）
        if self._compacting:
            yield AgentError("正在压缩上下文，请等待压缩完成后再继续")
            yield TurnFinished(answer="")
            return

        # ---- 上下文达到阈值：暂停接受新的普通提示词，直到 /compact 成功
        usage = self.context_usage()
        if self._blocked or usage.blocked:
            self._blocked = True
            yield AgentError(self._blocked_message(usage))
            yield TurnFinished(answer="")
            return

        cancelled = is_cancelled or (lambda: False)
        self.session.add_user(question)
        yield TurnStarted(question=question)

        answer = ""
        stopped = False
        rounds = max(1, self.config.max_tool_rounds)

        for _ in range(rounds):
            if stopped or cancelled():
                if not stopped:
                    yield AgentError("已停止")
                stopped = True
                break

            # ---- 1. 请求模型（流式优先；异常统一转 AgentError，不打断 UI）
            message: dict[str, Any] | None = None
            try:
                for item in self._request_events(cancelled):
                    if isinstance(item, _Final):
                        message = item.message
                        self._record_usage(item.usage)
                    else:
                        yield item  # AssistantDelta（边到达边渲染）
            except _Cancelled:
                yield AgentError("已停止")
                stopped = True
                break
            except LLMError as exc:
                yield AgentError(f"请求失败: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 - 引擎兜底，保证 TurnFinished
                yield AgentError(f"请求失败: {type(exc).__name__}: {exc}")
                break

            if message is None:
                yield AgentError("模型未返回消息")
                break

            # ---- 2. 汇报助手消息（Message 为权威全文，UI 侧"设置"而非"追加"）
            self.session.add_assistant(message)
            text = message.get("content") or ""
            yield AssistantMessage(text=text)
            if text.strip():
                answer = text.strip()

            # ---- 3. 无工具调用 => 本轮完成
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                break

            # ---- 4. 顺序执行工具，结果回填会话后进入下一轮请求
            for tc in tool_calls:
                if cancelled():
                    yield AgentError("已停止")
                    stopped = True
                    break
                pair = self._run_tool_call(tc)
                yield pair.started
                yield pair.finished
                self.session.add_tool_result(pair.finished.id, pair.finished.output)
            # 内层 break（停止）由外层顶部统一处理；正常则继续下一轮
        else:
            yield AgentError(f"超出最大工具调用轮数（{rounds}），已停止")

        # ---- 本轮结束后检查上下文占用：达到阈值则警告并暂停普通提问
        usage = self.context_usage()
        if usage.blocked:
            self._blocked = True
            yield ContextWarning(message=self._blocked_message(usage))

        self._autosave()
        yield TurnFinished(answer=answer)

    # ---------------------------------------------------------------- 内部
    def _request_events(
        self, is_cancelled: Callable[[], bool]
    ) -> Iterator[AgentEvent | _Final]:
        """请求一次模型回复。

        流式：逐个产出 :class:`AssistantDelta`，最后产出 :class:`_Final`；
        非流式：仅产出 :class:`_Final`。
        中途收到取消请求时抛出 :class:`_Cancelled`（已流出的增量保留在 UI 上）。
        """
        tools = self.registry.schemas or None
        messages = self.session.messages

        if self.config.stream and self.client.supports_stream:
            final: _Final | None = None
            text_parts: list[str] = []
            for part in self.client.stream(messages, tools):
                if is_cancelled():
                    raise _Cancelled
                if isinstance(part, TextDelta):
                    text_parts.append(part.text)
                    yield AssistantDelta(text=part.text)
                elif isinstance(part, ReplyArrived):
                    final = _Final(message=dict(part.message), usage=part.usage)
            if final is None:
                raise LLMError("流式响应未返回完整消息")
            if text_parts:
                final.message["content"] = "".join(text_parts)
            yield final
            return

        reply = self.client.complete(messages, tools)
        yield _Final(message=dict(reply.message), usage=reply.usage)

    def _record_usage(self, usage: dict[str, Any] | None) -> None:
        """记录 API 返回的 token 用量（用于上下文监控）。"""
        tokens = usage_total_tokens(usage)
        if tokens is not None:
            self._last_api_tokens = tokens

    def _blocked_message(self, usage: ContextUsage | None = None) -> str:
        """上下文达到阈值时的提示文本。"""
        usage = usage or self.context_usage()
        percent = int(self.context_config.warning_ratio * 100)
        return (
            f"上下文已占用 {usage.describe()}，达到模型窗口的 "
            f"{percent}% 上限，已暂停接收新的提示词。"
            "请执行 /compact 压缩上下文后再继续。"
        )

    # ---------------------------------------------------------------- 上下文压缩
    def compact(
        self, is_cancelled: Callable[[], bool] | None = None
    ) -> Iterator[AgentEvent]:
        """手动压缩上下文（``/compact`` 的实现）。

        事件顺序::

            CompactStarted(archive_id)
            [AssistantDelta] * n        (摘要流式生成)
            AssistantMessage            (摘要全文)
            CompactFinished(ok=True)

        流程：**先归档原始上下文**（必须成功）→ 仅调用 LLM 生成摘要（不传工具，
        禁止 Function Calling）→ 用摘要替换会话历史（保留 System Prompt 与工具定义）。
        任何失败都以 ``AgentError`` + ``CompactFinished(ok=False)`` 收尾，且
        **不修改**原有会话历史，可重新执行 ``/compact``。
        """
        cancelled = is_cancelled or (lambda: False)
        if self._compacting:
            yield AgentError("上下文压缩已在进行中，请稍候")
            yield CompactFinished(ok=False, archive_id="", message="压缩已在进行中")
            return

        history = self.session.messages
        if not any(
            m.get("role") != "system" and not self._is_agents_message(m)
            for m in history
        ):
            yield AgentError("当前会话没有可压缩的对话历史")
            yield CompactFinished(ok=False, archive_id="", message="没有可压缩的历史")
            return

        self._compacting = True
        try:
            started_at = datetime.now()
            archive_id = new_archive_id(self.context_dir, started_at)
            yield CompactStarted(archive_id=archive_id)

            # ---- 1. 先保存完整原始上下文（失败则中止，绝不替换）
            try:
                save_archive(self.context_dir, archive_id, history)
            except (OSError, TypeError, ValueError) as exc:
                yield AgentError(
                    f"原始上下文归档失败，已取消压缩：{type(exc).__name__}: {exc}"
                )
                yield CompactFinished(
                    ok=False, archive_id=archive_id,
                    message="归档失败，原上下文未改变",
                )
                return

            # ---- 2. 仅调用 LLM 生成摘要（不传工具，禁止工具调用）
            final: _Final | None = None
            try:
                for item in self._summary_events(history, cancelled):
                    if isinstance(item, _Final):
                        final = item
                    else:
                        yield item  # AssistantDelta
                if final is None:
                    raise LLMError("模型未返回摘要")
                summary = str(final.message.get("content") or "").strip()
                if not summary:
                    raise LLMError("模型返回的摘要为空")
            except _Cancelled:
                yield AgentError("已取消压缩，原上下文保持不变")
                yield CompactFinished(
                    ok=False, archive_id=archive_id,
                    message="已取消，原上下文未改变",
                )
                return
            except LLMError as exc:
                yield AgentError(
                    f"上下文压缩失败：{exc}（原上下文保持不变，可重新执行 /compact）"
                )
                yield CompactFinished(
                    ok=False, archive_id=archive_id,
                    message="摘要生成失败，原上下文未改变",
                )
                return
            except Exception as exc:  # noqa: BLE001 - 保证 CompactFinished
                yield AgentError(
                    f"上下文压缩失败：{type(exc).__name__}: {exc}"
                    "（原上下文保持不变，可重新执行 /compact）"
                )
                yield CompactFinished(
                    ok=False, archive_id=archive_id,
                    message="摘要生成失败，原上下文未改变",
                )
                return

            yield AssistantMessage(text=summary)

            # ---- 3. 用摘要替换历史（保留 System Prompt 与 AGENTS.md；工具定义原样保留）
            compacted_at = started_at.strftime("%Y-%m-%d %H:%M:%S")
            extra = [m for m in history if self._is_agents_message(m)]
            new_messages = build_compacted_messages(
                self._system_prompt, summary, archive_id, compacted_at,
                extra_messages=extra,
            )
            self.session.replace(new_messages)
            self._last_api_tokens = None
            self._blocked = False

            tokens = estimate_messages_tokens(
                new_messages,
                self.context_config.ascii_chars_per_token,
                self.context_config.message_overhead_tokens,
            )
            self._autosave()
            yield CompactFinished(
                ok=True,
                archive_id=archive_id,
                message=(
                    f"上下文压缩完成：原始上下文已归档为 {archive_id}.jsonl，"
                    f"会话已替换为约 {tokens} tokens 的摘要"
                ),
            )
        finally:
            self._compacting = False

    def _summary_events(
        self,
        history: list[dict[str, Any]],
        is_cancelled: Callable[[], bool],
    ) -> Iterator[AssistantDelta | _Final]:
        """请求 LLM 生成压缩摘要（不传工具声明，因此不可能发生工具调用）。"""
        request = build_compact_request(history)

        if self.config.stream and self.client.supports_stream:
            final: _Final | None = None
            text_parts: list[str] = []
            for part in self.client.stream(request, None):
                if is_cancelled():
                    raise _Cancelled
                if isinstance(part, TextDelta):
                    text_parts.append(part.text)
                    yield AssistantDelta(text=part.text)
                elif isinstance(part, ReplyArrived):
                    final = _Final(message=dict(part.message), usage=part.usage)
            if final is None:
                raise LLMError("流式响应未返回完整消息")
            if text_parts:
                final.message["content"] = "".join(text_parts)
            yield final
            return

        reply = self.client.complete(request, None)
        yield _Final(message=dict(reply.message), usage=reply.usage)

    def _run_tool_call(self, tc: dict[str, Any]) -> _ToolCallPair:
        """执行单个工具调用，构造起止事件（错误即数据，绝不抛出）。"""
        call_id = str(tc.get("id") or "")
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        arguments = self.registry.parse_arguments(fn.get("arguments"))

        started = ToolCallStarted(id=call_id, name=name, arguments=arguments)

        t0 = time.perf_counter()
        if "__parse_error__" in arguments:
            result = ToolResult(ok=False, output="错误: arguments 不是合法 JSON")
        elif not name:
            result = ToolResult(ok=False, output="错误: tool_call 缺少函数名")
        else:
            result = self.registry.execute(name, arguments)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        finished = ToolCallFinished(
            id=call_id, name=name, ok=result.ok, output=result.output,
            duration_ms=round(elapsed_ms, 1),
        )
        return _ToolCallPair(started=started, finished=finished)
