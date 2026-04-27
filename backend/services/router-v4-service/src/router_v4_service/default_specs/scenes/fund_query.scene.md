+++
scene_id = "fund_query"
version = "0.1.0"
name = "基金查询执行场景"
description = "承接 fund_query 意图，派发给基金执行 Agent。"
target_agent = "fund-agent"
references = []

[skill]
skill_id = "fund_query"
version = "0.1.0"
owner = "fund-agent"
path = "skills/fund_query.skill.md"
description = "由基金执行 Agent 按基金查询 Skill 读取产品、风险和持仓信息。"

[dispatch_contract]
task_type = "fund_query"
handoff_fields = ["raw_message", "user_profile_ref", "page_context_ref"]
+++

# 基金查询执行场景 Contract

## 场景职责

本场景只定义 Router 派发执行任务所需的契约。基金名称、产品范围、持仓鉴权、风险和收益查询由基金执行 Agent 处理。

## Router 派发边界

Router 只传递原始表达、上下文引用、intent_id、scene_id、task_type 和 skill_ref，不读取基金业务字段。
