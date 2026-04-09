# Dynamic Intent Graph Runtime Fix 改造方案

## 1. 背景与核心判断

当前 V2 的问题，不只是某几个函数写得不够优雅，而是整体边界定义不清：

- `backend/src` 下面同时堆了多个可独立部署的服务，以及它们的共享逻辑，物理结构和部署结构不一致。
- `IntentDefinition` 既承载 admin 注册模型，又承载运行时识别、图规划、Agent 调用、槽位映射，职责过载。
- `GraphRouterOrchestrator` 同时承担 API 应用服务、会话管理、上下文组装、图状态机、Agent 调度、事件发布、UI 展示文案等多种职责，已经成为超大类。
- 图运行时缺少清晰的上下游契约，导致 `_CONTEXT_KEY_ALIASES`、`_DEFAULT_INTENT_OUTPUT_KEYS`、`if context_key == "balance"` 这类补洞式逻辑出现。
- 推荐模式、普通对话模式、历史复用模式之间的数据协议没有正式建模，很多信息通过字符串前缀或隐式约定传递。

结论：

- 这不是“再修几个 if/else”能解决的问题。
- 需要按“服务目录隔离 + 共享包抽离 + contract-first + factory 编译”的方向重构。

---

## 2. 改造目标

### 2.1 目标一：按服务组织目录，而不是按技术层平铺

仓库里有几个服务，就应该有几个服务目录。每个服务目录只放该服务的入口、依赖、接口、应用逻辑和测试。

### 2.2 目标二：Intent 在 admin 注册时就提供完整运行契约

Intent 不是一条“描述 + agent_url”的记录，而应该是一份完整的 `IntentSpec`：

- 识别契约
- 槽位契约
- 图规划契约
- 执行契约
- 响应契约
- UI/推荐契约

然后由 `IntentFactory` 把 `IntentSpec` 编译成运行时对象。

### 2.3 目标三：Router 只做编排，不再承载业务语义补丁

Router Runtime 不应该知道：

- `balance` 是余额业务
- `query_account_balance` 是某个金融 intent
- 某个 Agent 的 cancel URL 怎么猜
- 推荐上下文要用什么魔法前缀字符串拼给大模型

这些都应该由契约和工厂层提供。

### 2.4 目标四：新增 100 个 intent 时，尽量不改 Router 核心代码

理想状态下，新增 intent 只涉及：

1. admin 注册 `IntentSpec`
2. 配置或部署对应 Agent
3. 补充对应测试

不需要进入 Router 核心执行代码打补丁。

---

## 3. 当前架构的核心问题

## 3.1 物理目录与服务边界不一致

当前 `backend/src` 目录把这些东西都平铺在一起：

- `admin_api`
- `router_api`
- `router_core`
- `intent_agents`
- `models`
- `persistence`
- `config`

这会带来两个问题：

- 从目录上看不出哪些是可独立部署服务，哪些是共享包。
- Router 服务、Admin 服务、Agent 服务之间的依赖很容易变成“直接 import 内部模块”，最终耦合越来越深。

## 3.2 Intent 模型过薄，运行时只能补硬编码

当前 admin 注册模型见 [models/intent.py](/root/intent-router/backend/src/models/intent.py)。

它有这些字段：

- `agent_url`
- `request_schema`
- `field_mapping`
- `slot_schema`
- `graph_build_hints`

看上去很多，但缺了最关键的几类契约：

- Agent 响应会产出哪些标准上下文字段
- 条件边消费的字段与哪些 intent 输出字段对齐
- 哪些槽位可跨节点共享，哪些只能当前节点使用
- Agent 的显式 `cancel_url`
- 推荐卡片和默认要素的结构化 UI 契约

因此运行时只能补洞：

- 用 `_CONTEXT_KEY_ALIASES` 做输出字段归一
- 用 `_DEFAULT_INTENT_OUTPUT_KEYS` 猜 intent 可能产出什么
- 用 `if context_key == "balance"` 改标题
- 用 `_cancel_url()` 猜 Agent 的取消地址

这些都是“契约没有前置建模，运行时被迫兜底”的表现。

## 3.3 `GraphRouterOrchestrator` 不是 orchestrator，而是大一统控制器

当前 [v2_orchestrator.py](/root/intent-router/backend/src/router_core/v2_orchestrator.py) 1933 行，混杂了：

- Session Store
- Message routing
- Recommendation routing
- Planner context 组装
- History prefill
- 图状态推进
- Graph status 计算
- Task 创建
- Agent dispatch
- Node chunk 处理
- Event 发布
- Snapshot 输出
- 前端展示文案

这意味着：

- 任何一个新需求都容易继续加到同一个文件
- 执行层、应用层、展示层相互污染
- 很难并行开发和模块测试

## 3.4 上下游数据结构缺少正式边界

当前存在多个隐式协议：

- Planner context 通过 `recent_messages` 中的字符串前缀传递
- Graph 条件通过 `GraphCondition.left_key` 和 Agent `payload` 的字面字段弱绑定
- 节点槽位、历史槽位、已完成节点输出、共享账户信息分散在多个结构里

结果是：

- recommendation 模式和 free dialog 模式的数据协议容易互相渗透
- Agent 响应字段名变了，图条件就可能失效
- 新增 intent 时，经常出现“planner 以为够了，agent 还要追问”

---

## 4. 目标目录结构

建议按“服务目录 + 共享包目录”重构。

```text
backend/
  services/
    admin-service/
      src/admin_service/
        app.py
        api/
        application/
        domain/
        infrastructure/
      tests/

    router-service/
      src/router_service/
        app.py
        api/
        application/
        planning/
        execution/
        session/
        presentation/
      tests/

    agent-runtime/
      src/agent_runtime/
        app.py
        sdk/
        adapters/
      tests/

    agents/
      transfer-money-agent/
        src/transfer_money_agent/
        tests/
      account-balance-agent/
        src/account_balance_agent/
        tests/
      forex-exchange-agent/
        src/forex_exchange_agent/
        tests/
      gas-bill-agent/
        src/gas_bill_agent/
        tests/
      fallback-agent/
        src/fallback_agent/
        tests/

  packages/
    intent-spec/
      src/intent_spec/
    graph-contract/
      src/graph_contract/
    agent-contract/
      src/agent_contract/
    shared-kernel/
      src/shared_kernel/
    intent-factory/
      src/intent_factory/
```

### 4.1 服务目录职责

- `admin-service`
  - 只负责 intent spec 注册、校验、版本管理、发布
- `router-service`
  - 只负责对话编排、图构建、图执行、会话状态、事件发布
- `agent-runtime`
  - 提供统一 agent SDK、公共适配器、执行外壳
- `agents/*`
  - 每个 agent 独立维护自己的业务逻辑

### 4.2 共享包职责

- `intent-spec`
  - admin 注册模型、版本化 schema、spec 校验器
- `graph-contract`
  - 图节点/边/条件/上下文 key 的标准模型
- `agent-contract`
  - AgentInvocationRequest / AgentInvocationResponse 标准协议
- `shared-kernel`
  - 纯通用枚举、错误码、时间/ID 工具
- `intent-factory`
  - 把 `IntentSpec` 编译为 Router 和 Agent 可直接消费的运行时对象

---

## 5. 新的数据结构设计

## 5.1 Admin 注册的核心对象：`IntentSpec`

建议把当前 `IntentPayload` 升级为版本化的 `IntentSpec`。

```json
{
  "spec_version": "v2.1",
  "intent_code": "exchange_forex",
  "status": "active",
  "display_name": "换外汇",
  "recognition": {
    "description": "执行单次外汇兑换",
    "examples": ["换200美元", "把人民币换成美元"],
    "priority": 90,
    "primary_threshold": 0.72,
    "candidate_threshold": 0.5
  },
  "slots": [
    {
      "slot_key": "card_number",
      "value_type": "account_number",
      "required": true,
      "allow_from_history": true,
      "share_scope": "graph_shared"
    },
    {
      "slot_key": "phone_last_four",
      "value_type": "phone_last4",
      "required": true,
      "allow_from_history": true,
      "share_scope": "graph_shared"
    },
    {
      "slot_key": "source_currency",
      "value_type": "string",
      "required": true,
      "share_scope": "node_only"
    },
    {
      "slot_key": "target_currency",
      "value_type": "string",
      "required": true,
      "share_scope": "node_only"
    },
    {
      "slot_key": "amount",
      "value_type": "currency",
      "required": true,
      "share_scope": "node_only"
    }
  ],
  "graph": {
    "intent_scope_rule": "单次换汇是一个 intent",
    "confirm_policy": "auto",
    "max_nodes_per_message": 4,
    "produces_context": [
      {
        "key": "exchanged_amount",
        "type": "currency"
      },
      {
        "key": "business_status",
        "type": "string"
      }
    ],
    "consumes_context": [],
    "planner_hints": {
      "single_node_examples": ["换200美元"],
      "multi_node_examples": ["先查余额，再换200美元"]
    }
  },
  "execution": {
    "run_url": "http://intent-forex-agent/run",
    "cancel_url": "http://intent-forex-agent/cancel",
    "request_mapping": {
      "sessionId": "$session.id",
      "taskId": "$task.id",
      "input": "$message.current",
      "account.cardNumber": "$slot_memory.card_number",
      "account.phoneLast4": "$slot_memory.phone_last_four",
      "exchange.sourceCurrency": "$slot_memory.source_currency",
      "exchange.targetCurrency": "$slot_memory.target_currency",
      "exchange.amount": "$slot_memory.amount"
    }
  },
  "response": {
    "slot_updates": {
      "card_number": "$payload.card_number",
      "phone_last_four": "$payload.phone_last_four"
    },
    "context_outputs": {
      "exchanged_amount": "$payload.exchanged_amount",
      "business_status": "$payload.business_status"
    }
  },
  "ui": {
    "recommendation_enabled": true,
    "default_recommendation_title": "换100美元"
  }
}
```

## 5.2 关键新增字段说明

### `graph.produces_context`

这是替代 `_DEFAULT_INTENT_OUTPUT_KEYS` 的正式字段。

作用：

- 明确一个 intent 在执行完成后能为 graph 提供哪些 canonical context key
- 条件边只能依赖这些已声明输出

### `execution.cancel_url`

这是替代 `_cancel_url()` 猜 URL 的正式字段。

作用：

- Router 不再推断 Agent URL 规则
- 每个 intent 明确自己的取消接口

### `response.context_outputs`

这是替代 `_CONTEXT_KEY_ALIASES` 运行时推断的正式映射。

作用：

- Agent 返回任意业务字段名
- 进入 graph runtime 前先标准化成 canonical context keys

### `slots[].share_scope`

作用：

- 定义槽位是否可在图内共享
- 避免所有槽位默认都可以历史继承或跨节点传递

建议值：

- `node_only`
- `graph_shared`
- `session_shared`

---

## 6. IntentFactory 设计

## 6.1 IntentFactory 的定位

Admin 注册后，不直接把 `IntentSpec` 原样丢给 Router。

应由 `IntentFactory` 完成“编译”：

- 校验 spec 完整性
- 生成识别视图
- 生成图规划视图
- 生成执行适配视图
- 生成响应标准化视图

## 6.2 Factory 产物

建议生成统一运行时对象 `CompiledIntent`：

```python
class CompiledIntent(BaseModel):
    identity: IntentIdentity
    recognition_profile: RecognitionProfile
    slot_profile: SlotProfile
    graph_profile: GraphProfile
    execution_profile: ExecutionProfile
    response_profile: ResponseProfile
    ui_profile: UIProfile | None = None
```

其中：

- `RecognitionProfile`
  - 给 recognizer / planner / graph_builder
- `SlotProfile`
  - 给 slot grounding / history prefill / graph shared slot policy
- `GraphProfile`
  - 给 graph runtime
- `ExecutionProfile`
  - 给 Agent dispatch
- `ResponseProfile`
  - 给 Agent 返回后的标准化

## 6.3 Factory 的收益

这样新增 100 个 intent 时：

- Router 只消费 `CompiledIntent`
- 不需要感知 admin 的原始存储结构
- Spec 变更可以在 factory 层做兼容

---

## 7. Router Service 的新分层

Router Service 建议重新拆成 6 层。

## 7.1 API 层

目录：

- `router_service/api/`

职责：

- 接收 HTTP 请求
- 做 DTO 校验
- 调 application service
- 返回 snapshot / SSE

禁止：

- 直接拼 prompt
- 直接改 graph
- 直接操作 task slot_memory

## 7.2 Application 层

目录：

- `router_service/application/`

职责：

- 处理 `HandleUserMessageCommand`
- 处理 `HandleActionCommand`
- 协调 session、planning、execution、presentation

它是薄应用服务，不做纯算法。

## 7.3 Session 层

目录：

- `router_service/session/`

职责：

- Session store
- long-term memory store
- shared slot context store
- session snapshot repository

## 7.4 Planning 层

目录：

- `router_service/planning/`

职责：

- 构造 `PlannerContext`
- 调 recognizer / graph_builder / planner
- 做 graph semantic validation
- 不做执行推进

建议拆为：

- `planner_context_builder.py`
- `recognition_service.py`
- `graph_planning_service.py`
- `recommendation_planning_service.py`

## 7.5 Execution 层

目录：

- `router_service/execution/`

职责：

- 图状态推进
- 条件评估
- ready node 选择
- task 构造
- node 执行
- response 标准化

建议拆为：

- `graph_engine.py`
- `condition_evaluator.py`
- `shared_slot_resolver.py`
- `node_task_factory.py`
- `node_runner.py`
- `agent_response_normalizer.py`

## 7.6 Presentation 层

目录：

- `router_service/presentation/`

职责：

- 构造 snapshot
- 发布 task/graph/session event
- 生成前端展示文案

禁止：

- 反向修改 graph 语义

---

## 8. 需要重建的上下游标准协议

## 8.1 Router 输入协议

目前 recommendation、proactive、普通对话三套上下文混在 message 里。

建议改成正式命令对象：

```python
class HandleUserMessageCommand(BaseModel):
    session_id: str
    cust_id: str
    user_message: str
    mode: Literal["free_dialog", "guided_selection", "proactive_recommendation"]
    recommendation_context: RecommendationContext | None
    proactive_selection_context: ProactiveSelectionContext | None
    client_metadata: ClientMetadata | None
```

这样 router 不需要再通过字符串前缀识别上下文来源。

## 8.2 Planner 输入协议

建议定义：

```python
class PlannerContext(BaseModel):
    user_message: str
    conversation_history: list[ConversationTurn]
    long_term_memory: list[MemoryFact]
    recommendation_context: RecommendationContext | None
    proactive_selection: ProactiveSelectionContext | None
    recognition_hint: RecognitionHint | None
    shared_slot_context: SharedSlotContext | None
```

这样：

- 普通模式和推荐模式都是不同字段，不再混在 `recent_messages`
- Prompt Builder 只负责序列化，不负责编造协议

## 8.3 Agent 调用协议

建议所有 Agent 统一接收：

```python
class AgentInvocationRequest(BaseModel):
    session: SessionRef
    task: TaskRef
    intent: IntentExecutionProfile
    message: CurrentMessage
    slot_memory: dict[str, Any]
    graph_context: GraphScopedContext
```

统一返回：

```python
class AgentInvocationResponse(BaseModel):
    event: str
    status: str
    display_message: str
    slot_updates: dict[str, Any]
    context_outputs: dict[str, Any]
    interaction: AgentInteraction | None
```

重点：

- `slot_updates` 用于更新节点槽位
- `context_outputs` 用于 graph 条件评估
- `display_message` 用于前端展示

这样 graph runtime 不再解析业务 payload 的随意字段。

---

## 9. 如何消除当前硬编码

## 9.1 `_CONTEXT_KEY_ALIASES`

### 根因

- 缺少全局 canonical context schema
- Agent 响应字段名不统一
- Intent 没有正式声明输出上下文字段

### 改造

- 新增 `ContextSchemaRegistry`
- 每个 `IntentSpec.response.context_outputs` 必须映射到 canonical keys
- Graph runtime 只认 canonical key，不认原始 payload 字段名

### 改造后

运行时不再需要：

- `_CONTEXT_KEY_ALIASES`
- `_DEFAULT_INTENT_OUTPUT_KEYS`

## 9.2 `if context_key == "balance"`

### 根因

- 隐含前置节点的展示文案来自运行时猜测

### 改造

- 新增 `ContextProviderSpec`
- 由 factory 编译得到“哪个 intent 可作为哪个 context key 的 provider”
- provider 的默认 title 从 intent spec 或 graph profile 中取

### 改造后

运行时不再写：

```python
if context_key == "balance":
    title = "查询账户余额"
```

## 9.3 `_cancel_url()`

### 根因

- 执行契约缺失显式 cancel 地址

### 改造

- `IntentSpec.execution.cancel_url` 必填
- `Task` 在创建时带入 `cancel_url`
- `StreamingAgentClient.cancel()` 只用显式地址

## 9.4 推荐上下文字符串前缀

### 根因

- Planner 缺少正式上下文对象

### 改造

- recommendation/proactive context 进入 `PlannerContext`
- prompt serializer 结构化输出 JSON

### 改造后

运行时不再依赖：

- `[FRONTEND_RECOMMENDATION_CONTEXT]`
- `[PROACTIVE_RECOMMENDATION_SELECTION]`

---

## 10. GraphRouterOrchestrator 的拆分蓝图

建议最终不再保留一个 1933 行的 orchestrator。

可以拆成下面这些主类：

### `ConversationApplicationService`

职责：

- 处理用户消息和动作命令
- 协调 session、planning、execution、presentation

### `GraphPlanningService`

职责：

- 调 recognizer / graph_builder / planner
- 返回 `PlannedGraphResult`

### `ExecutionGraphEngine`

职责：

- 纯图状态推进
- 条件评估
- ready node 决策

### `NodeTaskFactory`

职责：

- 把 GraphNode + CompiledIntent 变成 Task

### `NodeExecutionService`

职责：

- 调 Agent
- 处理 chunk
- 合并 slot updates / context outputs

### `PlannerContextBuilder`

职责：

- 统一构造 LLM 输入上下文

### `RouterEventPublisher`

职责：

- 发 session/graph/node 事件
- 构建前端 snapshot

---

## 11. 推荐的迁移步骤

## Phase 1：先重构目录，不改行为

目标：

- 把 `backend/src` 平铺结构迁到 `services/` + `packages/`
- 保持 import 兼容
- 不改变业务行为

产出：

- `admin-service`
- `router-service`
- `agent-runtime`
- `agents/*`
- `packages/*`

## Phase 2：引入 `IntentSpec` 与 `IntentFactory`

目标：

- 扩展 admin 注册模型
- 保持旧字段兼容
- 用 factory 生成 `CompiledIntent`

产出：

- `IntentSpec v2.1`
- `CompiledIntent`
- admin 侧 spec 校验器

## Phase 3：抽离 Execution Engine

目标：

- 将图状态机从 orchestrator 中移出
- 形成纯执行模块

首批迁移函数：

- `_refresh_node_states`
- `_condition_matches_from_condition`
- `_graph_status`
- `_next_ready_node`

## Phase 4：抽离 Planner Context

目标：

- recommendation/free dialog/proactive 三类上下文结构化
- 去掉字符串前缀协议

## Phase 5：重建 Agent 契约

目标：

- request/response 标准化
- `cancel_url` 显式化
- `context_outputs` 标准化

## Phase 6：删掉运行时业务补丁

在 Phase 2-5 完成后，再删除：

- `_CONTEXT_KEY_ALIASES`
- `_DEFAULT_INTENT_OUTPUT_KEYS`
- `_cancel_url()` 猜测逻辑
- recommendation 字符串前缀协议

---

## 12. 验收标准

改造完成后，至少满足：

1. 新增一个 intent，不改 Router 核心执行代码。
2. 新增一个条件依赖 intent，不改 graph runtime，只改 spec。
3. Agent 返回字段改名，只改 `response.context_outputs` 映射。
4. 推荐模式和普通模式用同一套 `PlannerContext`，不再靠字符串前缀混入。
5. Router 的纯执行状态机逻辑不再出现在应用层 orchestrator 中。
6. `GraphRouterOrchestrator` 若保留，应缩减成薄 facade，不超过 300-500 行。
7. 所有 deployable service 都有独立目录和独立入口。

---

## 13. 建议先做的三件事

如果只做最重要、最能止血的三件事，建议顺序是：

1. 先落 `IntentSpec` 扩展字段：
   - `execution.cancel_url`
   - `graph.produces_context`
   - `response.context_outputs`
   - `slots[].share_scope`

2. 先抽 `ExecutionGraphEngine`
   - 把图状态推进从 orchestrator 中剥离

3. 再重构 PlannerContext
   - 去掉 recommendation/proactive 的字符串前缀协议

这样能最快减少硬编码继续扩散。

---

## 14. 最终判断

这次 fix 分支不应再继续用“修一个现象、补一个 patch”的方式推进。

正确方向是：

- 先重构服务目录边界
- 再重构 intent 注册契约
- 再通过 intent factory 编译成运行时对象
- 最后把 router 拆成真正可维护的编排、规划、执行、展示分层

只有这样，后续再加 100 个 intent，Router 核心代码才不会继续失控膨胀。
