# 转账 Skill 卡片 v0.1

owner: transfer-agent
scene_id: transfer
task_type: transfer

## Router 使用边界

Router 只把这张卡片当成场景提供的 Skill 元数据，用于意图识别、上下文构建和任务派发。
Router 不执行转账流程，不调用风控、限额或转账 API，也不生成面向用户的最终办理结果。

## 执行 Agent 职责

transfer-agent 负责业务理解、自由表达提槽、收款人和金额校验、业务缺槽追问、风控、限额、用户确认、幂等控制、转账 API 调用和结构化结果输出。

字段收集由本 Skill 声明并由 transfer-agent 执行：

- `recipient`：收款人姓名、称呼或可解析的收款对象。
- `amount`：转账金额，必须在用户表达中明确出现。

用户一次性说出“我要转账300给小红”时，Agent 应一次性提取收款人和金额并进入确认；用户只说“我要转账”时，Agent 应一次性追问“转给谁”和“转账金额”。

## 误派处理

如果任务不属于转账，transfer-agent 返回 `ishandover=true`，并让 `output.data=[]`，Router 据此转交兜底 Agent。
