"""本地文件系统工具（10 个）：目录/文件的查询与 CRUD。

层级：core / 实现层；纯标准库。
防护沿用原版：拒绝删除磁盘根目录、拒绝覆盖已存在文件、读写前校验纯文本。
"""

from __future__ import annotations

import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any

from .base import ToolError, ToolSpec


# ------------------------------------------------------------------ 通用辅助
def read_text(path: str | Path) -> str:
    """安全读取纯文本：含 \\0 判定为二进制并拒绝；utf-8 -> cp936 回退解码。"""
    data = Path(path).read_bytes()
    if b"\x00" in data:
        raise ToolError(f"不是纯文本文件: {path}")
    for enc in ("utf-8", "cp936"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ToolError(f"无法识别的文本编码: {path}")


def _require_dir(path: str) -> str:
    if not os.path.isdir(path):
        raise ToolError(f"目录不存在: {path}")
    return path


def _require_file(path: str) -> str:
    if not os.path.isfile(path):
        raise ToolError(f"文件不存在: {path}")
    return path


def _args(args: dict[str, Any], *keys: str) -> list[str]:
    """取出必填参数，缺失时抛出可被模型理解的错误。"""
    missing = [k for k in keys if not args.get(k) and args.get(k) != ""]
    if missing:
        raise ToolError(f"缺少参数: {', '.join(missing)}")
    return [str(args[k]) for k in keys]


# ------------------------------------------------------------------ 工具实现
def _getcwd(args: dict[str, Any]) -> str:
    return os.getcwd()


def _listdir(args: dict[str, Any]) -> str:
    (p,) = _args(args, "path")
    _require_dir(p)
    items = sorted(os.listdir(p))
    if not items:
        return "空目录"
    return "\n".join(
        ("[目录] " if os.path.isdir(os.path.join(p, i)) else "[文件] ") + i for i in items
    )


def _get_file_type(args: dict[str, Any]) -> str:
    (p,) = _args(args, "path")
    if not os.path.exists(p):
        raise ToolError(f"路径不存在: {p}")
    if os.path.isdir(p):
        return "目录"
    ext = os.path.splitext(p)[1] or "(无扩展名)"
    mime = mimetypes.guess_type(p)[0] or "未知类型"
    return f"文件, 扩展名 {ext}, MIME类型 {mime}"


def _read_file(args: dict[str, Any]) -> str:
    (p,) = _args(args, "path")
    _require_file(p)
    return read_text(p)


def _create_file(args: dict[str, Any]) -> str:
    directory, filename = _args(args, "directory", "filename")
    p = os.path.join(directory, filename)
    if os.path.exists(p):
        raise ToolError(f"文件已存在: {p}")
    with open(p, "w", encoding="utf-8") as f:
        f.write(str(args.get("content", "") or ""))
    return f"已创建文件: {os.path.abspath(p)}"


def _edit_file(args: dict[str, Any]) -> str:
    (p,) = _args(args, "path")
    _require_file(p)
    read_text(p)  # 先确认是纯文本文件
    if "content" not in args:
        raise ToolError("缺少参数: content")
    with open(p, "w", encoding="utf-8") as f:
        f.write(str(args["content"]))
    return f"已编辑文件: {os.path.abspath(p)}"


def _create_directory(args: dict[str, Any]) -> str:
    (p,) = _args(args, "path")
    os.makedirs(p, exist_ok=True)
    return f"已创建目录: {os.path.abspath(p)}"


def _delete_directory(args: dict[str, Any]) -> str:
    (p,) = _args(args, "path")
    np = os.path.normpath(p)
    if os.path.isabs(np) and len(np) <= 3:
        raise ToolError("拒绝删除磁盘根目录")
    _require_dir(p)
    shutil.rmtree(p)
    return f"已删除目录及其全部内容: {os.path.abspath(p)}"


def _delete_file(args: dict[str, Any]) -> str:
    (p,) = _args(args, "path")
    _require_file(p)
    os.remove(p)
    return f"已删除文件: {os.path.abspath(p)}"


def _rename(args: dict[str, Any]) -> str:
    old, new = _args(args, "oldPath", "newPath")
    os.rename(old, new)
    return f"已重命名: {old} -> {new}"


# ------------------------------------------------------------------ 规格清单
def build_file_tools() -> list[ToolSpec]:
    """返回文件系统类工具规格列表（10 个）。"""
    return [
        ToolSpec(
            name="getcwd",
            description="获取当前工作目录（程序运行所在的绝对路径）",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_getcwd,
        ),
        ToolSpec(
            name="listdir",
            description="列出指定目录下的所有文件和子目录",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径，可以是相对路径，如 . 或 ./test"}},
                "required": ["path"],
            },
            handler=_listdir,
        ),
        ToolSpec(
            name="getFileType",
            description="获取指定文件（或目录）的类型，如扩展名、MIME类型",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
            handler=_get_file_type,
        ),
        ToolSpec(
            name="readFile",
            description="读取指定纯文本文件的内容，不支持二进制文件",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
            handler=_read_file,
        ),
        ToolSpec(
            name="createFile",
            description="在指定目录下创建一个新文件（可同时写入内容）",
            parameters={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "目录路径，如 . 或 ./test"},
                    "filename": {"type": "string", "description": "文件名，如 hello.txt"},
                    "content": {"type": "string", "description": "文件内容，可为空"},
                },
                "required": ["directory", "filename"],
            },
            handler=_create_file,
        ),
        ToolSpec(
            name="editFile",
            description="用新内容整体覆盖指定纯文本文件的原有内容",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "覆盖后的新内容"},
                },
                "required": ["path", "content"],
            },
            handler=_edit_file,
        ),
        ToolSpec(
            name="createDirectory",
            description="创建新目录（包括多级父目录），目录已存在时不报错",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径"}},
                "required": ["path"],
            },
            handler=_create_directory,
        ),
        ToolSpec(
            name="deleteDirectory",
            description="删除指定目录及其全部内容（支持非空目录），操作不可恢复",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径"}},
                "required": ["path"],
            },
            handler=_delete_directory,
        ),
        ToolSpec(
            name="deleteFile",
            description="删除指定文件，操作不可恢复",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
            handler=_delete_file,
        ),
        ToolSpec(
            name="rename",
            description="重命名或移动文件/目录",
            parameters={
                "type": "object",
                "properties": {
                    "oldPath": {"type": "string", "description": "原路径"},
                    "newPath": {"type": "string", "description": "新路径"},
                },
                "required": ["oldPath", "newPath"],
            },
            handler=_rename,
        ),
    ]
