# Router-Service 助手对接接口文档 v0.5

> 状态：当前版本  
> 适用分支：`test/v3-concurrency-test`

## 1. 本版结论

v0.5 在 v0.4 基础上收敛了任务完成语义：

1. `POST /api/v1/message` 只负责接收用户输入并推进当前任务。
2. Agent 即使返回了自己的 `completion_state=2`，Router 对上游也不会直接认定任务最终完成。
3. Router 对上游会先返回：
   - `status=waiting_assistant_completion`
   - `completion_state=1`
   - `completion_reason=assistant_confirmation_required`
4. 只有助手后续调用 `POST /api/v1/task/completion`，Router 才会把当前任务收敛为最终完成。
5. 多任务图中，后续任务必须等助手确认当前任务完成后，Router 才会继续往后执行。
6. `/api/v1/task/completion` 默认支持 SSE；当确认当前任务完成后，Router 可以继续把后续任务的执行事件流式推回上游。

## 2. 固定接口

### 2.1 用户消息入口

```http
POST /api/v1/message
```

请求体关键字段：

```json
{
  "sessionId": "sess_001",
  "txt": "给小明转账",
  "stream": true,
  "executionMode": "execute",
  "custId": "C0001",
  "config_variables": []
}
```

约定：

1. `sessionId` 从报文体获取。
2. `stream` 从报文体获取；缺省时默认为 `true`。
3. 该接口不返回 `snapshot`。
4. 顶层 `message` 是 Router 占位消息，不等于 Agent 原始输出。
5. Agent 原始业务结果统一放在顶层 `output`。

### 2.2 助手完成确认入口

```http
POST /api/v1/task/completion
```

请求体：

```json
{
  "sessionId": "sess_001",
  "taskId": "task_123",
  "completionSignal": 2,
  "stream": true
}
```

约定：

1. `completionSignal` 当前支持 `1` 或 `2`。
2. `2` 表示助手确认当前任务最终完成。
3. `stream=true` 时，除了当前任务完成事件，还可能继续收到后续任务的执行事件。

## 3. 完成态语义

### 3.1 顶层完成态只看 Router

顶层字段：

- `status`
- `completion_state`
- `completion_reason`

都是 Router 汇总后的结果，助手必须以顶层字段判断当前任务是否真正结束。

### 3.2 `output` 只放 Agent 原始输出

`output` 只承载 Agent `node_output.output` 的内容；Router 自己的字段不塞进 `output`。

如果 Agent 没有 `output`，Router 会返回：

```json
{
  "output": {}
}
```

### 3.3 意图识别帧

意图识别完成后的 SSE 帧用顶层 `stage` 标识，不占用 `output`：

```json
{
  "ok": true,
  "status": "running",
  "intent_code": "AG_TRANS",
  "completion_state": 0,
  "completion_reason": "intent_recognized",
  "stage": "intent_recognition",
  "details": {
    "primary": [{"intent_code": "AG_TRANS"}],
    "candidates": []
  },
  "output": {}
}
```

### 3.4 `task_list` 生命周期

`task_list[].name` 是助手后续可回传的任务标识，当前为 `task_xxx`。`task_list[].status` 是任务生命周期粗状态，只使用：

- `waiting`
- `running`
- `completed`
- `failed`
- `cancelled`

当前轮的细状态看顶层 `status` 和 `completion_reason`，例如 `waiting_user_input`、`waiting_assistant_completion`。

### 3.5 当前版本的核心状态

| 场景 | 顶层 `status` | 顶层 `completion_state` | 顶层 `completion_reason` |
|---|---|---:|---|
| 还在执行 | `running` | 0 | `running` |
| 缺槽位等用户补充 | `waiting_user_input` | 0 | `router_waiting_user_input` |
| Agent 已给出业务结果，等待助手确认 | `waiting_assistant_completion` | 1 | `assistant_confirmation_required` |
| 助手确认最终完成 | `completed` | 2 | `assistant_final_done` |

## 4. 典型时序

### 4.1 单任务

1. 助手调用 `POST /api/v1/message`
2. Router 调 Agent
3. Agent 返回业务结果，Router 向上游推：
   - 顶层：`waiting_assistant_completion`
   - `output`：Agent 原始结果
4. 助手确认业务闭环后，调用 `POST /api/v1/task/completion`
5. Router 返回：
   - 顶层：`completed`
   - `completion_state=2`

### 4.2 多任务串行

1. 任务 A 执行完后，Router 先返回 `waiting_assistant_completion`
2. 助手调用 `/api/v1/task/completion` 确认任务 A
3. Router 将任务 A 置为 `completed`
4. 如果图中后续任务可运行，Router 继续执行任务 B
5. 当 `stream=true` 时，任务 B 的后续 SSE 事件会在同一个 completion 调用里继续向上游推送

## 5. 示例

### 5.1 `/api/v1/message` 返回等待助手确认

```json
{
  "ok": true,
  "current_task": "task_123",
  "task_list": [{"name": "task_123", "status": "waiting"}],
  "status": "waiting_assistant_completion",
  "intent_code": "AG_TRANS",
  "completion_state": 1,
  "completion_reason": "assistant_confirmation_required",
  "slot_memory": {"payee_name": "小明", "amount": "200"},
  "message": "执行图等待助手确认完成态",
  "output": {
    "message": "已向小明转账 200 CNY，转账成功",
    "completion_state": 2,
    "completion_reason": "agent_final_done",
    "ishandover": true,
    "data": [
      {"isSubAgent": "True", "typIntent": "mbpTransfer", "answer": "||200|小明|"}
    ]
  }
}
```

### 5.2 `/api/v1/task/completion` 返回最终完成

```json
{
  "ok": true,
  "current_task": "task_123",
  "task_list": [{"name": "task_123", "status": "completed"}],
  "status": "completed",
  "intent_code": "AG_TRANS",
  "completion_state": 2,
  "completion_reason": "assistant_final_done",
  "slot_memory": {"payee_name": "小明", "amount": "200"},
  "message": "执行图已完成",
  "output": {
    "message": "已向小明转账 200 CNY，转账成功",
    "completion_state": 2,
    "completion_reason": "agent_final_done",
    "ishandover": true,
    "data": [
      {"isSubAgent": "True", "typIntent": "mbpTransfer", "answer": "||200|小明|"}
    ]
  }
}
```

## 6. assistant-service 转发口

仓内 `assistant-service` 现已补齐以下代理接口：

1. `POST /api/assistant/run`
2. `POST /api/assistant/run/stream`
3. `POST /api/assistant/task/completion`
4. `POST /api/assistant/task/completion/stream`

它们分别透传到 Router 的：

1. `POST /api/v1/message`
2. `POST /api/v1/message` + `stream=true`
3. `POST /api/v1/task/completion`
4. `POST /api/v1/task/completion` + `stream=true`

## 7. 联调注意事项

1. 不要再根据 Agent 自己的 `completion_state` 直接判定任务结束。
2. 任务是否真正结束，只看顶层 `completion_state`。
3. 顶层 `message` 是 Router 占位文案；业务结果请看 `output`。
4. SSE 最后一帧仍然是：

```text
event: done
data: [DONE]
```
