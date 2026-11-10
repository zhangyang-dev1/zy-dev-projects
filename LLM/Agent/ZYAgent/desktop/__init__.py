"""Desktop 前端（预留占位）：未来基于 PySide6 / PyQt / pywebview 等实现。

集成要点（详见本目录 README.md）：
* 在后台线程消费 ``engine.ask()`` 事件，经 Qt 信号槽 / 回调更新控件；
* ``AgentEvent`` 为不可变 dataclass，可安全跨线程传递。
"""
