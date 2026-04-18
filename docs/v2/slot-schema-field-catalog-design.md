# Slot Schema 与公共字段语义设计

## 1. 设计目标

当前 `slot_schema` 已经能表达一部分槽位约束，但它还不够支撑后续“大量 intent 动态注册、动态变更、Router 不随 intent 发版”的目标。

这一版设计要解决 4 个问题：

1. 注册时如何严谨声明字段名称、字段含义、字段类型
2. 多个 intent 共用同类字段时，如何复用语义而不是复制粘贴
3. Router 如何只依赖动态配置，而不是依赖静态 Python intent 模型
4. Agent、Router、Admin 三方边界如何清晰分层

## 2. 核心原则

### 2.1 Admin 拥有注册定义

intent 的事实来源必须在 `admin-service`：

- intent 基本信息
- `agent_url`
- `request_schema`
- `field_mapping`
- `slot_schema`
- `graph_build_hints`

这些内容应该由 Admin 存储和治理，而不是由 Router 内部维护。

### 2.2 Router 只消费动态配置

Router 不应该因为新增一个 intent、修改一个槽位定义、补一个意图示例，就改代码、发版或重启。

Router 应该做的是：

- 拉取 Admin 产出的 intent 注册文档
- 将其映射为 Router 自己的只读运行态结构
- 在识别、建图、调度时按动态数据运行

换句话说，Router 需要稳定的“通用协议”，不需要“每个 intent 的静态代码模型”。

### 2.3 Graph Runtime 属于 Router

Graph、Node、Edge、Runtime State、调度策略、条件语义，这些都属于 Router 内部实现。

它们不能出现在公共 intent 注册契约里，也不能要求各个 intent agent 了解这些结构。

### 2.4 Agent 只处理自己的意图

每个 intent agent 都应该具备完整处理自己请求的能力：

- 理解本意图收到的输入
- 多轮补齐要素
- 做确认和执行
- 返回执行结果

Router 的价值在于跨意图识别、编排和调度，而不是把 agent 内部逻辑抽进 Router。

## 3. 为什么 `slot_schema` 不能只是字段名

如果 `slot_schema` 只定义：

- `slot_key`
- `value_type`
- `required`

那么在复杂场景里会迅速失真。

例如转账意图里至少可能同时出现：

- 付款账户
- 收款账户
- 收款人姓名
- 转账金额
- 校验手机号后 4 位

其中“付款账户”和“收款账户”都可能是 `account_number`，如果没有更细的语义角色，LLM 和 Router 都无法稳定判断某个值该绑定到哪个槽位。

因此，注册阶段必须同时表达：

- 这个字段本身是什么
- 这个字段在当前 intent 里扮演什么角色
- 这个字段能否被历史、推荐、上下文复用

## 4. 两层模型

建议把字段建模拆成两层，而不是把所有信息都塞进一层 `slot_schema`。

### 4.1 第一层：公共字段语义库 `field_catalog`

这一层描述“字段本身是什么”，由 Admin 统一管理，可被多个 intent 复用。

建议至少包含：

- `field_code`：稳定机器标识，例如 `account_number`、`phone_last_four`、`amount`
- `label`
- `semantic_definition`
- `value_type`
- `format_rule`
- `normalization_rule`
- `validation_rule`
- `examples`
- `counter_examples`
- `sensitivity`
- `default_confirmation_policy`

这层解决的是“共性字段语义复用”问题。

### 4.2 第二层：意图内槽位定义 `slot_schema`

这一层描述“某个 intent 如何使用这个字段”。

建议至少包含：

- `slot_key`：intent 内唯一键
- `field_code`：引用公共字段语义
- `role`：当前 intent 中的业务角色
- `label`
- `description`
- `semantic_definition`
- `required`
- `bind_scope`
- `allow_from_history`
- `allow_from_recommendation`
- `allow_from_context`
- `confirmation_policy`
- `overwrite_policy`
- `prompt_hint`
- `examples`
- `counter_examples`

这层解决的是“同类字段在不同意图、不同角色下如何绑定”问题。

## 5. 为什么要把 `field_code` 和 `role` 分开

这是后续可扩展性的关键。

### 5.1 `field_code` 解决“共性”

例如下面三个槽位都可以复用 `account_number`：

- 查余额里的 `account_number`
- 转账里的 `payer_account_number`
- 转账里的 `recipient_account_number`

它们共用同一个字段语义，是因为：

- 格式规则相近
- 敏感级别相近
- 归一化方式相近

### 5.2 `role` 解决“语境”

但这三个槽位不能视为同一个业务槽位，因为角色不同：

- 查询时是“被查询账户”
- 转账时可能是“付款账户”
- 也可能是“收款账户”

如果没有 `role`，历史复用和多槽位绑定会频繁串位。

## 6. 推荐的数据结构

### 6.1 公共字段语义

```json
{
  "field_code": "account_number",
  "label": "银行卡号",
  "semantic_definition": "用于识别某张银行卡或账户的账号字段",
  "value_type": "account_number",
  "format_rule": {
    "min_length": 12,
    "max_length": 19
  },
  "normalization_rule": {
    "trim": true,
    "remove_spaces": true
  },
  "validation_rule": {
    "regex": "^[0-9]{12,19}$"
  },
  "sensitivity": "high",
  "examples": ["6222021234567890"],
  "counter_examples": ["6222 0212 3456 7890"]
}
```

### 6.2 意图内槽位定义

```json
{
  "slot_key": "recipient_account_number",
  "field_code": "account_number",
  "role": "recipient_account",
  "label": "收款卡号",
  "description": "本次转账的收款账户卡号",
  "semantic_definition": "收款方账户，不是付款账户，也不是用户本人默认账户",
  "required": true,
  "bind_scope": "node_input",
  "allow_from_history": false,
  "allow_from_recommendation": true,
  "allow_from_context": false,
  "confirmation_policy": "always",
  "overwrite_policy": "overwrite_if_new_nonempty",
  "examples": ["收款卡号 6222021234567890"],
  "counter_examples": ["我的卡号 6222021234567890"]
}
```

## 7. 推荐的注册文档形态

最终 intent 注册时，建议由 Admin 存储完整 JSON 文档。Router 只读取这份动态数据。

```json
{
  "intent_code": "transfer_money",
  "name": "转账",
  "description": "给指定收款人转账",
  "agent_url": "http://intent-transfer-money-agent/api/agent/run",
  "status": "active",
  "request_schema": {},
  "field_mapping": {},
  "slot_schema": [
    {
      "slot_key": "payer_account_number",
      "field_code": "account_number",
      "role": "payer_account",
      "required": true,
      "allow_from_history": true,
      "confirmation_policy": "when_ambiguous"
    },
    {
      "slot_key": "recipient_account_number",
      "field_code": "account_number",
      "role": "recipient_account",
      "required": true,
      "allow_from_history": false,
      "confirmation_policy": "always"
    },
    {
      "slot_key": "amount",
      "field_code": "amount",
      "role": "transfer_amount",
      "required": true,
      "allow_from_history": false,
      "confirmation_policy": "when_ambiguous"
    }
  ],
  "graph_build_hints": {
    "intent_scope_rule": "单次转账动作是一个 intent，卡号、金额、收款人都是槽位，不要拆成新的 intent",
    "planner_notes": "只有用户明确表达多个独立转账动作时，才允许生成多个节点"
  }
}
```

## 8. Admin / Router / Agent 的职责边界

### 8.1 Admin

Admin 负责：

- intent 注册与变更
- 公共字段语义库维护
- slot_schema 合法性校验
- graph build hints 配置治理
- 版本控制和发布

Admin 不负责：

- 多意图识别
- 执行图调度
- agent 内部补槽逻辑

### 8.2 Router

Router 负责：

- 读取动态 intent 注册文档
- 将注册文档映射为自己的 read model
- 将字段/槽位约束喂给 LLM 识别与建图
- 执行图编排与调度
- 调用 agent 并管理跨意图状态

Router 不负责：

- 维护字段语义的事实来源
- 维护每个 intent 的业务逻辑代码
- 随 intent 增加而新增代码模型

### 8.3 Agent

Agent 负责：

- 本意图多轮补槽
- 本意图确认策略
- 本意图执行逻辑
- 本意图执行结果返回

Agent 不应该感知：

- 全局 graph runtime
- 其他 intent 的内部实现
- Router 的识别和规划过程

## 9. Router 侧应该如何落地

Router 不应该直接把 Admin 的 JSON 文档当成内部运行态对象到处传。

建议 Router 内部拆成两层：

1. `registration document`
2. `router read model`

第一层是 Admin 的动态数据。

第二层是 Router 在刷新 catalog 时临时编译出的只读结构，例如：

- `intent_code`
- `agent_url`
- `slot_schema`
- `graph_build_hints`
- `dispatch_priority`

这样可以保证：

- Admin 协议可演进
- Router runtime 保持稳定
- 新增 intent 不需要 Router 增加新类

## 10. 关于公共字段的复用边界

有些字段是天然共性的，建议沉淀进 `field_catalog`：

- `account_number`
- `phone_last_four`
- `amount`
- `currency_code`
- `person_name`
- `id_number`
- `gas_account_id`
- `credit_card_number`

但注意不要把“角色”也公共化。

例如：

- `amount` 是公共字段
- `transfer_amount`、`repayment_amount`、`exchange_amount` 是不同角色

这两层不能混。

## 11. 推荐的校验规则

注册阶段，Admin 至少要做下面这些校验：

1. `slot_key` 在 intent 内唯一
2. `field_code` 必须存在于 `field_catalog`，或者明确声明为 intent 私有字段
3. `required=true` 的槽位必须给出明确语义说明
4. `allow_from_history=true` 的敏感字段必须显式配置确认策略
5. `counter_examples` 不能为空的场景要做规则提示
6. `graph_build_hints` 不允许与 `slot_schema` 发生语义冲突

## 12. 为什么这套设计更适合海量 intent

如果未来有 1 万个 intent，且每天都在变更：

- Admin 仍然只是存和发 JSON
- Router 仍然只是刷新 catalog
- Agent 仍然只维护自己

不会出现下面这种坏味道：

- 新增一个 intent，就要给 Router 加一个类
- 增加一个共性字段，就要改 Router 代码
- 某个 intent 的 slot 变化，要推动 Router 发版

这是这套设计最大的价值。

## 13. 分阶段落地建议

### 阶段 1

先补文档与注册模型设计，不立刻改完所有运行时代码：

- 明确 `field_catalog`
- 明确 `slot_schema.field_code`
- 明确 `slot_schema.role`
- 明确 Admin 拥有注册定义

### 阶段 2

Admin 侧新增字段语义管理能力：

- 新表或新 JSON 列存 `field_catalog`
- intent 注册页支持引用公共字段
- 注册接口返回完整 intent 文档

### 阶段 3

Router 改成纯动态消费：

- catalog refresh 读取 Admin 发布的动态文档
- Router 内部做 read model 编译
- 不再依赖共享 intent Python 模型

### 阶段 4

Agent 注册和执行协议进一步规范化：

- agent 输入 schema 与 slot_schema 解耦
- agent 只接收本意图所需的结构化请求
- Router 负责跨意图编排，agent 负责单意图完成

## 14. 结论

`slot_schema` 后续应该按“公共字段语义 + 意图内槽位角色”两层设计。

这样才能同时满足：

- 注册期严谨声明字段含义、字段名、字段类型
- 多 intent 复用共性字段
- Router 只依赖动态数据而不是静态 intent 代码模型
- graph runtime 继续保持在 Router 内部，不与注册契约耦合
