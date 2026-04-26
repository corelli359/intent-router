# Router V4 Service 设计开发与测试总结 v0.1

## 目标

在不改现有 `router-service` 主链路的前提下，新增独立 `router-v4-service`，落地 spec 驱动的意图识别层。

该服务只负责：

- 场景识别
- 加载场景方提供的 Router 侧 routing spec
- 按 routing spec 提取可直取槽位 hints
- 构造 Agent task
- 派发给场景执行 Agent
- 记录 Router 侧 session / transcript

该服务不负责：

- 业务确认
- 风控
- 限额判断
- 幂等处理
- 业务 API 调用

这些能力属于场景执行 Agent。

## 新增代码位置

```text
backend/services/router-v4-service/
├── pyproject.toml
├── README.md
└── src/router_v4_service/
    ├── api/
    │   ├── app.py
    │   └── schemas.py
    ├── core/
    │   ├── agent_client.py
    │   ├── config.py
    │   ├── context.py
    │   ├── extractor.py
    │   ├── matcher.py
    │   ├── models.py
    │   ├── runtime.py
    │   ├── spec_registry.py
    │   └── stores.py
    └── default_specs/
        ├── agents/agent-registry.json
        └── scenes/*.routing.json
```

## Spec 驱动落地方式

Router 侧 routing spec 驱动：

- `triggers`：驱动场景召回与负例抑制
- `routing_slots`：驱动 Router 可提取哪些槽位 hints
- `target_agent`：驱动派发目标
- `dispatch_contract`：驱动 Agent task 报文字段

Agent 侧 execution spec 不在 Router 中解释。Router 只把任务交给目标 Agent。

## 上下文生命周期落地

本版已经把“状态持久化 + 渐进加载 + 压缩/裁剪 + 按需检索”落到代码里，但保持在 Router 边界内：

- 状态持久化：默认 in-memory；设置 `ROUTER_V4_STATE_DIR` 后使用文件态 session store 和 transcript JSONL store。
- 渐进加载：每轮先加载 Router 规则、路由状态、场景索引；识别候选后加载候选摘要；选定场景后才加载 routing spec、routing slot spec、dispatch contract。
- 压缩/裁剪：`ContextBuilder` 根据 `ROUTER_V4_CONTEXT_MAX_CHARS` 做预算控制，核心块保留，低优先级块如 transcript/reference 会被裁剪，并在 `prompt_report.dropped_blocks` 中可观测。
- 按需检索：当前按 scene spec 的 `references` 返回引用清单，先完成接口形态；后续可替换为向量/关键词检索。
- 多轮续接：已派发 Agent 后，同一 Router session 的后续消息不重新识别，直接转发给已有 `agent_task_id`；Router 侧缺路由槽位时，会进入 `pending_scene_id` 并在下一轮补齐后派发。

## 已实现接口

```text
GET  /health
POST /api/router/v4/message
GET  /api/router/v4/sessions/{session_id}
```

## 默认场景

- `transfer` -> `transfer-agent`
- `fund_query` -> `fund-agent`
- `balance_query` -> `balance-agent`

默认 Agent endpoint 使用 `mock://`，用于本地闭环测试。

## 测试

新增测试：

```text
backend/tests/test_router_v4_service.py
```

覆盖：

- 默认 scene spec 和 agent registry 加载
- 转账场景识别、候选槽位提取、Agent 派发
- 后续多轮消息转发到已有 Agent task
- Router 侧 pending 场景跨轮补齐 required routing slots
- 文件态 session/transcript 持久化后跨 runtime 实例续接
- 上下文预算裁剪与核心块保留
- 未识别场景澄清
- 缺失 Agent 时不派发
- FastAPI message endpoint
- FastAPI session snapshot endpoint

测试命令：

```bash
pytest backend/tests/test_router_v4_service.py -q
```

结果：

```text
10 passed in 0.14s
```

编译检查：

```bash
PYTHONPATH=backend/services/router-v4-service/src python -m compileall -q backend/services/router-v4-service/src
```

结果：通过。

全量回归命令：

```bash
pytest -q
```

结果：

```text
50 failed, 251 passed, 4 skipped
```

失败集中在既有旧链路，不在新增 `router-v4-service`：

- `backend/tests/test_router_api_v2.py` 和 `backend/tests/test_assistant_service.py`：大量用例在创建旧 v2 session 时读取不到 `session_id`。抽查发现 `POST /api/router/v2/sessions` 和 `POST /api/router/sessions` 当前返回 404。
- `backend/tests/test_jwt_utils.py`：测试期望 `router_service.core.support.jwt_utils.generate_jwt` 支持 `issuer/not_before/extra_claims` 等参数，但旧实现目前是无参函数。

按本次要求“不要在原来的代码基础上改”，以上旧服务问题没有在本次改动中修复。

## 后续演进

1. 将文件态 session/transcript 替换为 Redis / SQL，并补 TTL、审计和清理任务。
2. 接入真实 Agent registry 和 HTTP dispatch/stream forward。
3. 引入 LLM / embedding recognizer，但保留 routing spec 强约束。
4. 将 `references` 替换为真实知识库检索，结合 scene/spec 版本做引用追踪。
5. 增加 scene spec health check、版本锁定和灰度发布。
