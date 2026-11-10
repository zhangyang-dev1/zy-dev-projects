"""命令行（纯控制台）前端：零依赖、保留原 chat_files.py 的使用体验。

同时充当 **最简参考实现**：演示任意前端如何只靠 ``AgentEngine.ask()`` 事件流
完成渲染（对照 ``tui/`` 的富界面实现）。
"""

from .renderer import ConsoleRenderer, run_console

__all__ = ["ConsoleRenderer", "run_console"]
