+++
scene_id = "transfer"
version = "0.1.0"
name = "转账执行场景"
description = "承接 transfer 意图，派发给转账执行 Agent。"
target_agent = "transfer-agent"
references = []

[skill]
skill_id = "transfer"
version = "0.1.0"
owner = "transfer-agent"
path = "skills/transfer.skill.md"
description = "由转账执行 Agent 按转账 Skill 补槽、风控、确认和执行。"

[dispatch_contract]
task_type = "transfer"
handoff_fields = ["raw_message", "user_profile_ref", "page_context_ref"]
+++

# 转账执行场景 Contract

## 场景职责

本场景只定义 Router 派发执行任务所需的契约。Router 已经在独立 intent spec 中完成意图识别；进入本场景后只负责找到执行 Agent 并构造任务。

## Agent 边界

收款人、金额、确认、风控、限额和转账 API 全部由 `transfer-agent` 按自己的 Skill 生命周期处理。

## Router 派发边界

Router 只传递原始表达、上下文引用、intent_id、scene_id、task_type 和 skill_ref，不提取、不存储、不展示业务字段。
