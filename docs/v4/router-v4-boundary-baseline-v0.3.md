# Router V4 三层边界基线 v0.3

## 一句话结论

系统只有三层：

```text
用户
  -> 掌银助手层
  -> 意图识别框架 / Router V4
  -> 场景执行 Agent
  -> 意图识别框架 / Router V4
  -> 掌银助手层
  -> 用户
```

不要把这三层混在一起：

- 助手不是 Router。助手负责用户入口、上下文、展示和最终话术。
- Router 不是业务 Agent。Router 负责识别、计划、派发、追踪和 handover。
- Agent 不是最终话术生成器。Agent 负责业务执行并返回结构化结果。

## 三个角色

### 1. 掌银助手层

包含掌银助手前端和助手后端。

助手层负责：

- 用户入口：接收输入、点击、卡片事件、页面上下文。
- 上游上下文：身份、渠道、权限、用户画像、页面状态、可用推荐卡片。
- 助手会话：用户可见会话、展示历史、SSE 连接管理。
- 主动推送：生成和展示推荐卡片，并在后续请求中携带 `push_context`。
- 调用 Router：把用户表达和上下文发给意图识别框架。
- 消费结构化状态：展示 task/graph 进度、Agent output。
- 最终结果：任务结束后由助手基于结构化结果生成用户可见表达。

助手层不负责：

- 场景业务执行。
- 业务风控、限额、交易确认。
- 直接调用转账、基金申购等业务 API。
- 替 Router 做 Agent 派发。

### 2. 意图识别框架 / Router V4

这是中间层服务，当前落地为 `router-v4-service`。

Router V4 负责：

- 加载 scene routing spec 和 agent registry。
- 根据用户表达、`push_context`、routing spec、Skill metadata 做场景识别。
- 生成单意图 task 或多意图 graph/tasks。
- 派发任务给场景执行 Agent。
- 维护 Router 侧 session/task/graph/transcript/event 状态。
- 处理 Agent 固定 handover 协议。
- 向助手返回结构化状态、事件、task id、stream url、Agent output。

Router V4 不负责：

- 用户可见最终话术。
- 业务确认、风控、限额、幂等。
- 业务 API 调用。
- 执行 Agent 内部 workflow。
- 在 runtime 里写正则、关键词词表、硬编码场景判断。
- 在 runtime 里从用户文本启发式提槽。

Router V4 内部组件边界：

```text
RouterV4Runtime
  只编排：session -> recognizer -> plan/task -> agent dispatch -> state/event

LLMIntentRecognizer
  只识别：输入 routing spec + push_context + Skill metadata，输出 selected scene(s) + routing slot hints

RoutingSlotProjector
  只投影：把 recognizer 返回的 slots 按 selected scene 的 routing_slots 白名单过滤

SpecRegistry
  只加载：scene routing spec + agent registry

AgentDispatchClient
  只派发：调用 Agent endpoint，不调用业务 API
```

### 3. 场景执行 Agent

由场景团队提供，和业务域绑定。

执行 Agent 负责：

- 判断任务是否属于自己。
- 场景内业务理解和业务提槽。
- 业务 workflow。
- 安全、风控、限额、幂等、交易确认。
- 调用业务 API。
- 返回结构化状态和结构化结果。
- 发现误派时返回固定 handover 协议。

执行 Agent 不负责：

- 选择其他业务 Agent。
- 管理助手会话和最终展示话术。
- 修改 Router 的路由状态。

## 报文流转

### 正常单意图

```text
1. 用户 -> 助手
   message = "我要转账"

2. 助手 -> Router V4
   session_id + message + user_profile + page_context + push_context?

3. Router V4 -> LLMIntentRecognizer
   scene index + routing spec + Skill metadata + message + push_context

4. LLMIntentRecognizer -> Router V4
   selected_scene_id = transfer
   routing_slots = {}

5. Router V4 -> transfer-agent
   AgentTask(scene_id, raw_message, routing_slots, context refs, skill)

6. transfer-agent -> Router V4
   task event / structured output

7. Router V4 -> 助手
   structured event / agent_output

8. 助手 -> 用户
   final response
```

### 主动推送

```text
1. 助手推送推荐卡片
   card + push_context.intents

2. 用户表达
   "就按这个办" / 点击卡片 / "两个都看一下"

3. 助手 -> Router V4
   source = assistant_push
   push_context.intents = ranked scene list

4. Router V4 -> LLMIntentRecognizer
   只能在 push_context 限定的候选中判断，或返回 no selected scene

5. Router V4
   selected 1 个 scene -> dispatch task
   selected 多个 scene -> planned graph/tasks
   selected 为空 -> no_action
```

这里取消的是“助手先问用户是否接受推荐”的前置确认。

业务安全确认仍属于执行 Agent 或业务域，不属于 Router。

## 提槽边界

提槽分两种：

| 类型 | 所属层 | 说明 |
| --- | --- | --- |
| 路由槽位 hints | Router V4 / recognizer | 只为了派发和上下文传递。必须由 scene routing spec 声明，runtime 只做白名单投影。 |
| 业务槽位 | 执行 Agent | 用于业务执行、确认、风控、限额、API 调用。Agent 拥有补槽、校验、纠错和确认权。 |

结论：

- Router runtime 不读用户文本做提槽。
- LLM recognizer 可以基于 spec 返回 routing slot hints。
- Agent 必须重新校验关键业务槽位，不能把 routing slot hints 当成最终业务事实。

## workflow / skill / API 使用规范

| 能力 | 助手层 | Router V4 | 执行 Agent |
| --- | --- | --- | --- |
| workflow | 用户交互旅程、展示和 SSE 消费 | 路由生命周期：recognize/plan/dispatch/track/handover | 业务流程：提槽/校验/确认/风控/执行 |
| skill | 不执行场景 skill | 使用 routing spec + Skill metadata 做识别上下文 | 拥有并执行 scene Skill |
| API | 调 Router、消费状态 | 只调基础设施 API：spec registry、state store、Agent dispatch、Agent callback | 调业务 API：转账、基金、风控、限额等 |

## handover 边界

Agent 发现误派时，只能返回：

```json
{
  "ishandover": true,
  "output": {
    "data": []
  }
}
```

Router 只在两个条件同时成立时改派 fallback agent。

不支持 `isHandover`，也不做字段兼容。

## 多轮上下文边界

Router V4 持久化的是路由态，不是业务态：

- active scene
- pending scene
- task id
- graph id
- target agent
- routing slot hints
- transcript/event
- handover records
- agent output

业务态留在执行 Agent：

- 业务步骤
- 业务槽位
- 风控结果
- 限额判断
- 待确认交易
- 幂等键

已派发任务的后续用户消息，Router 默认转发给 active Agent，不重新从头识别。

## 当前实现必须对齐的点

- `RouterV4Runtime` 不能恢复规则 matcher。
- `RouterV4Runtime` 不能恢复本地 slot extractor。
- 主动推送不能恢复硬编码接受/拒绝词表。
- `LLMIntentRecognizer` 是 Router V4 的内部识别组件，不是助手，也不是 Agent。
- `assistant_push_policy` 是结构化上下文，不是业务确认逻辑。
- 最终自然语言结果必须由助手生成，Router 只返回结构化状态和数据。
