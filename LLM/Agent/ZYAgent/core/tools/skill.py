"""Skill 加载工具：``loadSkill`` —— 按需把 Skill 正文读入对话上下文。

层级：core / 资源层。
依赖：仅标准库 + :mod:`core.skills` + RunCli 的解码/截断辅助函数。

行为（Agent Skills 规范）
------------------------
* 仅 ``name``        -> 读取该 Skill 的 ``SKILL.md`` **全文**；
* ``name`` + ``filename`` -> 读取 Skill 目录内的**附加文件**（不重复加载 SKILL.md）；
* 路径兼容：相对 Skill 目录 / 相对项目根 / 绝对路径；只给文件名时在 Skill 目录内
  递归搜索，唯一命中才读取，多个同名返回候选路径由 LLM 决定；
* **读取范围限定在该 Skill 目录内**（由 :func:`core.skills.resolve_skill_file` 保证）；
* 动态加载原则：Skill 完整内容**只有**在 LLM 调用本工具时才进入上下文。

错误即数据：Skill 不存在 / 文件未命中 / 多候选 / 越界等均转成 ``ok=False``
的清晰文本（注册表兜底），模型可自我纠正（重指定路径或换名重试）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from ..skills import Skill, resolve_skill_file, split_frontmatter
from .base import ToolError, ToolSpec
from .runcli import decode_output, truncate_stream

#: 对外暴露的工具名（Function Calling 路由键）
SKILL_TOOL_NAME = "loadSkill"

#: 创建技能的工具名
CREATE_SKILL_TOOL_NAME = "createSkill"

#: 单个 Skill 文件的最大保留字符数（超出截断并注明，保护上下文）
_MAX_SKILL_FILE_CHARS = 50_000

#: Skill 名称规范化：小写字母、数字、连字符（其余字符统一折叠为连字符）
_INVALID_NAME_CHARS = re.compile(r"[^a-z0-9]+")


def read_skill_text(path: Path) -> str:
    """读取 Skill 文件为文本（UTF-8 优先、GBK 回退、超长截断）。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ToolError(
            f"读取文件失败: {path.name}（{type(exc).__name__}: {exc}）"
        ) from exc
    return truncate_stream(decode_output(raw), _MAX_SKILL_FILE_CHARS)


def list_skill_files(skill: Skill, limit: int = 30) -> str:
    """列出 Skill 目录内的全部文件（相对路径），供"未命中"错误提示模型重试。"""
    try:
        files = sorted(
            item.relative_to(skill.dir).as_posix()
            for item in skill.dir.rglob("*")
            if item.is_file()
        )
    except OSError:  # pragma: no cover - 目录不可读
        return "（无法列出目录内容）"
    if not files:
        return "（该 Skill 目录为空）"
    lines = "\n".join(f"- {name}" for name in files[:limit])
    if len(files) > limit:
        lines += f"\n…（共 {len(files)} 个文件，仅显示前 {limit} 个）"
    return lines


def build_load_skill_tool(
    skills: Sequence[Skill],
    root: Path | str | None = None,
) -> ToolSpec:
    """构造 ``loadSkill`` 工具；``skills`` 为发现阶段加载的元信息列表。

    :param root: 项目根（用于解析"项目相对路径"式 filename）；缺省 ``Path.cwd()``。
    """
    skill_list = list(skills)
    by_name = {s.name: s for s in skill_list}
    root_path = Path(root) if root is not None else Path.cwd()

    def get_skill(name: str) -> Skill:
        skill = by_name.get(name)
        if skill is not None:
            return skill
        # 大小写不敏感兜底（唯一命中时接受）
        lowered = name.lower()
        hits = [s for s in skill_list if s.name.lower() == lowered]
        if len(hits) == 1:
            return hits[0]
        available = ", ".join(s.name for s in skill_list) or "（无）"
        raise ToolError(f"未找到名为 '{name}' 的 Skill。当前可用 Skill: {available}")

    def handler(args: dict[str, Any]) -> str:
        # ---- 1. Skill 名称（必需）
        name = str(args.get("name") or "").strip()
        if not name:
            raise ToolError(
                "name 不能为空，请提供 Skill 名称（来自 System Prompt 的技能清单）"
            )
        skill = get_skill(name)

        # ---- 2. 无 filename -> 返回 SKILL.md 全文
        filename = str(args.get("filename") or "").strip()
        if not filename:
            return read_skill_text(skill.path)

        # ---- 3. 有 filename -> 解析并读取附加文件（不重复加载 SKILL.md）
        try:
            resolved, candidates = resolve_skill_file(skill, filename, root=root_path)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        if resolved is not None:
            return read_skill_text(resolved)

        if candidates:  # 多个同名文件 -> 返回候选路径由 LLM 决定
            listing = "\n".join(
                f"- {c.relative_to(skill.dir).as_posix()}" for c in candidates
            )
            raise ToolError(
                f"Skill '{skill.name}' 中存在多个匹配 '{filename}' 的文件，"
                f"请用 filename 指定其中之一（相对 Skill 目录的路径）:\n{listing}"
            )

        raise ToolError(
            f"Skill '{skill.name}' 中未找到文件 '{filename}'。"
            f"该 Skill 可用文件（相对 Skill 目录）:\n{list_skill_files(skill)}"
        )

    return ToolSpec(
        name=SKILL_TOOL_NAME,
        description=(
            "按需加载 Agent Skill 的完整说明（loadSkill）。仅传 name 时返回该 Skill 的 SKILL.md 全文；"
            "同时传 filename 时返回该 Skill 目录内的附加文件（不会重复返回 SKILL.md）。"
            "可用技能清单见 System Prompt；Skill 的完整内容只有通过 loadSkill 加载后才进入上下文，"
            "加载后请严格遵循其中的工作流程，并可与 RunCli、fileOps 等工具配合完成任务。"
            "filename 可用相对 Skill 目录的路径、项目相对路径或该 Skill 内唯一的文件名。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称（来自 System Prompt 的技能清单）",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "可选：Skill 目录内的附加文件（相对路径或唯一文件名）；"
                        "留空则加载该 Skill 的 SKILL.md"
                    ),
                },
            },
            "required": ["name"],
        },
        handler=handler,
    )


# ====================================================================== createSkill
def sanitize_skill_name(name: str) -> str:
    """规范化 Skill 名称：小写字母、数字、连字符（其余字符折叠为 ``-``）。

    如 ``"My Cool Skill"`` -> ``"my-cool-skill"``；无有效字符时返回空串。
    """
    return _INVALID_NAME_CHARS.sub("-", name.strip().lower()).strip("-")


def derive_description(body: str) -> str:
    """Frontmatter 缺 ``description`` 时，从正文首行提炼一句简介（清理 Markdown 装饰）。"""
    for line in body.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        candidate = candidate.lstrip("#").lstrip(">").strip()
        candidate = candidate.replace("**", "").replace("`", "").strip()
        candidate = re.sub(r"^[-*]\s+", "", candidate)
        if candidate:
            return candidate if len(candidate) <= 200 else candidate[:199] + "…"
    return ""


def ensure_skill_frontmatter(name: str, context: str) -> tuple[str, str]:
    """确保 ``skillContext`` 具备合规 Frontmatter，返回 ``(完整 SKILL.md 文本, description)``。

    * 已有 Frontmatter -> 保留全部键，但 ``name`` 强制为规范名（与目录一致）、
      ``description`` 缺失时从正文首行补齐；
    * 无 Frontmatter  -> 自动合成 ``name`` + ``description``（取正文首行）；
    * 值用 JSON 双引号序列化（YAML 双引号标量是其超集），含换行/引号也安全。
    """
    meta, body = split_frontmatter(context)
    description = (meta.get("description") or "").strip() or derive_description(body)
    if not description:
        raise ToolError(
            "SKILL.md 缺少 description：请在 YAML Frontmatter 中提供 description 字段，"
            "或至少在正文首行写一句技能简介"
        )

    meta = {**meta, "name": name, "description": description}
    front = "\n".join(
        ["---", *(f"{key}: {json.dumps(value, ensure_ascii=False)}"
                  for key, value in meta.items()), "---", ""]
    )
    body_text = body.lstrip("\n")
    text = front + body_text
    if not text.endswith("\n"):
        text += "\n"
    return text, description


def build_create_skill_tool(root: Path | str | None = None) -> ToolSpec:
    """构造 ``createSkill`` 工具：在 ``<root>/.agents/skills/<name>/`` 下生成 SKILL.md。

    :param root: 项目根（即 Skill 发现时的根目录）；缺省 ``Path.cwd()``。
    """
    root_path = Path(root) if root is not None else Path.cwd()
    skills_dir = root_path.joinpath(".agents", "skills")

    def handler(args: dict[str, Any]) -> str:
        # ---- 1. 参数校验 + 名称规范化（小写字母、数字、连字符）
        raw_name = str(args.get("name") or "").strip()
        if not raw_name:
            raise ToolError("name 不能为空，请提供 Skill 名称（如 markdown-notes）")
        name = sanitize_skill_name(raw_name)
        if not name:
            raise ToolError(
                f"name '{raw_name}' 无法生成合法 Skill 名称：只允许小写字母、数字和连字符"
                "（如 web-research、python-tips）"
            )

        context = args.get("skillContext")
        if not isinstance(context, str) or not context.strip():
            raise ToolError(
                "skillContext 不能为空：请提供写入 SKILL.md 的完整内容"
                "（YAML Frontmatter 的 name/description + 正文）"
            )

        # ---- 2. 防覆盖：目标已存在则明确提示（错误即数据，不写入任何内容）
        skill_dir = skills_dir / name
        md_path = skill_dir / "SKILL.md"
        if md_path.is_file():
            raise ToolError(
                f"Skill '{name}' 已存在（{md_path}），不会覆盖原有内容。"
                "如需修改请用 editFile 编辑现有 SKILL.md；"
                "如需 references/、scripts/ 等附加文件请用 createDirectory/createFile 创建"
            )

        # ---- 3. 规范化 Frontmatter 后写入（独占创建，杜绝并发覆盖）
        text, description = ensure_skill_frontmatter(name, context)
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            with md_path.open("x", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except FileExistsError:
            raise ToolError(f"Skill '{name}' 已存在，未覆盖") from None
        except OSError as exc:
            raise ToolError(f"创建 Skill 失败：{type(exc).__name__}: {exc}") from exc

        renamed = f"（名称已规范化：'{raw_name}' → '{name}'）" if name != raw_name else ""
        return (
            f"已创建 Skill '{name}'{renamed}\n"
            f"- 目录: {skill_dir}\n"
            f"- 文件: SKILL.md（YAML Frontmatter: name + description + 正文）\n"
            f"- description: {description}\n"
            "该 Skill 将在下次启动 ZYAgent 时出现在技能清单中（当前会话提示词已固定，不热更新）。\n"
            "如需 references/、scripts/ 等目录或辅助文件，请继续调用 "
            "createDirectory / createFile 工具完成"
        )

    return ToolSpec(
        name=CREATE_SKILL_TOOL_NAME,
        description=(
            "创建新 Skill：在项目的 .agents/skills/ 下生成以 name 命名的子目录及 SKILL.md"
            "（含 YAML Frontmatter 的 name、description 与正文）。当用户要求创建/新增技能时调用。"
            "name 使用小写字母、数字与连字符（其他写法会被自动规范化）；"
            "skillContext 提供 SKILL.md 完整内容，自带 Frontmatter 时保留其 description，"
            "缺少时工具会自动补齐（description 取正文首行）。"
            "本工具只创建 SKILL.md；references/、scripts/ 等其他目录与辅助文件"
            "请继续调用 createDirectory / createFile 工具完成。"
            "已存在的 Skill 不会被覆盖（返回明确提示，可用 editFile 修改）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称：小写字母、数字与连字符（如 markdown-notes、python-tips）",
                },
                "skillContext": {
                    "type": "string",
                    "description": (
                        "写入 SKILL.md 的完整内容：YAML Frontmatter"
                        "（--- 包裹的 name、description）+ 正文；"
                        "缺少 Frontmatter 时工具会自动补齐"
                    ),
                },
            },
            "required": ["name", "skillContext"],
        },
        handler=handler,
    )
