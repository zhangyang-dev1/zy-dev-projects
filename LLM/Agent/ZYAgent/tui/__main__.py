"""``python -m tui`` 直接启动 TUI（使用默认 config.ini）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import create_engine  # noqa: E402

from .app import run_app  # noqa: E402

if __name__ == "__main__":
    run_app(create_engine())
