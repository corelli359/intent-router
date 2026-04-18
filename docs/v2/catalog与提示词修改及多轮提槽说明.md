# Catalog 与提示词修改及多轮提槽说明

## 1. 先说结论

当前这套 router 里，如果你要调效果，通常有三层入口：

1. 改 split catalog
2. 改 router 全局提示词
3. 改具体 intent agent 的提示词

优先顺序建议是：

1. 先改 catalog 里的业务语义
2. 再改 router 层 prompt
3. 最后才改 agent 层 prompt

原因很直接：

- catalog 是结构化约束，稳定，影响面可控
- router prompt 是全局行为，改动会影响整套路由
- agent prompt 是 intent 私有逻辑，适合做最后一层细调

---

## 2. split catalog 里哪个文件是干什么的

当前 file mode catalog 在这里：

- `k8s/intent/router-intent-catalog/intents.json`
- `k8s/intent/router-intent-catalog/field-catalogs.json`
- `k8s/intent/router-intent-catalog/slot-schemas.json`
- `k8s/intent/router-intent-catalog/graph-build-hints.json`

### 2.1 `intents.json`

这是意图主体文件。

这里放：

- `intent_code`
- `name`
- `description`
- `domain_code`
- `domain_name`
- `domain_description`
- `examples`
- `routing_examples`
- `agent_url`
- `field_mapping`
- `resume_policy`

如果你要新增或修改一个意图，第一步一定是这里。

### 2.2 `field-catalogs.json`

这是业务字段定义。

这里放每个 intent 的字段语义，例如：

- 金额
- 收款人姓名
- 收款卡号
- 手机号
- 日期

这里的字段会影响模型对“这段话里的值到底是什么”的理解。

重点字段：

- `label`
- `semantic_definition`
- `aliases`
- `examples`
- `counter_examples`

### 2.3 `slot-schemas.json`

这是 router 真正做提槽和校验时最关键的文件。

这里决定：

- 提哪些槽
- 哪些槽必填
- 槽位语义是什么
- 槽位别名有哪些
- 示例和反例是什么
- 当前槽位的 `prompt_hint` 是什么

如果你想提升槽位填充效果，这个文件通常比改全局 prompt 更直接。

重点字段：

- `slot_key`
- `field_code`
- `label`
- `description`
- `semantic_definition`
- `required`
- `aliases`
- `examples`
- `counter_examples`
- `prompt_hint`

### 2.4 `graph-build-hints.json`

这是 graph / planner 的辅助提示。

如果你只做单意图识别 + 提槽，这里可以先比较轻。

如果要影响：

- 多动作拆图
- 条件边
- 顺序关系
- 并行关系

那这里就很重要。

---

## 3. 如果你要新增或修改意图，怎么改

最小原则是：同一个 `intent_code` 在四个文件里都要对齐。

也就是说，新增一个意图时，至少要保证：

1. `intents.json` 有它
2. `field-catalogs.json` 有它
3. `slot-schemas.json` 有它
4. `graph-build-hints.json` 有它

哪怕最后一个先给 `{}`，也要有。

---

## 4. 如果你要修改“提示词”，到底改哪里

这个问题要分场景。

### 4.1 你想改“意图识别”的全局提示词

改这里：

- `backend/services/router-service/src/router_service/core/prompts/prompt_templates.py`

关键常量：

- `DEFAULT_RECOGNIZER_SYSTEM_PROMPT`
- `DEFAULT_RECOGNIZER_HUMAN_PROMPT`
- `DEFAULT_DOMAIN_ROUTER_SYSTEM_PROMPT`
- `DEFAULT_DOMAIN_ROUTER_HUMAN_PROMPT`
- `DEFAULT_LEAF_ROUTER_SYSTEM_PROMPT`
- `DEFAULT_LEAF_ROUTER_HUMAN_PROMPT`

对应含义：

- `DEFAULT_RECOGNIZER_*`
  - 平铺识别模式下的大入口意图识别

- `DEFAULT_DOMAIN_ROUTER_*`
  - hierarchical 模式下的大类识别

- `DEFAULT_LEAF_ROUTER_*`
  - hierarchical 模式下的小类 / leaf intent 识别

### 4.2 你想改“router 层提槽”的全局提示词

也改这里：

- `backend/services/router-service/src/router_service/core/prompts/prompt_templates.py`

关键常量：

- `DEFAULT_SLOT_EXTRACTOR_SYSTEM_PROMPT`
- `DEFAULT_SLOT_EXTRACTOR_HUMAN_PROMPT`

实际使用入口在：

- `backend/services/router-service/src/router_service/core/slots/extractor.py`

这部分负责 router 层的槽位抽取。

### 4.3 你想改“graph 规划”的提示词

还是这个文件：

- `backend/services/router-service/src/router_service/core/prompts/prompt_templates.py`

关键常量：

- `DEFAULT_GRAPH_PLANNER_SYSTEM_PROMPT`
- `DEFAULT_GRAPH_PLANNER_HUMAN_PROMPT`
- `DEFAULT_UNIFIED_GRAPH_BUILDER_SYSTEM_PROMPT`
- `DEFAULT_UNIFIED_GRAPH_BUILDER_HUMAN_PROMPT`
- `DEFAULT_TURN_INTERPRETER_SYSTEM_PROMPT`
- `DEFAULT_TURN_INTERPRETER_HUMAN_PROMPT`

这部分主要影响：

- 多意图拆分
- 顺序关系
- 条件关系
- graph 的 node / edge 构造

### 4.4 你想改“某个具体 intent agent”的提示词

那就不是 router 层了，而是 agent 自己的 prompt。

例如：

- 转账
  - `backend/services/agents/transfer-money-agent/src/transfer_money_agent/service.py`
  - 常量：`TRANSFER_MONEY_PROMPT`

- 查余额
  - `backend/services/agents/account-balance-agent/src/account_balance_agent/service.py`
  - 常量：`ACCOUNT_BALANCE_PROMPT`

- 燃气缴费
  - `backend/services/agents/gas-bill-agent/src/gas_bill_agent/service.py`
  - 常量：`GAS_BILL_PAYMENT_PROMPT`

- 信用卡还款
  - `backend/services/agents/credit-card-repayment-agent/src/credit_card_repayment_agent/service.py`
  - 常量：`CREDIT_CARD_REPAYMENT_PROMPT`

- 换汇
  - `backend/services/agents/forex-agent/src/forex_agent/service.py`
  - 常量：`FOREX_EXCHANGE_PROMPT`

---

## 5. 其实很多“提示词修改”，可以不改代码

这是当前结构里最值得利用的一点。

因为 router 的很多 prompt 会把注册后的 intent 定义、field catalog、slot schema 整体传进去。

所以你如果只是想调效果，很多时候不用先改 `prompt_templates.py`，而是直接改 catalog：

### 5.1 想让意图更容易识别

优先改：

- `intents.json`

重点改：

- `description`
- `examples`
- `routing_examples`

### 5.2 想让槽位更容易提出来

优先改：

- `field-catalogs.json`
- `slot-schemas.json`

重点改：

- `semantic_definition`
- `aliases`
- `examples`
- `counter_examples`
- `prompt_hint`

### 5.3 想让 graph 更稳

优先改：

- `graph-build-hints.json`

---

## 6. 当前 recognizer 有一部分提示词支持 env 覆盖

这个是特殊点。

在：

- `backend/services/router-service/src/router_service/settings.py`
- `backend/services/router-service/src/router_service/api/dependencies.py`

当前支持这两个环境变量：

- `ROUTER_LLM_RECOGNIZER_SYSTEM_PROMPT_TEMPLATE`
- `ROUTER_LLM_RECOGNIZER_HUMAN_PROMPT_TEMPLATE`

也就是说：

- 识别器 prompt 可以通过 env 临时覆盖
- 但 slot extractor / planner / unified builder 这些，目前主要还是代码默认值

所以如果你想“不改代码、只改配置”地试 prompt，目前主要能覆盖的是 recognizer。

---

## 7. 一个非常重要的部署注意点

当前 minikube 部署脚本不是单纯拿 repo 里的 json 直接上集群。

部署脚本：

- `scripts/minikube_deploy_intent.sh`

在 rollout 之前会做两件事：

1. `scripts/sync_financial_intents_to_db.py`
2. `scripts/export_router_intent_catalog_from_db.py`

这意味着：

- 你手改了 `k8s/intent/router-intent-catalog/*.json`
- 如果随后又直接跑部署脚本
- 那这些 json 可能会被 sqlite 重新导出的内容覆盖掉

所以当前正确心智是：

- `scripts/register_financial_intents.py` 和 sqlite 是 source-of-truth 的一部分
- `k8s/intent/router-intent-catalog/*.json` 更像部署导出物

如果你只是本地 file mode 调试，可以直接改 json。

如果你要按当前部署链路稳定上线，最好同步改：

- `scripts/register_financial_intents.py`
- 或数据库里的 intent 定义

然后再导出。

---

## 8. 多轮提槽的代码准备好了吗

结论：代码已经准备好了，但“全量业务稳定性”还没有完全等于“所有样例都稳过”。

### 8.1 已经准备好的部分

#### A. router 多轮会话接口

可以直接按 session 逐轮发消息：

- `POST /api/router/v2/sessions`
- `POST /api/router/v2/sessions/{session_id}/messages`

对应实现：

- `backend/services/router-service/src/router_service/api/routes/sessions.py`

#### B. analyze-only 验证脚本

已有：

- `scripts/analyze_intent_slots_only.py`

适合看：

- 当前识别了什么意图
- 当前 slot_memory 是什么
- 不触发下游 agent

#### C. 多轮回放验证脚本

已有：

- `scripts/verify_transfer_multiturn_dataset.py`

它会：

1. 创建 session
2. 按 CSV 逐轮发消息
3. 检查最终 intent
4. 检查最终 slot_memory

#### D. 数据集

已有：

- `docs/examples/transfer_money_multiturn_cases.csv`

结构是：

- 前面是答案字段
- 后面是 `user_turn_1 ~ user_turn_n`
- 最后是合并后的 `dialogue_text`

#### E. smoke 脚本

已有：

- `scripts/smoke_router_slot_flows.py`

#### F. 转账意图当前必填契约

当前转账按以下必填槽位执行：

- `amount`
- `payee_name`
- `payee_card_no`

### 8.2 当前真实状态要怎么理解

如果你问的是“代码框架和测试工具有没有准备好”，答案是：

- 已经准备好

如果你问的是“所有多轮样例是不是都已经稳定通过”，答案是：

- 不是

原因主要在两类：

1. 当前 LLM 调用存在限流 / 波动
2. 某些提槽语义和 prompt 还需要继续收紧

本轮已经确认过：

- 单条真实转账多轮样例可以跑通
- `scripts/verify_transfer_multiturn_dataset.py` 可以直接作为后续回归工具

所以现在不是“没准备代码”，而是“代码和验证框架已经有了，接下来要继续打磨准确率和稳定性”。

---

## 9. 实操建议

如果你下一步要自己调效果，建议按这个顺序：

1. 先改 `scripts/register_financial_intents.py`
   - 因为它是当前 finance intents 的 source-of-truth 之一

2. 再导出 catalog
   - 或重新跑部署脚本

3. 如果只是槽位问题，优先改：
   - `field-catalogs.json`
   - `slot-schemas.json`

4. 如果是全局识别偏差，再改：
   - `prompt_templates.py` 里的 recognizer / slot extractor prompt

5. 最后用这些脚本验证：
   - `scripts/analyze_intent_only.py`
   - `scripts/analyze_intent_slots_only.py`
   - `scripts/verify_transfer_multiturn_dataset.py`
   - `scripts/smoke_router_slot_flows.py`

---

## 10. 最短回答版

如果你只问两个问题：

### Catalog 怎么改

改这四个：

- `intents.json`
- `field-catalogs.json`
- `slot-schemas.json`
- `graph-build-hints.json`

### 提示词怎么改

- 改 router 全局 prompt：
  - `backend/services/router-service/src/router_service/core/prompts/prompt_templates.py`

- 改具体 intent agent prompt：
  - 各 agent 的 `service.py`

- 只是想调效果，不一定先改 prompt：
  - 很多时候先改 catalog 语义更有效

