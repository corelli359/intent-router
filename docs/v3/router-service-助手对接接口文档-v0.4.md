# Router-Service 助手对接接口文档 v0.4

> 状态：当前生效版本
> 日期：2026-04-23
> 适用对象：助手服务开发 / 联调人员 / 测试人员
> 替代版本：`v0.3`

## 1. 文档范围

本文只覆盖助手真正需要对接的两个接口：

1. `POST /api/v1/message`
2. `POST /api/v1/task/completion`

不覆盖以下内容：

1. Router 内部 graph / snapshot 结构
2. Router 对前端调试接口
3. Agent 下游内部协议细节
4. 历史草案和临时兼容字段

当前版本的核心原则只有四条：

1. 助手侧主入口固定为 `POST /api/v1/message`
2. 助手补完成态固定为 `POST /api/v1/task/completion`
3. 助手协议中不返回 `snapshot`
4. Router 字段与 Agent 原始 `output` 严格分层

---

## 2. v0.4 相比 v0.3 的变化

v0.3 的主要问题是：Router 自己生成的字段和 Agent 返回的业务字段混在同一个 `output` 对象里，助手侧很难判断字段归属，也不利于后续演进。

v0.4 明确改为：

1. **Router 自己生成的字段全部放在响应顶层**
2. **Agent 原始业务块统一放在顶层 `output` 字段里**
3. **如果当前没有 Agent 输出，则固定返回 `"output": {}`**
4. **顶层 `message` 是 Router 语义消息，不是 Agent 回答内容**
5. **顶层 `completion_state` / `completion_reason` 是 Router 汇总后的完成态**
6. **`output.completion_state` / `output.completion_reason` 如果存在，代表 Agent 原始信号**
7. **`slot_memory` 只放顶层，不放进 `output`**

也就是说，v0.4 的结构不是：

```json
{
  "ok": true,
  "output": {
    "status": "...",
    "slot_memory": {},
    "message": "...",
    "data": []
  }
}
```

而是：

```json
{
  "ok": true,
  "current_task": "...",
  "task_list": [],
  "status": "...",
  "intent_code": "...",
  "completion_state": 0,
  "completion_reason": "...",
  "slot_memory": {},
  "message": "...",
  "output": {}
}
```

---

## 3. 字段归属约定

### 3.1 Router 顶层字段

以下字段由 Router 负责生成和维护，必须出现在响应顶层：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 本次请求是否成功收口 |
| `current_task` | string | 当前任务标识 |
| `task_list` | array | 当前 session 下任务概览 |
| `status` | string | 当前任务状态 |
| `intent_code` | string | 当前任务对应意图 |
| `completion_state` | int | Router 汇总完成态：`0/1/2` |
| `completion_reason` | string | 汇总完成态原因 |
| `slot_memory` | object | Router 当前已确认的槽位 |
| `message` | string | Router 语义消息 |
| `errorCode` | string，可选 | Router 错误码 |
| `stage` | string，可选 | 语义模型失败阶段 |
| `details` | object，可选 | 错误细节 |
| `output` | object | Agent 原始输出块；无则 `{}` |

### 3.2 顶层 `message` 的语义

顶层 `message` 是 Router 给上游的统一语义消息，例如：

1. `请提供金额`
2. `执行图等待助手确认完成态`
3. `执行图已完成`
4. `意图识别服务暂不可用，请稍后重试。`

**顶层 `message` 不承担承载 Agent 回答正文的职责。**

如果 Agent 有自己的文本输出，例如节点文案、业务回执、流式中间结果，应放在：

1. `output.message`
2. `output.content`
3. `output.data`

具体保留什么，取决于 Agent 自己返回了什么。

### 3.3 顶层 `output` 的语义

顶层 `output` 只承载 **Agent 原始 `node_output.output` 业务块**。

规则如下：

1. Router 不再把自己的 `slot_memory` 塞进 `output`
2. Router 不再把自己的汇总 `completion_state` 覆盖到 `output`
3. 如果 Agent 原始块里本来就有 `completion_state` / `completion_reason`，可以原样保留在 `output`
4. 如果当前轮没有 Agent 输出，固定返回 `output: {}`
5. Router 不返回 `snapshot`

---

## 4. 会话约束

### 4.1 `sessionId` 必须稳定透传

助手侧需要保证：

1. 同一个用户会话，多轮都使用同一个 `sessionId`
2. 新用户会话，生成新的 `sessionId`

Router 会基于这个 `sessionId` 复用短期记忆，包括：

1. 最近消息
2. 当前业务对象状态
3. 当前业务的 `slot_memory`
4. session 级共享槽位

### 4.2 为什么重要

例如两轮转账：

1. 第一轮：`给小明转账`
2. 第二轮：`200`

只有两轮使用同一个 `sessionId`，第二轮才能复用第一轮已识别出的 `payee_name=小明`。

---

## 5. 主接口：`POST /api/v1/message`

## 5.1 用途

助手把当前用户输入转发给 Router，由 Router 完成：

1. 意图识别
2. 图调度
3. 槽位抽取
4. 缺槽追问
5. 必要时调度 Agent
6. 统一结果回传助手

---

## 5.2 请求头

非流：

```http
POST /api/v1/message
Content-Type: application/json
```

流式：

```http
POST /api/v1/message
Content-Type: application/json
Accept: text/event-stream
```

---

## 5.3 请求体

```json
{
  "sessionId": "assistant_session_001",
  "txt": "给小明转账",
  "custId": "C0001",
  "executionMode": "execute",
  "stream": false,
  "config_variables": [
    { "name": "custID", "value": "C0001" },
    { "name": "sessionID", "value": "assistant_session_001" },
    { "name": "currentDisplay", "value": "transfer_page" },
    { "name": "agentSessionID", "value": "assistant_session_001" }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sessionId` | string | 是 | 会话主键 |
| `txt` | string | 是 | 当前轮用户输入 |
| `custId` | string | 否 | 客户标识 |
| `executionMode` | string | 否 | `execute` 或 `router_only`，默认 `execute` |
| `stream` | boolean | 否 | 是否使用 SSE，默认 `true` |
| `config_variables` | array | 否 | 透传上下文字段 |

### `config_variables` 说明

每项固定格式：

```json
{
  "name": "xxx",
  "value": "yyy"
}
```

当前建议透传：

| name | 说明 |
|---|---|
| `custID` | 客户标识 |
| `sessionID` | 业务会话标识 |
| `currentDisplay` | 当前页面 |
| `agentSessionID` | 下游 Agent 会话标识 |
| `slots_data` | 可选，结构化槽位提示，值为 JSON 字符串 |

---

## 5.4 非流响应

### 统一结构

```json
{
  "ok": true,
  "current_task": "AG_TRANS#0",
  "task_list": [
    { "name": "AG_TRANS#0", "status": "waiting" }
  ],
  "status": "waiting_user_input",
  "intent_code": "AG_TRANS",
  "completion_state": 0,
  "completion_reason": "router_waiting_user_input",
  "slot_memory": {
    "payee_name": "小明"
  },
  "message": "请提供金额",
  "output": {}
}
```

### 顶层字段含义

| 字段 | 说明 |
|---|---|
| `ok` | 请求是否成功 |
| `current_task` | 当前任务标识，后续完成态回调也用它 |
| `task_list` | 当前会话中各任务概览 |
| `status` | 当前任务状态 |
| `intent_code` | 当前主任务意图 |
| `completion_state` | Router 汇总完成态 |
| `completion_reason` | 汇总完成态原因 |
| `slot_memory` | Router 当前已确认槽位 |
| `message` | Router 给上游的统一语义消息 |
| `output` | Agent 原始输出块；无则 `{}` |

---

## 5.5 典型响应示例

### 场景 A：缺金额，等待用户补槽

```json
{
  "ok": true,
  "current_task": "AG_TRANS#0",
  "task_list": [
    { "name": "AG_TRANS#0", "status": "waiting" }
  ],
  "status": "waiting_user_input",
  "intent_code": "AG_TRANS",
  "completion_state": 0,
  "completion_reason": "router_waiting_user_input",
  "slot_memory": {
    "payee_name": "小明"
  },
  "message": "请提供金额",
  "output": {}
}
```

说明：

1. 任务还没结束
2. 当前没有 Agent 输出
3. `output` 必须是空对象，不是缺省字段

### 场景 B：Agent 已处理完自己的侧，但还等待助手补完成态

```json
{
  "ok": true,
  "current_task": "task_123",
  "task_list": [
    { "name": "task_123", "status": "waiting" }
  ],
  "status": "waiting_assistant_completion",
  "intent_code": "AG_TRANS",
  "completion_state": 1,
  "completion_reason": "agent_partial_done",
  "slot_memory": {
    "payee_name": "小明",
    "amount": "200"
  },
  "message": "执行图等待助手确认完成态",
  "output": {
    "message": "已受理向小明转账 200 CNY，等待助手确认完成态",
    "ishandover": true,
    "handOverReason": "等待助手确认完成态",
    "completion_state": 1,
    "data": [
      {
        "isSubAgent": "True",
        "typIntent": "mbpTransfer",
        "answer": "||200|小明|"
      }
    ],
    "payload": {
      "agent": "transfer_money",
      "business_status": "accepted"
    }
  }
}
```

说明：

1. 顶层 `completion_state=1` 是 Router 汇总结果
2. `output.completion_state=1` 是 Agent 原始信号
3. 顶层 `message` 与 `output.message` 语义不同

### 场景 C：任务最终完成

```json
{
  "ok": true,
  "current_task": "task_123",
  "task_list": [
    { "name": "task_123", "status": "completed" }
  ],
  "status": "completed",
  "intent_code": "AG_TRANS",
  "completion_state": 2,
  "completion_reason": "joint_done",
  "slot_memory": {
    "payee_name": "小明",
    "amount": "200"
  },
  "message": "执行图已完成",
  "output": {
    "message": "已受理向小明转账 200 CNY，等待助手确认完成态",
    "ishandover": true,
    "handOverReason": "等待助手确认完成态",
    "completion_state": 1,
    "data": [
      {
        "isSubAgent": "True",
        "typIntent": "mbpTransfer",
        "answer": "||200|小明|"
      }
    ],
    "payload": {
      "agent": "transfer_money",
      "business_status": "accepted"
    }
  }
}
```

说明：

1. 顶层 `completion_state=2` 表示 Router 最终认定完成
2. `output` 仍然保留 Agent 原始输出，不会被 Router 汇总值覆盖

### 场景 D：Router 错误

```json
{
  "ok": false,
  "current_task": "",
  "task_list": [],
  "status": "failed",
  "intent_code": "",
  "completion_state": 2,
  "completion_reason": "router_error",
  "slot_memory": {},
  "message": "意图识别服务暂不可用，请稍后重试。",
  "errorCode": "ROUTER_LLM_UNAVAILABLE",
  "stage": "intent_recognition",
  "details": {
    "error_type": "ConnectError"
  },
  "output": {}
}
```

---

## 5.6 SSE 响应

当 `stream=true` 时，Router 返回 `text/event-stream`。

约束如下：

1. 每个 `event: message` 的 `data` 都是 **与非流同构的 v0.4 业务对象**
2. 结束帧固定为：

```text
event: done
data: [DONE]
```

### SSE 示例

```text
event: message
data: {"ok":true,"current_task":"task_123","task_list":[{"name":"task_123","status":"waiting"}],"status":"running","intent_code":"AG_TRANS","completion_state":0,"completion_reason":"running","slot_memory":{"payee_name":"小明","amount":"200"},"message":"","output":{"node_id":"validate_payee","message":"收款人校验通过","completion_state":0,"data":[{"answer":"收款人校验通过"}]}}

event: message
data: {"ok":true,"current_task":"task_123","task_list":[{"name":"task_123","status":"completed"}],"status":"completed","intent_code":"AG_TRANS","completion_state":2,"completion_reason":"agent_final_done","slot_memory":{"payee_name":"小明","amount":"200"},"message":"执行图已完成","output":{"node_id":"execute_transfer","message":"已向小明转账 200 CNY，转账成功","completion_state":2,"completion_reason":"agent_final_done","ishandover":true,"data":[{"isSubAgent":"True","typIntent":"mbpTransfer","answer":"||200|小明|"}]}}

event: done
data: [DONE]
```

注意：

1. 顶层 `message` 是 Router 消息
2. 业务流式节点文案在 `output.message`

---

## 6. 完成态回调：`POST /api/v1/task/completion`

## 6.1 用途

助手在合适时机把“助手侧完成信号”补回给 Router，用于和 Agent 信号汇总成最终完成态。

## 6.2 请求体

```json
{
  "sessionId": "assistant_session_001",
  "taskId": "task_123",
  "completionSignal": 1
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sessionId` | string | 是 | 会话标识 |
| `taskId` | string | 是 | 任务标识，取自 `/api/v1/message` 的 `current_task` |
| `completionSignal` | int | 是 | 助手侧完成信号，只允许 `1` 或 `2` |

## 6.3 响应

返回体与 `/api/v1/message` 保持同构，也是 v0.4 顶层结构。

---

## 7. 完成态汇总规则

Router 的最终完成态采用统一汇总语义：

| 值 | 含义 |
|---|---|
| `0` | 两侧都未完成 |
| `1` | 只有一侧完成 |
| `2` | Router 认定任务最终完成 |

达到 `2` 的典型情况有三类：

1. Agent 直接给 `2`
2. 助手直接给 `2`
3. Agent 给 `1`，助手也给 `1`，汇总后达到 `2`

因此：

1. **不能只看 `output.completion_state`**
2. **必须以顶层 `completion_state` 作为最终完成态判断依据**

---

## 8. 实施约束

### 8.1 助手侧必须遵守

1. 不依赖 `snapshot`
2. 不把顶层 `message` 当作 Agent 原始回答
3. 任务最终完成，以顶层 `completion_state` 判断
4. 需要回调完成态时，使用顶层 `current_task`

### 8.2 Router 侧必须遵守

1. 不返回 `snapshot`
2. `output` 只承载 Agent 原始输出块
3. 没有 Agent 输出时，`output` 固定为 `{}`
4. 不把 `slot_memory` 写进 `output`
5. 不把汇总完成态覆盖到 `output`

---

## 9. v0.4 对齐结论

当前联调时，助手只需要记住下面这组最小结论：

1. 发请求：`POST /api/v1/message`
2. 补完成态：`POST /api/v1/task/completion`
3. 看最终状态：顶层 `completion_state`
4. 读当前槽位：顶层 `slot_memory`
5. 读 Agent 原始业务块：顶层 `output`
6. 不看 `snapshot`
