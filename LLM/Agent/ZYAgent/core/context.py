"""上下文管理：Token 估算、原始上下文归档、全文检索与压缩摘要组装。

层级：core / 资源层；依赖：仅标准库。

设计要点
--------
* **归档**：压缩前把整段会话按行写成 JSONL（``.agents/context/<编号>.jsonl``），
  每条消息保留角色、内容、工具调用参数、工具返回结果与原始顺序；
* **检索**：:func:`search_context` 用最朴素的关键词包含匹配在归档中查找片段，
  返回「归档编号 + 消息位置 + 片段」，并限制总长度避免再次撑爆上下文；
* **压缩**：:data:`COMPACT_PROMPT` 是专用的压缩提示词，压缩后的会话由
  :func:`build_compacted_messages` 组装（System Prompt + 摘要 + 归档说明）。

本模块不依赖任何前端，也不依赖工具包（避免循环导入）：
检索失败抛 :class:`ContextError`，由 ``core/tools/recall.py`` 转成 ToolError。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

Message = dict[str, object]

#: 归档目录（相对项目根）
CONTEXT_DIR_PARTS = (".agents", "context")

#: 归档文件后缀
ARCHIVE_SUFFIX = ".jsonl"

#: 上下文占用阈值：达到模型窗口的 80% 就提醒并暂停普通提问
CONTEXT_THRESHOLD = 0.8

#: 每条消息的固定结构开销（role / 分隔符等）估算值
_MESSAGE_OVERHEAD = 4

#: 单次检索最多返回的匹配条数 / 总字符数 / 单条片段长度
_MAX_MATCHES = 8
_MAX_RESULT_CHARS = 4000
_SNIPPET_RADIUS = 120
_MAX_SNIPPET_CHARS = 360

#: 上下文压缩专用 Prompt（/compact 使用）
COMPACT_PROMPT = """你是一名 AI Agent 的上下文管理助手。当前会话即将达到上下文窗口限制，需要将历史对话压缩为一份高质量的工作摘要，以便 Agent 在新的上下文中继续执行任务。

请认真阅读提供的历史上下文，提取对后续工作仍然有价值的信息，并生成一份约 5,000 Tokens 的 Markdown 摘要。

摘要应重点保留以下内容：
1. **用户目标与要求**：用户希望完成什么任务？有哪些必须遵守的约束、偏好和验收条件？
2. **已经完成的工作**：哪些任务已经完成？实现了哪些功能？获得了哪些已经验证的结果？
3. **当前工作状态**：Agent 正在处理什么任务？当前进展到哪一步？是否存在尚未解决的问题？
4. **关键技术信息**：保留重要的文件路径、函数名称、接口定义、配置项、执行命令、错误信息和必要的代码片段。
5. **关键决策与原因**：记录用户和 Agent 已经确定的技术方案，以及重要的设计取舍。
6. **下一步任务**：明确接下来应该执行的操作，以及需要继续验证的事项。

请遵守以下要求：
- 优先保留对后续任务有直接影响的信息，删除寒暄、重复讨论、过时的尝试和无关内容。
- 对已经验证的事实与尚未验证的推测作出明确区分。
- 不要将计划完成的任务描述为已经完成。
- 重要的文件路径、函数名称、配置名称和命令应保留准确原文。
- 对失败的尝试，只保留有助于避免重复错误的信息。
- 如果信息存在冲突，应优先保留用户最新确认的要求，并简要说明仍然存在的冲突。
- 如果某些历史细节无法在摘要中完整保留，可简要说明它们已保存到原始上下文归档中，后续可以通过 `recallContext` 工具重新检索。
- 使用清晰的 Markdown 结构输出摘要，不要附加解释或无关内容。

**最终目标：使一个从未阅读过原始对话的 Agent，仅凭这份摘要，就能够理解当前任务、继承关键决策，并继续开展工作。**"""


class ContextError(Exception):
    """上下文归档/检索的业务错误（由 recallContext 转成 ToolError）。"""


# ====================================================================== Token 估算
def estimate_tokens(text: str, chars_per_token: float = 4.0) -> int:
    """粗略估算一段文本的 Token 数（拿不到 API usage 时使用）。

    规则：ASCII 字符约 ``chars_per_token`` 个算 1 token（默认 4），
    非 ASCII（中文等）约 1 个算 1 token。对中英混排的对话足够接近，
    且实现简单、可解释；比例可由 config.ini ``[context]`` 调整。
    """
    if not text:
        return 0
    per_token = chars_per_token if chars_per_token > 0 else 4.0
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    other_count = len(text) - ascii_count
    return max(1, round(ascii_count / per_token + other_count))


def estimate_message_tokens(
    message: Message,
    chars_per_token: float = 4.0,
    overhead: int = _MESSAGE_OVERHEAD,
) -> int:
    """估算单条消息的 Token 数（含工具调用参数）。"""
    total = max(0, int(overhead))
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content, chars_per_token)
    elif content is not None:
        total += estimate_tokens(json.dumps(content, ensure_ascii=False), chars_per_token)
    tool_calls = message.get("tool_calls")
    if tool_calls:
        total += estimate_tokens(
            json.dumps(tool_calls, ensure_ascii=False), chars_per_token
        )
    return total


def estimate_messages_tokens(
    messages: Sequence[Message],
    chars_per_token: float = 4.0,
    overhead: int = _MESSAGE_OVERHEAD,
) -> int:
    """估算整个消息列表的 Token 数。"""
    return sum(
        estimate_message_tokens(m, chars_per_token, overhead) for m in messages
    )


def usage_total_tokens(usage: dict | None) -> int | None:
    """从 API 返回的 ``usage`` 中取出上下文用量（优先 ``total_tokens``）。"""
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and total > 0:
        return total
    prompt = usage.get("prompt_tokens")
    if isinstance(prompt, int) and prompt > 0:
        completion = usage.get("completion_tokens")
        return prompt + (completion if isinstance(completion, int) else 0)
    return None


def format_tokens_k(tokens: int) -> str:
    """把 Token 数格式化为 ``12.3k`` / ``128k`` 形式。"""
    if tokens >= 1000:
        text = f"{tokens / 1000:.1f}"
        return (text[:-2] if text.endswith(".0") else text) + "k"
    return str(tokens)


@dataclass(frozen=True)
class ContextUsage:
    """一次上下文占用快照（供前端展示与阈值判断）。"""

    used: int
    window: int
    source: str = "estimate"  # "api"（模型 API 返回）或 "estimate"（本地估算）
    threshold: float = CONTEXT_THRESHOLD  # 暂停提问的占用比例（默认 80%）

    @property
    def ratio(self) -> float:
        return self.used / self.window if self.window > 0 else 0.0

    @property
    def percent(self) -> float:
        return self.ratio * 100

    @property
    def blocked(self) -> bool:
        """是否已达到配置的阈值（默认 80%，应暂停普通提问）。"""
        return self.ratio >= self.threshold

    def describe(self) -> str:
        """``12.3k / 128k（9.6%）`` 形式的简述。"""
        return (
            f"{format_tokens_k(self.used)} / {format_tokens_k(self.window)}"
            f"（{self.percent:.1f}%）"
        )


# ====================================================================== 归档
def context_directory(root: Path | str, relative: str | None = None) -> Path:
    """返回归档目录（不创建）。

    :param relative: 相对项目根的目录（来自 config.ini ``[context] archive_dir``）；
                     为绝对路径时直接使用。缺省 ``.agents/context``。
    """
    if relative:
        path = Path(relative).expanduser()
        return path if path.is_absolute() else Path(root) / path
    return Path(root).joinpath(*CONTEXT_DIR_PARTS)


def new_archive_id(directory: Path | str, now: datetime | None = None) -> str:
    """生成唯一归档编号（``20261022-153000``，同秒冲突时追加 ``-1``）。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    archive_id = stamp
    suffix = 1
    while (Path(directory) / f"{archive_id}{ARCHIVE_SUFFIX}").exists():
        archive_id = f"{stamp}-{suffix}"
        suffix += 1
    return archive_id


def save_archive(
    directory: Path | str, archive_id: str, messages: Sequence[Message]
) -> Path:
    """把消息按原始顺序写成 JSONL 归档文件，返回文件路径。

    每行一条消息：``{"index": 序号, "role": ..., "content": ..., ...}``，
    工具调用参数（``tool_calls``）与工具返回结果（``role=tool`` 的 content）
    均原样保留。文件用独占模式创建，杜绝并发覆盖。
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{archive_id}{ARCHIVE_SUFFIX}"
    with path.open("x", encoding="utf-8", newline="\n") as fh:
        for index, message in enumerate(messages):
            record = {"index": index, **message}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def list_archives(directory: Path | str) -> list[str]:
    """列出全部归档编号（新 → 旧）。"""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    ids = [p.stem for p in directory.glob(f"*{ARCHIVE_SUFFIX}") if p.is_file()]
    return sorted(ids, reverse=True)


def load_archive(directory: Path | str, archive_id: str) -> list[dict]:
    """读取一个归档文件，返回消息记录列表（跳过损坏行）。"""
    path = Path(directory) / f"{archive_id}{ARCHIVE_SUFFIX}"
    records: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


# ====================================================================== 检索
def split_keywords(text: str) -> list[str]:
    """把检索文本拆成关键词（按空白/常见标点切分，去重并保序）。"""
    raw = (text or "").strip()
    terms = [t.strip() for t in re.split(r"[\s,，、;；|]+", raw) if t.strip()]
    if not terms and raw:
        terms = [raw]
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            result.append(term)
    return result


def _record_text(record: dict) -> str:
    """把一条消息转成可检索文本（正文 + 工具调用参数）。"""
    parts: list[str] = []
    content = record.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))
    tool_calls = record.get("tool_calls")
    if tool_calls:
        parts.append(json.dumps(tool_calls, ensure_ascii=False))
    return "\n".join(parts)


def _snippet(
    text: str,
    terms: Sequence[str],
    radius: int = _SNIPPET_RADIUS,
    max_chars: int = _MAX_SNIPPET_CHARS,
) -> str:
    """截取命中位置附近的片段（压平空白并加上省略号）。"""
    lowered = text.lower()
    positions = [lowered.find(term.lower()) for term in terms]
    positions = [pos for pos in positions if pos >= 0]
    if not positions:
        return ""
    start = max(0, min(positions) - radius)
    end = min(len(text), min(positions) + radius)
    snippet = " ".join(text[start:end].split())
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet += "…"
    return snippet[:max_chars]


def search_context(
    directory: Path | str,
    text: str,
    archive_id: str | None = None,
    *,
    max_matches: int = _MAX_MATCHES,
    max_chars: int = _MAX_RESULT_CHARS,
    snippet_radius: int = _SNIPPET_RADIUS,
    snippet_chars: int = _MAX_SNIPPET_CHARS,
) -> str:
    """在归档中做全文关键词检索，返回给 LLM 的文本结果。

    * 指定 ``archive_id`` 时只检索该归档；否则检索目录下全部归档（新 → 旧）；
    * 关键词按「包含匹配」统计命中数，命中多的优先；返回片段 + 编号 + 消息位置；
    * 结果条数与总长度都有上限，避免检索结果再次占满上下文。
    """
    terms = split_keywords(text)
    if not terms:
        raise ContextError("text 不能为空：请提供要查找的关键词或文本内容")

    directory = Path(directory)
    available = list_archives(directory)
    if archive_id:
        archive_id = archive_id.strip()
        if archive_id.endswith(ARCHIVE_SUFFIX):
            archive_id = archive_id[: -len(ARCHIVE_SUFFIX)]
        if archive_id not in available:
            listing = "、".join(available[:20]) or "（暂无归档）"
            raise ContextError(
                f"未找到编号为 {archive_id} 的上下文归档。可用归档：{listing}"
            )
        targets = [archive_id]
    else:
        targets = available
        if not targets:
            return (
                f"尚无上下文归档（{directory} 为空）。"
                "请先执行 /compact 压缩上下文，之后才能检索历史记录。"
            )

    hits: list[tuple[int, str, int, dict, list[str]]] = []
    for aid in targets:  # 新归档在前，后续稳定排序保留该顺序
        try:
            records = load_archive(directory, aid)
        except OSError:
            continue  # 单个归档不可读不影响其它归档
        for record in records:
            content = _record_text(record)
            if not content:
                continue
            lowered = content.lower()
            matched = [term for term in terms if term.lower() in lowered]
            if not matched:
                continue
            try:
                index = int(record.get("index", 0))
            except (TypeError, ValueError):
                index = 0
            hits.append((len(matched), aid, index, record, matched))

    if not hits:
        return (
            f"未在 {len(targets)} 个归档中找到包含「{text.strip()}」的内容。"
            "可尝试更短的关键词，或省略 id 检索全部归档。"
        )

    hits.sort(key=lambda hit: -hit[0])  # 稳定排序：命中关键词越多越靠前

    lines = [
        f"在 {len(targets)} 个归档中找到 {len(hits)} 条匹配"
        f"（关键词：{'、'.join(terms)}）",
        "",
    ]
    used = 0
    shown = 0
    for _score, aid, index, record, matched in hits:
        if shown >= max_matches or used >= max_chars:
            break
        role = str(record.get("role") or "?")
        snippet = _snippet(
            _record_text(record), matched, snippet_radius, snippet_chars
        )
        block = f"[{aid}] 第 {index + 1} 条消息 · role={role}\n{snippet}"
        if shown > 0 and used + len(block) > max_chars:
            break
        lines.append(block)
        lines.append("")
        used += len(block) + 1
        shown += 1

    if shown < len(hits):
        lines.append(
            f"（仅显示前 {shown} 条，共 {len(hits)} 条匹配；"
            "可缩小关键词或指定 id 继续查找）"
        )
    return "\n".join(lines).strip()


# ====================================================================== 压缩
def build_compact_request(messages: Sequence[Message]) -> list[Message]:
    """组装压缩请求：原 System Prompt + 压缩指令 + 完整历史（不传工具）。"""
    request: list[Message] = []
    rest = [dict(m) for m in messages]
    if rest and rest[0].get("role") == "system":
        request.append(rest.pop(0))
    request.append({"role": "user", "content": COMPACT_PROMPT})
    request.extend(rest)
    return request


def build_archive_note(archive_id: str, compacted_at: str) -> str:
    """生成追加在摘要末尾的归档说明。"""
    return (
        f"此前上下文已于 {compacted_at} 完成压缩，原始上下文保存在编号为 "
        f"{archive_id} 的归档文件中。如需查找历史信息，请使用 recallContext 工具。"
    )


def build_compacted_messages(
    system_prompt: str,
    summary: str,
    archive_id: str,
    compacted_at: str,
    extra_messages: Sequence[Message] = (),
) -> list[Message]:
    """用摘要替换历史：保留 System Prompt，摘要末尾追加归档说明。

    ``extra_messages`` 用于「一次注入，全程复用」的常驻上下文（如 AGENTS.md）：
    它们排在 System Prompt 之后、摘要之前，压缩后继续保留。
    """
    content = (
        "以下内容是此前对话的压缩摘要，作为新上下文的起点：\n\n"
        f"{summary.strip()}\n\n---\n\n"
        f"{build_archive_note(archive_id, compacted_at)}"
    )
    messages: list[Message] = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(dict(m) for m in extra_messages)
    messages.append({"role": "user", "content": content})
    return messages
