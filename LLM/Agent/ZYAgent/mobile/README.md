# mobile/ —— 移动端（预留）

推荐路线：**复用 `web/` 前端 + PWA**。core 事件流协议（`AgentEvent.to_dict()`）
与 Web 版共用，移动端无需改动 core。

若需原生体验，可用 Kivy/BeeWare 实现，集成方式与 `desktop/` 相同：
后台线程消费 `engine.ask()`，事件经线程安全通道回到 UI 线程渲染。
