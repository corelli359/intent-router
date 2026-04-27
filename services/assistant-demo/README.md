# Assistant Demo Service

独立的掌银助手服务端示例。前端只调用本服务；本服务再调用 Router 和执行 Agent。

边界：

- 助手服务端负责会话入口、页面上下文、主动推送上下文、调用编排和最终用户可见话术。
- Router 只负责 spec-driven 意图识别、任务派发、追踪和 handover。
- 执行 Agent 只负责业务补槽、确认、风控、限额、业务 API 和结构化结果。

启动：

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8040
```

依赖：

- Router V4: `http://127.0.0.1:8024`
- Transfer Agent: `http://127.0.0.1:8031`

