# Intent-Router 通信协议规范

> 以 **转账（AG_TRANS）** 场景为例，自定义透传字段：`custID`、`sessionID`、`currentDisplay`、`agentSessionID`

---

## 1. 请求 Router 的报文格式

### 1.1 创建会话

```
POST /api/router/v2/sessions
Content-Type: application/json
```

```json
{
    "cust_id": "C000123456"
}
```

**响应：**

```json
{
    "session_id": "session_graph_xxxxx",
    "cust_id": "C000123456"
}
```

### 1.2 发送消息（Execute 模式）

```
POST /api/router/v2/sessions/{session_id}/messages
Content-Type: application/json
```

```json
{
    "session_id": "session_graph_xxxxx",
    "txt": "帮我转账500块给张三",
    "config_variables": [
        { "name": "custID",          "value": "C000123456" },
        { "name": "sessionID",       "value": "SES_20250421_001" },
        { "name": "currentDisplay",  "value": "transfer_page" },
        { "name": "agentSessionID", "value": "AGENT_SES_001" }
    ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | ✅ | 会话 ID（创建会话时返回） |
| `txt` | string | ✅ | 用户输入的自然语言 |
| `config_variables` | array | ❌ | **透传字段数组**，格式与子智能体一致 |
| `config_variables[].name` | string | — | 参数名 |
| `config_variables[].value` | string | — | 参数值 |

透传字段说明：

| name | 说明 | 示例 |
|------|------|------|
| `custID` | 业务系统客户标识 | `C000123456` |
| `sessionID` | 业务系统会话标识 | `SES_20250421_001` |
| `currentDisplay` | 当前前端展示页面标识 | `transfer_page` |
| `agentSessionID` | Agent 会话标识 | `AGENT_SES_001` |

> [!NOTE]
> `config_variables` 字段需要代码扩展支持（当前 `MessageRequest` 尚未定义该字段）。
> 在未扩展前，`custID` 和 `sessionID` 通过 `cust_id` 和 Router 内部 `session.id` 自动映射。

---

## 2. `intents.json` 中的 `field_mapping` 配置

`field_mapping` 定义了 Router 如何将内部变量 → 映射到子智能体请求报文的字段。

### 2.1 AG_TRANS 当前配置

```json
{
    "field_mapping": {
        "session_id":                                "$session.id",
        "txt":                                       "$message.current",
        "stream":                                    "true",

        "config_variables.custID":                   "$session.cust_id",
        "config_variables.sessionID":                "$session.id",
        "config_variables.currentDisplay":           "",
        "config_variables.agentSessionID":           "$session.id",

        "config_variables.slots_data.amount":        "$slot_memory.amount",
        "config_variables.slots_data.payer_card_no": "$slot_memory.payer_card_no",
        "config_variables.slots_data.payer_card_remark": "$slot_memory.payer_card_remark",
        "config_variables.slots_data.payee_name":    "$slot_memory.payee_name",
        "config_variables.slots_data.payee_card_no": "$slot_memory.payee_card_no",
        "config_variables.slots_data.payee_card_remark": "$slot_memory.payee_card_remark",
        "config_variables.slots_data.payee_card_bank": "$slot_memory.payee_card_bank",
        "config_variables.slots_data.payee_phone":   "$slot_memory.payee_phone"
    }
}
```

### 2.2 映射规则说明

```
target_path  →  source_expression
(发给Agent的位置)    (从Router取值的来源)
```

#### Target 路径规则

| Target 前缀 | 生成效果 | 示例 |
|---|---|---|
| `config_variables.slots_data.xxx` | 合并为 `slots_data` JSON 字符串 | `{"name":"slots_data","value":"{\"amount\":\"500\"}"}` |
| `config_variables.xxx` | 加入 `config_variables` 数组 | `{"name":"custID","value":"C000123456"}` |
| 普通路径（如 `session_id`） | 写入 payload 顶层 | `"session_id": "session_graph_xxx"` |

#### Source 表达式可用变量

| 表达式 | 值来源 | 示例 |
|---|---|---|
| `$session.id` | Router 会话 ID | `session_graph_xxx` |
| `$session.cust_id` | 客户 ID | `C000123456` |
| `$message.current` | 当前用户输入 | `帮我转账500块给张三` |
| `$slot_memory.xxx` | 图节点提取的槽位 | `$slot_memory.amount` → `500` |
| `$task.id` | 任务 ID | `task_xxx` |
| `$intent.code` | 意图代码 | `AG_TRANS` |
| `$intent.name` | 意图名称 | `转账` |
| `$context.recent_messages` | 最近消息列表 | `[{role, content}, ...]` |
| `$context.long_term_memory` | 长期记忆 | `[...]` |
| `$config_variables.xxx` | 前端透传参数 | `$config_variables.custID` → `C000123456` |
| 不带 `$` 的字符串 | 字面量 | `"true"`, `""` |

### 2.3 扩展 config_variables 透传的配置（需代码扩展后）

扩展后 `field_mapping` 可通过 `$config_variables.xxx` 引用前端透传的 `config_variables` 字段：

```json
{
    "field_mapping": {
        "session_id":                            "$session.id",
        "txt":                                   "$message.current",
        "stream":                                "true",

        "config_variables.custID":               "$config_variables.custID",
        "config_variables.sessionID":            "$config_variables.sessionID",
        "config_variables.currentDisplay":       "$config_variables.currentDisplay",
        "config_variables.agentSessionID":       "$config_variables.agentSessionID",

        "config_variables.slots_data.amount":    "$slot_memory.amount",
        "config_variables.slots_data.payee_name":"$slot_memory.payee_name"
    }
}
```

> [!TIP]
> `$config_variables.xxx` 从请求 Router 的 `config_variables` 数组中按 `name` 查找对应的 `value`，原样透传到子智能体的 `config_variables` 中。

---

## 3. Router → 子智能体的请求报文格式

Router 根据 `field_mapping` 组装后，发送到子智能体（如 `http://localhost:8101/api/agent/run`）：

```
POST /api/agent/run
Content-Type: application/json
Accept: text/event-stream
```

```json
{
    "session_id": "session_graph_xxxxx",
    "txt": "帮我转账500块给张三",
    "stream": "true",
    "config_variables": [
        {
            "name": "custID",
            "value": "C000123456"
        },
        {
            "name": "sessionID",
            "value": "SES_20250421_001"
        },
        {
            "name": "currentDisplay",
            "value": "transfer_page"
        },
        {
            "name": "agentSessionID",
            "value": "AGENT_SES_001"
        },
        {
            "name": "slots_data",
            "value": "{\"amount\": \"500\", \"payee_name\": \"张三\"}"
        }
    ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | Router 会话 ID |
| `txt` | string | 用户原始输入 |
| `stream` | string | 是否流式 (`"true"`) |
| `config_variables` | array | 键值对数组，传递上下文参数 |
| `config_variables[].name` | string | 参数名 |
| `config_variables[].value` | string | 参数值（`slots_data` 为 JSON 字符串） |

> [!IMPORTANT]
> `slots_data` 是特殊处理：所有 `config_variables.slots_data.*` 的映射会被合并为一个 JSON 字符串，最终只产生一条 `{"name": "slots_data", "value": "{...}"}` 记录。

---

## 4. 子智能体 → Router 的返回报文格式（SSE）

子智能体以 **Server-Sent Events (SSE)** 流式协议返回，格式为 `additional_kwargs.node_output.output` 嵌套结构：

### 4.1 SSE 事件流

```
event:message
data:{"content": "", "additional_kwargs": {"node_id": "answerDetail", "node_title": "返回去掉answerDetail", "node_output": {"output": "<inner_json>"}}, "response_metadata": {}, "type": "ai", ...}

event:message
data:{"content": "", "additional_kwargs": {"node_id": "end", "node_title": "结束", "node_output": {"output": "<inner_json>"}}, "response_metadata": {}, "type": "ai", ...}

event:done
data:[DONE]
```

### 4.2 SSE data 外层结构

```json
{
    "content": "",
    "additional_kwargs": {
        "node_id": "end",
        "node_title": "结束",
        "node_output": {
            "output": "<inner_json_string>"
        }
    },
    "response_metadata": {},
    "type": "ai",
    "name": null,
    "id": null,
    "example": false,
    "tool_calls": [],
    "invalid_tool_calls": [],
    "usage_metadata": null
}
```

### 4.3 `node_output.output` 内层 JSON 结构（字符串，需二次解析）

```json
{
    "isHandOver": true,
    "handOverReason": "已提供收款人和金额交易对象",
    "data": [
        {
            "isSubAgent": "True",
            "typIntent": "mbpTransfer",
            "answer": "||500|张三|"
        }
    ],
    "slot_memory": {
        "amount": "500",
        "payee_name": "张三"
    },
    "payload": {
        "agent": "transfer_money",
        "amount": "500",
        "ccy": null,
        "payer_card_no": null,
        "payer_card_remark": null,
        "payee_name": "张三",
        "payee_card_no": null,
        "payee_card_remark": null,
        "payee_card_bank": null,
        "payee_phone": null,
        "business_status": "success"
    },
    "status": "completed",
    "event": "final"
}
```

### 4.4 `answer` 字段格式

```
||金额|收款人姓名|
```

示例：`||500|张三|` → 金额=500，收款人=张三

> [!NOTE]
> Router 只关注 **end 节点**的 `node_output`，`answerDetail` 节点的输出会被忽略。

---

## 5. Router → 前端的最终返回报文格式

Router 提取子智能体 end 节点的 `node_output.output` 内容，清理掉内部字段，注入 `intent_code`，返回精简结果：

```json
{
    "ok": true,
    "output": {
        "isHandOver": true,
        "handOverReason": "已提供收款人和金额交易对象",
        "data": [
            {
                "isSubAgent": "True",
                "typIntent": "mbpTransfer",
                "answer": "||500|张三|"
            }
        ],
        "intent_code": "AG_TRANS"
    }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 请求是否成功 |
| `output` | object | 子智能体 end 节点的核心输出 |
| `output.isHandOver` | boolean | 是否完成交接 |
| `output.handOverReason` | string | 交接原因 |
| `output.data` | array | 业务数据 |
| `output.data[].isSubAgent` | string | 是否子智能体 |
| `output.data[].typIntent` | string | 业务意图类型 |
| `output.data[].answer` | string | 竖线分割的业务结果 |
| `output.intent_code` | string | Router 识别到的意图代码 |

> [!IMPORTANT]
> 以下子智能体返回的内部字段在 Router 最终响应中**已被移除**，不会暴露给前端：
> - `slot_memory` — 提槽记忆
> - `payload` — 完整业务载荷
> - `status` — 执行状态
> - `event` — 事件类型

---

## 完整数据流图

```mermaid
sequenceDiagram
    participant FE as 前端/调用方
    participant RT as Router (8000)
    participant AG as Transfer Agent (8101)

    FE->>RT: POST /sessions
    RT-->>FE: { session_id, cust_id }

    FE->>RT: POST /sessions/{id}/messages
    Note right of FE: { content, executionMode,<br>config_variables: [<br>  custID, sessionID,<br>  currentDisplay, agentSessionID ] }

    RT->>RT: 意图识别 → AG_TRANS
    RT->>RT: 图构建 → 提取 slot_memory
    RT->>RT: field_mapping 组装请求

    RT->>AG: POST /api/agent/run
    Note right of RT: { session_id, txt, stream,<br>config_variables: [<br>  custID, sessionID,<br>  currentDisplay, agentSessionID,<br>  slots_data: {amount, payee_name}<br>] }

    AG-->>RT: SSE: event:message (answerDetail)
    AG-->>RT: SSE: event:message (end node)
    Note left of AG: additional_kwargs.node_output.output<br>= { isHandOver, data[].answer,<br>slot_memory, payload, ... }
    AG-->>RT: SSE: event:done [DONE]

    RT->>RT: 提取 end 节点 node_output
    RT->>RT: 注入 intent_code
    RT->>RT: 移除内部字段

    RT-->>FE: { ok: true, output: {<br>  isHandOver, data[].answer,<br>  intent_code } }
```
