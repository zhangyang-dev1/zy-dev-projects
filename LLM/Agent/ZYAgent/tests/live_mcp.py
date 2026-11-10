"""MCP 真实联调：连接 .mcp.json 中的 Server，列工具并调用一次。

运行: python tests/live_mcp.py [工具名] [参数JSON]
缺省调用第一个工具（空参数）。
环境变量 ZY_MCP_ROOT 可覆盖项目根（缺省为 ZYAgent 源码根），用于指向
临时目录中的测试 .mcp.json。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.mcp_client import MCPManager  # noqa: E402
from core.tools import build_default_registry  # noqa: E402


def main() -> int:
    root = Path(os.environ.get("ZY_MCP_ROOT") or Path(__file__).resolve().parents[1])
    registry = build_default_registry()
    manager = MCPManager(init_timeout=30, call_timeout=60)

    print(f"配置: {root / '.mcp.json'}")
    manager.start(root, registry)
    print(manager.status())
    if not manager.registered:
        print("[FAIL] 没有任何 MCP 工具被注册")
        manager.stop()
        return 1

    # 展示发现的 MCP 工具 Schema
    mcp_names = [n for names in manager.registered.values() for n in names]
    for name in mcp_names:
        schema = next(s for s in registry.schemas if s["function"]["name"] == name)
        fn = schema["function"]
        print(f"\n== {name} ==\n描述: {fn['description'][:200]}\n"
              f"参数: {json.dumps(fn['parameters'], ensure_ascii=False)[:500]}")

    # 调用第一个工具
    target = sys.argv[1] if len(sys.argv) > 1 else mcp_names[0]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(f"\n调用 {target}({json.dumps(args, ensure_ascii=False)}) ...")
    result = registry.execute(target, args)
    print(f"ok={result.ok}")
    print(result.output[:3000])

    manager.stop()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
