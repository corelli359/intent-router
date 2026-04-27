# 基金查询 Skill 卡片 v0.1

owner: fund-agent
scene_id: fund_query
task_type: fund_query

## Router 交互边界

Router 不读取本 Skill 正文做意图识别，也不使用本 Skill 做上下文构建。Router 只在场景契约中传递 `skill_ref`，由 fund-agent 在执行阶段加载本 Skill。
Router 不做基金推荐、不做购买确认、不做适当性校验，也不调用基金业务 API。

## 执行 Agent 职责

fund-agent 负责基金名称或代码理解、产品查询、风险揭示、适当性校验、持仓或净值查询，以及结构化结果输出。

## 误派处理

如果任务不属于基金查询，fund-agent 返回 `ishandover=true`，并让 `output.data=[]`，Router 据此转交兜底 Agent。
