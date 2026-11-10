"""会话持久化：把对话保存为 JSONL「会话事件日志」。

层级：core / 资源层；依赖：仅标准库。

存储位置（默认）::

    <项目根>/.agents/sessions/<会话编号>.jsonl

文件格式（逐行 JSON，便于追加/排障）::

    第一行: {"type": "meta", "id": "...", "title": "...", "created_at": "...", ...}
    后续行: {"type": "message", "role": "user", "content": "..."}

设计要点
--------
* **一次会话一个文件**：编号形如 ``20261104-153000-ab12``，同一秒内也不会冲突；
* **快照式保存**：压缩（/compact）会整体替换历史，因此每次保存都原子地重写整个
  文件（临时文件 + ``os.replace``），既简单又能准确反映当前会话；
* **容错**：损坏的行会被跳过；非法会话编号不允许路径穿越。
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

Message = dict[str, object]

#: 默认会话目录（相对项目根）
SESSION_DIR = ".agents/sessions"

#: 文件后缀
SESSION_SUFFIX = ".jsonl"

#: 默认标题
DEFAULT_TITLE = "未命名会话"

#: 会话编号允许的字符（防止路径穿越）
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class SessionInfo:
    """一个已保存会话的元信息（用于 /resume 列表）。"""

    id: str
    title: str
    created_at: str
    updated_at: str
    model: str = ""
    messages: int = 0

    def describe(self) -> str:
        """列表中的一行简述。"""
        return (
            f"{self.title}  ·  {self.updated_at}  ·  {self.messages} 条消息"
            f"  ·  {self.id}"
        )


def session_directory(root: Path | str, relative: str | None = None) -> Path:
    """返回会话目录（不创建）。

    :param relative: config.ini ``[sessions] dir``；绝对路径时直接使用。
    """
    path = Path(relative).expanduser() if relative else Path(SESSION_DIR)
    return path if path.is_absolute() else Path(root) / path


def new_session_id(now: datetime | None = None) -> str:
    """生成会话编号：``YYYYMMDD-HHMMSS-xxxx``（时间 + 4 位随机十六进制）。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def make_title(text: str, limit: int = 40) -> str:
    """从首条用户消息生成标题（压平空白并截断）。"""
    title = " ".join((text or "").split())
    if not title:
        return DEFAULT_TITLE
    return title if len(title) <= limit else title[: limit - 1] + "…"


class SessionStore:
    """会话事件日志的读写门面。"""

    def __init__(
        self,
        root: Path | str,
        directory: str | None = None,
        max_listed: int = 50,
    ) -> None:
        self.root = Path(root)
        self.directory = session_directory(self.root, directory)
        self.max_listed = max(1, int(max_listed))

    # ------------------------------------------------------------ 路径
    def _path(self, session_id: str) -> Path:
        sid = (session_id or "").strip()
        if not sid or not _ID_PATTERN.match(sid) or ".." in sid:
            raise ValueError(f"非法会话编号: {session_id!r}")
        return self.directory / f"{sid}{SESSION_SUFFIX}"

    def exists(self, session_id: str) -> bool:
        try:
            return self._path(session_id).is_file()
        except ValueError:
            return False

    # ------------------------------------------------------------ 写入
    def save(
        self,
        session_id: str,
        messages: Sequence[Message],
        title: str = "",
        model: str = "",
    ) -> Path:
        """原子保存整个会话，返回文件路径。"""
        path = self._path(session_id)
        self.directory.mkdir(parents=True, exist_ok=True)

        previous = self._read_meta(path)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        final_title = (
            (title or "").strip()
            or str(previous.get("title") or "").strip()
            or self._derive_title(messages)
        )
        meta = {
            "type": "meta",
            "id": session_id,
            "title": final_title,
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
            "model": model,
            "messages": len(messages),
        }

        lines = [json.dumps(meta, ensure_ascii=False)]
        for message in messages:
            record = {"type": "message", **dict(message)}
            lines.append(json.dumps(record, ensure_ascii=False))

        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        return path

    # ------------------------------------------------------------ 读取
    def load(self, session_id: str) -> tuple[SessionInfo, list[Message]]:
        """读取一个会话；不存在或编号非法时抛 ``FileNotFoundError``。"""
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"会话不存在: {session_id}")
        meta: dict = {}
        messages: list[Message] = []
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "meta":
                    meta = record
                else:
                    record.pop("type", None)
                    messages.append(record)

        info = SessionInfo(
            id=session_id,
            title=str(meta.get("title") or DEFAULT_TITLE),
            created_at=str(meta.get("created_at") or ""),
            updated_at=str(meta.get("updated_at") or ""),
            model=str(meta.get("model") or ""),
            messages=len(messages),
        )
        return info, messages

    def list(self) -> list[SessionInfo]:
        """列出已保存会话（新 → 旧），最多 ``max_listed`` 条。"""
        if not self.directory.is_dir():
            return []
        items: list[SessionInfo] = []
        for path in self.directory.glob(f"*{SESSION_SUFFIX}"):
            if not path.is_file():
                continue
            meta = self._read_meta(path)
            if not meta:
                continue
            try:
                count = int(meta.get("messages") or 0)
            except (TypeError, ValueError):
                count = 0
            items.append(SessionInfo(
                id=path.stem,
                title=str(meta.get("title") or DEFAULT_TITLE),
                created_at=str(meta.get("created_at") or ""),
                updated_at=str(meta.get("updated_at") or ""),
                model=str(meta.get("model") or ""),
                messages=count,
            ))
        items.sort(key=lambda info: info.updated_at, reverse=True)
        return items[: self.max_listed]

    def delete(self, session_id: str) -> bool:
        """删除一个会话文件（返回是否真的删除）。"""
        try:
            path = self._path(session_id)
        except ValueError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _read_meta(path: Path) -> dict:
        """只读取首行 meta（列表场景避免加载全部消息）。"""
        if not path.is_file():
            return {}
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                first = fh.readline().strip()
        except OSError:
            return {}
        if not first:
            return {}
        try:
            meta = json.loads(first)
        except json.JSONDecodeError:
            return {}
        return meta if isinstance(meta, dict) and meta.get("type") == "meta" else {}

    @staticmethod
    def _derive_title(messages: Sequence[Message]) -> str:
        """从首条普通用户消息推导标题（跳过 AGENTS.md 注入消息）。"""
        for message in messages:
            if message.get("role") != "user":
                continue
            content = str(message.get("content") or "")
            if content.startswith("【项目上下文"):
                continue
            if content.strip():
                return make_title(content)
        return DEFAULT_TITLE
