# web/ —— Web 前端（预留）

本目录为未来 Web 版本的占位。core 层已经为 Web 做好准备，接入只需三步：

## 接入方式

1. **每会话一个引擎**：HTTP 无状态，需为每个会话（session id）持有独立的
   `core.AgentEngine`（内存字典或外部存储），因为引擎内含消息历史与工具注册表。
2. **事件即协议**：`engine.ask()` 产出的事件直接映射为推送消息：

   ```python
   for event in engine.ask(question):
       await websocket.send_text(json.dumps(event.to_dict(), ensure_ascii=False))
   ```

   `AgentEvent.to_dict()` 输出形如 `{"type": "ToolCallFinished", "id": ..., ...}`，
   前端按 `type` 分发即可（渲染契约与 `core/events.py` 完全一致）。
3. **异步化**：core 是同步阻塞式生成器，用 `asyncio.to_thread` 逐事件步进
   （参考 `tui/app.py` 的 `_ask`），或在线程池中运行并推入 asyncio 队列。

## 注意事项

* Web 场景下文件工具操作的是**服务器**文件系统，务必加路径白名单与权限控制；
* 建议为每个会话加并发锁：同一会话的两次 `ask()` 不要并行；
* 静态资源与页面代码放本目录，不改动 `core/`。
