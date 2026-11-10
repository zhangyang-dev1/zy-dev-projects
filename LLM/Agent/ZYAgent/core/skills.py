"""Skill 动态加载：发现（.agents/skills）、Frontmatter 解析、System Prompt 注入。

层级：core / 资源层；依赖：仅标准库。

规范（Agent Skills）::

    <项目根>/.agents/skills/<skill-name>/SKILL.md      # 每个 Skill 至少包含一个 SKILL.md
    <项目根>/.agents/skills/<skill-name>/...其他文件    # 可被 loadSkill(filename=...) 按需加载

* 项目根 = **程序启动时的工作目录**（``Path.cwd()``）；
* ``SKILL.md`` 开头以 YAML Frontmatter 声明必须字段 ``name`` 与 ``description``；
* **初始化阶段仅加载 name + description**（不读取完整正文）——正文只有在 LLM 调用
  ``loadSkill`` 工具时才进入对话上下文（动态加载原则，省 token 且不干扰主流程）。

设计要点
--------
* **Frontmatter 自解析**：只关心 ``name`` / ``description`` 两个标量键，支持引号值与
  ``|`` / ``>`` 块标量，不引入 PyYAML 依赖；
* **必须字段缺失即跳过**：缺 ``SKILL.md`` / 缺字段的目录被忽略（不阻塞 Agent 启动）；
* **路径安全**：:func:`resolve_skill_file` 保证只在 Skill 目录内解析文件，
  越界路径（绝对路径或 ``../`` 逃逸）直接拒绝——避免路径解析错误读到其他文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

#: Skill 发现目录（相对项目根的路径片段）
SKILLS_DIR_PARTS = (".agents", "skills")

#: 规范要求的说明文件名（发现时大小写不敏感，兼容 Windows 检出）
SKILL_MD_NAME = "SKILL.md"

#: Frontmatter 块：文件开头的 --- ... --- 区段
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)

#: 块标量指示符（本解析器把 | 与 > 都按行拼接，仅关心内容存在性）
_BLOCK_MARKERS = {"|", ">", "|-", ">-", "|+", ">+"}


# ====================================================================== 数据模型
@dataclass(frozen=True)
class Skill:
    """单个 Skill 的元信息（发现阶段只持有 name/description 与路径，不含正文）。"""

    name: str
    description: str
    dir: Path      #: Skill 根目录（含 SKILL.md）
    path: Path     #: SKILL.md 完整路径


# ====================================================================== Frontmatter
def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆分 YAML Frontmatter，返回 ``({键: 值}, 正文)``。

    无 Frontmatter 时返回 ``({}, 原文)``。只支持简单 ``key: value`` 行、引号包裹的
    标量、``|`` / ``>`` 块标量的续行，以及 ``#`` 注释与顶层列表项（忽略）。
    足以提取 ``name`` / ``description``，避免为两个字符串字段引入完整 YAML 依赖。
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    data: dict[str, str] = {}
    block_keys: set[str] = set()
    current: str | None = None
    for raw_line in match.group(1).splitlines():
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        # 块标量续行（缩进行归入当前键）
        if indent > 0 and current is not None and current in block_keys:
            data[current] = data[current] + stripped + "\n"
            continue

        if not stripped or stripped.startswith("#") or stripped.startswith("- "):
            continue
        if ":" not in stripped:
            continue

        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            continue

        if value in _BLOCK_MARKERS:
            block_keys.add(key)
            data[key] = ""
        else:
            block_keys.discard(key)
            data[key] = value.strip("\"'")
        current = key

    for key in block_keys:  # 去掉块标量尾部多余空行
        data[key] = data[key].strip()
    return data, text[match.end():]


def parse_frontmatter(text: str) -> dict[str, str]:
    """解析文件开头的 YAML Frontmatter，返回 ``{键: 值}``（无 Frontmatter 时为空 dict）。"""
    return split_frontmatter(text)[0]


# ====================================================================== 发现
def _find_skill_md(directory: Path) -> Path | None:
    """在 Skill 目录第一层查找 SKILL.md（精确优先，大小写不敏感兜底）。"""
    direct = directory / SKILL_MD_NAME
    if direct.is_file():
        return direct
    try:
        for item in sorted(directory.iterdir()):
            if item.is_file() and item.name.lower() == SKILL_MD_NAME.lower():
                return item
    except OSError:
        return None
    return None


def discover_skills(root: Path | str | None = None) -> list[Skill]:
    """扫描 ``<root>/.agents/skills/`` 下的各 Skill 子目录。

    * 缺少 ``SKILL.md``、Frontmatter 缺失、或 ``name`` / ``description`` 必须字段
      为空的目录会被**跳过**（不阻塞 Agent 启动）；
    * 返回按 name 排序的列表，保证 System Prompt 中清单顺序稳定。
    """
    base = Path(root) if root is not None else Path.cwd()
    skills_dir = base.joinpath(*SKILLS_DIR_PARTS)
    if not skills_dir.is_dir():
        return []

    found: list[Skill] = []
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        md_path = _find_skill_md(child)
        if md_path is None:
            continue
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta = parse_frontmatter(text)
        name = (meta.get("name") or "").strip()
        description = (meta.get("description") or "").strip()
        if not name or not description:
            continue  # 必须字段缺失 -> 跳过（规范要求 name 与 description 均必填）
        found.append(Skill(name=name, description=description, dir=child, path=md_path))

    return sorted(found, key=lambda s: s.name.lower())


# ====================================================================== System Prompt 注入
def build_skills_prompt(skills: Sequence[Skill]) -> str:
    """把可用 Skill 的 ``name`` / ``description`` 整理成 System Prompt 片段。

    无 Skill 时返回空串（System Prompt 保持原样）。
    """
    if not skills:
        return ""
    lines = ["You have access to the following skills:"]
    lines.extend(f"- '{skill.name}': {skill.description}" for skill in skills)
    lines.append(
        "Use the 'loadSkill' tool to load the full instructions of a skill when needed."
    )
    lines.append("You can also load additional files referenced by a skill.")
    return "\n".join(lines)


# ====================================================================== 文件解析（loadSkill 用）
def _within(path: Path, directory: Path) -> bool:
    """path 是否位于 directory 内部（含边界情况的 OSError 防护）。"""
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except OSError:  # pragma: no cover - 罕见的解析失败视为越界
        return False


def resolve_skill_file(
    skill: Skill,
    filename: str,
    root: Path | str | None = None,
) -> tuple[Path | None, list[Path]]:
    """在 **Skill 目录内部** 解析 ``filename``，返回 ``(唯一命中, 候选列表)``。

    匹配顺序（精确优先）::

        1. 绝对路径           -> 必须位于 Skill 目录内，否则抛 ValueError（越界）
        2. 相对 Skill 目录    -> 如 refs/template.md
        3. 相对项目根         -> 如 .agents/skills/<name>/refs/template.md
        4. 仅文件名递归查找   -> 在 Skill 目录内递归搜索（大小写不敏感）
                                 唯一命中 -> 读取；多个同名 -> 返回候选列表由 LLM 选择

    :returns: 唯一命中时 ``(路径, [])``；多个同名候选时 ``(None, 候选...)``；
              未命中时 ``(None, [])``。
    :raises ValueError: 绝对路径越出 Skill 目录（读取范围限定在 Skill 目录内）。
    """
    name = (filename or "").strip().strip("\"'").replace("\\", "/")
    if not name:
        raise ValueError("filename 不能为空")
    base = skill.dir.resolve()
    root_path = Path(root) if root is not None else None

    # 1) 绝对路径：越界直接拒绝
    candidate = Path(name)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if not _within(resolved, base):
            raise ValueError(f"读取范围限定在 Skill 目录内，绝对路径越界: {name}")
        return (resolved if resolved.is_file() else None), []

    # 2) 相对 Skill 目录（../ 逃逸会被 _within 拦截）
    direct = (skill.dir / name).resolve()
    if _within(direct, base) and direct.is_file():
        return direct, []

    # 3) 相对项目根（如 .agents/skills/<name>/notes.md）
    if root_path is not None:
        project = (root_path / name).resolve()
        if _within(project, base) and project.is_file():
            return project, []

    # 4) 仅文件名 -> Skill 目录内递归查找（大小写不敏感）
    basename = Path(name).name.lower()
    matches = sorted(
        {
            item.resolve()
            for item in skill.dir.rglob("*")
            if item.is_file() and item.name.lower() == basename
        }
    )
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        return None, matches
    return None, []
