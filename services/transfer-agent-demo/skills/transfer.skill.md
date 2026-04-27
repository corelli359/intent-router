+++
required_slots = ["recipient", "amount"]
confirmation_step = "waiting_confirmation"
submit_tool = "transfer.submit"
+++

# Skill：transfer

## 元数据

- skill_id: transfer
- version: 0.1.0
- owner_agent: transfer-agent
- task_type: transfer

## 执行边界

本 Skill 只处理转账办理。Router 可以传递 `skill_ref` 和原始用户表达，但 Intent ReAct 第一步不得加载本 Skill 正文，也不得据此提取收款人、金额等业务字段。

本 Skill 负责收款人提取、金额提取、缺失字段提示、用户确认、风控检查、限额检查、转账 API 调用和结构化输出。

## 输入

- router_session_id
- task_id
- intent_id
- scene_id
- raw_message
- source
- push_context
- business_context
- context_refs

## 内部状态

- recipient：由 Skill ReAct 决策收集
- amount：由 Skill ReAct 决策收集
- currency：默认 CNY
- amount_source：user_message | business_memory
- skill_step：start | collecting_transfer_fields | waiting_confirmation | completed | cancelled | handover

## 执行步骤

1. 读取 Router 派发的任务快照，确认 `scene_id=transfer`。
2. 如果任务不属于转账，返回 `ishandover=true` 且 `output.data=[]`。
3. 将本 Skill、Router task snapshot、`business_context`、当前 task memory 和用户本轮表达交给 LLM 做结构化决策。
4. LLM 在一次理解中输出 `slots_patch` 和 `action`。
5. Runtime 代码只校验 LLM 结构化结果、更新 task memory、推进状态机，不使用本地规则提槽。
6. 如果收款人或金额缺失，一次性询问所有缺失字段。
7. 如果收款人和金额都完整，向用户发起确认。
8. 用户确认后，输出 `tool_call.name=transfer.submit`，参数中携带风控、限额和转账 API 所需字段。
9. Runtime 调用 Skill 声明的 tool adapter，产出结构化执行结果。

## 槽位策略

- recipient：收款人姓名、别名或可解析的收款对象。
- amount：用户明确表达的转账金额。
- 用户只说“我要转账”时，提示：“可以，请告诉我转给谁、转账金额是多少？”
- 用户说“我要转账300给小红”时，应一次性得到收款人和金额，并进入确认。
- 用户说“给李四转一样的钱”“向李四也转这么多”时，`李四` 是本轮明确收款人，金额从 `business_context.last_completed_for_same_scene.data.amount` 继承。
- 不采用固定问答表单；多轮表达按自由对话累积字段。

## 上下文引用策略

- 当用户表达“一样的钱”“同样金额”“也转这么多”“照上次金额”等指代金额时，Skill ReAct 应读取任务中的 `business_context.last_completed_for_same_scene.data.amount`。
- 继承上一笔金额时，必须把 `amount_source` 标记为 `business_memory`。
- 继承金额后仍然必须进入确认，不能直接执行转账。
- 如果 `business_context` 中没有可用金额，按缺失金额处理并追问。

## LLM 决策输出

LLM 必须只返回结构化 JSON：

```json
{
  "task_supported": true,
  "action": "ask_missing | ask_confirmation | submit | cancel | handover",
  "required_slots_complete": true,
  "confirmation_observed": false,
  "slots_patch": {
    "recipient": "string|null",
    "amount": "string|null",
    "currency": "string|null",
    "amount_source": "user_message | business_memory | null"
  },
  "tool_call": {
    "name": "transfer.submit|null",
    "arguments": {
      "result_type": "transfer_result",
      "recipient": "string|null",
      "amount": "string|null",
      "currency": "CNY",
      "risk_status": "passed|null",
      "limit_status": "passed|null"
    }
  },
  "assistant_message": "string",
  "reason": "string"
}
```

Runtime 只接受上述结构化决策，并负责状态校验、确认门禁和 tool 执行门禁。提交转账时必须携带 `tool_call.name=transfer.submit`，并在 `arguments.result_type` 中写 `transfer_result`，这样助手可以按结构化结果生成最终话术。

当 `recipient` 和 `amount` 都已经从用户表达、task memory 或 `business_context` 中得到时，`required_slots_complete` 必须为 `true`，`action` 必须是 `ask_confirmation`，不能再写 `ask_missing`。只有收款人或金额仍缺失时，`required_slots_complete=false` 且 `action=ask_missing`。

当 task memory 中 `skill_step=waiting_confirmation` 且用户本轮表达“确认”“是的”“继续”“办理”等明确同意时，`confirmation_observed` 必须为 `true`，`action` 必须为 `submit`，并输出 `tool_call.name=transfer.submit`。不能再次返回 `ask_confirmation`。

## 误派处理

当任务不属于本 Skill 时，必须返回：

```json
{
  "ishandover": true,
  "output": {"data": []}
}
```

## 输出契约

执行中：

```json
{
  "status": "running",
  "assistant_message": "string"
}
```

执行完成：

```json
{
  "status": "completed",
  "output": {
    "data": [{"type": "transfer_result", "status": "success"}],
    "risk": {"status": "passed"},
    "limit": {"status": "passed"},
    "business_api": {"name": "transfer.submit", "status": "success"}
  }
}
```
