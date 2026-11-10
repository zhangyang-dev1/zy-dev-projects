"""``python -m cli`` 运行控制台前端。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import create_engine  # noqa: E402

from .renderer import run_console  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run_console(create_engine()))
