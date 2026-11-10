"""fetchWeb 联调脚本（真实网络：curl 抓取 + trafilatura 提取）。

用法::

    python tests/live_fetch.py https://example.com/article

验证：subprocess 调 curl 抓 HTML -> trafilatura 提取正文转 Markdown，
打印回传给 LLM 的完整文本（URL + 标题 + 正文）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows GBK 控制台容错

from core.tools import build_fetch_tool  # noqa: E402


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    spec = build_fetch_tool(timeout=30)
    print(f"fetch: {url}\n" + "-" * 60)
    try:
        out = spec.handler({"url": url})
    except Exception as exc:  # noqa: BLE001 - 联调脚本原样展示错误
        print(f"[ToolError] {exc}")
        return 1
    print(out)
    print("-" * 60)
    print(f"总字符数: {len(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
