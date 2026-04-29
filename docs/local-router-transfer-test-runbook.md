# Local Router Transfer Test Runbook

This runbook starts `router-service` and `transfer-money-agent` locally with
reload enabled, then verifies single-task and multi-task flows with `curl`.

## 1. Prepare the branch and virtualenv

```bash
git switch fix/v3-preprod-test

# Create .venv if it does not exist.
/Users/hongyang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m venv .venv

.venv/bin/python -m pip install \
  -e backend/services/router-service \
  -e backend/services/agents/transfer-money-agent \
  -e backend/services/agents/gas-bill-agent
```

## 2. Export a local transfer catalog

The local catalog points `AG_TRANS` to the transfer agent on port `8102`.

```bash
.venv/bin/python scripts/export_transfer_only_router_catalog.py \
  --output-dir .local-data/router-transfer-only-catalog \
  --agent-url http://127.0.0.1:8102/api/agent/run
```

For the multi-task transfer + gas-bill scenario, export a two-intent local
catalog:

```bash
.venv/bin/python -c "from pathlib import Path; from scripts.register_financial_intents import build_payloads; from scripts.router_catalog_files import write_split_catalog; payloads=[]; overrides={'transfer_money':'http://127.0.0.1:8102/api/agent/run','pay_gas_bill':'http://127.0.0.1:8104/api/agent/run'}; [payloads.append({**p,'agent_url':overrides[p['intent_code']]}) for p in build_payloads() if p.get('intent_code') in overrides]; write_split_catalog(Path('.local-data/router-transfer-gas-catalog'), intents=payloads); print('[OK] exported transfer+gas catalog:', Path('.local-data/router-transfer-gas-catalog').resolve())"
```

## 3. Configure LLM environment

Use placeholders here. Do not commit real keys.

```bash
export ROUTER_LLM_API_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export ROUTER_LLM_API_KEY="<your-api-key>"
export ROUTER_LLM_MODEL="qwen3.6-flash-2026-04-16"
```

The transfer agent falls back to the same `ROUTER_LLM_*` variables, so separate
`TRANSFER_MONEY_AGENT_LLM_*` values are optional.

The gas-bill agent also falls back to the same `ROUTER_LLM_*` variables, so
separate `GAS_BILL_PAYMENT_AGENT_LLM_*` values are optional.

## 4. Start transfer agent

Run in terminal 1:

```bash
ROUTER_LLM_API_BASE_URL="$ROUTER_LLM_API_BASE_URL" \
ROUTER_LLM_API_KEY="$ROUTER_LLM_API_KEY" \
ROUTER_LLM_MODEL="$ROUTER_LLM_MODEL" \
.venv/bin/python -m uvicorn transfer_money_agent.app:app \
  --reload \
  --host 127.0.0.1 \
  --port 8102
```

Health check:

```bash
curl -sS http://127.0.0.1:8102/health
```

Expected:

```json
{"status":"ok","service":"transfer-money-agent","llm_ready":true}
```

For the multi-task scenario, also start gas-bill agent in terminal 2:

```bash
ROUTER_LLM_API_BASE_URL="$ROUTER_LLM_API_BASE_URL" \
ROUTER_LLM_API_KEY="$ROUTER_LLM_API_KEY" \
ROUTER_LLM_MODEL="$ROUTER_LLM_MODEL" \
.venv/bin/python -m uvicorn gas_bill_agent.app:app \
  --reload \
  --host 127.0.0.1 \
  --port 8104
```

Health check:

```bash
curl -sS http://127.0.0.1:8104/health
```

Expected:

```json
{"status":"ok","service":"gas-bill-payment-agent","llm_ready":true}
```

## 5. Start router

Run in another terminal:

```bash
ROUTER_INTENT_CATALOG_BACKEND="file" \
ROUTER_INTENT_CATALOG_FILE=".local-data/router-transfer-only-catalog/intents.json" \
ROUTER_INTENT_FIELD_CATALOG_FILE=".local-data/router-transfer-only-catalog/field-catalogs.json" \
ROUTER_INTENT_SLOT_SCHEMA_FILE=".local-data/router-transfer-only-catalog/slot-schemas.json" \
ROUTER_INTENT_GRAPH_BUILD_HINTS_FILE=".local-data/router-transfer-only-catalog/graph-build-hints.json" \
ROUTER_LLM_API_BASE_URL="$ROUTER_LLM_API_BASE_URL" \
ROUTER_LLM_API_KEY="$ROUTER_LLM_API_KEY" \
ROUTER_LLM_MODEL="$ROUTER_LLM_MODEL" \
.venv/bin/python -m uvicorn router_service.api.app:app \
  --reload \
  --host 127.0.0.1 \
  --port 8012
```

Health check:

```bash
curl -sS http://127.0.0.1:8012/health
```

Expected:

```json
{"status":"ok"}
```

For the multi-task transfer + gas-bill scenario, restart router with the
two-intent catalog:

```bash
ROUTER_INTENT_CATALOG_BACKEND="file" \
ROUTER_INTENT_CATALOG_FILE=".local-data/router-transfer-gas-catalog/intents.json" \
ROUTER_INTENT_FIELD_CATALOG_FILE=".local-data/router-transfer-gas-catalog/field-catalogs.json" \
ROUTER_INTENT_SLOT_SCHEMA_FILE=".local-data/router-transfer-gas-catalog/slot-schemas.json" \
ROUTER_INTENT_GRAPH_BUILD_HINTS_FILE=".local-data/router-transfer-gas-catalog/graph-build-hints.json" \
ROUTER_LLM_API_BASE_URL="$ROUTER_LLM_API_BASE_URL" \
ROUTER_LLM_API_KEY="$ROUTER_LLM_API_KEY" \
ROUTER_LLM_MODEL="$ROUTER_LLM_MODEL" \
.venv/bin/python -m uvicorn router_service.api.app:app \
  --reload \
  --host 127.0.0.1 \
  --port 8012
```

## 6. Run transfer curl test

Start a transfer:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "curl_transfer_test_001",
    "txt": "给小明转500元",
    "stream": true,
    "executionMode": "execute",
    "config_variables": [
      {"name": "agentSessionID", "value": "curl_transfer_test_001"},
      {"name": "currentDisplay", "value": "transfer_page"}
    ]
  }'
```

Observed SSE shape:

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"transfer_money","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: transfer_money","output":{},"stage":"intent_recognition","details":{"primary":[{"intent_code":"transfer_money","confidence":0.95,"reason":"llm returned a match"}],"candidates":[]}}

event: message
data: {"ok":true,"current_task":"<task-id>","task_list":[{"name":"<task-id>","status":"waiting"}],"status":"waiting_assistant_completion","intent_code":"transfer_money","completion_state":1,"completion_reason":"assistant_confirmation_required","slot_memory":{"payee_name":"小明","amount":"500"},"message":"执行图等待助手确认完成态","output":{"event":"final","content":"已受理向小明转账 500 CNY，等待助手确认完成态","ishandover":true,"handOverReason":"等待助手确认完成态","completion_state":1,"completion_reason":"agent_partial_done","data":[{"isSubAgent":"True","typIntent":"mbpTransfer","answer":"||500|小明|"}],"payload":{"agent":"transfer_money","amount":"500","ccy":null,"payer_card_no":null,"payer_card_remark":null,"payee_name":"小明","payee_card_no":null,"payee_card_remark":null,"payee_card_bank":null,"payee_phone":null,"business_status":"success"},"node_id":"<node-id>"}}

event: done
data: [DONE]
```

Check the second `event: message` frame:

- `current_task` is non-empty. Record it for the completion callback.
- `status` is `waiting_assistant_completion`.
- `slot_memory.amount` is `500`.
- `slot_memory.payee_name` is `小明`.
- `completion_state` is `1`.
- `completion_reason` is `assistant_confirmation_required`.

Record `current_task` from the response, then complete it:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "curl_transfer_test_001",
    "taskId": "<current_task-from-message-response>",
    "completionSignal": 2,
    "stream": true
  }'
```

Observed SSE shape:

```text
event: message
data: {"ok":true,"current_task":"<task-id>","task_list":[{"name":"<task-id>","status":"completed"}],"status":"completed","intent_code":"transfer_money","completion_state":2,"completion_reason":"assistant_final_done","slot_memory":{"payee_name":"小明","amount":"500"},"message":"执行图已完成","output":{"event":"final","content":"已受理向小明转账 500 CNY，等待助手确认完成态","ishandover":true,"handOverReason":"等待助手确认完成态","completion_state":1,"completion_reason":"agent_partial_done","data":[{"isSubAgent":"True","typIntent":"mbpTransfer","answer":"||500|小明|"}],"payload":{"agent":"transfer_money","amount":"500","ccy":null,"payer_card_no":null,"payer_card_remark":null,"payee_name":"小明","payee_card_no":null,"payee_card_remark":null,"payee_card_bank":null,"payee_phone":null,"business_status":"success"},"node_id":"<node-id>"}}

event: done
data: [DONE]
```

Check the `event: message` frame:

- `status` is `completed`.
- `task_list[0].status` is `completed`.
- `completion_state` is `2`.
- `completion_reason` is `assistant_final_done`.

## 7. Run transfer + gas-bill multi-task curl test

This scenario uses the two-intent catalog and all three services:

- router on `8012`
- transfer-money-agent on `8102`
- gas-bill-agent on `8104`

Start a multi-task request:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "multi_transfer_gas_001",
    "txt": "先给小明转500元，再给燃气户号123456缴费100元",
    "stream": true,
    "executionMode": "execute",
    "config_variables": [
      {"name": "agentSessionID", "value": "multi_transfer_gas_001"},
      {"name": "currentDisplay", "value": "multi_task_page"}
    ]
  }'
```

Observed first SSE frame:

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"transfer_money","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: transfer_money, pay_gas_bill","output":{},"stage":"intent_recognition","details":{"primary":[{"intent_code":"transfer_money","confidence":0.98,"reason":"llm returned a match"},{"intent_code":"pay_gas_bill","confidence":0.98,"reason":"llm returned a match"}],"candidates":[]}}
```

Check:

- `details.primary` contains both `transfer_money` and `pay_gas_bill`.
- The top-level `intent_code` is the first task, `transfer_money`.

Observed second SSE frame:

```text
event: message
data: {"ok":true,"current_task":"<transfer-task-id>","task_list":[{"name":"<transfer-task-id>","status":"waiting"},{"name":"<gas-task-id>","status":"waiting"}],"status":"waiting_assistant_completion","intent_code":"transfer_money","completion_state":1,"completion_reason":"assistant_confirmation_required","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图等待助手确认完成态","output":{"event":"final","content":"已受理向小明转账 500 CNY，等待助手确认完成态","ishandover":true,"handOverReason":"等待助手确认完成态","completion_state":1,"completion_reason":"agent_partial_done","data":[{"isSubAgent":"True","typIntent":"mbpTransfer","answer":"||500|小明|"}],"payload":{"agent":"transfer_money","amount":"500","ccy":null,"payer_card_no":null,"payer_card_remark":null,"payee_name":"小明","payee_card_no":null,"payee_card_remark":null,"payee_card_bank":null,"payee_phone":null,"business_status":"success"},"node_id":"<node-id>"}}

event: done
data: [DONE]
```

Check:

- The router creates two tasks.
- The first task is the transfer task.
- The second task is waiting.
- The first task enters `waiting_assistant_completion`.

Complete the first task:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "multi_transfer_gas_001",
    "taskId": "<transfer-task-id>",
    "completionSignal": 2,
    "stream": true
  }'
```

Observed behavior:

```text
event: message
data: {"ok":true,"current_task":"<transfer-task-id>","task_list":[{"name":"<transfer-task-id>","status":"completed"},{"name":"<gas-task-id>","status":"waiting"}],"status":"completed","intent_code":"transfer_money","completion_state":2,"completion_reason":"assistant_final_done",...}

event: done
data: [DONE]
```

Check:

- The transfer task is `completed`.
- The gas-bill task is still `waiting`.
- The gas-bill task is not executed by this completion call.

Continue to the second task explicitly:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "multi_transfer_gas_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "execute",
    "config_variables": [
      {"name": "agentSessionID", "value": "multi_transfer_gas_001"},
      {"name": "currentDisplay", "value": "multi_task_page"}
    ]
  }'
```

Supported continue messages include:

- `继续`
- `继续执行`
- `继续下一个`
- `继续下一步`
- `下一步`
- `下一个`
- `执行下一个`
- `执行下一步`

Observed continue response:

```text
event: message
data: {"ok":true,"current_task":"<gas-task-id>","task_list":[{"name":"<transfer-task-id>","status":"completed"},{"name":"<gas-task-id>","status":"running"}],"status":"running","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"running","slot_memory":{"gas_account_number":"123456","amount":"100"},"message":"","output":{"event":"final","content":"已为燃气户号 123456 缴费 100 元","ishandover":true,"status":"completed","payload":{"agent":"pay_gas_bill","gas_account_number":"123456","amount":"100","business_status":"completed"},"node_id":"<node-id>"}}

event: done
data: [DONE]
```

Important observation:

- The second gas-bill task only advances after the explicit `/api/v1/message`
  continue call.
- The gas-bill agent returns a completed business output, but the router frame
  still reports top-level `status="running"` and `completion_state=0` until the
  assistant confirms the second task.

Complete the second task:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "multi_transfer_gas_001",
    "taskId": "<gas-task-id>",
    "completionSignal": 2,
    "stream": true
  }'
```

Observed final SSE shape:

```text
event: message
data: {"ok":true,"current_task":"<gas-task-id>","task_list":[{"name":"<transfer-task-id>","status":"completed"},{"name":"<gas-task-id>","status":"completed"}],"status":"completed","intent_code":"pay_gas_bill","completion_state":2,"completion_reason":"assistant_final_done","slot_memory":{"gas_account_number":"123456","amount":"100"},"message":"执行图已完成","output":{"event":"final","content":"已为燃气户号 123456 缴费 100 元","ishandover":true,"status":"completed","payload":{"agent":"pay_gas_bill","gas_account_number":"123456","amount":"100","business_status":"completed"},"node_id":"<node-id>"}}

event: done
data: [DONE]
```

Check:

- Both tasks are `completed`.
- The top-level status is `completed`.
- `completion_state` is `2`.
- `completion_reason` is `assistant_final_done`.

Design note:

The current product contract for multi-task assistant protocol is:

1. `/api/v1/message` creates and starts the graph.
2. `/api/v1/task/completion` only completes the current task.
3. A fresh `/api/v1/message` with an explicit continue phrase, such as `继续`,
   advances the next task.
4. Each task still requires `/api/v1/task/completion` to become completed.

## 8. Verify `currentDisplay` and `recommendTask`

These tests use `router_only` mode so the router validates intent planning and
slot extraction without calling the execution agent.

### `currentDisplay` should not override explicit `txt`

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "current_display_conflict_001",
    "txt": "给小红转300元",
    "stream": true,
    "executionMode": "router_only",
    "currentDisplay": [
      {"role": "assistant", "content": "推荐事项 1：给小明转账500元"}
    ]
  }'
```

Expected:

- `intent_code` is `transfer_money`.
- `slot_memory.payee_name` is `小红`.
- `slot_memory.amount` is `300`.

### `recommendTask` should support indexed selection

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "recommend_task_select_001",
    "txt": "执行第一个",
    "stream": true,
    "executionMode": "router_only",
    "recommendTask": [
      {
        "intent_code": "transfer_money",
        "title": "给小明转账500元",
        "slot_memory": {"payee_name": "小明", "amount": "500"}
      }
    ]
  }'
```

Observed after the prompt/context fix:

```text
event: message
data: {..."status":"ready_for_dispatch","intent_code":"transfer_money","slot_memory":{"amount":"500","payee_name":"小明"},...}

event: done
data: [DONE]
```

Expected:

- `txt=执行第一个` selects `recommendTask[0]`.
- `intent_code` is `transfer_money`.
- `slot_memory.payee_name` is `小明`.
- `slot_memory.amount` is `500`.
- `status` is `ready_for_dispatch` in `router_only` mode.

### Explicit `txt` still wins over `recommendTask`

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "recommend_task_conflict_001",
    "txt": "给小红转300元",
    "stream": true,
    "executionMode": "router_only",
    "recommendTask": [
      {
        "intent_code": "transfer_money",
        "title": "给小明转账500元",
        "slot_memory": {"payee_name": "小明", "amount": "500"}
      }
    ]
  }'
```

Expected:

- `intent_code` is `transfer_money`.
- `slot_memory.payee_name` is `小红`.
- `slot_memory.amount` is `300`.
- The recommendation value `小明/500` must not override the explicit user
  message.

### Selecting and modifying `recommendTask`

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "recommend_task_modify_001",
    "txt": "第一个改成给小红转300元",
    "stream": true,
    "executionMode": "router_only",
    "recommendTask": [
      {
        "intent_code": "transfer_money",
        "title": "给小明转账500元",
        "slot_memory": {"payee_name": "小明", "amount": "500"}
      }
    ]
  }'
```

Expected:

- `txt` still selects `recommendTask[0]` for the intent.
- The modified slots from the current message win.
- `slot_memory.payee_name` is `小红`.
- `slot_memory.amount` is `300`.

## 9. Full flow: recommendation, continue, missing slots, and slot fill

This section records the exact curl commands and observed key SSE output from
the end-to-end validation. It verifies these rules:

- `recommendTask` is candidate context, not a whitelist.
- `currentDisplay` can help resolve what the user is pointing at, but
  structured slots should come from `txt` or `recommendTask.slot_memory/slots`.
- Completing one task does not auto-run the next task.
- A later `继续` turn may continue the current graph and reuse the original
  `recommendTask` context for the waiting node.
- If the next task has slots in `txt` or its corresponding `recommendTask`, it
  can execute. If not, the router asks for missing slots.

### Scenario A: second recommended task has slots, so `继续` executes it

Start with three recommendations and select the first and third:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "full_flow_slots_present_001",
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
      {"name": "agentSessionID", "value": "full_flow_slots_present_001"}
    ]
  }'
```

Observed key SSE output:

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"transfer_money","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: transfer_money, pay_gas_bill","output":{},"stage":"intent_recognition","details":{"primary":[{"intent_code":"transfer_money","confidence":0.99,"reason":"llm returned a match"},{"intent_code":"pay_gas_bill","confidence":0.99,"reason":"llm returned a match"}],"candidates":[]}}

event: message
data: {"ok":true,"current_task":"task_abfc2e57c9","task_list":[{"name":"task_abfc2e57c9","status":"waiting"},{"name":"task_3f91305baa","status":"waiting"}],"status":"waiting_assistant_completion","intent_code":"transfer_money","completion_state":1,"completion_reason":"assistant_confirmation_required","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图等待助手确认完成态",...}

event: done
data: [DONE]
```

Important checks:

- The first and third recommendations were selected.
- `intent_recognition` contains `transfer_money` and `pay_gas_bill`.
- Two tasks were created.
- Current task slot memory is from the first recommendation: `小明/500`.

Complete the first task:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "full_flow_slots_present_001",
    "taskId": "task_abfc2e57c9",
    "completionSignal": 2,
    "stream": true
  }'
```

Observed key SSE output:

```text
event: message
data: {"ok":true,"current_task":"task_abfc2e57c9","task_list":[{"name":"task_abfc2e57c9","status":"completed"},{"name":"task_3f91305baa","status":"waiting"}],"status":"completed","intent_code":"transfer_money","completion_state":2,"completion_reason":"assistant_final_done","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图已完成",...}

event: done
data: [DONE]
```

Important check:

- Completing task 1 does not auto-run task 2.
- Task 2 stays `waiting`.

Continue to the second task:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "full_flow_slots_present_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {"role": "assistant", "content": "转账已完成，下一项是燃气缴费"}
    ],
    "config_variables": [
      {"name": "agentSessionID", "value": "full_flow_slots_present_001"}
    ]
  }'
```

Observed key SSE output:

```text
event: message
data: {"ok":true,"current_task":"task_3f91305baa","task_list":[{"name":"task_abfc2e57c9","status":"completed"},{"name":"task_3f91305baa","status":"running"}],"status":"running","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"running","slot_memory":{"gas_account_number":"333333","amount":"120"},"message":"","output":{"event":"final","content":"已为燃气户号 333333 缴费 120 元","ishandover":true,"status":"completed","payload":{"agent":"pay_gas_bill","gas_account_number":"333333","amount":"120","business_status":"completed"},"node_id":"<node-id>"}}

event: done
data: [DONE]
```

Important checks:

- `继续` advances task 2.
- The gas task uses the corresponding recommendation slots:
  `gas_account_number=333333`, `amount=120`.
- Because slots are present, it executes instead of asking the user.

### Scenario B: second recommended task has no slots, so `继续` asks

Start with the third recommendation missing slot values:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "full_flow_slots_missing_001",
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
      {"name": "agentSessionID", "value": "full_flow_slots_missing_001"}
    ]
  }'
```

Observed key SSE output:

```text
event: message
data: {"ok":true,"current_task":"","task_list":[],"status":"running","intent_code":"transfer_money","completion_state":0,"completion_reason":"intent_recognized","slot_memory":{},"message":"意图识别完成: transfer_money, pay_gas_bill",...}

event: message
data: {"ok":true,"current_task":"task_b5f0a94522","task_list":[{"name":"task_b5f0a94522","status":"waiting"},{"name":"task_dfa0f99573","status":"waiting"}],"status":"waiting_assistant_completion","intent_code":"transfer_money","completion_state":1,"completion_reason":"assistant_confirmation_required","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图等待助手确认完成态",...}

event: done
data: [DONE]
```

Complete the first task:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "full_flow_slots_missing_001",
    "taskId": "task_b5f0a94522",
    "completionSignal": 2,
    "stream": true
  }'
```

Observed key SSE output:

```text
event: message
data: {"ok":true,"current_task":"task_b5f0a94522","task_list":[{"name":"task_b5f0a94522","status":"completed"},{"name":"task_dfa0f99573","status":"waiting"}],"status":"completed","intent_code":"transfer_money","completion_state":2,"completion_reason":"assistant_final_done","slot_memory":{"amount":"500","payee_name":"小明"},"message":"执行图已完成",...}

event: done
data: [DONE]
```

Continue to the second task:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "full_flow_slots_missing_001",
    "txt": "继续",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {"role": "assistant", "content": "转账已完成，下一项是燃气缴费"}
    ],
    "config_variables": [
      {"name": "agentSessionID", "value": "full_flow_slots_missing_001"}
    ]
  }'
```

Observed key SSE output:

```text
event: message
data: {"ok":true,"current_task":"task_dfa0f99573","task_list":[{"name":"task_b5f0a94522","status":"completed"},{"name":"task_dfa0f99573","status":"waiting"}],"status":"waiting_user_input","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"router_waiting_user_input","slot_memory":{},"message":"请提供燃气户号、缴费金额","output":{}}

event: done
data: [DONE]
```

Important checks:

- The second task is selected, but it does not execute.
- `slot_memory` is empty because neither `txt=继续` nor the corresponding
  `recommendTask` carried gas account or amount.
- Router asks for missing slots.

Fill the missing slots with a new user message:

```bash
curl -sS -N -X POST http://127.0.0.1:8012/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "full_flow_slots_missing_001",
    "txt": "户号333333，缴费120元",
    "stream": true,
    "executionMode": "execute",
    "currentDisplay": [
      {"role": "assistant", "content": "请补充燃气户号和缴费金额"}
    ],
    "config_variables": [
      {"name": "agentSessionID", "value": "full_flow_slots_missing_001"}
    ]
  }'
```

Observed key SSE output:

```text
event: message
data: {"ok":true,"current_task":"task_dfa0f99573","task_list":[{"name":"task_b5f0a94522","status":"completed"},{"name":"task_dfa0f99573","status":"running"}],"status":"running","intent_code":"pay_gas_bill","completion_state":0,"completion_reason":"running","slot_memory":{"gas_account_number":"333333","amount":"120"},"message":"","output":{"event":"final","content":"已为燃气户号 333333 缴费 120 元","ishandover":true,"status":"completed","payload":{"agent":"pay_gas_bill","gas_account_number":"333333","amount":"120","business_status":"completed"},"node_id":"<node-id>"}}

event: done
data: [DONE]
```

Important checks:

- The missing gas slots are extracted from the new `txt`.
- The waiting gas task advances and executes.

## Notes

- `.venv/` and `.local-data/` are ignored by git.
- If local port access fails in the Codex sandbox, run the same commands in a
  normal terminal or approve local network/port access when prompted.
- If an API key was pasted into chat or logs, rotate it before reuse.
