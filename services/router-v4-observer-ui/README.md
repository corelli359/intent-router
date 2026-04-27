# Router V4 Observer UI

独立的 Router V4 观察前端，不挂载到原有 `frontend/` 工作区。

它只做两件事：

- 左侧展示掌银助手对话。
- 右侧查看 Spec + Skill 如何驱动识别、加载、派发和 Agent 接管。
- 加载证据会展示每个阶段读取的 spec/json/md/reference、读取时机和内容摘录。

启动方式：

```bash
python -m http.server 3010 --bind 127.0.0.1
```

然后打开：

```text
http://127.0.0.1:3010
```

依赖后端：

```bash
ROUTER_V4_ENV_FILE=.env.local PYTHONPATH=backend/services/router-v4-service/src \
  python -m uvicorn router_v4_service.api.app:app --host 127.0.0.1 --port 8024
```

转账执行 Agent 是独立服务：

```bash
cd services/transfer-agent-demo
python -m uvicorn app:app --host 127.0.0.1 --port 8031
```

当前前端调用真实 `/api/router/v4/message` 和独立 `/api/transfer-agent/turn`，不会调用 Router 内置模拟接口。
Router V4 默认走 LLM 识别；`.env.local` 需要提供 OpenAI-compatible LLM 配置。
