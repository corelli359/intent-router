# Router Service 联调接口测试文档 v0.5

本文档用于交付给调用方联调验证。示例默认 Router 已可访问，调用方只需要替换
`BASE_URL`。所有示例均使用生产助手协议入口：

- `POST /api/v1/message`
- `POST /api/v1/task/completion`

## 1. 通用约定

```bash
export BASE_URL="http://127.0.0.1:8012"
```

SSE 请求统一使用：

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{...}'
```

SSE 返回格式：

```text
event: message
data: {...}

event: done
data: [DONE]
```

关键顶层字段：

| 字段 | 含义 |
| --- | --- |
| `ok` | 请求是否成功 |
| `current_task` | 当前任务 ID |
| `task_list` | 当前 session 内任务列表 |
| `status` | 当前路由/任务状态 |
| `intent_code` | 当前意图 |
| `completion_state` | 完成态，`0` 未完成，`1` 等助手确认，`2` 已完成 |
| `completion_reason` | 完成原因 |
| `slot_memory` | Router 当前识别到的槽位 |
| `message` | 面向助手/用户的提示 |
| `output` | agent 或 router_only 输出 |
| `stage` | 流式阶段，例如 `intent_recognition` |

## 2. 单任务：转账

### 2.1 发起转账

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_transfer_001",
    "txt": "给小明转500元",
    "stream": true,
    "executionMode": "execute",
    "config_variables": [
      {"name": "agentSessionID", "value": "it_transfer_001"}
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"transfer_money","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: transfer_money","output":{},"stage":"intent_recognition",...}

event: message
data: {"ok":true,"current_task":"<transfer-task-id>","task_list":[{"name":"<transfer-task-id>","status":"waiting"}],"status":"waiting_assistant_completion","intent_code":"transfer_money","completion_state":1,"completion_reason":"assistant_confirmation_required","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图等待助手确认完成态",...}

event: done
data: [DONE]
```

判断标准：

- 第一帧必须是 `stage=intent_recognition`。
- 第二帧 `status=waiting_assistant_completion`。
- `slot_memory.amount=500`。
- `slot_memory.payee_name=小明`。
- 记录 `current_task`，用于 completion 回调。

### 2.2 确认当前任务完成

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/task/completion" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_transfer_001",
    "taskId": "<transfer-task-id>",
    "completionSignal": 2,
    "stream": true
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"<transfer-task-id>","task_list":[{"name":"<transfer-task-id>","status":"completed"}],"status":"completed","intent_code":"transfer_money","completion_state":2,"completion_reason":"assistant_final_done","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图已完成",...}

event: done
data: [DONE]
```

判断标准：

- `completion_state=2`。
- `completion_reason=assistant_final_done`。
- `task_list[0].status=completed`。

## 3. 推荐任务 + 多任务推进：第二个任务有槽位

该场景验证：

- `recommendTask` 是候选任务上下文。
- 用户可说“选第一个和第三个”。
- completion 只完成当前任务，不自动推进下一个任务。
- 用户显式说 `继续` 后，第二个任务先流式输出当前意图，再输出执行结果。
- 第二个推荐任务带槽位时，`继续` 后可直接执行。

### 3.1 发起推荐多选

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_recommend_slots_present_001",
    "txt": "选第一个和第三个",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {
        "role": "assistant",
        "content": "当前推荐：1. 给小明转账500元；2. 给小刚转账200元；3. 给燃气户号333333缴费120元"
      }
    ],
    "recommendTask": [
      {
        "intent_code": "transfer_money",
        "title": "给小明转账500元",
        "slot_memory": {"payee_name": "小明", "amount": "500"}
      },
      {
        "intent_code": "transfer_money",
        "title": "给小刚转账200元",
        "slot_memory": {"payee_name": "小刚", "amount": "200"}
      },
      {
        "intent_code": "pay_gas_bill",
        "title": "给燃气户号333333缴费120元",
        "slot_memory": {"gas_account_number": "333333", "amount": "120"}
      }
    ],
    "config_variables": [
      {"name": "agentSessionID", "value": "it_recommend_slots_present_001"}
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"transfer_money","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: transfer_money, pay_gas_bill","output":{},"stage":"intent_recognition","details":{"primary":[{"intent_code":"transfer_money",...},{"intent_code":"pay_gas_bill",...}],"candidates":[]}}

event: message
data: {"ok":true,"current_task":"<transfer-task-id>","task_list":[{"name":"<transfer-task-id>","status":"waiting"},{"name":"<gas-task-id>","status":"waiting"}],"status":"waiting_assistant_completion","intent_code":"transfer_money","completion_state":1,"completion_reason":"assistant_confirmation_required","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图等待助手确认完成态",...}

event: done
data: [DONE]
```

判断标准：

- 识别帧包含 `transfer_money` 和 `pay_gas_bill`。
- 生成两个 task。
- 当前任务是第一个推荐任务：`小明/500`。
- 第二个推荐任务处于 `waiting`。

### 3.2 完成第一个任务

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/task/completion" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_recommend_slots_present_001",
    "taskId": "<transfer-task-id>",
    "completionSignal": 2,
    "stream": true
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"<transfer-task-id>","task_list":[{"name":"<transfer-task-id>","status":"completed"},{"name":"<gas-task-id>","status":"waiting"}],"status":"completed","intent_code":"transfer_money","completion_state":2,"completion_reason":"assistant_final_done","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图已完成",...}

event: done
data: [DONE]
```

判断标准：

- 第一个 task 为 `completed`。
- 第二个 task 仍为 `waiting`。
- completion 不会自动推进第二个任务。

### 3.3 显式继续第二个任务

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_recommend_slots_present_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {"role": "assistant", "content": "转账已完成，下一项是燃气缴费"}
    ],
    "config_variables": [
      {"name": "agentSessionID", "value": "it_recommend_slots_present_001"}
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: pay_gas_bill","output":{},"stage":"intent_recognition","details":{"primary":[{"intent_code":"pay_gas_bill","confidence":0.99,"reason":"continued current graph"}],"candidates":[]}}

event: message
data: {"ok":true,"current_task":"<gas-task-id>","task_list":[{"name":"<transfer-task-id>","status":"completed"},{"name":"<gas-task-id>","status":"running"}],"status":"running","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"running","slot_memory":{"gas_account_number":"333333","amount":"120"},"message":"","output":{"event":"final","content":"已为燃气户号 333333 缴费 120 元","ishandover":true,"status":"completed","payload":{"agent":"pay_gas_bill","gas_account_number":"333333","amount":"120","business_status":"completed"},"node_id":"<node-id>"}}

event: done
data: [DONE]
```

判断标准：

- `继续` 后第一帧仍必须是 `stage=intent_recognition`。
- 第一帧 `intent_code=pay_gas_bill`。
- 第二帧输出执行结果。
- `slot_memory.gas_account_number=333333`。
- `slot_memory.amount=120`。

## 4. 推荐任务 + 多任务推进：第二个任务缺槽

该场景验证：

- 推荐出来的第二个任务不等于可直接执行。
- 如果 `txt` 和对应 `recommendTask` 都没有槽位信息，Router 必须反问。
- 用户后续用 `txt` 补槽后，任务继续执行。

### 4.1 发起推荐多选，第三个推荐无槽位

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_recommend_slots_missing_001",
    "txt": "选第一个和第三个",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {
        "role": "assistant",
        "content": "当前推荐：1. 给小明转账500元；2. 给小刚转账200元；3. 燃气缴费"
      }
    ],
    "recommendTask": [
      {
        "intent_code": "transfer_money",
        "title": "给小明转账500元",
        "slot_memory": {"payee_name": "小明", "amount": "500"}
      },
      {
        "intent_code": "transfer_money",
        "title": "给小刚转账200元",
        "slot_memory": {"payee_name": "小刚", "amount": "200"}
      },
      {
        "intent_code": "pay_gas_bill",
        "title": "燃气缴费",
        "slot_memory": {}
      }
    ],
    "config_variables": [
      {"name": "agentSessionID", "value": "it_recommend_slots_missing_001"}
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"transfer_money","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: transfer_money, pay_gas_bill",...}

event: message
data: {"ok":true,"current_task":"<transfer-task-id>","task_list":[{"name":"<transfer-task-id>","status":"waiting"},{"name":"<gas-task-id>","status":"waiting"}],"status":"waiting_assistant_completion","intent_code":"transfer_money","completion_state":1,"completion_reason":"assistant_confirmation_required","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图等待助手确认完成态",...}

event: done
data: [DONE]
```

### 4.2 完成第一个任务

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/task/completion" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_recommend_slots_missing_001",
    "taskId": "<transfer-task-id>",
    "completionSignal": 2,
    "stream": true
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"<transfer-task-id>","task_list":[{"name":"<transfer-task-id>","status":"completed"},{"name":"<gas-task-id>","status":"waiting"}],"status":"completed","intent_code":"transfer_money","completion_state":2,"completion_reason":"assistant_final_done","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图已完成",...}

event: done
data: [DONE]
```

### 4.3 显式继续，Router 反问缺槽

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_recommend_slots_missing_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {"role": "assistant", "content": "转账已完成，下一项是燃气缴费"}
    ],
    "config_variables": [
      {"name": "agentSessionID", "value": "it_recommend_slots_missing_001"}
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: pay_gas_bill","output":{},"stage":"intent_recognition",...}

event: message
data: {"ok":true,"current_task":"<gas-task-id>","task_list":[{"name":"<transfer-task-id>","status":"completed"},{"name":"<gas-task-id>","status":"waiting"}],"status":"waiting_user_input","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"router_waiting_user_input","slot_memory":{},"message":"请提供燃气户号、缴费金额","output":{}}

event: done
data: [DONE]
```

判断标准：

- `继续` 后先输出 `pay_gas_bill` 意图帧。
- 第二帧必须是 `waiting_user_input`。
- `slot_memory={}`。
- `message=请提供燃气户号、缴费金额`。

### 4.4 用户补槽后执行

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_recommend_slots_missing_001",
    "txt": "户号333333，缴费120元",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {"role": "assistant", "content": "请补充燃气户号和缴费金额"}
    ],
    "config_variables": [
      {"name": "agentSessionID", "value": "it_recommend_slots_missing_001"}
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"<gas-task-id>","task_list":[{"name":"<transfer-task-id>","status":"completed"},{"name":"<gas-task-id>","status":"running"}],"status":"running","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"running","slot_memory":{"gas_account_number":"333333","amount":"120"},"message":"","output":{"event":"final","content":"已为燃气户号 333333 缴费 120 元","ishandover":true,"status":"completed","payload":{"agent":"pay_gas_bill","gas_account_number":"333333","amount":"120","business_status":"completed"},"node_id":"<node-id>"}}

event: done
data: [DONE]
```

判断标准：

- 槽位从用户本轮 `txt` 提取。
- `slot_memory.gas_account_number=333333`。
- `slot_memory.amount=120`。
- 燃气任务执行。

## 5. `txt` / `currentDisplay` / `recommendTask` 优先级

### 5.1 `txt` 独立生效

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_txt_only_001",
    "txt": "给小红转300元",
    "stream": true,
    "executionMode": "router_only"
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,...,"stage":"intent_recognition","intent_code":"transfer_money",...}

event: message
data: {"ok":true,"status":"ready_for_dispatch","intent_code":"transfer_money","slot_memory":{"payee_name":"小红","amount":"300"},...}
```

### 5.2 `currentDisplay` 不覆盖显式 `txt`

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_current_display_conflict_001",
    "txt": "给小红转300元",
    "stream": true,
    "executionMode": "router_only",
    "currentDisplay": [
      {"role": "assistant", "content": "推荐事项1：给小明转账500元"}
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"status":"ready_for_dispatch","intent_code":"transfer_money","slot_memory":{"payee_name":"小红","amount":"300"},...}
```

判断标准：

- `currentDisplay` 不能覆盖当前用户明确输入。
- 最终槽位必须是 `小红/300`，不是 `小明/500`。

### 5.3 `recommendTask` 可提供结构化槽位

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_recommend_select_001",
    "txt": "执行第一个",
    "stream": true,
    "executionMode": "router_only",
    "currentDisplay": [
      {"role": "assistant", "content": "推荐事项1：给小明转账500元"}
    ],
    "recommendTask": [
      {
        "intent_code": "transfer_money",
        "title": "给小明转账500元",
        "slot_memory": {"payee_name": "小明", "amount": "500"}
      }
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,...,"stage":"intent_recognition","intent_code":"transfer_money",...}

event: message
data: {"ok":true,"status":"ready_for_dispatch","intent_code":"transfer_money","slot_memory":{"amount":"500","payee_name":"小明"},...}
```

判断标准：

- `txt=执行第一个` 可引用推荐任务。
- 结构化槽位来自 `recommendTask.slot_memory`。

### 5.4 三者同时存在时 `txt` 优先

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_txt_priority_001",
    "txt": "给小红转300元",
    "stream": true,
    "executionMode": "router_only",
    "currentDisplay": [
      {"role": "assistant", "content": "推荐事项1：给小明转账500元"}
    ],
    "recommendTask": [
      {
        "intent_code": "transfer_money",
        "title": "给小明转账500元",
        "slot_memory": {"payee_name": "小明", "amount": "500"}
      }
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"status":"ready_for_dispatch","intent_code":"transfer_money","slot_memory":{"payee_name":"小红","amount":"300"},...}
```

判断标准：

- `txt` 明确给出的槽位优先。
- 推荐任务不得覆盖当前输入。

## 6. 边界场景

### 6.1 没有待办任务时直接说 `继续`

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_continue_first_turn_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {"role": "assistant", "content": "无待办任务"}
    ]
  }'
```

当前期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: 未命中主意图","output":{},"stage":"intent_recognition","details":{"primary":[],"candidates":[]}}

event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"unrecognized","intent_code":"","completion_state":2,"completion_reason":"router_no_match","slot_memory":{},"message":"暂未识别到明确事项，请换一种说法或补充更多上下文。","output":{}}

event: done
data: [DONE]
```

判断标准：

- 没有待办任务时，不会凭空推进。
- 当前提示是通用 no-match 提示。

### 6.2 等待补槽时说 `继续`

先创建一个缺槽燃气任务：

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_continue_waiting_slot_001",
    "txt": "燃气缴费",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {"role": "assistant", "content": "用户在燃气缴费页面"}
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,...,"stage":"intent_recognition","intent_code":"pay_gas_bill",...}

event: message
data: {"ok":true,"current_task":"<gas-task-id>","task_list":[{"name":"<gas-task-id>","status":"waiting"}],"status":"waiting_user_input","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"router_waiting_user_input","slot_memory":{},"message":"请提供燃气户号、缴费金额","output":{}}
```

等待补槽时发送 `继续`：

```bash
curl -sS -N -X POST "$BASE_URL/api/v1/message" \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "it_continue_waiting_slot_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {"role": "assistant", "content": "请提供燃气户号、缴费金额"}
    ]
  }'
```

期望关键返回：

```text
event: message
data: {"ok":true,"current_task":"<gas-task-id>","task_list":[{"name":"<gas-task-id>","status":"waiting"}],"status":"waiting_user_input","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"router_waiting_user_input","slot_memory":{},"message":"请提供燃气户号、缴费金额","output":{}}

event: done
data: [DONE]
```

判断标准：

- 等待补槽时，`继续` 不会跳过反问。
- 不会调用 agent。
- 仍然要求补齐缺失槽位。

## 7. 联调验收清单

- `/api/v1/message` `stream=true` 可收到 SSE。
- 可识别请求的第一帧为 `stage=intent_recognition`。
- 单任务转账可进入 `waiting_assistant_completion`。
- `/api/v1/task/completion` 可将当前 task 置为 `completed`。
- completion 不自动推进后续 task。
- 多任务后续推进必须显式调用 `/api/v1/message` 并发送 `txt=继续`。
- `继续` 后也先输出当前意图帧，再输出执行结果或反问。
- `recommendTask.slot_memory/slots` 有槽位时可用于推荐任务执行。
- `recommendTask` 没有槽位时不能猜，必须反问。
- 等待补槽时，用户补充 `txt` 后可继续执行。
- `currentDisplay` 不覆盖显式 `txt`。
- `txt` 与 `recommendTask` 冲突时，以 `txt` 为准。

## 8. k8s catalog + router_only 实测记录

测试时间：2026-04-29。

测试前提：

- Router 使用 `.venv` 和 `--reload` 启动。
- Router catalog 使用 `k8s/intent/router-intent-catalog/`。
- 子智能体停止：`8102/8104` 均无法连接。
- 请求统一带 `"stream": true` 和 `"executionMode": "router_only"`。

启动命令模板：

```bash
ROUTER_LLM_API_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1" \
ROUTER_LLM_API_KEY="<your-api-key>" \
ROUTER_LLM_MODEL="qwen3.6-flash-2026-04-16" \
ROUTER_INTENT_CATALOG_BACKEND="file" \
ROUTER_INTENT_CATALOG_FILE="k8s/intent/router-intent-catalog/intents.json" \
ROUTER_INTENT_FIELD_CATALOG_FILE="k8s/intent/router-intent-catalog/field-catalogs.json" \
ROUTER_INTENT_SLOT_SCHEMA_FILE="k8s/intent/router-intent-catalog/slot-schemas.json" \
ROUTER_INTENT_GRAPH_BUILD_HINTS_FILE="k8s/intent/router-intent-catalog/graph-build-hints.json" \
.venv/bin/python -m uvicorn router_service.api.app:app \
  --reload \
  --host 127.0.0.1 \
  --port 8012
```

健康检查：

```bash
curl -sS http://127.0.0.1:8012/health
```

实际输出：

```text
{"status":"ok"}
```

### 8.1 基础转账

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_single_k8s_20260429_001",
    "txt": "给小明转500元",
    "stream": true,
    "executionMode": "router_only"
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"AG_TRANS","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: AG_TRANS","output":{},"stage":"intent_recognition",...}

event: message
data: {"ok":true,"current_task":"task_9c9489db9d","task_list":[{"name":"task_9c9489db9d","status":"waiting"}],"status":"ready_for_dispatch","intent_code":"AG_TRANS","completion_state":0,"completion_reason":"router_ready_for_dispatch","slot_memory":{"amount":"500","payee_name":"小明"},"message":"路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",...}

event: done
data: [DONE]
```

结论：通过。k8s catalog 下转账意图码为 `AG_TRANS`。

### 8.2 recommendTask 多任务推进

k8s catalog 中燃气/缴费类意图码为 `AG_PAYMENT`。当前 k8s `slot-schemas.json` 中 `AG_PAYMENT` 没有必填槽位，因此 router_only 下会直接停在可分发边界，不会反问燃气户号或金额。

发起第一个和第三个推荐任务：

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_recommend_mixed_k8s_20260429_001",
    "txt": "选第一个和第三个",
    "stream": true,
    "executionMode": "router_only",
    "currentDisplay": [
      {"role": "assistant", "content": "当前推荐：1. 给小明转账500元；2. 给小刚转账200元；3. 缴燃气费"}
    ],
    "recommendTask": [
      {"intent_code": "AG_TRANS", "title": "给小明转账500元", "slot_memory": {"payee_name": "小明", "amount": "500"}},
      {"intent_code": "AG_TRANS", "title": "给小刚转账200元", "slot_memory": {"payee_name": "小刚", "amount": "200"}},
      {"intent_code": "AG_PAYMENT", "title": "缴燃气费", "slot_memory": {}}
    ]
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"AG_PAYMENT","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: AG_PAYMENT, AG_TRANS","output":{},"stage":"intent_recognition",...}

event: message
data: {"ok":true,"current_task":"task_beea3c4a91","task_list":[{"name":"task_beea3c4a91","status":"waiting"},{"name":"task_1d095fd36f","status":"waiting"}],"status":"ready_for_dispatch","intent_code":"AG_TRANS","completion_state":0,"completion_reason":"router_ready_for_dispatch","slot_memory":{"amount":"500","payee_name":"小明"},"message":"路由识别完成：事项「给小明转账500元」已具备执行条件，当前为 router_only 模式，未调用执行 agent",...}

event: done
data: [DONE]
```

完成第一个任务：

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_recommend_mixed_k8s_20260429_001",
    "taskId": "task_beea3c4a91",
    "completionSignal": 2,
    "stream": true
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,"current_task":"task_beea3c4a91","task_list":[{"name":"task_beea3c4a91","status":"completed"},{"name":"task_1d095fd36f","status":"waiting"}],"status":"completed","intent_code":"AG_TRANS","completion_state":2,"completion_reason":"assistant_final_done","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图已完成","output":{}}

event: done
data: [DONE]
```

显式继续第二个任务：

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_recommend_mixed_k8s_20260429_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "router_only",
    "currentDisplay": [
      {"role": "assistant", "content": "给小明转账500元已完成，下一项是缴燃气费"}
    ]
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"AG_PAYMENT","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: AG_PAYMENT","output":{},"stage":"intent_recognition","details":{"primary":[{"intent_code":"AG_PAYMENT","confidence":0.99,"reason":"continued current graph"}],"candidates":[]}}

event: message
data: {"ok":true,"current_task":"task_1d095fd36f","task_list":[{"name":"task_beea3c4a91","status":"completed"},{"name":"task_1d095fd36f","status":"waiting"}],"status":"ready_for_dispatch","intent_code":"AG_PAYMENT","completion_state":0,"completion_reason":"router_ready_for_dispatch","slot_memory":{},"message":"路由识别完成：事项「缴燃气费」已具备执行条件，当前为 router_only 模式，未调用执行 agent",...}

event: done
data: [DONE]
```

结论：通过。completion 只完成当前任务，不自动推进；继续时先输出第二任务意图帧，再输出 router_only 分发边界。

### 8.3 补槽

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_missing_slot_k8s_20260429_001",
    "txt": "转账",
    "stream": true,
    "executionMode": "router_only"
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,...,"status":"running","intent_code":"AG_TRANS","message":"意图识别完成: AG_TRANS","stage":"intent_recognition",...}

event: message
data: {"ok":true,"current_task":"task_6e38628f84","task_list":[{"name":"task_6e38628f84","status":"waiting"}],"status":"waiting_user_input","intent_code":"AG_TRANS","completion_reason":"router_waiting_user_input","slot_memory":{},"message":"请提供金额、收款人姓名","output":{}}

event: done
data: [DONE]
```

补充槽位：

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_missing_slot_k8s_20260429_001",
    "txt": "给小红转300元",
    "stream": true,
    "executionMode": "router_only"
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,"current_task":"task_6e38628f84","task_list":[{"name":"task_6e38628f84","status":"waiting"}],"status":"ready_for_dispatch","intent_code":"AG_TRANS","completion_reason":"router_ready_for_dispatch","slot_memory":{"amount":"300","payee_name":"小红"},"message":"路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",...}

event: done
data: [DONE]
```

结论：通过。等待补槽时不会调用子智能体，补齐后进入 router_only 分发边界。

### 8.4 currentDisplay 历史记忆

从 `currentDisplay` 复用历史金额：

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_current_display_same_amount_20260429_001",
    "txt": "给小红也转同样金额",
    "stream": true,
    "executionMode": "router_only",
    "currentDisplay": [
      {"role": "user", "content": "给小明转500元"},
      {"role": "assistant", "content": "已受理向小明转账500元，等待确认"},
      {"role": "user", "content": "确认完成"},
      {"role": "assistant", "content": "转账任务已完成"}
    ]
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,...,"stage":"intent_recognition","intent_code":"AG_TRANS",...}

event: message
data: {"ok":true,"status":"ready_for_dispatch","intent_code":"AG_TRANS","slot_memory":{"payee_name":"小红","amount":"500"},...}

event: done
data: [DONE]
```

从 `currentDisplay` 复用历史收款人：

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_current_display_same_payee_20260429_001",
    "txt": "给刚才那个收款人转300元",
    "stream": true,
    "executionMode": "router_only",
    "currentDisplay": [
      {"role": "user", "content": "给小明转500元"},
      {"role": "assistant", "content": "已受理向小明转账500元，等待确认"},
      {"role": "user", "content": "确认完成"},
      {"role": "assistant", "content": "转账任务已完成"}
    ]
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,...,"stage":"intent_recognition","intent_code":"AG_TRANS",...}

event: message
data: {"ok":true,"status":"ready_for_dispatch","intent_code":"AG_TRANS","slot_memory":{"amount":"300","payee_name":"小明"},...}

event: done
data: [DONE]
```

结论：通过。当前用户输入出现明确指代时，Router 可以从 `currentDisplay` 历史中提取必要槽位；当前 `txt` 明确给出的槽位仍优先。

### 8.5 继续边界

没有待办任务时直接说继续：

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_continue_no_pending_20260429_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "router_only"
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"AG_ASK","completion_reason":"intent_recognized","message":"意图识别完成: AG_ASK","stage":"intent_recognition",...}

event: message
data: {"ok":true,"current_task":"task_f61870ea0a","task_list":[{"name":"task_f61870ea0a","status":"waiting"}],"status":"draft","intent_code":"AG_ASK","completion_reason":"running","slot_memory":{},"message":"执行图等待节点确认","output":{}}

event: done
data: [DONE]
```

补槽未完成时说继续：

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "ro_continue_during_slot_20260429_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "router_only"
  }'
```

实际关键输出：

```text
event: message
data: {"ok":true,"current_task":"task_db0cf8dd0f","task_list":[{"name":"task_db0cf8dd0f","status":"waiting"}],"status":"waiting_user_input","intent_code":"AG_TRANS","completion_reason":"router_waiting_user_input","slot_memory":{},"message":"请提供金额、收款人姓名","output":{}}

event: done
data: [DONE]
```

结论：没有待办任务时不会推进业务任务；已有任务等待补槽时，`继续` 不会跳过当前任务，仍要求补齐缺失槽位。
