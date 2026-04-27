+++
scene_id = "transfer"
version = "0.1.0"
name = "转账"
description = "识别用户向他人转账、汇款、打款的请求，并派发给转账执行 Agent。"
target_agent = "transfer-agent"
references = ["references/transfer-routing.md"]
skill_fields = []

[skill]
skill_id = "transfer"
version = "0.1.0"
owner = "transfer-agent"
path = "skills/transfer.skill.md"
description = "由转账执行 Agent 按转账 Skill 补槽、风控、确认和执行。"

[triggers]
keywords = ["转账", "转给", "汇款", "打钱", "打款"]
negative_keywords = ["记录", "明细", "历史"]
examples = ["我要转账", "我想转账", "我我要转账", "给张三转5000块", "向李四汇款"]
negative_examples = ["查询转账记录"]

[dispatch_contract]
task_type = "transfer"
handoff_fields = ["raw_message", "user_profile_ref", "page_context_ref"]
+++

# 转账路由 Spec

## 意图边界

当用户表达向他人转账、汇款、打款、付款时，命中本场景。只要用户明确表达办理转账，即使没有收款人或金额，也应命中本场景并派发给 `transfer-agent`。

## 正例

- 我要转账
- 我想转账
- 我我要转账
- 给张三转5000块
- 向李四汇款

## 反例

- 查询转账记录
- 看一下转账明细

## 职责边界

Router 只识别转账意图并派发任务。收款人、金额、确认、风控、限额和转账 API 全部由转账执行 Agent 按 Skill 处理。

