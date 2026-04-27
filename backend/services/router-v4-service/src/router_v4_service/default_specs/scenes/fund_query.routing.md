+++
scene_id = "fund_query"
version = "0.1.0"
name = "基金查询"
description = "识别用户查询基金详情、风险、收益、持仓或产品信息的请求，并派发给基金执行 Agent。"
target_agent = "fund-agent"
references = ["references/fund-routing.md"]
skill_fields = []

[skill]
skill_id = "fund_query"
version = "0.1.0"
owner = "fund-agent"
path = "skills/fund_query.skill.md"
description = "由基金执行 Agent 按基金查询 Skill 读取产品、风险和持仓信息。"

[triggers]
keywords = ["基金", "QDII", "ETF", "净值", "持仓", "收益", "风险等级"]
negative_keywords = ["转账", "汇款"]
examples = ["沪深300ETF怎么样", "我想了解QDII基金"]
negative_examples = ["给张三转5000块"]

[dispatch_contract]
task_type = "fund_query"
handoff_fields = ["raw_message", "user_profile_ref", "page_context_ref"]
+++

# 基金查询路由 Spec

## 意图边界

当用户表达查询基金产品、净值、收益、风险等级、持仓或 QDII/ETF 等产品信息时，命中本场景。

## 正例

- 沪深300ETF怎么样
- 我想了解QDII基金

## 反例

- 给张三转5000块
- 我要汇款

## 职责边界

Router 只识别基金查询意图并派发任务。基金名称、产品范围、持仓鉴权、风险和收益查询由基金执行 Agent 按 Skill 处理。

