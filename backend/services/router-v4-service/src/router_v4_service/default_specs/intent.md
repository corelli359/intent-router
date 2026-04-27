+++
catalog_id = "bank-assistant-intents"
version = "0.1.0"

[[intents]]
intent_id = "transfer"
scene_id = "transfer"
version = "0.1.0"
name = "转账"
description = "识别用户表达办理转账、汇款、打款、付款的请求。"
target_agent = "transfer-agent"
references = []
skill = { skill_id = "transfer", version = "0.1.0", owner = "transfer-agent", path = "skills/transfer.skill.md", description = "转账执行 Skill，负责提槽、确认、风控、限额和转账 API。" }
dispatch_contract = { task_type = "transfer", handoff_fields = ["raw_message", "user_profile_ref", "page_context_ref"] }

[[intents]]
intent_id = "balance_query"
scene_id = "balance_query"
version = "0.1.0"
name = "余额查询"
description = "识别用户查询账户余额、银行卡余额、可用余额的请求。"
target_agent = "balance-agent"
references = []
skill = { skill_id = "balance_query", version = "0.1.0", owner = "balance-agent", path = "skills/balance_query.skill.md", description = "余额查询 Skill，负责账户范围、鉴权、余额读取和结果输出。" }
dispatch_contract = { task_type = "balance_query", handoff_fields = ["raw_message", "user_profile_ref", "page_context_ref"] }

[[intents]]
intent_id = "fund_query"
scene_id = "fund_query"
version = "0.1.0"
name = "基金查询"
description = "识别用户查询基金产品、净值、收益、风险等级或持仓信息的请求。"
target_agent = "fund-agent"
references = []
skill = { skill_id = "fund_query", version = "0.1.0", owner = "fund-agent", path = "skills/fund_query.skill.md", description = "基金查询 Skill，负责产品解析、风险收益查询和结构化输出。" }
dispatch_contract = { task_type = "fund_query", handoff_fields = ["raw_message", "user_profile_ref", "page_context_ref"] }
+++

# 意图目录

本文件是 Intent ReAct Runtime 的第一份 spec，用来决定本轮第一步 action：`select_intent`、`plan_multi_intent` 或 `no_action`。本步只读取本文件，不读取 `skill_ref.path` 指向的 Skill 正文；命中意图后，后续 ReAct 步骤再按 `skill_ref` 渐进加载 Skill。

## 全局规范

- 意图框架从本文件开始执行 ReAct，而不是先分类再进入另一套流程。
- 本步只做意图选择、计划拆分、`skill_ref` 定位和派发契约组装。
- 本步不提取收款人、金额、账户范围、基金代码等业务字段。
- 业务字段提取、确认、限额、风控、鉴权、API 调用和结果组装，必须由后续 Skill ReAct 步骤按 Skill 说明执行。
- 各意图自己的命中条件、正反例和业务边界写在对应意图小节里，不在全局层重复展开。
- 如果用户表达属于某个业务办理，即使业务字段不完整，也应先命中对应意图；缺失字段由执行 Agent 按 Skill 处理。
- 如果无法识别到明确意图，Router 应进入兜底路径，不臆造业务字段。

## 加载约定

- Intent ReAct 第一步加载：本文件的全局规范、意图清单、对应意图说明、`target_agent`、`dispatch_contract` 和 `skill_ref`。
- Skill ReAct 后续步骤加载：命中意图后，运行时按 `skill_ref.path` 渐进式读取对应 Skill 文档。
- 多轮场景中，运行时依赖 task memory、business memory 和最近上下文延续，不从头重建业务状态。
- `skill_ref` 是后续 Skill ReAct 的入口引用，不是本步直接执行的规则正文。

## transfer

### 说明

当用户表达向他人转账、汇款、打款、付款时，命中本意图。只要用户明确表达办理转账，即使没有收款人或金额，也应命中本意图。

### 正例

- 我要转账
- 我想转账
- 我我要转账
- 给张三转5000块
- 向李四汇款

### 反例

- 查询转账记录
- 看一下转账明细

### 执行边界

本意图只判断是否进入转账办理。收款人、金额、确认、限额、风控和业务 API 都属于 `transfer.skill.md`。

## balance_query

### 说明

当用户表达查询账户余额、银行卡余额、可用余额或“还有多少钱”时，命中本意图。

### 正例

- 查一下余额
- 我卡里还有多少钱

### 反例

- 给张三转5000块
- 查询基金收益

### 执行边界

本意图只判断是否查询余额。账户选择、鉴权、余额读取和结果组织属于 `balance_query.skill.md`。

## fund_query

### 说明

当用户表达查询基金产品、净值、收益、风险等级、持仓或 QDII/ETF 等产品信息时，命中本意图。

### 正例

- 沪深300ETF怎么样
- 我想了解QDII基金

### 反例

- 给张三转5000块
- 我要汇款

### 执行边界

本意图只判断是否查询基金。基金名称、产品范围、持仓鉴权、风险和收益查询属于 `fund_query.skill.md`。
