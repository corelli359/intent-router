# Router V4 Service 设计开发与测试总结 v0.1

## 当前结论

本版把 v4 spec 结构进一步收敛为：**一个意图目录 `intent.md` + 按需 Skill reference**。

- Router 识别阶段只读取 `default_specs/intent.md`。
- `intent.md` 内集中维护所有意图清单、意图边界、正反例、目标 Agent、派发契约和 `skill_ref`。
- Router 识别结果只包含 `intent_id`、置信度和理由。
- Router 命中意图后，直接使用该 intent 条目里的 `target_agent`、`dispatch_contract`、`skill_ref` 创建 Agent task。
- Router 不读取 `skills/*.skill.md` 正文，不提槽、不追问、不执行业务 API。
- 执行 Agent 按 `skill_ref.path` 加载自己的 Skill md，并按 Skill 步骤执行提槽、追问、确认、风控、限额、API 和 handover。

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
        ├── intent.md
        ├── agents/agent-registry.md
        └── skills/*.skill.md
```

## 分层边界

### 助手层

负责用户入口、页面上下文、主动推送上下文、最终用户可见话术。助手可以调用 Router，也可以在 Router 派发后调用执行 Agent，但最终展示给用户的结果由助手生成。

### 意图识别 / Router 层

负责：

- 加载单个 `intent.md`。
- 调用 LLM 识别 `intent_id`。
- 从命中 intent 条目中读取 `target_agent`、`skill_ref`、`dispatch_contract`。
- 创建 Agent task。
- 跟踪 Router 侧 session、task、graph、handover。

不负责：

- 提取收款人、金额等业务字段。
- 加载 Skill md 正文。
- 业务缺槽追问。
- 业务确认、风控、限额、API 调用。

### 执行 Agent 层

负责按 `skill_ref.path` 加载自己的 Skill md，并根据 Skill 完成业务生命周期。以转账为例，`transfer-agent` 加载 `services/transfer-agent-demo/skills/transfer.skill.md`，处理自由表达提槽、缺槽追问、确认、执行和 `ishandover=true && output.data=[]`。

## 渐进式加载

当前 `prompt_report.load_trace` 的主线是：

```text
turn_start          -> router_boundary / routing_state
before_recognition  -> intent_catalog
after_recognition   -> recognized_intents
after_recognition   -> skill_reference
before_dispatch     -> dispatch_contract
```

这里的关键点是：识别阶段只读一个 `intent.md`。`skill_reference` 只是同一目录里的引用信息，不是 Skill 正文。Skill 加载证据只应来自执行 Agent 的 `agent.skill_loaded` 事件。

## 已验证场景

```bash
PYTHONPATH=backend/services/router-v4-service/src pytest backend/tests/test_router_v4_service.py -q
PYTHONPATH=services/assistant-demo pytest services/assistant-demo/tests -q
PYTHONPATH=services/transfer-agent-demo pytest services/transfer-agent-demo/tests -q
python -m compileall -q backend/services/router-v4-service/src/router_v4_service services/assistant-demo services/transfer-agent-demo
node --check services/router-v4-observer-ui/app.js
```

## 关键修正

- 删除默认 `intents/*.intent.md`、`routes/intent-routes.md`、`scenes/*.scene.md` 源。
- 新增 `default_specs/intent.md` 作为唯一 Router 识别目录。
- `IntentSpec` 自身携带 `scene_id`、`target_agent`、`skill_ref`、`dispatch_contract`。
- `prompt_report` 从 `intent_markdown_index/scene_contract` 改为 `intent_catalog/skill_reference`。
- Router task payload 保留 `intent_id`、`scene_id`、`skill_ref`、`intent_catalog_hash`。
- 默认 Skill 文档改为可执行规范结构：Metadata、Boundary、Inputs、State、Steps、Slot Policy、Handover、Output Contract。

## 当前限制

- v4 仍是 demo 级文件态 / 内存态存储，生产需替换 Redis / SQL。
- SSE 目前只保留 task 级 `stream_url` / `resume_token` 和 graph/task 状态，尚未接真实长连接消费。
- 转账 Agent demo 仍是本地模拟执行，后续应替换为真实业务 API 和真实 Agent runtime。
