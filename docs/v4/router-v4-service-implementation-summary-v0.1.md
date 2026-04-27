# Router V4 Service 设计开发与测试总结 v0.1

## 当前结论

本版已把意图识别、场景契约、执行 Skill 拆开：

- 意图识别层只读取 `default_specs/intents/*.intent.md`。
- Router 识别结果只包含 `intent_id`、置信度和理由。
- Router 通过 `default_specs/routes/intent-routes.md` 把 `intent_id` 映射到执行 `scene_id`。
- Router 在意图命中后才读取 `default_specs/scenes/*.scene.md`，只用于派发契约。
- Router 不读取 `skills/*.skill.md` 正文，不提槽、不追问、不执行业务 API。
- 执行 Agent 在自己的生命周期内加载 Skill md，完成业务提槽、确认、风控、限额、API 和 handover。

## 代码位置

```text
backend/services/router-v4-service/
├── README.md
├── pyproject.toml
└── src/router_v4_service/
    ├── api/
    ├── core/
    │   ├── context.py
    │   ├── models.py
    │   ├── recognizer.py
    │   ├── runtime.py
    │   ├── spec_registry.py
    │   └── stores.py
    └── default_specs/
        ├── intents/*.intent.md
        ├── routes/intent-routes.md
        ├── scenes/*.scene.md
        ├── agents/agent-registry.md
        ├── skills/*.skill.md
        └── references/*-intent.md
```

## 分层边界

### 助手层

负责用户入口、页面上下文、主动推送上下文、最终用户可见话术。助手可以调用 Router，也可以在 Router 派发后调用执行 Agent，但最终展示给用户的结果由助手生成。

### 意图识别 / Router 层

负责：

- 加载独立 intent markdown spec。
- 调用 LLM 识别 `intent_id`。
- 通过路由映射找到执行场景。
- 加载场景派发契约。
- 创建 Agent task。
- 跟踪 Router 侧 session、task、graph、handover。

不负责：

- 提取收款人、金额等业务字段。
- 加载 Skill md 正文。
- 业务缺槽追问。
- 业务确认、风控、限额、API 调用。

### 执行 Agent 层

负责加载自己的 Skill md，并根据 Skill 完成业务生命周期。以转账为例，`transfer-agent` 加载 `services/transfer-agent-demo/skills/transfer.skill.md`，处理自由表达提槽、缺槽追问、确认、执行和 `ishandover=true && output.data=[]`。

## 渐进式加载

当前 `prompt_report.load_trace` 的主线是：

```text
turn_start           -> router_boundary / routing_state
before_recognition   -> intent_markdown_index
after_recognition    -> recognized_intents
after_intent_mapped  -> scene_contract
before_dispatch      -> dispatch_contract
before_recognition   -> retrieved_references
```

这里的关键点是：识别前没有 `scene_contract`，更没有 `skill_card`。Skill 加载证据只应来自执行 Agent 的 `agent.skill_loaded` 事件。

## 已验证场景

```bash
PYTHONPATH=backend/services/router-v4-service/src pytest backend/tests/test_router_v4_service.py -q
PYTHONPATH=services/assistant-demo pytest services/assistant-demo/tests -q
PYTHONPATH=services/transfer-agent-demo pytest services/transfer-agent-demo/tests -q
python -m compileall -q backend/services/router-v4-service/src/router_v4_service services/assistant-demo services/transfer-agent-demo
node --check services/router-v4-observer-ui/app.js
```

结果：

```text
router-v4-service: 22 passed
assistant-demo: 2 passed
transfer-agent-demo: 3 passed
compileall: passed
observer-ui js check: passed
```

## 关键修正

- 删除默认 `*.routing.md` 场景源，改为 `*.intent.md` + `*.scene.md`。
- 删除 Router prompt/report 中的 `agent_field_policy` 和 `skill_card`。
- LLM recognizer 输入从 scene spec 改为 independent intent spec。
- LLM recognizer 输出从 `selected_scene_id` 改为 `selected_intent_id`。
- Router task payload 增加 `intent_id`，并把 `skill` 改为 `skill_ref`。
- Observer UI 改为展示 `intent_markdown_index -> recognized_intents -> scene_contract -> dispatch_contract -> agent.skill_loaded`。
- 默认 Skill 文档改写边界：Router 不读取 Skill 正文，Agent 执行阶段加载。

## 当前限制

- v4 仍是 demo 级文件态 / 内存态存储，生产需替换 Redis / SQL。
- SSE 目前只保留 task 级 `stream_url` / `resume_token` 和 graph/task 状态，尚未接真实长连接消费。
- 转账 Agent demo 仍是本地模拟执行，后续应替换为真实业务 API 和真实 Agent runtime。
