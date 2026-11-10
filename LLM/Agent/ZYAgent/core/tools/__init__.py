"""工具包入口：统一创建默认工具注册表（系统工具 + 文件工具 + 可选 searchWeb）。"""

from __future__ import annotations

from ..config import SearchConfig
from .base import ToolError, ToolHandler, ToolRegistry, ToolResult, ToolSpec
from .fetch import FETCH_TOOL_NAME, build_curl_command, build_fetch_tool, extract_markdown, fetch_url
from .files import build_file_tools, read_text
from .recall import RECALL_TOOL_NAME, build_recall_tool
from .runcli import RUN_CLI_TOOL_NAME, build_run_cli_tool, decode_output, truncate_stream
from .search import SEARCH_TOOL_NAME, build_search_tool, clip_query, format_references
from .skill import (
    CREATE_SKILL_TOOL_NAME,
    SKILL_TOOL_NAME,
    build_create_skill_tool,
    build_load_skill_tool,
    list_skill_files,
    read_skill_text,
    sanitize_skill_name,
)
from .system import build_system_tools, is_leap_year, run_date

__all__ = [
    "CREATE_SKILL_TOOL_NAME",
    "FETCH_TOOL_NAME",
    "RECALL_TOOL_NAME",
    "RUN_CLI_TOOL_NAME",
    "SEARCH_TOOL_NAME",
    "SKILL_TOOL_NAME",
    "ToolError",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_curl_command",
    "build_create_skill_tool",
    "build_default_registry",
    "build_fetch_tool",
    "build_file_tools",
    "build_load_skill_tool",
    "build_recall_tool",
    "build_run_cli_tool",
    "build_search_tool",
    "build_system_tools",
    "clip_query",
    "decode_output",
    "extract_markdown",
    "fetch_url",
    "format_references",
    "is_leap_year",
    "list_skill_files",
    "read_skill_text",
    "read_text",
    "run_date",
    "sanitize_skill_name",
    "truncate_stream",
]


def build_default_registry(search: SearchConfig | None = None) -> ToolRegistry:
    """构建默认工具注册表：2 系统 + 10 文件 + searchWeb + fetchWeb + RunCli = 15 个。

    ``searchWeb`` 与 ``fetchWeb`` **始终注册**：未配置 api_key 时，调用会返回一段
    清晰的配置指引文本（错误即数据），由模型转述给用户，而不是让工具静默消失。

    ``createSkill`` / ``loadSkill`` / ``recallContext`` 不在此注册：它们依赖
    项目根（``.agents/``）与 Skill 发现结果，由 :class:`AgentEngine` 注册——
    ``createSkill`` 与 ``recallContext`` 恒注册，``loadSkill`` 仅在发现
    ``.agents/skills/`` 下存在 Skill 时注册（即 17 或 18 个工具）。
    """
    registry = ToolRegistry()
    registry.register_many(build_system_tools())
    registry.register_many(build_file_tools())
    registry.register(build_search_tool(search if search is not None else SearchConfig()))
    registry.register(build_fetch_tool())
    registry.register(build_run_cli_tool())
    return registry
