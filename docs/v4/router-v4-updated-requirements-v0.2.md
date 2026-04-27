# Router V4 更新需求说明 v0.2

## 目标

在 `router-v4-service` 新实现上，补充四类核心需求：

1. 任务结束后的最终用户结果由助手生成。
2. Agent 发现误派时，通过固定 `ishandover` 协议触发 Router 改派兜底 Agent。
3. 助手主动推送推荐卡片后，不再先问用户是否接受推荐；Router 直接结合用户表达和推送意图清单判断、规划、执行。
4. 多意图场景考虑 SSE 长连接过长的问题，支持任务级拆流。

本文是 v0.2 需求基线，后续代码实现、测试和接口文档以此为准。

## 边界原则

### 助手层

助手层负责：

- 接收前端/掌银助手上下文。
- 推送推荐卡片。
- 调用 Router。
- 消费 Router/Agent 的结构化状态与结果。
- 最终组织用户可见结果。

任务结束后，不再由助手和 Agent 联合生成最终表达，也不由 Agent 直接决定最终用户话术。Agent 返回结构化结果，助手根据结构化结果生成最终展示。

### Router 层

Router 负责：

- 根据 routing spec 做场景识别。
- 根据主动推送意图清单约束候选场景。
- 生成单意图或多意图执行计划。
- 派发任务给执行 Agent。
- 处理 Agent handover 信号并改派兜底 Agent。
- 维护 Router 侧 session、task、graph、event 状态。
- 向助手返回结构化状态、事件和 Agent output。

Router 不负责：

- 业务确认。
- 风控。
- 限额。
- 幂等。
- 业务 API 调用。
- 最终用户话术生成。

### 执行 Agent 层

执行 Agent 负责：

- 判断任务是否属于自己。
- 执行业务流程。
- 返回结构化执行结果。
- 对业务安全、限额、交易确认、业务 API 负责。

如果 Agent 判断任务不属于自己，必须使用固定 handover 协议通知 Router。

### 兜底 Agent

当前先只考虑一个统一兜底 Agent：

```text
fallback-agent
```

Router 在误派场景下改派到该 Agent。后续如果业务域扩展为多个兜底 Agent，再由 agent registry 或 fallback policy 扩展。

## 需求 R1：任务最终结果由助手生成

### 新逻辑

Agent 只返回结构化结果：

```json
{
  "status": "completed",
  "output": {
    "data": [
      {
        "type": "balance",
        "currency": "CNY",
        "amount": "1000.00"
      }
    ]
  }
}
```

Router 记录和转发：

```json
{
  "task_id": "task_001",
  "status": "completed",
  "scene_id": "balance_query",
  "target_agent": "balance-agent",
  "agent_output": {
    "data": [
      {
        "type": "balance",
        "currency": "CNY",
        "amount": "1000.00"
      }
    ]
  }
}
```

助手根据 Router 返回的结构化结果生成最终用户表达。

### 验收标准

- Router 不拼接最终自然语言结果。
- Agent 不直接控制最终展示话术。
- Router output 中保留 Agent 原始结构化结果。
- 助手可以基于 `agent_output.data` 独立生成用户结果。

## 需求 R2：误派 Agent 的 handover 处理

### 固定协议

Agent 判断任务不属于自己时，返回：

```json
{
  "ishandover": true,
  "output": {
    "data": []
  }
}
```

字段名固定为 `ishandover`。

不支持、不兼容、不兜底识别以下写法：

```json
{
  "isHandover": true
}
```

### Router 判断条件

Router 只有在两个条件同时满足时触发 handover：

- `ishandover == true`
- `output.data == []`

如果只满足其中一个，Router 不应当直接改派兜底 Agent，而应记录为异常 Agent output，交由助手或监控处理。

### Router 处理动作

触发 handover 后，Router 执行：

1. 标记当前任务为 `handover_requested`。
2. 记录原始 scene、原 target agent、原 task id、用户原文、Agent 返回。
3. 构造 fallback task。
4. 派发给 `fallback-agent`。
5. 将 fallback 任务与原任务建立关联。
6. 返回助手结构化事件，说明发生兜底改派。

### 防循环要求

同一个用户输入或同一个 Router task 最多触发一次 handover。

如果 `fallback-agent` 仍返回：

```json
{
  "ishandover": true,
  "output": {
    "data": []
  }
}
```

Router 不再继续改派，直接标记为 `fallback_failed` 或 `handover_exhausted`，交由助手生成兜底回复。

### fallback task 需要携带的上下文

```json
{
  "router_session_id": "sess_001",
  "original_task_id": "task_001",
  "original_scene_id": "fund_query",
  "original_agent": "fund-agent",
  "raw_message": "我想查一下这个推荐",
  "handover_reason": {
    "ishandover": true,
    "output": {
      "data": []
    }
  },
  "routing_slots": {},
  "push_context": {},
  "scene_spec_hash": "sha256:..."
}
```

### 事件建议

```text
task.handover_requested
task.fallback_dispatched
task.fallback_completed
task.handover_exhausted
```

## 需求 R3：主动推送推荐卡片不再进行用户确认

### 旧流程

```text
助手问用户是否接受推荐
-> 用户确认或表达
-> Router 做意图识别
-> Router 派发 Agent
```

### 新流程

```text
助手直接推送推荐卡片
-> 用户点击、表达或继续输入
-> Router 基于用户表达 + 推送意图清单判断
-> Router 规划并派发 Agent
```

取消的是“是否接受推荐”的前置确认。

业务安全确认、限额确认、交易确认仍然属于执行 Agent 或业务域，不属于 Router。

### Assistant -> Router 请求建议

```json
{
  "session_id": "sess_001",
  "message": "就按这个办",
  "source": "assistant_push",
  "push_context": {
    "push_id": "push_001",
    "card_id": "card_001",
    "intents": [
      {
        "intent_code": "fund_query",
        "scene_id": "fund_query",
        "rank": 1
      },
      {
        "intent_code": "balance_query",
        "scene_id": "balance_query",
        "rank": 2
      }
    ]
  },
  "user_profile": {},
  "page_context": {}
}
```

### Router 判断边界

主动推送模式下：

- Router runtime 不内置接受、拒绝、多意图等文本词表。
- Router 候选场景必须优先受 `push_context.intents` 约束。
- LLM recognizer 基于用户表达、`push_context.intents`、routing spec 和 Skill metadata 输出 `selected_scene_id` 或 `selected_scene_ids`。
- recognizer 未选择任何场景时，Router 返回 `no_action`，不派发 Agent。
- recognizer 选择一个场景时，Router 创建单 task 并派发对应 Agent。
- recognizer 选择多个场景时，Router 创建 graph/tasks，按任务拆流。
- 用户表达完全偏离推荐清单时，是否退回普通 routing spec 识别或进入 fallback policy，由产品策略配置决定；不在 Router runtime 中写死。

### 不进入 Router 确认态

主动推送模式不应出现：

```text
是否接受推荐？
是否确认执行这个推荐？
```

Router 的职责是判断、规划和派发。是否需要业务确认，由执行 Agent 根据业务规则决定。

## 需求 R4：多意图 SSE 拆流

### 背景

多意图执行可能包含多个 Agent、多个业务步骤、多个等待点。如果所有事件都压在一个 SSE 长连接里，连接时间过长会影响助手层稳定性和用户体验。

### 拆流目标

- Router 快速返回 plan。
- 每个 task 可以独立流式消费。
- 单个慢任务不阻塞其他任务。
- SSE 可断开、可恢复。
- 助手可以按任务展示进度。

### 推荐模式

第一步，助手调用 Router 创建计划：

```text
POST /api/router/v4/message
```

Router 返回：

```json
{
  "status": "planned",
  "graph_id": "graph_001",
  "stream_mode": "split_by_task",
  "tasks": [
    {
      "task_id": "task_001",
      "scene_id": "balance_query",
      "target_agent": "balance-agent",
      "stream_url": "/api/router/v4/streams/task_001"
    },
    {
      "task_id": "task_002",
      "scene_id": "fund_query",
      "target_agent": "fund-agent",
      "stream_url": "/api/router/v4/streams/task_002"
    }
  ]
}
```

第二步，助手按任务订阅：

```text
GET /api/router/v4/streams/{task_id}
```

每个任务流独立关闭。

### SSE 事件建议

```text
plan.created
task.created
task.dispatched
task.started
task.delta
task.handover_requested
task.fallback_dispatched
task.completed
task.failed
task.cancelled
task.stream_closed
graph.completed
```

### 恢复能力

Router 应返回 task 级 `resume_token`：

```json
{
  "task_id": "task_001",
  "resume_token": "rt_001"
}
```

助手断开后可带 token 恢复：

```text
GET /api/router/v4/streams/task_001?resume_token=rt_001
```

v0.2 可以先实现 task event store 和 task snapshot，SSE resume 可以作为后续增强，但接口设计需要预留。

## 状态模型建议

### Task 状态

```text
created
planned
dispatched
running
handover_requested
fallback_dispatched
completed
failed
cancelled
handover_exhausted
```

### Graph 状态

```text
created
planned
running
partially_completed
completed
failed
cancelled
```

### Router session 需要记录

- `session_id`
- `active_graph_id`
- `active_task_ids`
- `source`
- `push_context`
- `raw_messages`
- `selected_scene_ids`
- `target_agents`
- `agent_task_ids`
- `handover_records`
- `agent_outputs`
- `assistant_result_status`

## 典型场景

### 场景 1：误派后进入 fallback-agent

```text
用户：查一下这个推荐
Router：识别为 fund_query，派发 fund-agent
fund-agent：判断不属于自己，返回 ishandover=true + output.data=[]
Router：标记 handover_requested
Router：派发 fallback-agent
fallback-agent：返回结构化兜底结果
Router：归档并返回助手
助手：生成最终用户结果
```

### 场景 2：主动推送后用户直接办理

```text
助手：推送基金推荐卡片，携带 fund_query intent
用户：就按这个办
Router：source=assistant_push，只在推送意图清单内判断
Router：选择 fund_query，派发 fund-agent
fund-agent：执行业务规则，必要时做业务确认
Router：返回结构化结果
助手：生成最终用户结果
```

### 场景 3：主动推送后多意图执行

```text
助手：推送包含余额查询和基金推荐的卡片
用户：都看一下
Router：生成 graph，包含 balance_query 和 fund_query 两个 task
Router：返回 split_by_task plan
助手：分别订阅 task_001 和 task_002
两个任务分别完成
Router：graph completed
助手：汇总生成最终结果
```

## v0.2 实现优先级

P0：

- 固定 `ishandover` 协议识别。
- 统一 fallback-agent 改派。
- 主动推送 `push_context.intents` 协议。
- Router 不生成最终用户结果。
- 补充专项测试。

P1：

- 多意图 plan/task 状态模型。
- task snapshot 查询。
- task 级 SSE 拆流基础接口。

P2：

- SSE resume token。
- fallback policy 可配置化。
- 主动推送偏离意图清单时的策略配置。
- 真实 event store / Redis / SQL 持久化。

## 验收标准

- Agent 返回 `ishandover=true` 且 `output.data=[]` 时，Router 改派 `fallback-agent`。
- Router 不识别 `isHandover`。
- fallback 不循环派发。
- 主动推送场景不出现“是否接受推荐”的 Router 确认态。
- Router 能基于 `push_context.intents` 限定候选意图。
- 多意图场景可以返回 plan，并具备任务级状态。
- 任务结束后，Router 返回结构化结果，最终用户话术由助手生成。
