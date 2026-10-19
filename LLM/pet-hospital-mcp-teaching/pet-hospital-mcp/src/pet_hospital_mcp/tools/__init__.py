"""MCP 工具集合。

阶段一只有 ``list_pets``。后续阶段新增工具时：

1. 在本目录新增一个模块，复用 ``pet_hospital_mcp.rest_client``、
   ``pet_hospital_mcp.errors`` 与 ``pet_hospital_mcp.logging_config``；
2. 在 :func:`register_all` 中追加一行注册；
3. 不改动传输层与服务器装配代码。
"""

from __future__ import annotations

from typing import Any

from . import list_pets

__all__ = ["register_all", "list_pets"]


def register_all(server: Any) -> None:
    """把全部工具注册到给定 MCPServer。"""
    list_pets.register(server)
