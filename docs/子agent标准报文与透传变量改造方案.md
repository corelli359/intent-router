# 子agent标准报文与透传变量改造方案

## 1. 目标

当前 Router 到子 agent 的请求体主要依赖 `field_mapping` 动态组装。  
这种方式虽然灵活，但会把三类信息混在一起：

- 会话和控制变量
- 用户原始输入
- Router 提取出的槽位

真实业务接入时，子 agent 更适合消费固定标准报文，而不是继续理解 Router 内部的嵌套 payload 结构。  
本方案目标是：

1. 子 agent 报文统一收敛为固定 envelope。
2. 槽位结果统一进入 `slots_data`。
3. 业务侧传来的其他变量统一作为 `config_variables` 原样透传。
4. Router 仍然保留现有意图识别、提槽、追问逻辑，不重写识别主链路。
5. 首期采用最小化改造，优先支持 `AG_TRANS`。

## 2. 标准报文

子 agent 报文统一采用如下格式：

```json
{
  "session_id": "1635501196813426",
  "txt": "给陈广荣转账500元",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "1631102265490929" },
    { "name": "sessionID", "value": "1635501196813426" },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "1635501196813426" },
    { "name": "slots_data", "value": "{\"payee_name\":\"陈广荣\",\"amount\":\"500\"}" }
  ]
}
```

其中：

- `session_id`
  Router 对子 agent 的本次会话标识。
- `txt`
  当前轮实际用户输入。
- `stream`
  子 agent 是否按流式语义返回。
- `config_variables`
  业务变量数组。
- `slots_data`
  固定作为保留变量名，承载 Router 当前已确认的槽位 JSON 字符串。

## 3. 边界拆分

改造后需要把三层边界拆清楚：

### 3.1 `slot_schema`

只负责：

- Router 要提哪些槽
- 哪些槽必填
- 缺失时要不要追问

不负责：

- 子 agent 最终请求体长什么样
- 业务变量怎么传

### 3.2 `config_variables`

只负责：

- 业务调用方透传给子 agent 的上下文变量

不负责：

- Router 提槽语义
- 必填校验

### 3.3 `slots_data`

只负责：

- 承载 Router 本轮最终确认的槽位结果

规则固定为：

- 只放 `slot_key -> value`
- 只放非空槽位
- 不放 `null`
- 不放别名
- 由 Router 生成并覆盖，调用方不能作为最终真值源

## 4. 配置设计

### 4.1 新增配置字段

建议在 intent catalog 中新增两个字段：

```json
{
  "agent_payload_mode": "config_variables_envelope",
  "agent_payload_config": {
    "session_id_field": "session_id",
    "txt_field": "txt",
    "stream_field": "stream",
    "config_variables_field": "config_variables",
    "slots_data_name": "slots_data",
    "slots_data_encoding": "json_string"
  }
}
```

含义如下：

- `agent_payload_mode`
  - `legacy_mapping`
    继续使用当前 `field_mapping` 逐字段组包。
  - `config_variables_envelope`
    使用统一标准报文。
- `agent_payload_config.session_id_field`
  顶层会话字段名，默认 `session_id`。
- `agent_payload_config.txt_field`
  顶层文本字段名，默认 `txt`。
- `agent_payload_config.stream_field`
  顶层流式字段名，默认 `stream`。
- `agent_payload_config.config_variables_field`
  顶层变量数组字段名，默认 `config_variables`。
- `agent_payload_config.slots_data_name`
  槽位变量名，默认 `slots_data`。
- `agent_payload_config.slots_data_encoding`
  首期固定为 `json_string`。

### 4.2 `AG_TRANS` 配置示例

`AG_TRANS` 建议按如下方式配置：

```json
{
  "intent_code": "AG_TRANS",
  "name": "立即发起一笔转账交易",
  "description": "实时转账交易执行。",
  "agent_url": "http://intent-appointment-agent.intent.svc.cluster.local:8000/api/agent/run",
  "status": "active",
  "dispatch_priority": 986,
  "agent_payload_mode": "config_variables_envelope",
  "agent_payload_config": {
    "session_id_field": "session_id",
    "txt_field": "txt",
    "stream_field": "stream",
    "config_variables_field": "config_variables",
    "slots_data_name": "slots_data",
    "slots_data_encoding": "json_string"
  },
  "request_schema": {
    "type": "object",
    "required": ["session_id", "txt", "stream", "config_variables"],
    "properties": {
      "session_id": { "type": "string" },
      "txt": { "type": "string" },
      "stream": { "type": "boolean" },
      "config_variables": {
        "type": "array",
        "items": {
          "type": "object",
          "required": ["name", "value"],
          "properties": {
            "name": { "type": "string" },
            "value": { "type": "string" }
          }
        }
      }
    }
  },
  "field_mapping": {},
  "resume_policy": "resume_same_task"
}
```

这里有两条强约束：

1. `field_mapping` 清空。  
   标准报文模式下，不再向子 agent 平铺 `transfer.amount`、`payee.name` 这类字段。

2. `request_schema` 对齐真实子 agent 协议。  
   不再沿用旧的嵌套请求体约束。

### 4.3 `slot_schema` 配置不需要重写

以 `AG_TRANS` 为例，仍然保持如下语义：

- 必填：`amount`、`payee_name`
- 选填：`payer_card_no`、`payer_card_remark`、`payee_card_no`、`payee_card_remark`、`payee_card_bank`、`payee_phone`

示例：

```json
[
  { "slot_key": "amount", "required": true },
  { "slot_key": "payee_name", "required": true },
  { "slot_key": "payee_card_no", "required": false },
  { "slot_key": "payee_card_bank", "required": false }
]
```

语义固定为：

- 必填/选填只影响追问逻辑
- 不影响下传逻辑
- 只要识别出来，必填和选填都可以进 `slots_data`

## 5. Router 入参兼容设计

### 5.1 对外请求格式

Router 对外建议兼容以下请求格式：

```json
{
  "session_id": "1635501196813426",
  "txt": "给陈广荣转账500元",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "1631102265490929" },
    { "name": "sessionID", "value": "1635501196813426" },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "1635501196813426" }
  ],
  "executionMode": "execute"
}
```

### 5.2 入口字段兼容规则

建议 Router API 增加以下兼容：

- `txt`
  作为 `content` 的别名。
- `config_variables`
  作为会话级透传变量入口。
- `stream`
  接收并保留，用于下游子 agent 报文。
- `session_id`
  作为 body 内兼容字段。

### 5.3 `stream` 的解释

需要明确两层流式概念：

- Router 自己是不是 SSE
  仍由 `/messages` 与 `/messages/stream` 路径决定。
- 发给子 agent 的 `stream`
  由 body 字段透传。

也就是说，`stream=true` 不改变 Router API 入口语义，只影响下游标准报文。

### 5.4 `session_id` 的校验规则

如果走：

`POST /sessions/{session_id}/messages`

则校验：

- path 里的 `session_id`
- body 里的 `session_id`

若两者同时存在且不一致，应直接返回 `400`。

## 6. `config_variables` 运行规则

### 6.1 Session 级缓存

Router 需要在 session 上保存一份透传变量缓存。

规则如下：

1. 当前轮请求带了 `config_variables`
   刷新 session 缓存。
2. 当前轮请求没带 `config_variables`
   复用上一轮缓存。
3. 当前轮再次带入 `config_variables`
   按 `name` 合并，同名覆盖。

### 6.2 `slots_data` 的所有权

`slots_data` 必须定义为 Router-owned 保留变量。

规则如下：

1. 调用方即使传了 `slots_data`，也只作为输入参考。
2. Router 出站给子 agent 时，必须用当前 `task.slot_memory` 重新生成 `slots_data`。
3. 最终出站变量中的 `slots_data` 只能有一份。

### 6.3 多轮补槽

多轮时的行为必须是：

- 透传变量保持稳定
- 只有 `slots_data` 随 Router 当前已确认槽位更新

这样子 agent 才能只关心两件事：

- 当前轮文本 `txt`
- 截至当前轮的完整 `slots_data`

## 7. 组包逻辑

### 7.1 首轮出站

上游发给 Router：

```json
{
  "session_id": "1635501196813426",
  "txt": "给陈广荣转账500元",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "1631102265490929" },
    { "name": "sessionID", "value": "1635501196813426" },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "1635501196813426" }
  ]
}
```

Router 提槽结果：

```json
{
  "payee_name": "陈广荣",
  "amount": "500"
}
```

发给子 agent：

```json
{
  "session_id": "1635501196813426",
  "txt": "给陈广荣转账500元",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "1631102265490929" },
    { "name": "sessionID", "value": "1635501196813426" },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "1635501196813426" },
    { "name": "slots_data", "value": "{\"payee_name\":\"陈广荣\",\"amount\":\"500\"}" }
  ]
}
```

### 7.2 第二轮补槽

用户继续输入：

```json
{
  "session_id": "1635501196813426",
  "txt": "他的收款卡尾号6222",
  "stream": true
}
```

此时调用方即使不再传 `config_variables`，Router 也应复用上一轮缓存。

新的子 agent 报文应为：

```json
{
  "session_id": "1635501196813426",
  "txt": "他的收款卡尾号6222",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "1631102265490929" },
    { "name": "sessionID", "value": "1635501196813426" },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "1635501196813426" },
    { "name": "slots_data", "value": "{\"payee_name\":\"陈广荣\",\"amount\":\"500\",\"payee_card_no\":\"6222\"}" }
  ]
}
```

### 7.3 无槽位意图

如果某个意图当前只做意图识别，没有配置 slot schema，也可以统一走标准报文：

```json
{
  "session_id": "1635501196813426",
  "txt": "我要看看附近优惠",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "1631102265490929" },
    { "name": "sessionID", "value": "1635501196813426" },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "1635501196813426" },
    { "name": "slots_data", "value": "{}" }
  ]
}
```

## 8. 子 agent 侧解析约定

子 agent 收到标准 envelope 后，先把 `config_variables` 数组转成字典：

```json
{
  "custID": "1631102265490929",
  "sessionID": "1635501196813426",
  "currentDisplay": "",
  "agentSessionID": "1635501196813426",
  "slots_data": "{\"payee_name\":\"陈广荣\",\"amount\":\"500\"}"
}
```

再把 `slots_data` 反序列化为：

```json
{
  "payee_name": "陈广荣",
  "amount": "500"
}
```

子 agent 内部只需要区分：

- 顶层控制字段：`session_id`、`txt`、`stream`
- 透传业务变量：`config_variables`
- Router 槽位结果：`slots_data`

不再需要从 `payer/payee/transfer` 这种嵌套业务对象里反推哪些字段属于槽位。

## 9. 兼容策略

首期建议双模式并存：

- `legacy_mapping`
  兼容现有逐字段 mapping 的子 agent。
- `config_variables_envelope`
  对齐真实业务标准报文。

这样可以做到：

1. 先只把 `AG_TRANS` 切到新模式。
2. 其他 intent 继续保留旧模式。
3. 等联调完成后，再逐个迁移。

## 10. 最小化实施范围

首期只改以下能力：

1. Router API 支持接收 `txt`、`stream`、`config_variables`。
2. Session / task context 支持缓存与续传 `config_variables`。
3. `RequestPayloadBuilder` 新增 `config_variables_envelope` 组包模式。
4. `AG_TRANS` 改为使用新模式。
5. `transfer-money-agent` 与 `fallback-agent` 先兼容标准 envelope。

不在首期范围内：

- 全量 intent 一次性切换
- 动态自定义 `config_variables` 变量映射规则
- `slots_data` 之外的复杂结构化多字段拆分

## 11. 测试清单

至少要覆盖以下测试：

1. Router 收到 `txt/config_variables` 能正常进入识别主链路。
2. `AG_TRANS` 在新模式下能生成标准 envelope。
3. `slots_data` 会覆盖调用方传入的旧值。
4. 第二轮补槽时，`config_variables` 不丢。
5. 无槽位意图会生成 `slots_data="{}"`。
6. `transfer-money-agent` 能正确解析 `slots_data`。
7. `fallback-agent` 能兼容标准 envelope。

## 12. 结论

这次改造的核心不是重写提槽，而是把 Router 到子 agent 的协议边界理顺：

- `slot_schema` 管提槽
- `config_variables` 管透传
- `slots_data` 管最终槽位结果

只要这三层边界固定下来，后续无论是继续接真实业务子 agent，还是逐步替换老的 `field_mapping` 报文，复杂度都会明显下降。
