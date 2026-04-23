# Router-Service 助手对接接口文档 v0.3

> 状态：当前联调版本
> 日期：2026-04-23
> 适用对象：助手服务开发 / 联调人员
> 目标：让助手侧按固定接口直接对接 Router，不需要自行理解 Router 内部实现

## 1. 文档范围

这份文档只覆盖助手侧真正要调用的 Router 接口：

1. `POST /api/v1/message`
2. `POST /api/v1/task/completion`

不覆盖以下内容：

1. Router 内部 graph/snapshot 结构
2. Router 直连前端调试接口
3. Agent 下游内部协议细节
4. 历史讨论版协议草案

结论先说清楚：

1. **助手侧主入口就是 `/api/v1/message`**
2. **任务完成态回调入口就是 `/api/v1/task/completion`**
3. **助手侧不需要使用 snapshot 接口**
4. **同一轮多次对话必须持续使用同一个 `sessionId`**

---

## 2. 接口总览

| 场景 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 助手转发用户输入到 Router | `POST` | `/api/v1/message` | 主链路，支持非流和 SSE |
| 助手补任务完成态 | `POST` | `/api/v1/task/completion` | 任务级完成态回调 |

统一约束：

1. 上下游对接都使用 JSON 请求体；
2. `/api/v1/message` 的非流响应为 `ok + output`；
3. `/api/v1/message` 的 SSE 响应中，每个 `event: message` 的 `data` 与非流 `output` 同构；
4. `/api/v1/task/completion` 响应也为 `ok + output`；
5. 助手侧消费时，不要依赖 `snapshot` 字段。

---

## 3. 会话约束

### 3.1 `sessionId` 是助手侧必须稳定透传的主键

助手侧只需要保证：

1. 同一个用户会话，多轮请求持续使用同一个 `sessionId`
2. 新用户会话，生成新的 `sessionId`

当前 Router 的行为是：

1. 如果该 `sessionId` 首次出现，则创建并绑定运行时 session
2. 如果该 `sessionId` 已存在，则在原 session 上继续处理

所以，**助手侧不需要先额外调用“创建 session”接口再调用主链路**。  
对助手联调来说，直接调用 `/api/v1/message` 即可。

### 3.2 为什么这点重要

例如两轮场景：

1. 第一轮：`给小明转账`
2. 第二轮：`200`

只有两轮都使用同一个 `sessionId`，Router 才能复用第一轮沉淀下来的短期记忆，例如：

1. 最近消息 `recent_messages`
2. waiting node 上的 `slot_memory`
3. session 级共享槽位

如果第二轮换了 `sessionId`，Router 会把它当成新会话处理，之前的短期记忆无法继续使用。

---

## 4. 主接口：`POST /api/v1/message`

## 4.1 用途

助手把用户当前输入转发给 Router，由 Router 完成：

1. 意图识别
2. 图规划
3. 提槽
4. 必要时追问
5. 槽位齐全后路由到下游 Agent
6. 将统一结果返回给助手

---

## 4.2 请求头

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

## 4.3 请求体

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
| `sessionId` | string | 是 | 会话标识；同一会话多轮必须保持不变 |
| `txt` | string | 是 | 当前轮用户输入原文；不能为空 |
| `custId` | string | 否 | 客户标识；如无可暂不传 |
| `executionMode` | string | 否 | `execute` 或 `router_only`，默认 `execute` |
| `stream` | boolean | 是 | `false` 返回 JSON，`true` 返回 SSE |
| `config_variables` | array | 否 | 透传字段数组 |

### `config_variables` 约束

每项格式固定为：

```json
{
  "name": "xxx",
  "value": "yyy"
}
```

当前建议透传这些字段：

| name | 说明 |
|---|---|
| `custID` | 业务客户标识 |
| `sessionID` | 业务会话标识 |
| `currentDisplay` | 当前页面或展示上下文 |
| `agentSessionID` | 透传给下游 Agent 的会话标识 |
| `slots_data` | 可选，结构化槽位提示，值为 JSON 字符串 |

### `slots_data` 规则

`slots_data` 是特例：

1. 助手可以不传
2. 助手传了也只作为提示
3. Router 发给下游 Agent 时，会基于当前真实 `slot_memory` 统一收敛
4. Router 发给下游 Agent 时最终只保留一条 `slots_data`

也就是说：

**除了 `slots_data` 之外，其他 `config_variables` 都可以理解为纯透传字段。**

---

## 4.4 非流响应

### 响应体格式

```json
{
  "ok": true,
  "output": {
    "current_task": "task_123",
    "task_list": [
      { "name": "task_123", "status": "waiting" }
    ],
    "completion_state": 0,
    "completion_reason": "router_waiting_user_input",
    "node_id": "slot_collect",
    "intent_code": "AG_TRANS",
    "status": "waiting_user_input",
    "isHandOver": false,
    "handOverReason": "waiting_user_input",
    "message": "请提供金额",
    "data": [],
    "slot_memory": {
      "payee_name": "小明"
    }
  }
}
```

### 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 请求是否成功收口 |
| `output` | object | 统一业务输出 |

### `output` 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `current_task` | string | 当前任务标识 |
| `task_list` | array | 当前 session 下任务列表概览 |
| `completion_state` | int | 当前任务完成态：`0/1/2` |
| `completion_reason` | string | 当前完成态原因 |
| `node_id` | string | 当前节点标识 |
| `intent_code` | string | 当前任务对应意图 |
| `status` | string | 当前 Router 任务状态 |
| `isHandOver` | boolean | 是否交接控制权 |
| `handOverReason` | string | 交接原因 |
| `message` | string | 返回给助手展示或继续处理的消息 |
| `data` | array | 下游 Agent 原始业务载荷或 Router 归一载荷 |
| `slot_memory` | object | 当前已识别到的槽位结果 |

### 关键语义

#### 1. `snapshot` 不会出现在助手协议返回里

助手侧代码不要再兼容 `snapshot`。

#### 2. `completion_state` 语义

| 值 | 含义 |
|---|---|
| `0` | 当前任务未结束 |
| `1` | 某一侧结束，仍等待另一侧 |
| `2` | Router 已认定当前任务最终结束 |

#### 3. `isHandOver` 不等于任务结束

不能仅凭 `isHandOver=true` 判定任务已完成。  
任务是否真正结束，以 `completion_state` 为准。

#### 4. `current_task` 是后续完成态回调的定位主键

如果助手后续需要补任务完成态，应把 `current_task` 原样作为 `taskId` 传回 Router。

#### 5. `status=waiting_assistant_completion` 的语义

当下游 Agent 返回：

1. 已完成自己的处理
2. 但仅给出 `completion_state=1`

那么 Router 会把当前任务收敛成：

- `status=waiting_assistant_completion`
- `completion_state=1`
- `completion_reason=agent_partial_done`

这时说明：

1. Agent 已经单侧完成
2. 任务还不能算最终完成
3. 助手后续还需要调用 `/api/v1/task/completion`

这一点非常关键：

**Agent 完成不等于任务最终完成。**

---

## 4.5 SSE 响应

当请求体中 `stream=true` 时，Router 返回 SSE。

### 事件类型

当前固定两类：

1. `event: message`
2. `event: done`

### SSE 示例

```text
event: message
data: {"current_task":"task_123","task_list":[{"name":"task_123","status":"waiting"}],"completion_state":0,"completion_reason":"router_waiting_user_input","node_id":"slot_collect","intent_code":"AG_TRANS","status":"waiting_user_input","isHandOver":false,"handOverReason":"waiting_user_input","message":"请提供金额","data":[],"slot_memory":{"payee_name":"小明"}}

event: done
data: [DONE]
```

### SSE 消费约束

1. 每个 `event: message` 的 `data`，字段结构与非流 `output` 同构
2. 助手侧可以复用同一套字段解析逻辑
3. 读到 `event: done` 或 `data: [DONE]` 后结束本次流消费

也就是说：

**非流看 `response.output`，流式看每帧 `data`，两者业务字段是同一套。**

---

## 4.6 单轮与多轮示例

### 示例一：单轮追问

请求：

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

期望理解：

1. 识别意图：`AG_TRANS`
2. 提到槽位：`payee_name=小明`
3. 缺少 `amount`
4. Router 追问金额

典型返回：

```json
{
  "ok": true,
  "output": {
    "current_task": "task_123",
    "task_list": [
      { "name": "task_123", "status": "waiting" }
    ],
    "completion_state": 0,
    "completion_reason": "router_waiting_user_input",
    "node_id": "slot_collect",
    "intent_code": "AG_TRANS",
    "status": "waiting_user_input",
    "isHandOver": false,
    "handOverReason": "waiting_user_input",
    "message": "请提供金额",
    "data": [],
    "slot_memory": {
      "payee_name": "小明"
    }
  }
}
```

### 示例二：第二轮补槽

第二轮必须沿用同一个 `sessionId`：

```json
{
  "sessionId": "assistant_session_001",
  "txt": "200",
  "custId": "C0001",
  "executionMode": "execute",
  "stream": false,
  "config_variables": [
    { "name": "custID", "value": "C0001" },
    { "name": "sessionID", "value": "assistant_session_001" },
    { "name": "currentDisplay", "value": "transfer_confirm_page" },
    { "name": "agentSessionID", "value": "assistant_session_001" }
  ]
}
```

期望理解：

1. Router 从当前 session 里恢复上一轮短期记忆
2. 复用已识别的 `payee_name=小明`
3. 当前轮只补 `amount=200`
4. 槽位齐全后继续调用下游 Agent

当前转账 Agent 的联调约定是：槽位齐全后先给出 Agent 单侧完成信号，等待助手补完成态。典型返回如下：

```json
{
  "ok": true,
  "output": {
    "current_task": "task_123",
    "task_list": [
      { "name": "task_123", "status": "waiting" }
    ],
    "completion_state": 1,
    "completion_reason": "agent_partial_done",
    "node_id": "end",
    "intent_code": "AG_TRANS",
    "status": "waiting_assistant_completion",
    "isHandOver": true,
    "handOverReason": "等待助手确认完成态",
    "message": "已受理向小明转账 200 CNY，等待助手确认完成态",
    "data": [
      {
        "isSubAgent": "True",
        "typIntent": "mbpTransfer",
        "answer": "||200|小明|"
      }
    ],
    "slot_memory": {
      "payee_name": "小明",
      "amount": "200"
    }
  }
}
```

此时助手侧应立刻使用同一个 `sessionId` 和当前返回的 `current_task` 调用 `/api/v1/task/completion`。

### 示例三：Agent 单侧完成，等待助手补完成态

如果下游 Agent 返回 `completion_state=1`，则典型返回会是：

```json
{
  "ok": true,
  "output": {
    "current_task": "task_123",
    "task_list": [
      { "name": "task_123", "status": "waiting" }
    ],
    "completion_state": 1,
    "completion_reason": "agent_partial_done",
    "node_id": "end",
    "intent_code": "AG_TRANS",
    "status": "waiting_assistant_completion",
    "isHandOver": true,
    "handOverReason": "等待助手确认完成态",
    "message": "已受理向小明转账 200 CNY，等待助手确认完成态",
    "data": [
      {
        "isSubAgent": "True",
        "typIntent": "mbpTransfer",
        "answer": "||200|小明|"
      }
    ],
    "slot_memory": {
      "payee_name": "小明",
      "amount": "200"
    }
  }
}
```

此时助手侧应继续调用 `/api/v1/task/completion`，而不是把本任务直接当成最终完成。

---

## 5. 任务完成态回调：`POST /api/v1/task/completion`

## 5.1 用途

当助手侧需要对某个已存在任务补充完成态时，调用该接口。

典型场景：

1. Agent 单侧只给了部分完成信号，助手要补最终完成
2. 助手单侧即可决定当前任务结束

---

## 5.2 请求头

```http
POST /api/v1/task/completion
Content-Type: application/json
```

---

## 5.3 请求体

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
| `sessionId` | string | 是 | 所属会话标识 |
| `taskId` | string | 是 | 要更新的任务标识，必须来自上一轮返回的 `current_task` |
| `completionSignal` | int | 是 | 只允许 `1` 或 `2` |

### `completionSignal` 语义

| 值 | 含义 |
|---|---|
| `1` | 助手单侧完成，但不一定立刻终结任务 |
| `2` | 助手单侧即可直接终结任务 |

说明：

1. 助手侧不需要发送 `0`
2. 该接口只负责推进完成态，不承载其他业务字段

---

## 5.4 完成态收敛规则

Router 内部按“双侧完成信号”收敛当前任务状态：

| Agent 信号 | 助手信号 | Router 输出 | 说明 |
|---|---:|---:|---|
| `0` | `0` | `completion_state=0` | 都未结束 |
| `1` | `0` | `completion_state=1` | Agent 单侧完成，等待助手 |
| `0` | `1` | `completion_state=1` | 助手单侧完成，等待 Agent |
| `1` | `1` | `completion_state=2` | 双侧都完成 |
| `2` | `0/1/2` | `completion_state=2` | Agent 单侧即可最终完成 |
| `0/1` | `2` | `completion_state=2` | 助手单侧即可最终完成 |

关键约束：

1. `completion_state=2` 是不可回退终态；
2. 重复调用 `/api/v1/task/completion` 不应把任务重复推进；
3. `node_id=end` 不等于任务完成；
4. `isHandOver=true` 不等于任务完成；
5. 是否需要助手补完成态，只看 `completion_state=1` 以及 `current_task` 是否有效。

推荐助手侧伪代码：

```python
output = call_router_message(...)

if output["completion_state"] == 1:
    call_router_task_completion(
        sessionId=session_id,
        taskId=output["current_task"],
        completionSignal=1,
    )
```

---

## 5.5 响应体

响应仍然是统一结构：

```json
{
  "ok": true,
  "output": {
    "current_task": "task_123",
    "task_list": [
      { "name": "task_123", "status": "completed" }
    ],
    "completion_state": 2,
    "completion_reason": "joint_done",
    "node_id": "end",
    "intent_code": "AG_TRANS",
    "status": "completed",
    "isHandOver": true,
    "handOverReason": "等待助手确认完成态",
    "message": "执行图已完成",
    "data": [
      {
        "isSubAgent": "True",
        "typIntent": "mbpTransfer",
        "answer": "||200|小明|"
      }
    ],
    "slot_memory": {
      "payee_name": "小明",
      "amount": "200"
    }
  }
}
```

说明：

1. 返回的不是简单回显请求体
2. 返回的是 Router 当前统一收敛后的任务态结果
3. 助手收到后可以继续按和 `/api/v1/message` 相同的 `output` 字段结构处理
4. `handOverReason`、`data`、`slot_memory` 会尽量保留任务收口前的业务上下文，助手侧不应假设这些字段会被清空

---

## 5.6 常见错误

| HTTP | 含义 |
|---|---|
| `400` | 请求体不合法，例如 `completionSignal` 超出允许值 |
| `404` | `sessionId` 不存在，或 `taskId` 在该 session 下不存在 |

---

## 6. 助手侧实现建议

## 6.1 最小实现步骤

助手侧最小实现只需要做到：

1. 生成并维护稳定的 `sessionId`
2. 把用户原话写入 `txt`
3. 调用 `/api/v1/message`
4. 解析 `ok + output` 或 SSE `event: message`
5. 如需补任务完成态，再调用 `/api/v1/task/completion`

---

## 6.2 最重要的几个消费规则

### 规则一：同一会话持续复用 `sessionId`

这是多轮追问、短期记忆、补槽能够成立的前提。

### 规则二：不要依赖 `isHandOver` 直接判定结束

任务是否真正结束，以 `completion_state` 为准。

### 规则三：要保存 `current_task`

后续如果需要补完成态，`taskId` 就来自这里。

### 规则四：SSE 与非流用同一套业务字段解析

非流：

1. 解析 `response.output`

流式：

1. 解析每个 `event: message` 的 `data`

两者字段结构是一致的。

### 规则五：不要期待 `snapshot`

助手对接协议里不需要 `snapshot`。

---

## 7. 联调建议

给助手同学联调时，建议直接使用仓库中的这个脚本：

`scripts/demo_router_assistant_api.py`

它的作用是：

1. 直接模拟助手调用 Router
2. 使用真实助手协议字段
3. 两轮演示 `给小明转账` -> `200`
4. 直观看 Router 如何复用短期记忆

如果要单独演示完成态汇报接口，再使用：

`scripts/demo_router_task_completion_api.py`

该脚本当前默认会直接直连真实 Router，并演示两种链路：

1. `agent 1 + assistant 1 => 2`
2. `agent 1 + assistant 2 => 2`

如果要证明上下文缓存和 session 级短期记忆，再使用：

`scripts/demo_router_context_cache_api.py`

该脚本会直连真实 Router，串联：

1. 第一轮 `给小明转账`，证明 Router 在当前任务中记住 `payee_name=小明`
2. 第二轮只输入 `200`，证明 Router 不依赖上游 `slots_data` 也能复用上一轮槽位
3. 必要时调用 `/api/v1/task/completion` 收口任务
4. 读取开发调试快照 `GET /api/router/v2/sessions/{sessionId}`，证明 `current_graph/pending_graph` 已释放，`shared_slot_memory` 仍保留 `payee_name` 和 `amount`

注意：第 4 步只是开发侧证明短期记忆，不是助手生产对接必须调用的接口。

---

## 8. 最终对齐结论

给助手侧的实现口径，最终收敛成下面四句话：

1. 主接口调 `POST /api/v1/message`
2. 完成态回调调 `POST /api/v1/task/completion`
3. 同一会话必须稳定透传同一个 `sessionId`
4. 非流和 SSE 按同一套 `output` 结构解析
