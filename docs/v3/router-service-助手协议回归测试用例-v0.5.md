# router-service 助手协议回归测试用例 v0.5

更新时间：2026-04-28

## 1. 测试场景矩阵

本回归以 **SSE 流式链路为主**。非流式只作为兼容对照，不作为主要验收口径。

| ID | 场景 | 主接口 | 用户路径 | 必验状态 / SSE 帧 | 自动化现状 |
|---|---|---|---|---|---|
| S01 | 意图识别后立即推流 | `/api/v1/message` `stream=true` | `给小明转账` | 首个业务前置帧为 `intent_recognized`，`output.stage=intent_recognition` | 已覆盖 |
| S02 | 多轮提槽：首轮缺金额 | `/api/v1/message` `stream=true` | `给小明转账` | 识别帧后返回 `waiting_user_input`，保留 `payee_name=小明` | 已覆盖 |
| S03 | 多轮提槽：补金额后等待助手确认 | `/api/v1/message` `stream=true` | 同一 `sessionId` 继续发 `200` | 返回 `waiting_assistant_completion`，不是 `completed` | 已覆盖 |
| S04 | 多轮槽位覆盖 / 纠错 | `/api/v1/message` `stream=true` | `我要转账` -> `小刚` -> `小红吧` -> `200` | 最终槽位必须是 `payee_name=小红, amount=200` | 已有非流覆盖，需补流式 |
| S05 | router_only 多轮提槽 | `/api/v1/message` `stream=true`, `executionMode=router_only` | 同 S04 | 不调用 agent，最终到 `ready_for_dispatch` | 已有非流覆盖，需补流式 |
| S06 | Agent 工作流多帧透传 | `/api/v1/message` `stream=true` | `给小明转账200` | 先推校验中间帧，再推执行结果帧 | 已覆盖 |
| S07 | Agent 输出扁平化 | `/api/v1/message` `stream=true` | Agent SSE 返回包装结构 | 顶层字段稳定，`output.slot_memory` 不外泄 | 已覆盖 |
| S08 | 单任务助手确认完成 | `/api/v1/task/completion` `stream=true` | 对 `current_task` 发 `completionSignal=2` | 返回 `completed`、`assistant_final_done`、`done` | 已覆盖 |
| S09 | 多任务确认后继续执行 | `/api/v1/task/completion` `stream=true` | 任务 A 确认完成后继续任务 B | 同一个 completion SSE 中先有 A completed，再有 B 状态帧 | 已覆盖 |
| S10 | 多意图 / 待确认图 | `/api/v1/message` `stream=true` | `先查余额，再给张三转账200` | 生成多节点 `task_list`，必要时返回待确认图 / actions | 需补流式 |
| S11 | 意图取消：补槽中取消当前意图 | `/api/v1/message` `stream=true` | `给小明转账` -> `不转了/取消` | 当前节点或图进入 `cancelled`，返回 `assistant_cancel` | 需补自动化 |
| S12 | 意图切换：补槽中提出新意图 | `/api/v1/message` `stream=true` | `给小明转账` -> `先不转了，查余额` | 旧任务取消或挂起，新识别帧和新 `task_list` 生效 | 需补自动化 |
| S13 | 识别服务失败 | `/api/v1/message` `stream=true` | LLM/识别不可用 | 不推误导性 `intent_recognized`，直接失败帧 | 已覆盖 |
| S14 | 旧 session 接口不可用 | 路由注册检查 | 不调用独立 session API | active router 中无 `/sessions` 生产入口 | 已覆盖 |
| S15 | assistant-service 流式代理 | `/api/assistant/run/stream` | 助手服务转发 router SSE | 不重写 SSE frame，透传 `message/done` | 已覆盖 |

## 2. 测试目标

1. `sessionId` 只从请求体传入，不能依赖独立 session 创建接口。
2. 主链路以 `POST /api/v1/message` + `stream=true` 验证。
3. 完成态以 `POST /api/v1/task/completion` + `stream=true` 验证。
4. 意图识别完成后必须先给上游推一帧识别结果，保证客户端可持续展示打字机效果。
5. Agent 给出业务结果后，Router 顶层必须先进入 `waiting_assistant_completion`；只有助手确认后才是 `completed`。
6. 多任务图中，助手确认当前任务完成后，Router 必须继续调度后续 ready 节点。
7. 多轮提槽、槽位覆盖、意图取消、意图切换、多意图图确认都必须有明确回归路径。

不覆盖前端，不覆盖 admin。

## 3. 自动化回归命令

在仓库根目录执行：

```bash
pytest backend/tests/test_router_api_v2.py \
  backend/tests/test_assistant_service.py \
  backend/tests/test_graph_orchestrator.py \
  backend/tests/test_router_api_errors.py \
  -q
```

当前基线结果：

```text
41 passed
```

可选语法检查：

```bash
python -m compileall -q backend/services/router-service/src \
  backend/tests/test_router_api_v2.py \
  backend/tests/test_assistant_service.py \
  backend/tests/test_graph_orchestrator.py
```

## 4. SSE 判定规则

回归测试只消费：

- `event: message`
- `event: done`

识别结果帧：

```text
payload.output.stage == "intent_recognition"
payload.completion_reason == "intent_recognized"
```

业务状态帧：

```text
payload.output.stage != "intent_recognition"
```

结束帧：

```text
event: done
data: [DONE]
```

测试断言必须区分识别帧和业务帧，不能简单拿第一帧当最终业务结果。

## 5. 接口基线

### 5.1 消息入口

```http
POST /api/v1/message
```

流式请求基线：

```json
{
  "sessionId": "assistant_regression_001",
  "txt": "给小明转账",
  "stream": true,
  "executionMode": "execute",
  "custId": "C0001",
  "config_variables": [
    {"name": "custID", "value": "C0001"},
    {"name": "sessionID", "value": "assistant_regression_001"}
  ]
}
```

### 5.2 完成态回调

```http
POST /api/v1/task/completion
```

流式请求基线：

```json
{
  "sessionId": "assistant_regression_001",
  "taskId": "task_xxx",
  "completionSignal": 2,
  "stream": true
}
```

`completionSignal` 只允许：

- `1`：助手侧确认阶段性完成
- `2`：助手侧确认最终完成

## 6. 流式测试用例

### TC-S01 意图识别后立即推流

目的：上游客户端必须在意图识别后立刻收到可展示帧，不能等到补槽或 agent 执行结束才有输出。

请求：

```bash
curl -N http://127.0.0.1:8000/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "assistant_tc_s01",
    "txt": "给小明转账",
    "stream": true,
    "custId": "C0001",
    "config_variables": [
      {"name": "custID", "value": "C0001"},
      {"name": "sessionID", "value": "assistant_tc_s01"}
    ]
  }'
```

期望 SSE 顺序：

1. `message`：识别帧

```json
{
  "ok": true,
  "status": "running",
  "intent_code": "AG_TRANS",
  "completion_state": 0,
  "completion_reason": "intent_recognized",
  "output": {
    "stage": "intent_recognition",
    "primary": [{"intent_code": "AG_TRANS"}],
    "candidates": []
  }
}
```

2. `message`：业务状态帧

```json
{
  "ok": true,
  "status": "waiting_user_input",
  "intent_code": "AG_TRANS",
  "completion_state": 0,
  "completion_reason": "router_waiting_user_input",
  "slot_memory": {"payee_name": "小明"},
  "message": "请提供金额"
}
```

3. `done`

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_waiting_then_completed`
- `backend/tests/test_assistant_service.py::test_assistant_service_end_to_end_stream_with_real_router_app`

### TC-S02 多轮提槽：补金额后等待助手确认

目的：同一个 `sessionId` 的第二轮输入必须沿用首轮槽位，且 agent 业务结果不能让 Router 顶层直接 completed。

步骤：

1. 用 TC-S01 的 `sessionId` 发送 `给小明转账`。
2. 同一个 `sessionId` 发送：

```bash
curl -N http://127.0.0.1:8000/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "assistant_tc_s01",
    "txt": "200",
    "stream": true,
    "custId": "C0001",
    "config_variables": [
      {"name": "custID", "value": "C0001"},
      {"name": "sessionID", "value": "assistant_tc_s01"}
    ]
  }'
```

期望第二轮 SSE：

- 存在识别结果帧。
- 最后一个业务帧：

```json
{
  "ok": true,
  "status": "waiting_assistant_completion",
  "completion_state": 1,
  "completion_reason": "assistant_confirmation_required",
  "slot_memory": {"payee_name": "小明", "amount": "200"},
  "message": "执行图等待助手确认完成态",
  "output": {
    "message": "已向小明转账 200 CNY，转账成功",
    "completion_state": 2,
    "completion_reason": "agent_final_done"
  }
}
```

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_waiting_then_completed`
- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_preserves_agent_workflow_frames`
- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_flattens_agent_output_wrapper_without_expanding_contract`

### TC-S03 多轮槽位覆盖 / 用户纠错

目的：用户在补槽过程中修改前面给过的槽位，Router 必须以最新用户表达为准。

流式步骤：

| 轮次 | `txt` | 期望 |
|---:|---|---|
| 1 | `我要转账` | `waiting_user_input`，提示补金额和收款人 |
| 2 | `小刚` | `slot_memory.payee_name=小刚`，继续提示补金额 |
| 3 | `小红吧` | `slot_memory.payee_name=小红`，覆盖小刚 |
| 4 | `200` | `slot_memory.payee_name=小红, amount=200`，进入 `waiting_assistant_completion` |

每一轮都使用：

```json
{
  "sessionId": "assistant_tc_s03",
  "stream": true,
  "config_variables": [
    {"name": "custID", "value": "C0001"},
    {"name": "sessionID", "value": "assistant_tc_s03"}
  ]
}
```

补充自动化要求：

- 当前已有非流式断言：`backend/tests/test_router_api_v2.py::test_v1_message_assistant_protocol_keeps_latest_payee_across_multiple_waiting_turns`
- 需要新增同等流式断言，逐轮解析 SSE 的最后一个业务帧。

### TC-S04 router_only 多轮提槽

目的：`executionMode=router_only` 时也必须完整走识别、提槽、槽位覆盖和 ready 判断，但不能调用下游 agent。

流式步骤同 TC-S03，额外设置：

```json
{
  "executionMode": "router_only"
}
```

期望：

- 每轮仍有识别结果帧。
- 缺槽时返回 `waiting_user_input`。
- 槽位齐全时返回 `ready_for_dispatch`。
- `completion_reason="router_ready_for_dispatch"`。
- `output={}`。
- agent 调用次数为 0。

补充自动化要求：

- 当前已有非流式断言：`backend/tests/test_router_api_v2.py::test_v1_message_router_only_keeps_latest_payee_across_multiple_waiting_turns`
- 需要新增 `stream=true` 版本。

### TC-S05 Agent 工作流多帧透传

目的：下游 agent 是 SSE 时，Router 不能只等最终结果；需要把中间工作流状态持续推给上游。

请求：

```json
{
  "sessionId": "assistant_tc_s05",
  "txt": "给小明转账200",
  "stream": true,
  "config_variables": [
    {"name": "custID", "value": "C0001"},
    {"name": "sessionID", "value": "assistant_tc_s05"}
  ]
}
```

期望业务帧顺序：

1. `running`：`output.node_id=validate_payee`，`output.message=收款人校验通过`
2. `waiting_assistant_completion`：`output.node_id=execute_transfer`，`output.message=已向小明转账 200 CNY，转账成功`
3. `done`

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_preserves_agent_workflow_frames`

### TC-S06 Agent 输出扁平化

目的：Agent 返回包装结构时，Router 对上游的顶层字段必须稳定，不能把内部 `slot_memory` 塞进 `output`。

期望：

- 每个业务帧都只包含助手协议顶层字段集合。
- 顶层 `slot_memory` 保存当前槽位。
- `output.slot_memory` 不存在。
- 最终业务帧仍是 `waiting_assistant_completion`。

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_flattens_agent_output_wrapper_without_expanding_contract`

### TC-S07 单任务助手确认完成

目的：确认 `POST /api/v1/task/completion` 才是 Router 顶层完成态的唯一收口。

前置：TC-S02 返回 `waiting_assistant_completion`，记录 `current_task`。

请求：

```bash
curl -N http://127.0.0.1:8000/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "assistant_tc_s01",
    "taskId": "current_task_from_tc_s02",
    "completionSignal": 2,
    "stream": true
  }'
```

期望：

- `status="completed"`
- `completion_state=2`
- `completion_reason="assistant_final_done"`
- `task_list[0].status="completed"`
- 最后一帧为 `done`

自动化覆盖：

- `backend/tests/test_assistant_service.py::test_assistant_service_end_to_end_stream_with_real_router_app`
- `backend/tests/test_router_api_v2.py::test_v1_task_completion_with_real_router_app_returns_completed_output`

### TC-S08 多任务确认后继续执行

目的：确认 completion 不是单纯改状态，而是能在同一个 SSE 响应中继续推动后续 ready 节点。

前置：

- graph 中 node1 为 `waiting_assistant_completion`。
- node2 依赖 node1。
- 助手对 node1 调 `completionSignal=2`。

期望 SSE：

1. node1 完成帧：

```json
{
  "status": "completed",
  "completion_state": 2,
  "completion_reason": "assistant_final_done"
}
```

2. node2 后续执行帧：

```json
{
  "status": "waiting_assistant_completion",
  "completion_state": 1,
  "completion_reason": "assistant_confirmation_required"
}
```

3. `done`

自动化覆盖：

- `backend/tests/test_graph_orchestrator.py::test_handle_task_completion_drains_ready_downstream_node_after_assistant_confirmation`
- `backend/tests/test_router_api_v2.py::test_v1_task_completion_stream_confirms_current_task_then_runs_next_task`

### TC-S09 多意图 / 待确认图

目的：多意图输入不能被压成一个单任务结果；Router 必须暴露可被上游展示和确认的任务列表。

请求：

```json
{
  "sessionId": "assistant_tc_s09",
  "txt": "先查余额，再给张三转账 200 元，卡号 6222020100049999999，尾号 1234",
  "stream": true,
  "config_variables": [
    {"name": "custID", "value": "C0001"},
    {"name": "sessionID", "value": "assistant_tc_s09"}
  ]
}
```

期望：

- 有识别结果帧，且候选/主意图能体现多意图。
- 业务帧 `task_list` 至少包含两个任务。
- `current_task` 必须是 `task_list` 中的一个。
- 如果图需要确认，返回 `graph.proposed` / `graph.waiting_confirmation` 对应的助手 payload。
- 如果当前策略直接执行第一个节点，后续仍必须通过 `task_list` 保留剩余任务状态。

补充自动化要求：

- 当前已有非流式基本断言：`backend/tests/test_router_api_v2.py::test_v2_router_message_assistant_protocol_supports_multi_intent_graph`
- 需要新增流式版本，并断言 `task_list`、图确认状态和 `done` 帧。

### TC-S10 意图取消：补槽中取消当前意图

目的：用户在补槽中明确取消当前事项时，Router 不能继续等待原槽位，也不能误触发 agent。

步骤：

1. `stream=true` 发 `给小明转账`，进入 `waiting_user_input`。
2. 同一 `sessionId` 发 `不转了` 或 `取消`。

期望：

- 有识别/决策帧，不应继续返回原任务的补金额提示。
- 当前 node 或 graph 进入 `cancelled`。
- 顶层：

```json
{
  "status": "cancelled",
  "completion_state": 2,
  "completion_reason": "assistant_cancel",
  "message": "执行图已取消"
}
```

- `task_list` 中原任务为 `cancelled`，或 session 回到 idle 且没有 active task。
- agent 调用次数为 0。

补充自动化要求：

- 需要新增端到端流式测试。
- 需要覆盖自然语言取消和显式 action-flow 取消两种路径；当前生产助手入口优先按自然语言 `/api/v1/message` 回归。

### TC-S11 意图切换：补槽中提出新意图

目的：用户在当前意图等待补槽时提出新的业务目标，Router 必须能够重规划，而不是把新输入错误填入旧槽位。

步骤：

1. `stream=true` 发 `给小明转账`，进入 `waiting_user_input`。
2. 同一 `sessionId` 发 `先不转了，帮我查余额`。

期望：

- 旧转账任务取消、挂起或被重规划替换，但不能继续提示转账金额。
- 新识别帧的 `intent_code` 指向查余额类意图。
- 新业务帧的 `current_task` / `task_list` 指向新任务。
- 如果查余额无需槽位，进入 `waiting_assistant_completion` 或 `ready_for_dispatch`。

补充自动化要求：

- 需要新增端到端流式测试。
- 需要覆盖 `cancel_current` 和 `replan` 两类 TurnDecision。

### TC-S12 识别失败时不推误导性成功帧

目的：LLM 或识别服务不可用时，不应先推 `intent_recognized`，应直接返回失败帧。

期望：

```json
{
  "ok": false,
  "status": "failed",
  "completion_state": 2,
  "completion_reason": "router_error",
  "errorCode": "ROUTER_LLM_UNAVAILABLE",
  "message": "意图识别服务暂不可用，请稍后重试。",
  "output": {}
}
```

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_surfaces_llm_unavailable`
- `backend/tests/test_router_api_v2.py::test_v2_router_message_assistant_protocol_returns_ok_false_when_recognizer_is_unavailable`

### TC-S13 旧 session 接口不再作为生产入口

目的：防止重新引入独立 session API。

检查：

```bash
rg -n "/api/router/v2/sessions|/api/router/sessions|create_session|session.create|get_orchestrator_v2|get_event_broker_v2" \
  backend/services/router-service/src backend/tests scripts README.md docs/v3 docs/v4 \
  -g '!docs/archive/**' -g '!**/__pycache__/**'
```

期望：

- active router 服务、active 后端测试、active 脚本中无独立 session 生产入口引用。
- 前端不作为本轮 router 回归范围。

自动化覆盖：

- `backend/tests/test_router_api_errors.py::test_legacy_router_session_endpoints_are_removed`

### TC-S14 assistant-service 流式代理不改写 SSE

目的：助手服务只是代理 router SSE，不应重写 `event: message` / `event: done`。

请求入口：

```http
POST /api/assistant/run/stream
POST /api/assistant/task/completion/stream
```

期望：

- 请求转发到 router 时 `stream=true`。
- 返回 `content-type` 为 `text/event-stream`。
- SSE body 与 router 返回保持一致。

自动化覆盖：

- `backend/tests/test_assistant_service.py::test_assistant_service_proxies_router_stream_without_rewriting_events`
- `backend/tests/test_assistant_service.py::test_assistant_service_proxies_router_task_completion_stream_without_rewriting_events`

## 7. 非流式兼容对照

非流式只验证协议兼容，不替代流式主回归。

必须保留的对照点：

1. `stream=false` 时仍返回助手协议顶层字段。
2. 不返回 `snapshot`。
3. `waiting_assistant_completion` 与 `completed` 的完成态语义和流式一致。
4. 错误场景仍返回结构化 `ok=false`。

已有覆盖：

- `backend/tests/test_router_api_v2.py::test_v2_router_message_assistant_protocol_waiting_response_for_missing_slots`
- `backend/tests/test_router_api_v2.py::test_v2_router_message_assistant_protocol_returns_output_after_second_turn`
- `backend/tests/test_assistant_service.py::test_assistant_service_end_to_end_non_stream_with_real_router_app`

## 8. 回归通过标准

一次回归通过需要同时满足：

1. 自动化命令通过，当前基线为 `41 passed`。
2. 所有 `stream=true` 主链路均以 `event: done` 收尾。
3. 每个 `/api/v1/message` 流式请求在可识别场景下先推 `intent_recognized`。
4. 多轮提槽使用同一 `sessionId`，槽位持续且可被后续用户输入覆盖。
5. Agent 业务完成后，Router 顶层先返回 `waiting_assistant_completion`，不能直接 `completed`。
6. `/api/v1/task/completion` 确认后，单任务能最终完成，多任务能继续调度后续 ready 节点。
7. 意图取消、意图切换、多意图图确认必须补齐流式自动化后才能算完整回归闭环。
8. active router 代码中没有独立 session 入口复活。
9. admin 相关代码不参与 router 回归。
