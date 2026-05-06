# Router Service 需求梳理与 TODO v0.1

状态：当前梳理草案  
更新时间：2026-05-06  
适用分支：`test/v3-concurrency-test`

## 1. 本文档目的

本文档用于重新收敛当前分支的真实需求状态，重点回答三个问题：

1. 当前已经落地了什么。
2. 哪些内容只是已有设计或草案，还没有进入主运行时代码。
3. 从当前 v3 形态演进到更明确的 spec 驱动 / skill runtime 还剩哪些 TODO。

本文档以当前分支代码为准。若历史文档、归档文档与当前实现冲突，以当前实现为准。

## 2. 当前结论

当前分支不是严格意义上的“一等 `SkillSpec + SkillRuntime`”。

更准确的定义是：

> 当前是 **intent catalog / spec-like metadata 驱动的动态图运行时**。Router 已经消费 `field_catalog`、`slot_schema`、`graph_build_hints` 等结构化元数据完成识别、编图、提槽、校验、调度和助手协议投影；但 skill 仍停留在演进设计层，还没有成为主运行时抽象。

当前已具备的 spec-like 能力：

1. Intent 由 catalog 管理，支持 memory / database / postgres / file 后端。
2. file 模式支持拆分挂载 `intents`、`field_catalogs`、`slot_schemas`、`graph_build_hints`。
3. `IntentDefinition` 已经包含 `request_schema`、`field_mapping`、`field_catalog`、`slot_schema`、`graph_build_hints`、`resume_policy`。
4. Graph builder / planner / slot extractor / slot validator 会消费这些元数据。
5. Assistant v0.5 协议已经固定 `POST /api/v1/message` 和 `POST /api/v1/task/completion`。

当前尚未具备的 skill runtime 能力：

1. 没有 source-of-truth 级别的 `SkillSpec` 模型。
2. 没有 `SkillRegistry`、`SkillMatcher`、`SkillExecutor` 抽象。
3. 没有 `SpecBundle` 版本绑定。
4. 没有独立 skill assets 目录或 skill 发布单位。
5. 当前执行后端仍是 `agent_url` / HTTP agent client，而不是 executor contract。

## 3. 当前有效边界

### 3.1 Router 负责

1. `POST /api/v1/message` 消息入口。
2. 会话状态、任务状态和业务对象状态维护。
3. 基于 active intents 做识别和候选管理。
4. 构造 `ExecutionGraphState`，维护节点状态和图状态。
5. Router 侧槽位提取、槽位校验、历史预填和推荐上下文注入。
6. waiting node / pending graph 下的续轮解释。
7. 调用下游 agent 并把 agent 流式输出折叠成助手协议。
8. 通过 `POST /api/v1/task/completion` 接收助手完成确认。
9. 通过 SSE 输出 `message` / `done` 帧。

### 3.2 Router 不负责

1. 不执行真实业务动作。
2. 不判断业务结果真实性。
3. 不替代 agent 做领域强校验。
4. 不把 `recommendTask` / `currentDisplay` 透传给下游 agent。
5. 不维护完整长期记忆治理系统。
6. 当前不作为通用 skill plugin runtime。

### 3.3 Agent 负责

1. 接收 Router 下发的标准化请求。
2. 做本意图业务语义的最终防守性校验。
3. 返回业务执行结果、等待用户输入、等待确认或失败。
4. 不掌握跨意图图编排权。

### 3.4 上游助手 / 客户端负责

1. 调用 `POST /api/v1/message` 提交用户输入。
2. 消费 SSE 帧，区分识别帧和业务状态帧。
3. 以顶层 `completion_state` / `completion_reason` 判断任务是否最终完成。
4. 在 agent 业务结果展示完成后调用 `POST /api/v1/task/completion`。

## 4. 当前固定协议

### 4.1 消息入口

```http
POST /api/v1/message
```

当前请求关键字段：

```json
{
  "sessionId": "sess_001",
  "txt": "给小明转账200元",
  "stream": true,
  "executionMode": "execute",
  "custId": "C0001",
  "recommendTask": [],
  "currentDisplay": [],
  "config_variables": []
}
```

当前 `executionMode` 只有：

1. `execute`
2. `router_only`

注意：`per_node` 在 v4 草案中出现，但当前分支的公开请求模型不支持。

### 4.2 任务完成入口

```http
POST /api/v1/task/completion
```

当前请求关键字段：

```json
{
  "sessionId": "sess_001",
  "taskId": "task_123",
  "completionSignal": 2,
  "stream": true
}
```

当前语义：

1. `completionSignal=1` 表示阶段性完成信号。
2. `completionSignal=2` 表示助手确认当前任务最终完成。
3. Agent 自己返回 `completion_state=2` 不等于 Router 顶层完成。
4. Router 必须先进入 `waiting_assistant_completion`，再由助手 completion 回调收敛为最终完成。

### 4.3 当前未公开的动作入口

代码内部存在 graph action flow，支持：

1. `confirm_graph`
2. `cancel_graph`
3. `cancel_node`

但当前助手 v0.5 公开生产入口只固定了：

1. `POST /api/v1/message`
2. `POST /api/v1/task/completion`

因此如果后续需要客户端显式确认 / 取消图，需要补齐公开协议、路由实现和回归测试。

## 5. 需求主线

### R1. 稳定助手协议

需求：

1. `sessionId` 必须来自请求体。
2. 主入口必须支持 stream / non-stream，但验收以 stream 为主。
3. SSE 必须使用 `event: message` 和最终 `event: done`。
4. 意图识别后必须先推 `stage=intent_recognition` 帧。
5. 顶层 `message` 是 Router 占位文案，真实业务结果在 `output`。
6. 顶层 `completion_state` / `completion_reason` 是唯一完成态判断依据。

现状：基本已落地。

剩余：

1. 补齐部分流式回归。
2. 对错误帧、取消帧、图确认帧继续固化验收样例。

### R2. Catalog / Spec-like 元数据驱动

需求：

1. Intent 目录是运行时事实来源。
2. Router 不应在业务逻辑中硬编码意图正则或槽位规则。
3. `field_catalog` 定义字段语义。
4. `slot_schema` 定义槽位、必填、来源权限、提示文案。
5. `graph_build_hints` 定义图构造和确认策略提示。
6. file 模式应支持拆分挂载，便于部署环境只读配置。

现状：已落地到 `IntentDefinition` / `IntentPayload` / file repository / SQL repository / graph builder / slot extractor / slot validator。

剩余：

1. 需要把当前“spec-like intent metadata”的正式口径写入主需求文档。
2. 需要决定是否继续叫 intent spec，还是进入 `SkillSpec v0.1` 兼容层。
3. 需要补 catalog schema 的版本字段、校验命令和示例。

### R3. Router 侧理解和槽位硬门

需求：

1. Router 在 agent 调用前完成基础槽位准备。
2. 必填槽位缺失时进入 `waiting_user_input`，不能静默调用 agent。
3. 推荐、历史、当前消息三类来源必须可区分。
4. `allow_from_history` / `allow_from_recommendation` 必须控制槽位复用。
5. 用户纠错时必须覆盖旧槽位。

现状：核心链路已落地。

剩余：

1. 多轮槽位覆盖 / 纠错的流式回归未补齐。
2. router_only 多轮提槽的流式回归未补齐。
3. 同一意图内目标变更的完整语义仍未完全治理。

### R4. 动态图编排

需求：

1. 单意图退化成单节点图。
2. 多意图构造成多节点图。
3. 图节点按依赖关系推进。
4. 节点支持 waiting / running / completed / failed / cancelled / skipped。
5. 条件依赖不满足时 downstream 节点应跳过。
6. 图状态和 `task_list` 必须来自同一运行态。

现状：主干已落地动态图 runtime。

剩余：

1. 多意图 / 待确认图的流式回归仍需补齐。
2. 图确认 / 取消是否成为公开协议需要拍板。
3. `task_list` 是否需要显式暴露 graph version 需要拍板。

### R5. 助手完成态闭环

需求：

1. Agent 输出业务结果后，Router 顶层进入 `waiting_assistant_completion`。
2. 只有助手调用 completion 回调后，Router 才把任务置为最终完成。
3. 多任务图中后续任务推进必须以当前任务完成闭环为前提。
4. completion SSE 必须能返回当前任务完成事件和必要的后续执行事件。

现状：v0.5 已固定并有回归。

剩余：

1. 需要明确 completion 后自动 drain 与客户端单节点推进之间的产品选择。
2. 如果引入 `per_node`，completion 行为要和当前 `execute` 模式分流。

### R6. 打断、取消、切换与恢复

需求：

1. waiting node 下用户补充信息时恢复当前节点。
2. waiting node 下用户取消时取消当前节点或图。
3. waiting node 下用户提出新意图时触发 replan。
4. 旧图应保留审计状态，新图成为焦点。

现状：有 turn interpreter 和内部 action flow，部分链路已覆盖。

剩余：

1. 意图取消自动化未补齐。
2. 意图切换自动化未补齐。
3. 取消语义需要统一到助手协议文档。
4. 是否公开 `/api/v1/action` 需要决策。

### R7. 推荐任务和当前展示上下文

需求：

1. `recommendTask` / `currentDisplay` 只作为 Router 理解和规划上下文。
2. 这些上下文不能进入下游 agent `input_context`。
3. 用户可通过“选第一个和第三个”触发推荐任务规划。
4. 推荐任务中带槽位时可直接进入分发；缺槽时必须反问。

现状：核心边界已覆盖。

剩余：

1. 需要补流式等价回归。
2. 需要补推荐任务缺槽与用户继续推进的端到端验收样例。

### R8. 可观测、可测试、可压测

需求：

1. Router diagnostics 必须能解释识别、编图、槽位和降级原因。
2. 识别失败不能推误导性 `intent_recognized`。
3. fake LLM / fast fake / agent barrier 应支持压测和边界验证。
4. 回归测试以 SSE 为主。

现状：已有测试矩阵和多组回归命令。

剩余：

1. 当前文档中的测试基线需要定期重跑更新。
2. 需要补齐 P0 场景后再形成一次完整验收报告。

### R9. Spec / Skill 演进

需求方向：

1. 保留 Router 中心编排权。
2. 将 intent 元数据升格为声明式能力规格。
3. 将 agent HTTP 调用抽象为 executor contract。
4. 将 slot、guardrail、turn policy、execution contract 纳入统一 spec bundle。
5. session 或 graph 绑定 spec bundle 版本，保证多轮可回放和可审计。

现状：只有归档设计稿，没有主运行时代码。

剩余：

1. 定义 `SkillSpec v0.1` 最小字段。
2. 定义 `IntentDefinition -> LegacyIntentSkillSpec` 兼容映射。
3. 定义 `SkillExecutor` 协议和 HTTP agent executor 的第一版实现。
4. 定义 skill assets 存放结构、版本号和校验命令。
5. 决定 Admin / catalog 是否继续管理 intent，还是开始管理 skill registry。

## 6. TODO 看板

### P0：当前 v3 协议和回归补齐

| ID | TODO | 状态 | 备注 |
|---|---|---|---|
| P0-01 | 将 v0.5 助手完成态语义同步回主需求文档 | 未开始 | 当前主需求文档还是 2026-04-18 口径 |
| P0-02 | 补 S04 多轮槽位覆盖 / 纠错流式回归 | 未完成 | 现有矩阵标记“已有非流覆盖，需补流式” |
| P0-03 | 补 S05 router_only 多轮提槽流式回归 | 未完成 | 验证最终 `ready_for_dispatch` |
| P0-04 | 补 S10 多意图 / 待确认图流式回归 | 未完成 | 验证 `task_list`、图卡片、actions |
| P0-05 | 补 S11 意图取消自动化 | 未完成 | 覆盖 `assistant_cancel` |
| P0-06 | 补 S12 意图切换自动化 | 未完成 | 覆盖 waiting 下 replan |
| P0-07 | 补 S16 `recommendTask/currentDisplay` 流式等价回归 | 未完成 | 确认不透传 agent |
| P0-08 | 重跑并更新完整回归基线 | 未开始 | 包含单元、assistant 协议、真实 LLM smoke 可选 |

### P1：协议产品决策

| ID | TODO | 状态 | 备注 |
|---|---|---|---|
| P1-01 | 决定是否引入 `executionMode=per_node` | 待决策 | v4 草案有该方向，当前代码不支持 |
| P1-02 | 决定 completion 后是否自动推进后续 ready 节点 | 待决策 | 当前 v0.5 和 v4 草案口径不同 |
| P1-03 | 决定是否公开 `/api/v1/action` | 待决策 | 内部 action flow 已存在，公开路由未暴露 |
| P1-04 | 统一 `confirm_graph` / `cancel_graph` / `cancel_node` 的助手协议输出 | 待设计 | 需要 SSE 与 JSON 样例 |
| P1-05 | 明确 graph version / task version 是否进入 `task_list` | 待设计 | 用于客户端识别整图替换 |

### P2：Spec 驱动收敛

| ID | TODO | 状态 | 备注 |
|---|---|---|---|
| P2-01 | 给 catalog JSON 定义显式 schema/version | 未开始 | 覆盖 intents / fields / slots / hints |
| P2-02 | 增加 catalog 校验脚本 | 未开始 | 校验字段引用、slot key、agent_url、confirm policy |
| P2-03 | 增加 catalog 示例和最小接入模板 | 未开始 | 新意图接入不改 Router 代码 |
| P2-04 | 将 prompt 中的约束来源映射回结构化 spec 字段 | 未开始 | 降低 prompt 聚合不可治理风险 |
| P2-05 | 明确 `graph_build_hints` 的稳定字段和弃用字段 | 未开始 | 防止临时 prompt hint 膨胀 |

### P3：Skill Runtime 演进

| ID | TODO | 状态 | 备注 |
|---|---|---|---|
| P3-01 | 定义 `SkillSpec v0.1` | 未开始 | 先兼容当前 `IntentDefinition` |
| P3-02 | 实现 `IntentDefinition -> LegacyIntentSkillSpec` 映射 | 未开始 | 保持外部 API 不变 |
| P3-03 | 定义 `SkillExecutor` 协议 | 未开始 | 第一版只包 HTTP agent |
| P3-04 | 引入 `SkillRegistry` 兼容层 | 未开始 | 底层仍可读取 intent catalog |
| P3-05 | session / graph 绑定 spec bundle version | 未开始 | 解决多轮可回放和版本漂移 |
| P3-06 | 设计 skill assets 目录与发布流程 | 未开始 | 包括 spec、prompt、eval cases、references |
| P3-07 | 评估 Admin 从 intent registry 演进为 skill registry | 未开始 | 涉及运营面和数据模型 |

## 7. 建议的下一步顺序

建议先不要直接大改成 skill runtime，先按下面顺序推进：

1. 先完成 P0，保证当前 v3 协议和测试闭环可信。
2. 再完成 P1，拍板客户端推进模型和 action 公开协议。
3. 然后做 P2，把现有 intent metadata 正式收敛成可校验 spec。
4. 最后做 P3，在不破坏现有 API 的前提下引入 skill 兼容层。

这样做的原因是：当前 Router 已经有可运行的动态图和助手协议，最大的风险不是少一个新名词，而是当前协议、测试和元数据边界没有完全收敛。先把 v3 基线打稳，再升级成 skill runtime，迁移成本最低。

## 8. 验收口径

当前 v3 基线验收应至少包含：

```bash
pytest backend/tests/test_router_api_v2.py \
  backend/tests/test_assistant_service.py \
  backend/tests/test_graph_orchestrator.py \
  backend/tests/test_router_api_errors.py \
  -q
```

推荐任务 / currentDisplay / 识别顺序 / blocked-turn 相关检查也应纳入合并前检查：

```bash
pytest backend/tests/test_prompt_templates.py \
  backend/tests/test_llm_integration.py::test_llm_intent_recognizer_uses_registered_intent_catalog_payload \
  backend/tests/test_llm_integration.py::test_llm_graph_planner_converts_structured_payload_to_execution_graph \
  backend/tests/test_v2_graph_builder.py::test_unified_graph_builder_can_force_confirmation_from_intent_hints \
  backend/tests/test_router_api_v2.py::test_v2_message_uses_recommend_task_and_current_display_only_for_router_context \
  -q

pytest \
  backend/tests/test_graph_compiler.py::test_graph_compiler_defers_recognition_event_until_it_matches_graph_order \
  backend/tests/test_v2_presentation.py::test_graph_event_publisher_pushes_pending_graph_card_payload \
  backend/tests/test_router_api_v2.py::test_v1_message_stream_aligns_recognition_with_graph_card_task_order \
  -q

pytest \
  backend/tests/test_understanding_service.py::test_waiting_node_turn_can_merge_action_and_intent_recognition_in_one_call \
  backend/tests/test_llm_integration.py::test_llm_blocked_turn_interpreter_merges_action_and_intent_recognition \
  backend/tests/test_llm_integration.py::test_turn_decision_clears_intent_fields_for_non_replan_actions \
  backend/tests/test_prompt_templates.py::test_v2_turn_interpreter_prompt_accepts_expected_variables \
  -q
```

可选真实 LLM smoke：

```bash
ROUTER_ENV_FILE=.env.local RUN_REAL_LLM_TEST=1 \
PYTHONPATH=backend/services/router-service/src \
pytest \
  backend/tests/integration/test_real_llm_runtime_script.py \
  backend/tests/integration/test_real_llm_intent_recognizer_only.py \
  backend/tests/integration/test_real_llm_blocked_turn_interpreter.py \
  -q -s
```

## 9. 关联文档

当前优先关联：

1. `docs/v3/router-service-需求说明文档.md`
2. `docs/v3/router-service-架构设计文档.md`
3. `docs/v3/router-service-助手对接接口文档-v0.5.md`
4. `docs/v3/router-service-助手协议回归测试用例-v0.5.md`
5. `docs/v3/router-service-联调接口测试文档-v0.5.md`

设计参考但不代表当前实现：

1. `docs/v3/archive/router-service-skill驱动架构演进方案.md`
2. `docs/v4/多意图规划执行：客户端管控循环需求与设计文档.md`
