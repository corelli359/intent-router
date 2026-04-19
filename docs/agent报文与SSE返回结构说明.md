# Agent 报文与 SSE 返回结构说明

## 1. 目的

本说明以当前 `dynamic-intent-graph-runtime-fix` 分支已经合入的代码为准，整理：

- Router 发给子 agent 的最新请求结构
- 子 agent 返回给 Router 的最新 SSE / JSON 结构
- Router 端当前的解析约束
- 旧结构与新结构的对照关系

这份文档只描述当前实现，不描述历史设计意图。

## 2. Router -> Agent 最新请求结构

### 2.1 标准顶层结构

当前 Router 发给子 agent 的请求体已经收敛为：

```json
{
  "session_id": "session_graph_xxx",
  "txt": "给小红转200",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "cust_demo" },
    { "name": "sessionID", "value": "session_graph_xxx" },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "session_graph_xxx" },
    { "name": "slots_data", "value": "{\"payee_name\":\"小红\",\"amount\":\"200\"}" }
  ]
}
```

顶层字段语义：

- `session_id`
  Router 当前 session id。
- `txt`
  当前轮真正下发给 agent 的文本。
- `stream`
  Router 当前固定下发为 `true`。
- `config_variables`
  变量数组，格式统一为 `[{name, value}]`。

### 2.2 `config_variables` 当前格式

每个元素都必须是：

```json
{
  "name": "变量名",
  "value": "字符串值"
}
```

当前实现里，`value` 最终都会被整理成字符串：

- 如果源值是普通标量，直接 `str(value)`
- 如果源值是 `dict`，会先 `json.dumps(..., ensure_ascii=False)`

### 2.3 `slots_data`

`slots_data` 是当前最关键的保留变量，格式为 JSON 字符串：

```json
{
  "name": "slots_data",
  "value": "{\"payee_name\":\"小红\",\"amount\":\"200\"}"
}
```

约束如下：

- `slots_data.value` 必须是 JSON 字符串，不是对象
- 里面保存的是 Router 当前已形成的 `slot_memory`
- key 使用标准 `slot_key`
- 当前实现不会自动保留 `null` 槽位

## 3. Catalog 中如何声明到 Agent 的映射

### 3.1 当前可用 target path

当前 `field_mapping` 已经不再主要用于组装业务嵌套对象，而是用于写入标准 envelope。

常见 target path 如下：

```json
{
  "session_id": "$session.id",
  "txt": "$message.current",
  "stream": "true",
  "config_variables.custID": "$session.cust_id",
  "config_variables.sessionID": "$session.id",
  "config_variables.currentDisplay": "",
  "config_variables.agentSessionID": "$session.id",
  "config_variables.slots_data.amount": "$slot_memory.amount",
  "config_variables.slots_data.payee_name": "$slot_memory.payee_name"
}
```

当前含义：

- `session_id`
  写入顶层 `session_id`
- `txt`
  写入顶层 `txt`
- `stream`
  写入顶层 `stream`
- `config_variables.<name>`
  追加一个 `{name, value}` 变量
- `config_variables.slots_data.<slot_key>`
  进入 `slots_data` 这份 JSON 字符串内部

### 3.2 当前默认组包行为

如果某个 intent 没有显式 `field_mapping`，当前 Router 默认也会生成新结构：

```json
{
  "session_id": "...",
  "txt": "...",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "..." },
    { "name": "sessionID", "value": "..." },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "..." }
  ]
}
```

如果当前 task 已有 `slot_memory`，还会自动追加：

```json
{
  "name": "slots_data",
  "value": "{\"...\":\"...\"}"
}
```

## 4. Agent 侧当前请求基类

多个 agent 现在都改成继承 `ConfigVariablesRequest`。

当前结构是：

```python
class ConfigVariablesRequest(BaseModel):
    session_id: str = Field(alias="session_id")
    txt: str = ""
    stream: bool = True
    config_variables: list[dict[str, str]] = Field(default_factory=list)
```

并提供两个基础方法：

### 4.1 `get_config_value(name, default="")`

从 `config_variables` 中按变量名取值。

### 4.2 `get_slots_data()`

从 `config_variables` 中取出 `slots_data`，并做 JSON 反序列化。

返回示例：

```json
{
  "payee_name": "小红",
  "amount": "200"
}
```

## 5. Agent -> Router 最新返回结构

### 5.1 推荐返回 payload

当前 Router 最容易稳定消费的是这种结构：

```json
{
  "event": "final",
  "content": "已向小红转账 200 CNY，转账成功",
  "ishandover": true,
  "status": "completed",
  "slot_memory": {
    "payee_name": "小红",
    "amount": "200"
  },
  "payload": {
    "business_status": "success"
  }
}
```

字段语义：

- `event`
  一般是 `message` 或 `final`
- `content`
  给 Router / 用户显示的文本
- `ishandover`
  是否已交回 Router
- `status`
  当前任务状态
- `slot_memory`
  agent 侧确认后的槽位
- `payload`
  业务补充返回

### 5.2 当前 agent 常用 SSE 包装

当前多个 agent 的 `handle_stream()` 实现会按下面形式输出：

```text
event:message
data:{"event":"final","content":"...","ishandover":true,"status":"completed","slot_memory":{},"payload":{}}

event:done
data:[DONE]
```

也就是：

- 第一帧 `event:message`
- `data:` 后面是业务 JSON
- 结束时补一帧 `event:done` + `[DONE]`

## 6. Router 当前支持的返回解析格式

Router 的 `StreamingAgentClient` 当前支持以下几类返回：

### 6.1 普通 JSON

如果 `content-type` 是普通 `application/json`，支持：

- 单个对象
- 对象中带 `events` 数组
- 直接返回数组

### 6.2 SSE / NDJSON

如果是流式返回，Router 会逐行消费：

- 支持 `data:...`
- 空行视为一个 SSE frame 结束
- `[DONE]` 会被忽略

### 6.3 当前兼容的字段别名

Router 现在已经兼容：

- `ishandover`
- `isHandOver`

### 6.4 当前兼容的 content 提取位置

Router 当前会按优先级取内容：

1. `content`
2. `message`
3. `data[0].answer`

### 6.5 当前兼容的嵌套输出格式

Router 还支持从如下结构里抽出真正 payload：

```json
{
  "additional_kwargs": {
    "node_output": {
      "output": "{\"event\":\"final\",\"content\":\"...\"}"
    }
  }
}
```

也就是当前兼容：

- `additional_kwargs.node_output.output`
- 其中 `output` 必须是一个 JSON 字符串

### 6.6 `slot_memory` 合并规则

如果 agent 返回了：

```json
{
  "slot_memory": {
    "amount": "200"
  }
}
```

Router 会把它 merge 到当前 `task.slot_memory`。

## 7. Router 当前的状态解析约束

### 7.1 `status`

当前 Router 能直接识别的 canonical 状态值：

- `waiting_user_input`
- `waiting_confirmation`
- `completed`
- `failed`

### 7.2 没有 `status` 时

Router 会退化为：

- `ishandover == false`
  视为 `waiting_user_input`
- 否则
  视为 `completed`

### 7.3 多事件 SSE 的消费方式

当前 Router 已改成：

- 不在收到第一个终态 chunk 时立刻 `break`
- 而是继续把整条 SSE 流消费完

原因是：

- 某些 legacy agent 会在一条流里发多个终态样式事件
- 如果 Router 过早 `break`，后续 frame 会被直接丢掉

## 8. 当前约束清单

### 8.1 对 Catalog 的约束

- `request_schema` 现在需要和新报文结构对齐
- 常见 required 至少包括：
  - `session_id`
  - `txt`
- 如果使用 `config_variables`，`field_mapping` 应使用：
  - `config_variables.<name>`
  - `config_variables.slots_data.<slot_key>`

### 8.2 对 Agent 请求的约束

- 必须能接收：
  - `session_id`
  - `txt`
  - `stream`
  - `config_variables`
- `config_variables[].value` 按当前实现应视为字符串
- `slots_data` 需要 agent 自己反序列化

### 8.3 对 Agent 返回的约束

- 最稳妥的业务 JSON 应包含：
  - `event`
  - `content`
  - `ishandover`
  - `status`
- 若要把补槽结果回写 Router，应返回 `slot_memory`
- 若只返回 SSE 包装而没有内层业务 JSON，Router 很难做稳定状态投影

## 9. 旧结构 vs 新结构对照

### 9.1 Router -> Agent

旧结构：

```json
{
  "sessionId": "...",
  "taskId": "...",
  "intentCode": "...",
  "input": "...",
  "intent": {},
  "context": {
    "recentMessages": [],
    "longTermMemory": []
  },
  "slots": {}
}
```

新结构：

```json
{
  "session_id": "...",
  "txt": "...",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "..." },
    { "name": "sessionID", "value": "..." },
    { "name": "slots_data", "value": "{\"amount\":\"200\"}" }
  ]
}
```

对照关系：

- `sessionId` -> `session_id`
- `input` -> `txt`
- `slots` -> `config_variables.slots_data`
- 旧的 `intent/context` 不再默认作为顶层对象下发

### 9.2 Agent -> Router

旧结构常见是：

- 单个 JSON
- 非严格 SSE
- 只返回 `message`
- 或返回 `data[0].answer`

新结构推荐是：

```json
{
  "event": "message|final",
  "content": "...",
  "ishandover": false,
  "status": "waiting_user_input|completed|failed",
  "slot_memory": {},
  "payload": {}
}
```

并通过 SSE 封装为：

```text
event:message
data:{...}

event:done
data:[DONE]
```

## 10. 建议

基于当前已经合入的实现，后续建议统一执行三条规范：

1. 所有新 agent 一律接 `ConfigVariablesRequest`
2. 所有需要 Router 回写槽位的 agent 一律返回 `slot_memory`
3. 所有新 agent 一律用标准 SSE：
   - `event:message`
   - `data:{业务 JSON}`
   - `event:done`
   - `data:[DONE]`

这样 Router 和业务 agent 的协议边界会更稳定，后续做兼容和演进也更容易。
