# Router V4 Service 设计开发与测试总结 v0.1

## 当前结论

本版把 v4 spec 结构进一步收敛为：**从 `intent.md` 开始的 Intent ReAct Runtime + 按需 Skill ReAct**。

- Intent ReAct 第一步只读取 `default_specs/intent.md`，本步 action 是 `select_intent / plan_multi_intent / no_action`。
- `intent.md` 顶层只维护全局识别规范和加载约定；各意图小节分别维护自己的说明、正反例、执行边界、目标 Agent、派发契约和 `skill_ref`。
- Intent ReAct 第一步结果只包含选中的 `intent_id`、计划、置信度和理由，不提取业务字段。
- 命中意图后，Runtime 使用该 intent 条目里的 `target_agent`、`dispatch_contract`、`skill_ref` 创建 task，并进入后续 Skill ReAct。
- Runtime 会把已完成任务的紧凑业务结果沉淀为 `business_memory`，新任务派发时作为 `business_context` 给后续 Skill ReAct 使用。
- 后续 Skill ReAct 按 `skill_ref.path` 加载 Skill md，将 Skill、task snapshot、`business_context`、task memory 和本轮用户表达交给 LLM，按结构化 `SkillDecision` 执行提槽、追问、确认、风控、限额、API 和 handover。
- Skill md 可带 TOML frontmatter 作为机器可校验的执行约束，例如 `required_slots`、`confirmation_step`、`submit_tool`；runtime 只按这些 Skill 声明做通用校验，不在代码里写转账字段规则。

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
    │   ├── skill_runtime.py
    │   ├── tool_runtime.py
    │   └── stores.py
    └── default_specs/
        ├── intent.md
        ├── agents/agent-registry.md
        └── skills/*.skill.md
```

## 分层边界

### 助手层

负责用户入口、页面上下文、主动推送上下文、最终用户可见话术。助手可以调用 Router，也可以在 Router 派发后调用执行 Agent，但最终展示给用户的结果由助手生成。

### 意图框架 / Router 层

负责：

- 从 `intent.md` 开始执行 Intent ReAct。
- 调用 LLM 产生 `select_intent / plan_multi_intent / no_action` 决策。
- 从命中 intent 条目中读取 `target_agent`、`skill_ref`、`dispatch_contract`。
- 创建 Agent task。
- 跟踪 Router 侧 session、task、graph、handover。
- 维护完成态业务记忆，并在新 task payload 中传递 `business_context`。
- 命中意图后按 `skill_ref` 加载 Skill，继续 Skill ReAct。

不负责：

- 在 Intent ReAct 第一步提取收款人、金额等业务字段。
- 把 `business_context` 当作当前任务槽位。
- 用本地正则、hardcode 或 matcher 执行业务提槽。
- 脱离 Skill 说明自行决定业务确认、风控、限额、API 调用。

### 执行 Agent 层

业务 Agent / API 是 Skill 中声明或引用的业务能力归属方，不再代表“提槽归 Agent、识别归 Router”的功能切分。以转账为例，Runtime 加载 `transfer.skill.md` 后调用 LLM 输出结构化 `SkillDecision`；代码只负责校验决策、维护 task memory、确认门禁、执行 Skill 声明的 tool/API adapter 和 `ishandover=true && output.data=[]`。

## 渐进式加载

当前 `prompt_report.load_trace` 的主线是：

```text
turn_start          -> router_boundary / routing_state
after_state         -> business_memory       # 仅当有完成态业务记忆
intent_react_start  -> intent_catalog
intent_react_done   -> intent_react_decision
after_intent_react  -> skill_reference
before_dispatch     -> dispatch_contract
skill_react         -> skill_loaded / skill_react_decision
```

这里的关键点是：从 `intent.md` 开始已经是 ReAct。`business_memory` 是已完成任务的结构化结果，不是第一步对当前输入的提槽；`skill_reference` 是后续 Skill ReAct 的入口。只有命中意图后才按需加载 Skill 正文。

## 跨任务上下文

当前已落地的最小闭环：

```text
第一笔转账完成
-> Router 记录 business_memory.last_completed_by_scene.transfer
-> active task 可以结束 / 清理
-> 用户发起新转账：“给李四转一样的钱”
-> Intent ReAct 第一步选择 transfer，但不解析“金额=200”
-> Runtime 创建 task 时带 business_context.last_completed_for_same_scene
-> Skill ReAct 加载 transfer.skill.md，并把 business_context 交给 LLM
-> LLM 输出 amount=200、amount_source=business_memory
-> Runtime 进入确认，不直接执行
```

这解决的是“任务结束后上下文仍可用”，同时不破坏 Router 不提槽的边界。

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
- 新增 `default_specs/intent.md` 作为唯一 Router 识别目录，顶层只放全局规范，不在全局层展开各意图边界。
- `IntentSpec` 自身携带 `scene_id`、`target_agent`、`skill_ref`、`dispatch_contract`。
- `prompt_report` 从 `intent_markdown_index/scene_contract` 改为 `intent_catalog/skill_reference/skill_react_decision`。
- Router task payload 保留 `intent_id`、`scene_id`、`skill_ref`、`intent_catalog_hash`。
- Router session 新增 `business_memory`，task payload 新增 `business_context`。
- `transfer.skill.md` 新增 `required_slots=["recipient","amount"]` 等 frontmatter，runtime 用它校验 LLM action 是否与槽位完整性一致。
- 默认 Skill 文档改为中文可读的可执行规范结构：元数据、执行边界、输入、内部状态、执行步骤、槽位策略、上下文引用策略、LLM 决策输出、误派处理、输出契约。

## 当前限制

- v4 仍是 demo 级文件态 / 内存态存储，生产需替换 Redis / SQL，并为 `business_memory` 增加 TTL、归档和隐私治理。
- SSE 目前只保留 task 级 `stream_url` / `resume_token` 和 graph/task 状态，尚未接真实长连接消费。
- 转账 tool adapter 仍是 demo 级本地适配器，后续应替换为真实业务 API / RPC adapter。
