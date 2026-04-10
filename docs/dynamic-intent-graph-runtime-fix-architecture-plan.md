# Dynamic Intent Graph Runtime Fix 改造方案

## 1. 前提边界

这份方案以以下前提为基础，这些前提不是建议，而是架构边界：

- `admin-service`、`router-service`、各 `agent-service` 由不同团队维护。
- 每个服务都应当是无状态服务。
- 服务之间必须代码隔离，不能出现 `service -> service` 的内部代码依赖。
- 各服务之间的唯一协作方式是：
  - 读写数据库中的标准化数据
  - 调用标准 HTTP/SSE 接口
- `router-service` 唯一感知 intent 的方式，是从数据库中读取已发布的 intent 路由元数据。
- 每个 `agent-service` 对自己的单意图必须具备完整处理能力；如果没有 Router，它也应该能独立完成该意图。
- Router 的存在意义不是接管业务数据，而是做：
  - 理解
  - 编排
  - 调度

这意味着：

- Router 不应该持有领域业务模型。
- Router 不应该理解 agent 的内部表结构或业务对象。
- Agent 不应该依赖 Router 的内部实现。
- Admin 不应该和 Router 共享业务实现代码。

---

## 2. 当前架构的真实问题

## 2.1 当前目录结构没有体现服务边界

当前 `backend/src` 下平铺了：

- `admin_api`
- `router_api`
- `router_core`
- `intent_agents`
- `models`
- `persistence`
- `config`

问题不在于“看起来乱”，而在于：

- deployable service 和共享逻辑混在一起
- 从目录上看不出团队边界
- Router 很容易直接 import 本不该依赖的内部模块

这对单团队原型没问题，但对多团队长期维护是错误的物理结构。

## 2.2 Router 侧当前存在不该出现的业务补丁

典型例子：

- `_CONTEXT_KEY_ALIASES`
- `_DEFAULT_INTENT_OUTPUT_KEYS`
- `if context_key == "balance"`
- `_cancel_url()` 猜 URL

这些东西之所以出现，不是因为单个函数写坏了，而是因为：

- intent 路由元数据不完整
- agent 输出结果没有被标准化为 Router 可直接消费的意图结果
- graph 条件依赖引用的字段没有在注册阶段被明确约束
- Router 在运行时被迫去猜测

本质问题是：**Router 被迫承担了它不该承担的跨服务语义补洞职责。**

## 2.3 `GraphRouterOrchestrator` 已经不是 orchestrator，而是系统总控器

当前 [v2_orchestrator.py](/root/intent-router/backend/src/router_core/v2_orchestrator.py) 1933 行，混杂了：

- session 管理
- message routing
- recommendation routing
- planner context 拼装
- history prefill
- graph 状态推进
- node 调度
- agent 调用
- event 发布
- snapshot 构造
- UI 文案

这会导致两个直接后果：

- 增加意图时，虽然不一定每次都要改 orchestrator，但任何新场景都容易继续往这里塞逻辑
- 执行引擎、应用服务、展示层耦合，后续无法稳定演进

## 2.4 当前 Router 不是无状态服务

当前 `GraphSessionStore` 仍然是内存态，这和目标架构不一致。

如果 Router 是无状态服务，那么：

- session
- graph
- node runtime state
- active task / pending graph

都不能只存在单实例内存里，必须进入外部状态存储。

---

## 3. 正确的角色划分

## 3.1 Admin Service

职责：

- 注册和管理 intent 路由元数据
- 校验 intent 元数据是否完整
- 把“可发布”的 intent routing spec 写入数据库

不负责：

- graph 编排
- 节点调度
- 业务执行

## 3.2 Router Service

职责：

- 从数据库加载已发布的 intent routing spec
- 用大模型做意图识别
- 把意图编排为 graph
- 把 graph 节点调度到对应 agent
- 接收 agent 返回结果
- 依据意图结果推动 graph 前进

不负责：

- 业务数据管理
- 业务执行规则
- 领域模型持久化

一句话：

**Router 只做理解、编排、调度。**

## 3.3 Agent Service

职责：

- 完整处理自己的单意图
- 自己维护业务数据与领域规则
- 根据 Router 传入的输入与槽位完成交互或执行
- 把本意图的结果以统一 envelope 形式返回给 Router

一句话：

**每个 agent 都是单意图能力所有者。**

---

## 4. 目标目录结构

这里不再使用“共享业务 package”的思路，而是强调：

- 服务目录隔离
- 协议目录独立
- 可选极薄 SDK

```text
backend/
  services/
    admin-service/
      src/admin_service/
        api/
        application/
        domain/
        infrastructure/
      tests/

    router-service/
      src/router_service/
        api/
        application/
        planning/
        execution/
        session/
        presentation/
        infrastructure/
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

  contracts/
    intent-routing-spec/
    agent-protocol/
    router-callback-protocol/

  sdks/
    agent-sdk/   # 可选，且必须非常薄；如果组织不接受共享代码，可直接删除
```

### 4.1 `services/` 的含义

这里每个目录都对应独立服务、独立团队、独立部署单元。

### 4.2 `contracts/` 的含义

这里只放协议，不放共享业务实现。

包括：

- JSON Schema
- OpenAPI
- event schema
- 路由元数据 schema

### 4.3 `sdks/` 的含义

这里只允许极薄的序列化/反序列化和 client 壳子。

例如：

- `agent-sdk` 只提供：
  - `AgentInvocationRequest`
  - `AgentInvocationResponse`
  - 基础 FastAPI adapter

不能放：

- graph engine
- router core
- admin 校验业务逻辑

如果组织要求绝对零共享代码，那么 `sdks/` 可以完全不要，只保留 `contracts/`。

---

## 5. 数据归属矩阵

这是整个系统最关键的边界。

| 数据类型 | 归属服务 | Router 是否持有 |
|---|---|---|
| intent 路由元数据 | admin-service / DB | 只读加载 |
| 用户会话状态 | router-service 外部状态存储 | 是 |
| graph / node 运行状态 | router-service 外部状态存储 | 是 |
| agent 业务数据 | 各 agent 自己的 DB / 下游系统 | 否 |
| 单意图处理规则 | 各 agent | 否 |
| graph 条件依赖的节点结果 | Router 保存节点 result 副本 | 是 |

要点：

- Router 可以保存“节点结果副本”，因为这是编排状态的一部分。
- Router 可以保存 `slot_memory`，因为这是多轮编排运行态的一部分。
- 但 Router 不应保存 agent 的完整业务对象和领域数据。
- `slot_memory` 不是账户、订单、换汇申请等业务主数据，它只是 Router 为了补槽、续跑、跨节点传递而保留的运行态输入。

---

## 6. 核心数据结构设计

## 6.1 数据库中的核心对象：`IntentRoutingSpec`

这里不再把它定义成重型业务 `IntentSpec`，而是明确它是：

**Router 用来理解、编排、调度的元数据。**

建议字段如下：

```json
{
  "spec_version": "v2.1",
  "intent_code": "query_account_balance",
  "status": "active",
  "display_name": "查询账户余额",
  "recognition": {
    "description": "查询账户余额，需要卡号和手机号后四位",
    "examples": ["帮我查一下余额", "看看工资卡里还有多少钱"],
    "dispatch_priority": 100,
    "primary_threshold": 0.7,
    "candidate_threshold": 0.5
  },
  "slot_schema": [
    {
      "slot_key": "card_number",
      "value_type": "account_number",
      "required": true,
      "allow_from_history": true
    },
    {
      "slot_key": "phone_last_four",
      "value_type": "phone_last4",
      "required": true,
      "allow_from_history": true
    }
  ],
  "graph": {
    "intent_scope_rule": "单次查询余额是一个 intent",
    "confirm_policy": "auto",
    "max_nodes_per_message": 4,
    "result_schema": {
      "balance": {
        "value_type": "number",
        "description": "查询到的账户余额"
      }
    }
  },
  "dispatch": {
    "run_url": "http://intent-account-balance-agent/run",
    "cancel_url": "http://intent-account-balance-agent/cancel",
    "timeout_seconds": 30
  },
  "ui": {
    "recommendation_enabled": true
  }
}
```

### 6.1.1 这份 spec 不是什么

它不是：

- agent 的完整业务 schema
- 下游系统的数据模型
- 账户领域模型
- 换汇领域模型

它只是 Router 需要知道的最小路由契约。

### 6.1.2 `graph.result_schema` 的作用

这是替代 `_CONTEXT_KEY_ALIASES`、`_DEFAULT_INTENT_OUTPUT_KEYS` 的关键。

它表达的是：

- 这个 intent 在 `ishandover=true` 后，会向 Router 交回怎样的 `result`
- graph 条件判断只允许引用这些已注册的结果字段

例如：

- `query_account_balance` -> `result.balance`
- `query_credit_card_repayment` -> `result.due_amount`, `result.minimum_due`
- `exchange_forex` -> `result.exchanged_amount`, `result.business_status`

这样 Router 不需要再去猜：

- `available_balance`
- `remaining_balance`
- `left_balance`

因为 Router 只认：

- `result.balance`

如果 agent 内部字段不是这个名字，那是 agent 自己或其 adapter 的事，不该让 graph runtime 去兜底。

## 6.2 Router 内存中的对象：`IntentCatalogEntry`

Router 启动或热刷新时，从数据库读取 `IntentRoutingSpec`，转换成内存对象：

```python
class IntentCatalogEntry(BaseModel):
    intent_code: str
    recognition: RecognitionProfile
    slot_schema: list[SlotDefinition]
    graph_profile: GraphProfile
    dispatch_profile: DispatchProfile
    ui_profile: UIProfile | None = None
```

注意：

- 这不是共享业务实现对象
- 只是 Router 本地缓存的只读路由元数据

## 6.3 Planner 输出对象：`PlannedNodeDraft`

大模型规划出的只是“草稿节点”，它不具备执行能力。

```python
class PlannedNodeDraft(BaseModel):
    intent_code: str
    title: str
    source_fragment: str | None
    slot_memory: dict[str, Any]
```

## 6.4 Router 中的轻量节点工厂：`IntentNodeFactory`

这里的 Factory 必须是轻量的，不负责业务建模，只负责节点化。

输入：

- `IntentCatalogEntry`
- `PlannedNodeDraft`

输出：

- `ExecutableGraphNode`

```python
class ExecutableGraphNode(BaseModel):
    node_id: str
    intent_code: str
    title: str
    slot_schema: list[SlotDefinition]
    dispatch_profile: DispatchProfile
    result_schema: dict[str, ResultFieldDefinition]
    slot_memory: dict[str, Any]
    result: dict[str, Any] = Field(default_factory=dict)
```

这里要注意：

- `ExecutableGraphNode` 不包含业务对象
- 只包含运行 graph 所需的最小节点元数据

## 6.4.1 Slot Binding 设计

这里需要明确一个关键点：

**Router 不是只要拿到 `slot_memory` 就够了，还要尽量知道“这个值为什么会落到这个槽位里”。**

否则后续会持续出现这类问题：

- 条件阈值金额被错绑到执行金额槽位
- 推荐默认值、用户修改值、历史复用值互相污染
- 同一句话里两个金额、两个姓名、两个账号时，对号入座不稳定

因此，注册阶段和运行阶段都要把槽位从“普通字段”升级成“带语义、作用域、来源和置信度的绑定对象”。

### 注册阶段：`IntentSlotDefinition` 不能只定义字段名

每个 slot 至少应带这些信息：

- `slot_key`
- `value_type`
- `semantic_definition`
- `bind_scope`
- `examples`
- `counter_examples`
- `allow_from_history`
- `allow_from_recommendation`
- `confirmation_policy`

建议结构：

```python
class IntentSlotDefinition(BaseModel):
    slot_key: str
    value_type: str
    semantic_definition: str = ""
    bind_scope: Literal["node_input", "condition_operand", "shared_prefill"] = "node_input"
    examples: list[str] = Field(default_factory=list)
    counter_examples: list[str] = Field(default_factory=list)
    allow_from_history: bool = False
    allow_from_recommendation: bool = True
    confirmation_policy: Literal["never", "when_ambiguous", "always"] = "when_ambiguous"
```

作用：

- `semantic_definition` 告诉 LLM 这个槽位到底表示什么
- `counter_examples` 告诉 LLM 哪些看起来相似的表达不要塞进这个槽位
- `bind_scope` 强制区分：
  - 节点执行输入
  - 条件表达式操作数
  - 推荐/共享上下文预填

### 运行阶段：节点里要保存 `slot_bindings`

除了 `slot_memory`，节点还应保存绑定细节：

```python
class SlotBindingState(BaseModel):
    slot_key: str
    value: Any
    source: Literal["user_message", "history", "recommendation", "agent", "runtime_prefill"]
    source_text: str | None = None
    confidence: float | None = None
```

节点示意：

```python
class ExecutableGraphNode(BaseModel):
    intent_code: str
    slot_memory: dict[str, Any]
    slot_bindings: list[SlotBindingState] = Field(default_factory=list)
```

这样 Router 后续能知道：

- 这个值是用户本轮说的，还是推荐默认值
- 这个值是历史复用的，还是 agent 多轮补回来的
- 这个值到底对应了哪段原文

### `slot_memory` 和 `slot_bindings` 的边界

- `slot_memory` 是可执行输入
- `slot_bindings` 是解释性元数据

也就是说：

- agent 真正执行时仍然主要消费 `slot_memory`
- Router 和前端用 `slot_bindings` 做确认、审计、调试和冲突处理

### 槽位来源优先级

运行时建议固定以下优先级：

1. 用户本轮明确修改
2. 用户本轮明确表达
3. agent 当前轮补回
4. 已确认的推荐默认值
5. 已确认的历史复用值

注意：

- 推荐默认值不是历史
- 历史复用不是用户当前输入
- 条件阈值不是节点执行槽位

### 最重要的提示词约束

在 unified builder / planner prompt 中，必须明确写死这些约束：

- 条件阈值金额只能进入 `edge.condition.right_value`
- 节点执行金额只能进入对应节点的 `slot_memory.amount`
- 如果一句话里出现多个相同 `value_type` 的值，必须按 `slot_schema.semantic_definition` 和 `counter_examples` 对号入座
- 当 LLM 能判断绑定关系时，应输出 `slot_bindings`
- 判断不稳时宁可留空并触发 agent 多轮补槽，也不要猜

## 6.5 Router -> Agent 请求协议：`AgentInvocationRequest`

Router 发给 agent 的东西应该尽量少，核心就是当前输入与槽位。

```python
class AgentInvocationRequest(BaseModel):
    session_id: str
    task_id: str
    node_id: str
    intent_code: str
    input: str
    slot_memory: dict[str, Any]
```

如果需要扩展，可以再加只读上下文：

- `recent_messages`
- `long_term_memory`

但这些是辅助信息，不改变核心边界。

### 6.5.1 Router 对 agent 的请求边界

Router 发给 agent 的，本质上只有两类东西：

- 当前用户输入
- 当前节点可用的槽位输入

Router 不应该在这里替 agent 组装复杂业务对象，例如：

- account domain object
- transfer order object
- forex order object

如果某个 agent 需要这些对象，应由 agent 自己去读取、组装和管理。

## 6.6 Agent -> Router 返回协议：`AgentInvocationResponse`

每个 agent 返回的应该是统一 envelope，不是统一业务字段。

```python
class AgentInvocationResponse(BaseModel):
    ishandover: bool
    message: str
    slot_updates: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
```

这里最关键的是：

- `slot_updates` 是当前意图补回来的槽位
- `result` 是这个意图自己的输出结果

不是：

- `condition_outputs`
- `graph_outputs`

Router 后续 graph 条件判断，直接读节点 `result`。

### 6.6.1 Router 对 `result` 的消费边界

Router 可以做的事情只有：

- 保存节点 `result` 作为 graph runtime state
- 基于已注册的 `result_schema` 校验字段可用性
- 在条件边评估时读取上游节点 `result`

Router 不应该做的事情是：

- 解释 `result` 背后的业务表结构
- 依赖某个 agent 私有 payload 结构
- 根据经验猜测一个字段是不是“余额”“剩余额度”“可用金额”

### 6.6.2 `ishandover` 的语义

`ishandover=true` 表示：

- 当前 agent 本轮处理已经把控制权交回 Router
- Router 此时可以读取：
  - `slot_updates`
  - `result`

并推动 graph 前进。

`ishandover=false` 表示：

- 当前节点仍然处于 agent 多轮交互中
- Router 不应推进后继节点

### 6.6.3 节点“最终完成”与 `ishandover` 不是一回事

后续如果存在“前端执行完成后再回调确认”的场景，可以在 Router 侧增加：

- `WAITING_EXECUTION_ACK`

并提供回调接口，例如：

```python
POST /api/router/v2/nodes/{node_id}/ack
```

也就是说：

- `ishandover` 解决“当前 agent 阶段是否交回 Router”
- callback flag 解决“节点最终是否被外部确认完成”

---

## 7. Graph 条件判断应如何工作

这是 `_CONTEXT_KEY_ALIASES` 的真正替代方案。

## 7.1 条件依赖的不是“业务 payload”，而是“上游节点 result”

例如条件：

- `balance > 20000`

真实含义是：

- 等待上游余额查询节点 `ishandover=true`
- 读取该节点 `result.balance`
- 判断是否大于 `20000`

也就是说：

- 条件依赖的是“某个上游 intent 已经产出的标准化结果”
- 不是依赖 Planner 临时猜出来的上下文字段
- 也不是依赖 Router 从历史 payload 里二次推理出来的字段

不是：

- Router 去 agent payload 里猜 `available_balance`
- 或 `remaining_balance`
- 或 `left_balance`

## 7.2 为什么 `_CONTEXT_KEY_ALIASES` 会出现

因为当前系统里没有把这件事前置建模：

- graph 条件写的字段
- agent 返回给 Router 的结果字段
- intent 注册时声明的输出字段

三者没有统一。

所以运行时才被迫写：

- `_CONTEXT_KEY_ALIASES`
- `_DEFAULT_INTENT_OUTPUT_KEYS`

## 7.3 正确解法

### 注册阶段

每个 intent 必须声明自己的 `graph.result_schema`

例如：

```json
"graph": {
  "result_schema": {
    "balance": { "value_type": "number" }
  }
}
```

### agent 阶段

agent 返回的 `result` 必须满足该 schema

例如：

```json
{
  "ishandover": true,
  "message": "查询成功",
  "slot_updates": {},
  "result": {
    "balance": 8000
  }
}
```

### graph runtime 阶段

graph 条件只允许引用注册过的结果字段：

- `result.balance`
- `result.due_amount`
- `result.exchanged_amount`

这样就不再需要 runtime alias。

## 7.4 对 legacy agent 的兼容

如果当前某些 agent 还不能立刻按统一 envelope 返回，那么兼容逻辑只能放在：

- agent 自己的 adapter
- 或 router 的 dispatch adapter 边界层

不能继续往 graph runtime 里塞业务 alias 补丁。

也就是说：

- 兼容可以有
- 但兼容层必须在 I/O 边界
- 不能污染 graph 核心

---

## 8. 轻量 `IntentNodeFactory` 的职责

这里明确收缩职责，不再做重型编译器。

`IntentNodeFactory` 只负责：

1. 根据 `intent_code` 从 Router catalog 找到 `IntentCatalogEntry`
2. 把 `PlannedNodeDraft` 节点化为 `ExecutableGraphNode`
3. 为节点绑定：
   - `slot_schema`
   - `dispatch_profile`
   - `result_schema`
4. 初始化空的 `result`

它不负责：

- 业务数据加载
- 领域对象构造
- agent payload 解释
- graph condition 评估

一句话：

**它只是把“识别出来的意图草稿”变成“可被 graph 调度的轻量节点”。**

---

## 9. GraphRouterOrchestrator 的拆分蓝图

当前问题不只是类太大，而是职责错位。

建议拆成以下模块。

## 9.1 `ConversationApplicationService`

职责：

- 接收用户消息和动作命令
- 协调 planning / execution / session / presentation

它是薄应用服务。

## 9.2 `IntentCatalogLoader`

职责：

- 从数据库加载 `IntentRoutingSpec`
- 刷新 Router 本地 catalog cache

Router 知道有哪些 intent，只能通过它。

## 9.3 `GraphPlanningService`

职责：

- 调 recognizer
- 调 planner / graph_builder
- 产出 `PlannedGraphDraft`

## 9.4 `IntentNodeFactory`

职责：

- 把 graph draft 中的每个 `PlannedNodeDraft` 节点化

## 9.5 `ExecutionGraphEngine`

职责：

- 纯图状态推进
- 判断哪个节点 ready
- 判断条件是否成立
- 计算 graph status

这部分必须是纯执行引擎，不涉及：

- HTTP
- prompt
- session persistence

## 9.6 `NodeDispatchService`

职责：

- 根据 `dispatch_profile.run_url` 调 agent
- 接收 `AgentInvocationResponse`
- 更新：
  - `slot_memory`
  - `result`
  - node 状态

## 9.7 `RouterEventPublisher`

职责：

- 发布 session/node/graph 事件
- 构造前端 snapshot

---

## 9.8 当前 fix 分支已落地的两刀

这不是目标态的全部拆分，但已经形成了明确方向：

- 第一刀已落地：`ExecutionGraphEngine`
  - 当前实现为 [v2_graph_runtime.py](/root/intent-router/backend/src/router_core/v2_graph_runtime.py)
  - 已承接 graph/node ready-block-skip 判定、条件匹配、graph status 归约、waiting/ready node 选择
- 第二刀已落地：`GraphSnapshotPresenter + GraphEventPublisher`
  - 当前实现为 [v2_presentation.py](/root/intent-router/backend/src/router_core/v2_presentation.py)
  - 已承接 graph/node/session payload 组装、graph terminal message、recognition / graph_builder / node runtime 事件发布
- 当前的 [v2_orchestrator.py](/root/intent-router/backend/src/router_core/v2_orchestrator.py) 仍然偏大，但已经从“状态推进 + 展示拼装 + 事件格式化 + API surface 假设”收缩为：
  - 会话应用服务
  - planning / execution 协调
  - pending graph / waiting node 转向
  - history prefill 与 recommendation 路由

这意味着后续继续拆分时，应优先落在：

- `GraphPlanningService`
- `NodeDispatchService`
- `SessionRepository / SessionStore adapter`

而不是再把展示逻辑塞回 orchestrator。

---

## 10. 无状态要求下的状态存储重构

如果服务必须无状态，那么当前 in-memory session store 必须被替换。

## 10.1 Router 需要外置的状态

Router 运行时需要外置存储：

- session
- graph
- node
- active task
- pending graph
- waiting ack 状态

推荐：

- Redis：做热态运行状态
- Postgres：做持久化审计和恢复

## 10.2 Admin 持有的状态

Admin 持有：

- `IntentRoutingSpec`
- 发布版本
- 激活状态

## 10.3 Agent 持有的状态

每个 agent 自己持有：

- 自己的领域业务数据
- 执行记录
- 风控/订单/账务相关信息

Router 不接管。

---

## 11. 如何消除当前硬编码

## 11.1 `_CONTEXT_KEY_ALIASES`

### 根因

- graph 条件字段没有和意图输出字段在注册阶段对齐
- agent 返回结果字段没有统一进入 `result`

### 改造

- 注册 `graph.result_schema`
- agent 统一返回 `result`
- graph 条件只引用已注册的 result 字段

### 改造后

删除：

- `_CONTEXT_KEY_ALIASES`
- `_DEFAULT_INTENT_OUTPUT_KEYS`

## 11.2 `if context_key == "balance"`

### 根因

- runtime 在猜“哪个 intent 是某个条件字段的 provider”

### 改造

- 在 intent 路由元数据中明确声明 `result_schema`
- planner 只能挂到合法 provider 节点
- 如果需要隐含前置节点，provider title 从该 intent 的 metadata 中读取

### 改造后

runtime 不再写金融特定 if 分支。

## 11.3 `_cancel_url()`

### 根因

- dispatch 契约没有显式 `cancel_url`

### 改造

- `dispatch.cancel_url` 明确注册
- `NodeDispatchService` 只按显式字段调用

## 11.4 recommendation 上下文字符串前缀

### 根因

- PlannerContext 没有正式对象模型

### 改造

- recommendation / proactive selection 全部进入结构化 `PlannerContext`
- prompt serializer 只负责序列化

---

## 12. 迁移步骤

## Phase 1：重构目录边界，不改行为

目标：

- 从 `backend/src` 平铺结构迁到 `services/ + contracts/`
- 保持现有 API 行为不变

产出：

- `admin-service`
- `router-service`
- `agents/*`
- `contracts/*`

## Phase 2：引入 `IntentRoutingSpec`

目标：

- admin 注册模型升级
- 数据库存储 Router 可消费的最小路由元数据

关键新增字段：

- `graph.result_schema`
- `dispatch.run_url`
- `dispatch.cancel_url`

## Phase 3：Router 通过数据库加载 catalog

目标：

- Router 不再从代码知道 intent
- 启动和热刷新都只通过数据库读取

产出：

- `IntentCatalogLoader`
- `Router catalog cache`

## Phase 4：落轻量 `IntentNodeFactory`

目标：

- planner 输出从 draft node 变成 executable node
- 不引入重型业务编译器

## Phase 5：统一 agent envelope

目标：

- Router -> Agent 统一输入协议
- Agent -> Router 统一返回：
  - `ishandover`
  - `message`
  - `slot_updates`
  - `result`

## Phase 6：抽离 ExecutionGraphEngine

目标：

- 将纯图执行逻辑从 orchestrator 中剥离

首批迁移函数：

- `_refresh_node_states`
- `_condition_matches_from_condition`
- `_graph_status`
- `_next_ready_node`

当前状态：

- 已完成，并已落地为 `v2_graph_runtime.py`
- 同时已追加第二刀 `v2_presentation.py`

## Phase 6.5：收敛运行面为单一 V2 API Surface

目标：

- `/api/router` 成为 canonical V2 入口
- `/api/router/v2` 仅保留兼容别名
- `/chat` 直接复用 V2 前端
- Router runtime 只构造一套 `GraphRouterOrchestrator`

当前状态：

- 已完成当前分支内的服务入口收敛
- `router_api/routes/sessions.py` 已成为当前 canonical graph route，实现同一套图运行时协议
- `router_api/app.py` 与平台根 `app.py` 将该路由同时挂载到 `/api/router/*` 与 `/api/router/v2/*`
- `router_api/dependencies.py` 已收敛为单一 orchestrator / broker runtime
- legacy V1 API 测试已显式退役，不再作为主运行面

## Phase 7：外置 Router 状态

目标：

- 去掉内存 session store
- 让 Router 成为真正无状态服务

## Phase 8：删除运行时补丁

在前述步骤完成后，删除：

- `_CONTEXT_KEY_ALIASES`
- `_DEFAULT_INTENT_OUTPUT_KEYS`
- `_cancel_url()` 猜测逻辑
- recommendation 字符串前缀协议

---

## 13. 验收标准

改造完成后，至少满足：

1. 新增一个 intent，不改 Router 核心执行代码。
2. 新增一个条件依赖 intent，不改 graph runtime，只改数据库中的 routing spec。
3. agent 内部业务字段怎么命名，Router 不关心；Router 只消费 `result`。
4. graph 条件只依赖上游节点 `result` 中已注册的字段。
5. Router 与 agent 的边界清晰为：
   - Router 传输入和槽位
   - Agent 回 `ishandover + slot_updates + result`
6. Router 不再依赖任何其他服务的内部代码。
7. Router 状态外置，成为无状态服务。
8. `GraphRouterOrchestrator` 被拆成薄应用服务，不再承载纯执行引擎和展示逻辑。

---

## 14. 最终判断

这次 fix 分支的正确方向，不是继续给 Router 塞补丁，而是：

1. 先把服务边界做对
2. 再把数据库中的路由元数据做对
3. 再把 Router-Agent 交互 envelope 做对
4. 最后拆 Router 内部代码

一句话总结：

**Agent 负责把一个意图做完整，Router 负责把多个意图组织起来。**

只要这个边界不再被破坏，后续即使新增大量 intent 和 agent 服务，Router 核心代码也不会继续失控膨胀。
