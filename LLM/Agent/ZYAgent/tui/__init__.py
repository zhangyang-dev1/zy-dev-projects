"""TUI 前端包：基于 Textual 的终端界面。

运行::

    python -m tui            # 从项目根目录
    python main.py --ui tui  # 或经由入口
"""

from .app import ZYAgentApp, run_app, show_config_notice

__all__ = ["ZYAgentApp", "run_app", "show_config_notice"]
