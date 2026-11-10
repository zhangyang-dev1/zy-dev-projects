# desktop/ —— 桌面前端（预留占位）

未来可基于 PySide6 / PyQt6 / pywebview 等实现原生桌面 GUI。

当前图形界面由项目根目录的 `gui.py` 提供（运行 `python main.py` 即启动）。

## 接入要点

若未来实现 `desktop/` 前端，遵循以下约定：

* **事件消费**：`engine.ask()` 为阻塞式同步生成器，需在后台线程
  （`threading.Thread` 或 Qt 的 `QThread`）中逐步 `for event in engine.ask(...)`，
  再通过信号槽 / 回调把 `AgentEvent` 安全回传到 UI 线程渲染。
* **事件契约**：`core/events.py` 中的事件为不可变 dataclass，可安全跨线程传递。
  重点处理 `AssistantDelta`（流式增量）、`AssistantMessage`（权威全文）、
  `ToolCallStarted/Finished`（工具卡片）、`ContextWarning`（上下文告警）、
  `TurnFinished`（本轮结束）。
* **不反向依赖**：`desktop/` 只允许 `import core.*`，不得被 `core` 反向导入。
