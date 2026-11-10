"""ZYAgent 核心层（core）：与界面完全无关的业务逻辑。

分层结构（自下而上）::

    core/
    ├── 基础层   config.py   配置加载 / 首次运行自举
    │            events.py   事件契约（core -> UI 的唯一通信协议）
    ├── 资源层   llm/        LLM 客户端端口 + OpenAI 兼容适配器
    │            tools/      工具契约 + 注册表 + 18 个工具（基础 + searchWeb/fetchWeb/RunCli/loadSkill/createSkill/recallContext）
    │            skills.py   Skill 发现（.agents/skills）+ Frontmatter 解析 + Prompt 注入
    │            context.py  上下文管理：Token 估算 + 原始上下文归档 + 全文检索 + 压缩摘要
    │            history.py  会话持久化（.agents/sessions/<编号>.jsonl 事件日志）
    │            mcp_client.py MCP Client（.mcp.json → 异步连接/发现/名称映射/调用桥接）
    ├── 领域层   session.py  会话（消息历史）
    └── 编排层   engine.py   AgentEngine：提问 -> LLM -> 工具 -> 回答 循环 + 压缩 + 会话管理

**依赖规则（软件工程隔离规范）**::

    tui/ · cli/ · web/ · desktop/ · mobile/  ──依赖──▶  core/
    core/  ──✘── 不得 import 任何前端模块

新增前端只需消费 ``AgentEngine.ask()`` 返回的事件流（见 ``events.py`` 渲染契约）。
"""

from .config import (
    AppConfig,
    ConfigError,
    ContextConfig,
    LLMConfig,
    MCPConfig,
    SearchConfig,
    SessionConfig,
    UIConfig,
    ensure_config,
    load_app_config,
    load_search_config,
    load_settings,
    resolve_config_path,
)
from .engine import AGENTS_MD_NAME, AGENTS_MD_PREFIX, AgentEngine
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
from .mcp_client import MCPManager
from .session import ConversationSession
from .skills import Skill, build_skills_prompt, discover_skills

__all__ = [
    "AGENTS_MD_NAME",
    "AGENTS_MD_PREFIX",
    "AgentEngine",
    "AgentError",
    "AgentEvent",
    "AppConfig",
    "AssistantDelta",
    "AssistantMessage",
    "CompactFinished",
    "CompactStarted",
    "ConfigError",
    "ContextConfig",
    "ContextWarning",
    "ConversationSession",
    "LLMConfig",
    "MCPConfig",
    "MCPManager",
    "SearchConfig",
    "SessionConfig",
    "SessionInfo",
    "SessionStore",
    "Skill",
    "ToolCallFinished",
    "ToolCallStarted",
    "TurnFinished",
    "TurnStarted",
    "UIConfig",
    "build_skills_prompt",
    "create_engine",
    "discover_skills",
    "ensure_config",
    "load_app_config",
    "load_search_config",
    "load_settings",
    "new_session_id",
    "resolve_config_path",
]


def create_engine(config_path: str | None = None) -> AgentEngine:
    """便捷工厂：读取配置并构建开箱即用的引擎（所有前端共用）。

    * ``[search]`` 配置了 ``api_key`` 时自动注册 ``searchWeb`` 联网搜索工具；
    * 以**程序启动时的工作目录**为项目根，发现 ``.agents/skills/`` 下的 Skill
      （仅 name/description），注入 System Prompt 并注册 ``loadSkill`` 工具；
    * 同根读取 ``.mcp.json``，**异步**连接配置的 MCP Server 并注册其工具；
    * 同根读取 ``AGENTS.md``，首次加载时注入会话历史（一次注入，全程复用）。
    """
    app = load_app_config(config_path)
    return AgentEngine(
        app.llm,
        search=app.search,
        context_config=app.context,
        mcp_config=app.mcp,
        session_config=app.sessions,
        ui_config=app.ui,
    )
