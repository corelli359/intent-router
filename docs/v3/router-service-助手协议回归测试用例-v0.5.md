# router-service 助手协议回归测试用例 v0.5

更新时间：2026-04-28

## 1. 测试目标

验证 router 当前助手对接口径稳定可回归：

1. 会话标识只从请求体 `sessionId` 传入，不再依赖独立 session 创建接口。
2. 主入口固定为 `POST /api/v1/message`。
3. 助手完成态回调固定为 `POST /api/v1/task/completion`。
4. 流式模式下，意图识别完成后会先推一帧识别结果，后续继续推补槽、执行、完成等状态。
5. 助手确认完成态后，router 会继续调度后续 ready 节点。

不覆盖前端，不覆盖 admin。

## 2. 自动化回归命令

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
38 passed
```

可选语法检查：

```bash
python -m compileall -q backend/services/router-service/src \
  backend/tests/test_router_api_v2.py \
  backend/tests/test_assistant_service.py
```

## 3. 接口基线

### 3.1 消息入口

```http
POST /api/v1/message
```

请求体必须包含：

```json
{
  "sessionId": "assistant_regression_001",
  "txt": "给小明转账",
  "stream": false,
  "executionMode": "execute",
  "custId": "C0001",
  "config_variables": [
    {"name": "custID", "value": "C0001"},
    {"name": "sessionID", "value": "assistant_regression_001"}
  ]
}
```

### 3.2 完成态回调

```http
POST /api/v1/task/completion
```

请求体：

```json
{
  "sessionId": "assistant_regression_001",
  "taskId": "task_xxx",
  "completionSignal": 1
}
```

`completionSignal` 只允许：

- `1`：助手侧确认阶段性完成
- `2`：助手侧确认最终完成

## 4. 测试用例

### TC-01 非流式首轮补槽

目的：确认 `sessionId` 从 body 生效，并返回助手协议顶层结构。

请求：

```bash
curl -sS http://127.0.0.1:8000/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "assistant_tc_01",
    "txt": "给小明转账",
    "stream": false,
    "custId": "C0001",
    "config_variables": [
      {"name": "custID", "value": "C0001"},
      {"name": "sessionID", "value": "assistant_tc_01"}
    ]
  }'
```

期望：

- HTTP 200
- `ok=true`
- 不返回 `snapshot`
- `intent_code="AG_TRANS"`
- `status="waiting_user_input"`
- `completion_state=0`
- `slot_memory.payee_name="小明"`
- `message` 提示补金额

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v2_router_message_assistant_protocol_waiting_response_for_missing_slots`
- `backend/tests/test_assistant_service.py::test_assistant_service_end_to_end_non_stream_with_real_router_app`

### TC-02 非流式二轮完成

目的：确认同一个 `sessionId` 的第二轮消息继续使用已有上下文。

步骤：

1. 先执行 TC-01。
2. 再发送：

```bash
curl -sS http://127.0.0.1:8000/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "assistant_tc_01",
    "txt": "200",
    "stream": false,
    "custId": "C0001",
    "config_variables": [
      {"name": "custID", "value": "C0001"},
      {"name": "sessionID", "value": "assistant_tc_01"}
    ]
  }'
```

期望：

- HTTP 200
- `ok=true`
- `status="completed"`
- `completion_state=2`
- `completion_reason="agent_final_done"`
- `slot_memory={"amount":"200","payee_name":"小明"}`
- `output.data[0].answer` 包含 `||200|小明|`

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v2_router_message_assistant_protocol_returns_output_after_second_turn`
- `backend/tests/test_assistant_service.py::test_assistant_service_end_to_end_non_stream_with_real_router_app`

### TC-03 SSE 首帧推送意图识别结果

目的：上游客户端需要打字机效果，router 必须在识别完成后推送一帧可展示的识别结果。

请求：

```bash
curl -N http://127.0.0.1:8000/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "assistant_tc_03",
    "txt": "给小明转账",
    "stream": true,
    "custId": "C0001",
    "config_variables": [
      {"name": "custID", "value": "C0001"},
      {"name": "sessionID", "value": "assistant_tc_03"}
    ]
  }'
```

期望 SSE 事件顺序：

1. `event: message`，识别结果帧：

```json
{
  "ok": true,
  "status": "running",
  "intent_code": "AG_TRANS",
  "completion_state": 0,
  "completion_reason": "intent_recognized",
  "message": "意图识别完成: AG_TRANS",
  "output": {
    "stage": "intent_recognition",
    "primary": [{"intent_code": "AG_TRANS"}],
    "candidates": []
  }
}
```

2. `event: message`，业务状态帧：

```json
{
  "ok": true,
  "status": "waiting_user_input",
  "intent_code": "AG_TRANS",
  "completion_state": 0,
  "message": "请提供金额"
}
```

3. `event: done`

```text
data: [DONE]
```

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_waiting_then_completed`
- `backend/tests/test_assistant_service.py::test_assistant_service_end_to_end_stream_with_real_router_app`

### TC-04 SSE 执行完成流

目的：确认第二轮流式请求仍会先推识别结果，再推执行完成结果。

步骤：

1. 先用 `stream=true` 发送 `"给小明转账"`。
2. 再对同一个 `sessionId` 发送 `"200"`。

期望第二轮 SSE：

- 存在识别结果帧，`completion_reason="intent_recognized"`。
- 存在完成帧，`status="completed"`。
- 完成帧 `completion_state=2`。
- 完成帧 `completion_reason="agent_final_done"`。
- 完成帧 `output.data[0].answer` 包含 `||200|小明|`。
- 最后一帧为 `event: done`。

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_waiting_then_completed`
- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_preserves_agent_workflow_frames`
- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_flattens_agent_output_wrapper_without_expanding_contract`

### TC-05 助手确认完成态后继续执行

目的：确认 `POST /api/v1/task/completion` 不只是改状态，还会触发 graph 继续调度后续 ready 节点。

前置状态：

- 当前 graph 中 node1 为 `waiting_assistant_completion`。
- node2 依赖 node1 完成。
- agent 已对 node1 给出阶段性完成信号，助手再回调 `completionSignal=1`。

请求：

```bash
curl -sS http://127.0.0.1:8000/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "assistant_tc_05",
    "taskId": "task_from_current_task",
    "completionSignal": 1
  }'
```

期望：

- node1 转为 `completed`。
- graph 重新 drain。
- 依赖 node1 的 node2 从 `blocked`/`ready` 继续执行。
- 若后续节点完成，最终返回完成态；若后续节点还需输入，则返回对应等待态。

自动化覆盖：

- `backend/tests/test_graph_orchestrator.py::test_handle_task_completion_drains_ready_downstream_node_after_assistant_confirmation`
- `backend/tests/test_router_api_v2.py::test_v1_task_completion_real_chain_waits_for_assistant_then_joins_to_completed`

### TC-06 识别失败时不推误导性识别成功帧

目的：LLM 或识别服务不可用时，不应先推 `intent_recognized`，应直接返回失败帧。

期望：

- `status="failed"`
- `completion_state=2`
- `errorCode="ROUTER_LLM_UNAVAILABLE"`
- `message="意图识别服务暂不可用，请稍后重试。"`
- `output={}`

自动化覆盖：

- `backend/tests/test_router_api_v2.py::test_v1_message_stream_assistant_protocol_surfaces_llm_unavailable`
- `backend/tests/test_router_api_v2.py::test_v2_router_message_assistant_protocol_returns_ok_false_when_recognizer_is_unavailable`

### TC-07 旧 session 接口不再作为生产入口

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

## 5. SSE 帧判定规则

回归测试解析 SSE 时，只消费 `event: message` 和 `event: done`。

识别结果帧判定：

```text
payload.output.stage == "intent_recognition"
payload.completion_reason == "intent_recognized"
```

业务状态帧判定：

```text
payload.output.stage != "intent_recognition"
```

这样可以保证：

- 上游客户端可先展示识别结果，形成持续输出。
- 后续补槽、执行、完成帧不受影响。
- 非流式接口不增加额外中间状态。

## 6. 回归通过标准

一次回归通过需要同时满足：

1. 自动化命令 `38 passed`。
2. TC-03 SSE 首帧为识别结果帧。
3. TC-05 助手 completion 后能继续执行后续 ready 节点。
4. active router 代码中没有独立 session 入口复活。
5. admin 相关代码不参与 router 回归。
