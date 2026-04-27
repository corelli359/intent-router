# Router V4 Service 设计开发与测试总结 v0.1

## 目标

在不改现有 `router-service` 主链路的前提下，新增独立 `router-v4-service`，落地 spec 驱动的意图识别层。

该服务只负责：

- 场景识别
- 加载场景方提供的 Router 侧 routing spec
- 接收 LLM 根据 spec/skill 识别出的路由槽位 hints，并投影到场景 spec
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

补充约束：Router runtime 不做正则匹配、不做 keyword/词表匹配、不做硬编码推送接受/拒绝判断，也不做本地启发式提槽。场景选择、主动推送承接、多意图判断、路由槽位 hints 均由 LLM recognizer 基于 scene routing spec、push_context 和 scene-owned Skill metadata 产出。

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
    │   ├── models.py
    │   ├── recognizer.py
    │   ├── runtime.py
    │   ├── slot_projector.py
    │   ├── spec_registry.py
    │   └── stores.py
    └── default_specs/
        ├── agents/agent-registry.json
        └── scenes/*.routing.json
```

## Spec 驱动落地方式

Router 侧 routing spec 驱动：

- `triggers`：作为 LLM 识别场景的正负例材料，不由 Router runtime 做字符串匹配
- `routing_slots.extraction`：作为 LLM 提槽说明，不由 Router runtime 解释执行
- `skill`：绑定场景执行 Skill 元信息，进入识别上下文和 Agent task payload
- `target_agent`：驱动派发目标
- `dispatch_contract`：驱动 Agent task 报文字段

Agent 侧 execution spec 不在 Router 中解释。Router 只把任务和 Skill 绑定信息交给目标 Agent。

## 上下文生命周期落地

本版已经把“状态持久化 + 渐进加载 + 压缩/裁剪 + 按需检索”落到代码里，但保持在 Router 边界内：

- 状态持久化：默认 in-memory；设置 `ROUTER_V4_STATE_DIR` 后使用文件态 session store 和 transcript JSONL store。
- 渐进加载：每轮先加载 Router 边界、路由状态、场景索引；识别候选后加载候选摘要；选定场景后才加载 routing spec、routing slot spec、dispatch contract。
- 压缩/裁剪：`ContextBuilder` 根据 `ROUTER_V4_CONTEXT_MAX_CHARS` 做预算控制，核心块保留，低优先级块如 transcript/reference 会被裁剪，并在 `prompt_report.dropped_blocks` 中可观测。
- 按需检索：当前按 scene spec 的 `references` 返回引用清单，先完成接口形态；后续可替换为向量/关键词检索。
- 多轮续接：已派发 Agent 后，同一 Router session 的后续消息不重新识别，直接转发给已有 `agent_task_id`；Router 侧缺路由槽位时，会进入 `pending_scene_id` 并在下一轮补齐后派发。

## 已实现接口

```text
GET  /health
POST /api/router/v4/message
GET  /api/router/v4/sessions/{session_id}
GET  /api/router/v4/sessions/{session_id}/tasks/{task_id}
GET  /api/router/v4/sessions/{session_id}/graphs/{graph_id}
POST /api/router/v4/agent-output
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
- LLM recognizer OpenAI-compatible 调用、候选槽位返回、Agent 派发
- runtime 不依赖规则 matcher 或本地 slot extractor
- 后续多轮消息转发到已有 Agent task
- Router 侧 pending 场景跨轮补齐 required routing slots
- 文件态 session/transcript 持久化后跨 runtime 实例续接
- 上下文预算裁剪与核心块保留
- 未识别场景澄清
- 缺失 Agent 时不派发
- FastAPI message endpoint
- FastAPI session snapshot endpoint
- 主动推送 `push_context.intents` 候选约束与结构化 `assistant_push_policy`
- LLM 未选择推送意图时返回 `no_action`，不进入 Router 确认态
- 多意图返回 `planned` + task 级 stream URL
- Agent structured output 回传后，Router 只记录结构化结果，由助手生成最终表达
- 固定 `ishandover=true` 且 `output.data=[]` 时改派 `fallback-agent`
- 不接受 `isHandover` 驼峰写法
- fallback 不循环改派
- task snapshot endpoint

测试命令：

```bash
pytest backend/tests/test_router_v4_service.py -q
```

结果：

```text
22 passed in 0.16s
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

## v0.2 更新实现

依据 `docs/v4/router-v4-updated-requirements-v0.2.md`，本次新增：

- `source=assistant_push` 和 `push_context.intents`：Router 在主动推送模式下优先受推荐意图清单约束。
- 推送选择：Router runtime 不判断接受/拒绝词表，由 LLM recognizer 基于用户表达、`push_context.intents`、routing spec 和 Skill metadata 产出 selected scene；未选择时返回 `no_action`。
- 多意图计划：用户表达“都看一下/一起处理”时返回 `planned`，生成 graph 和多个 task，每个 task 带 `stream_url` 和 `resume_token`。
- Agent output callback：`POST /api/router/v4/agent-output` 接收结构化执行结果，Router 记录 `agent_output`，不生成最终用户话术。
- handover：只识别固定 `ishandover=true` 且 `output.data=[]`；触发后标记原任务 `handover_requested` 并派发 `fallback-agent`。
- 防循环：fallback task 再次 handover 时标记 `handover_exhausted`，不继续改派。
- task/graph snapshot：支持助手按 session 查询任务和图状态。

## v0.3 运行时边界收敛

本次依据“保持 v3 边界，Router runtime 不能有正则匹配、hardcode、启发式提槽，必须 spec 驱动 + skill”的要求，完成以下调整：

- 删除 v4 service 内的规则 `matcher.py` 和本地启发式 `extractor.py`。
- 新增 `core/recognizer.py`：只保留 LLM recognizer，输入为 scene routing spec、push_context、Skill metadata，输出为 selected scene、multi scene、confidence、reason、routing_slots。
- 新增 `core/slot_projector.py`：只负责把 LLM 返回的槽位按场景 `routing_slots` 白名单过滤和投影，不读取用户文本。
- `runtime.py` 删除推送接受/拒绝词表；主动推送统一交给 LLM 根据 `push_context.intents` 和 `assistant_push_policy` 判断。
- 默认 spec 的槽位定义从 `extractor` 改为 `extraction.type=llm`，并增加 `skill` 绑定。
- `RouterV4Settings.recognizer_backend` 默认改为 `llm`，不再提供 rules fallback。

真实 LLM 冒烟：

- `我要转账` -> `transfer` -> `transfer-agent`
- `查一下余额` -> `balance_query` -> `balance-agent`
- 单推荐 `就按这个办` -> `fund_query` -> `fund-agent`
- 多推荐 `两个都看一下` -> `planned`，拆成 `balance_query` 和 `fund_query` 两个 task，`stream_mode=split_by_task`

备注：真实 LLM 调用中出现过一次外部请求超时，runtime 直接返回 `llm_recognition_failed`，没有规则 fallback。

## v0.4 可观测与话术边界修正

针对功能联调中暴露的问题，本次修正：

- `RouterV4Output.response` 不再透出 mock Agent 的自然语言派发消息，单意图派发返回机器状态 `task_dispatched`。
- 观察前端不再把 Router 结构化状态展示成“助手结果”，改为“意图服务状态”；最终用户结果仍应由助手在 Agent output 完成后生成。
- `agent_dispatched` 事件增加 `skill` 和 `task_payload` 摘要，可以看到任务如何带着 `skill_id`、`routing_slots`、`context_refs` 触发执行 Agent。
- `prompt_report.load_trace` 增加逐项渐进式加载轨迹，展示加载阶段、文件、JSON path、内容摘要、是否被预算裁剪。
- 默认场景增加可见 markdown：
  - `default_specs/skills/transfer.skill.md`
  - `default_specs/skills/fund_query.skill.md`
  - `default_specs/skills/balance_query.skill.md`
  - `default_specs/references/*-routing.md`

以 `我我要转账` 为例，成功链路会显示：

```text
turn_start          -> router_boundary / routing_state
before_recognition  -> scene_index: 三个 *.routing.json
after_recognition   -> scene_candidates: transfer + reasons + routing slot hints
after_scene_selected -> transfer.routing.json / transfer.skill.md / transfer-routing.md
before_dispatch     -> dispatch_contract + task_payload
```

再次强调：`transfer.skill.md` 在 Router 中只是场景绑定 Skill card 和派发上下文，真正执行、补槽、确认、风控、API 调用仍由 `transfer-agent` 完成。

架构方案与业务旅程已更新到：

```text
docs/v4/router-v4-architecture-and-journey-v0.2.html
```

未在本轮实现：

- 真正 SSE 长连接与 resume 消费逻辑。当前先返回 task 级 `stream_url` / `resume_token` 并维护 task snapshot，为后续 SSE 接入预留。

## 后续演进

1. 将文件态 session/transcript 替换为 Redis / SQL，并补 TTL、审计和清理任务。
2. 接入真实 Agent registry 和 HTTP dispatch/stream forward。
3. 引入 LLM / embedding recognizer，但保留 routing spec 强约束。
4. 将 `references` 替换为真实知识库检索，结合 scene/spec 版本做引用追踪。
5. 增加 scene spec health check、版本锁定和灰度发布。
