+++
scene_id = "balance_query"
version = "0.1.0"
name = "余额查询执行场景"
description = "承接 balance_query 意图，派发给余额查询 Agent。"
target_agent = "balance-agent"
references = []

[skill]
skill_id = "balance_query"
version = "0.1.0"
owner = "balance-agent"
path = "skills/balance_query.skill.md"
description = "由余额查询 Agent 按余额查询 Skill 读取账户范围并返回结果。"

[dispatch_contract]
task_type = "balance_query"
handoff_fields = ["raw_message", "user_profile_ref", "page_context_ref"]
+++

# 余额查询执行场景 Contract

## 场景职责

本场景只定义 Router 派发执行任务所需的契约。账户范围、鉴权、余额读取和结果组织由余额查询 Agent 处理。

## Router 派发边界

Router 只传递原始表达、上下文引用、intent_id、scene_id、task_type 和 skill_ref，不读取余额业务字段。
