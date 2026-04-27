# 转账执行 Skill v0.1

owner: transfer-agent
scene_id: transfer
task_type: transfer

## 执行边界

transfer-agent 只处理转账办理。Router 只派发原始表达、上下文引用和 Skill 元数据，不提供最终业务事实；业务提槽是否发生、提哪些字段、怎么追问都由本 Skill 决定。

本 Skill 声明的转账字段：

- `recipient`：收款人姓名、称呼或可解析的收款对象。
- `amount`：转账金额，必须在用户表达中明确出现。

## 生命周期

1. 读取 Router 任务快照，确认 `scene_id=transfer` 且目标 Agent 为 `transfer-agent`。
2. 渐进加载本 Skill，确定本场景由 Agent 负责收款人、金额、确认、风控、限额和业务 API。
3. 每轮都先整体理解用户自由表达，一次性提取能拿到的收款人和金额。
4. 缺哪些字段就一次性追问哪些字段；如果收款人和金额都缺失，要同时询问“转给谁”和“转账金额”。
5. 收款人和金额齐备后，必须询问用户确认，不能直接转账。
6. 用户确认后，执行风控、限额和转账 API，再通过 Router `agent-output` 回写结构化结果。
7. 如果任务不属于转账，返回 `ishandover=true` 且 `output.data=[]`，交由 Router 派发兜底 Agent。

## 输出约定

完成时输出：

- `data[0].type=transfer_result`
- `data[0].status=success`
- `risk.status=passed`
- `limit.status=passed`
- `business_api.name=transfer.submit`
