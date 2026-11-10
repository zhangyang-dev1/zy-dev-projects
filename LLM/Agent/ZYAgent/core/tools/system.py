"""内置系统工具：getDate（系统日期时间）、isLeapYear（闰年判断）。

层级：core / 实现层；纯标准库，Windows 下依赖 cmd 的 ``date /t & time /t``。
"""

from __future__ import annotations

import datetime
import subprocess
import sys

from .base import ToolSpec


def run_date() -> str:
    """获取当前日期时间：优先本地命令（与原版一致），失败回退 datetime。"""
    if sys.platform.startswith("win"):
        try:
            out = subprocess.run("date /t & time /t", shell=True, capture_output=True, timeout=10)
            raw = out.stdout or out.stderr
            # date/time 命令输出编码随控制台环境变化：utf-8 -> cp936 回退
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("cp936", errors="replace")
            text = text.strip()
            if text:
                return " ".join(text.split())
        except Exception:  # noqa: BLE001 - 命令失败则回退
            pass
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_leap_year(year: int) -> bool:
    """闰年规则：普通年能被 4 整除且不能被 100 整除；世纪年能被 400 整除。"""
    return (year % 4 == 0 and year % 100 != 0) or year % 400 == 0


def build_system_tools() -> list[ToolSpec]:
    """返回系统类工具规格列表。"""
    return [
        ToolSpec(
            name="getDate",
            description="获取当前电脑的日期和时间。当用户询问现在几点、今天几号等时间日期问题时调用",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda args: run_date(),
        ),
        ToolSpec(
            name="isLeapYear",
            description="判断一个年份是否闰年。当用户询问某年是否闰年时调用",
            parameters={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "要判断的年份，例如 2024"},
                },
                "required": ["year"],
            },
            handler=lambda args: _handle_leap_year(args),
        ),
    ]


def _handle_leap_year(args: dict) -> str:
    from .base import ToolError

    try:
        year = int(args["year"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError("year 参数必须是整数年份，例如 2024") from exc
    return f"{year}年{'是闰年' if is_leap_year(year) else '不是闰年'}"
