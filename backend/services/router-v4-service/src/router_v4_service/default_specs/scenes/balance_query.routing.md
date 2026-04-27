+++
scene_id = "balance_query"
version = "0.1.0"
name = "余额查询"
description = "识别用户查询账户余额、银行卡余额、可用余额的请求，并派发给余额查询 Agent。"
target_agent = "balance-agent"
references = ["references/balance-routing.md"]
skill_fields = []

[skill]
skill_id = "balance_query"
version = "0.1.0"
owner = "balance-agent"
path = "skills/balance_query.skill.md"
description = "由余额查询 Agent 按余额查询 Skill 读取账户范围并返回结果。"

[triggers]
keywords = ["余额", "可用余额", "账户余额", "银行卡余额", "还有多少钱"]
negative_keywords = ["转账", "基金"]
examples = ["查一下余额", "我卡里还有多少钱"]
negative_examples = ["给张三转5000块"]

[dispatch_contract]
task_type = "balance_query"
handoff_fields = ["raw_message", "user_profile_ref", "page_context_ref"]
+++

# 余额查询路由 Spec

## 意图边界

当用户表达查询账户余额、银行卡余额、可用余额或“还有多少钱”时，命中本场景。

## 正例

- 查一下余额
- 我卡里还有多少钱

## 反例

- 给张三转5000块
- 查询基金收益

## 职责边界

Router 只识别余额查询意图并派发任务。账户范围、鉴权、余额读取和结果组织由余额查询 Agent 按 Skill 处理。

