#!/usr/bin/env python
"""ZYAgent 程序入口（薄壳）：装载配置 -> 构建 core 引擎 -> 启动前端。

用法::

    python main.py                      # 启动终端 TUI 界面（默认）
    python main.py 当前目录下有哪些文件    # 单次提问（走控制台流式输出后退出）
    python main.py --ui cli             # 交互式控制台（/help /tools /reset /quit）
    python main.py --ui tui             # 终端 TUI 界面
    python main.py --config path/to/config.ini

首次在某个目录运行时，若没有 ``config.ini``，会自动生成一份带 mock 信息的
完整模板，并提示用户修改后重新启动。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import ConfigError, create_engine
from core.config import ensure_config

#: 可用前端注册表
FRONTENDS = ("tui", "cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ZYAgent",
        description="DeepSeek Function Calling Agent（core 与 UI 分离架构）",
    )
    parser.add_argument(
        "-u", "--ui", choices=FRONTENDS, default=None,
        help="前端类型（缺省：有提问参数时用 cli，否则 tui）",
    )
    parser.add_argument("-c", "--config", default=None,
                        help="config.ini 路径（缺省为当前工作目录下的 config.ini）")
    parser.add_argument("question", nargs="*", help="单次提问内容（可多个词）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    question = " ".join(args.question).strip()
    ui = args.ui or ("cli" if question else "tui")

    # ---- 首次运行自举：当前目录没有 config.ini 时生成完整模板
    try:
        cfg_path, created = ensure_config(args.config)
    except OSError as exc:
        print(f"无法创建配置文件: {exc}", file=sys.stderr)
        return 2

    if created:
        if ui == "tui":
            from tui import show_config_notice

            show_config_notice(cfg_path)
        else:
            print(
                f"已生成配置文件: {cfg_path}\n"
                "当前是 mock 配置：请修改 [llm] 的 api_key / base_url / model 后"
                "重新启动 ZYAgent。",
                file=sys.stderr,
            )
        return 0 if ui == "tui" else 2

    try:
        engine = create_engine(str(cfg_path))
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    if ui == "tui":
        from tui import run_app

        run_app(engine, initial_question=question or None)
        return 0

    from cli import run_console

    return run_console(engine, question=question or None)


if __name__ == "__main__":
    raise SystemExit(main())
