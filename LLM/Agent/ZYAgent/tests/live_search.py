"""searchWeb 联调脚本（真实网络，消耗 1 次免费搜索额度）。

用法::

    python tests/live_search.py "搜索关键词"

验证：读取 config.ini [search] 的 Baidu-qianfan-APIKEY -> 调用百度千帆
「百度AI搜索」-> 打印整理后的 摘要+标题+URL 文本（即回传给 LLM 的内容）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(errors="replace")  # Windows GBK 控制台容错

from core import create_engine, load_search_config  # noqa: E402


def main() -> int:
    query = " ".join(sys.argv[1:]) or "今天有什么科技新闻"

    search = load_search_config()
    print(f"config enabled : {search.enabled}")
    print(f"key            : {search.api_key[:16]}..." if search.api_key else "key            : (空)")
    print(f"endpoint       : {search.endpoint}")
    print(f"query          : {query}\n" + "-" * 60)

    engine = create_engine()          # 与正式程序同一条装配路径
    result = engine.registry.execute("searchWeb", {"query": query})
    print(f"ok = {result.ok}")
    print(result.output)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
