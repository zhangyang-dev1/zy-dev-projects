"""``recallContext`` 工具：检索上下文压缩前的原始历史归档。

层级：core / 资源层。
依赖：仅标准库 + :mod:`core.context`（全文关键词检索）。

归档目录：``<项目根>/.agents/context/<编号>.jsonl``（由 ``/compact`` 写入）。
本工具是 LLM 的普通 Function Calling 工具：压缩摘要缺少细节时，模型可主动
调用它按关键词检索原始记录（无需向量数据库/Embedding）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import ContextConfig
from ..context import ContextError, context_directory, search_context
from .base import ToolError, ToolSpec

#: 对外暴露的工具名（Function Calling 路由键）
RECALL_TOOL_NAME = "recallContext"


def build_recall_tool(
    root: Path | str | None = None,
    context_config: ContextConfig | None = None,
) -> ToolSpec:
    """构造 ``recallContext`` 工具。

    :param root: 项目根（与 ``.agents/skills`` 同根）；缺省 ``Path.cwd()``。
    :param context_config: 归档目录与检索长度限制（来自 config.ini）；缺省内置默认值。
    """
    cfg = context_config or ContextConfig()
    directory = context_directory(
        root if root is not None else Path.cwd(), cfg.archive_dir
    )

    def handler(args: dict[str, Any]) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ToolError(
                "text 不能为空：请提供要查找的关键词或文本内容"
                "（建议使用具体的文件名、函数名或短语）"
            )
        archive_id = str(args.get("id") or "").strip()
        try:
            return search_context(
                directory,
                text,
                archive_id=archive_id or None,
                max_matches=cfg.recall_max_matches,
                max_chars=cfg.recall_max_chars,
                snippet_radius=cfg.recall_snippet_radius,
                snippet_chars=cfg.recall_snippet_chars,
            )
        except ContextError as exc:
            raise ToolError(str(exc)) from exc

    return ToolSpec(
        name=RECALL_TOOL_NAME,
        description=(
            "检索上下文压缩前的原始历史记录（recallContext）。当压缩摘要缺少某些细节时使用："
            "传入关键词 text，在项目 .agents/context/ 的归档文件中做本地全文关键词匹配，"
            "返回相关原始片段、归档编号与消息位置。可选 id 指定某个归档（如 20261022-153000），"
            "省略则在所有历史归档中检索。一次只返回少量最相关片段；"
            "建议用具体、简短的关键词（文件名/函数名/命令/错误信息）分多次检索。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "可选：归档编号（文件名去掉 .jsonl），如 20261022-153000；"
                        "省略则在全部历史归档中检索"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "必需：要查找的关键词或文本内容（建议使用具体、简短的关键词）",
                },
            },
            "required": ["text"],
        },
        handler=handler,
    )
