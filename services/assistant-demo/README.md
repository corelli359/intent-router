# Assistant Demo Service

独立的掌银助手服务端示例。前端只调用本服务；本服务再调用 Router 和执行 Agent。

边界：

- 助手服务端负责会话入口、页面上下文、主动推送上下文、调用编排和最终用户可见话术。
- Router 从 `intent.md` 开始执行 Intent ReAct，命中后按 `skill_ref` 进入 Skill ReAct。
- Skill / tool 负责业务补槽、确认、风控、限额、业务 API 和结构化结果。
- `/api/assistant/turn/stream` 返回 `text/event-stream`，用于前端打字机式展示助手最终话术。

启动：

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8040
```

流式验证：

```bash
curl -N -X POST http://127.0.0.1:8040/api/assistant/turn/stream \
  -H 'content-type: application/json' \
  -d '{"sessionId":"sess-stream-demo","message":"给张三转200"}'
```

依赖：

- Router V4: `http://127.0.0.1:8024`
- Transfer Agent: `http://127.0.0.1:8031`

可选环境变量：

- `ASSISTANT_DEMO_ROUTER_BASE_URL`
- `ASSISTANT_DEMO_TRANSFER_AGENT_BASE_URL`

如果不传，默认回落到本地端口；部署到 K8s 时应改为集群内 Service 地址。
