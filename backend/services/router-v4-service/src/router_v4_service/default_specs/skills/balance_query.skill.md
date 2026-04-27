# 余额查询 Skill 卡片 v0.1

owner: balance-agent
scene_id: balance_query
task_type: balance_query

## Router 交互边界

Router 不读取本 Skill 正文做意图识别，也不使用本 Skill 做上下文构建。Router 只在场景契约中传递 `skill_ref`，由 balance-agent 在执行阶段加载本 Skill。
Router 不直接访问账户系统，也不生成最终的余额展示文案。

## 执行 Agent 职责

balance-agent 负责账户选择、权限校验、余额查询和结构化结果输出。

## 误派处理

如果任务不属于余额查询，balance-agent 返回 `ishandover=true`，并让 `output.data=[]`，Router 据此转交兜底 Agent。
