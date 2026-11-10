"""Web 前端（预留占位）：未来基于 FastAPI/Flask 等实现。

集成要点（详见本目录 README.md）：
* 每个会话创建一个 ``core.AgentEngine`` 实例；
* 把 ``engine.ask()`` 事件包装成 SSE / WebSocket 消息（``AgentEvent.to_dict()`` 已可序列化）。
"""
