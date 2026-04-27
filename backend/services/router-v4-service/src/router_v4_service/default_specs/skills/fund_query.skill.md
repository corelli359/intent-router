# Skill：fund_query

## 元数据

- skill_id: fund_query
- version: 0.1.0
- owner_agent: fund-agent
- task_type: fund_query

## 执行边界

本 Skill 只处理基金查询。Intent ReAct 第一步只读取 `intent.md` 中的 `skill_ref` 和意图说明，不加载本 Skill 正文，也不提取基金产品字段。

本 Skill 负责基金产品解析、产品代码识别、风险等级查询、净值查询、收益查询、持仓查询和结构化输出。

## 输入

- router_session_id
- task_id
- intent_id
- scene_id
- raw_message
- context_refs

## 内部状态

- fund_scope：基金查询范围
- product_id：基金产品标识
- query_status：查询状态
- suitability_status：适当性状态

## 执行步骤

1. 确认任务属于基金查询。
2. 解析基金产品、基金代码或产品范围。
3. 查询产品概况、风险等级、净值、持仓或收益数据。
4. 在需要时执行适当性和信息披露规则。
5. 返回结构化基金数据。

## 槽位策略

- 基金产品解析由本 Skill 的业务说明负责。
- Router 不提取基金名称、代码、风险等级、收益区间等业务字段。
- 用户表达不完整时，由本 Skill 决定追问、按上下文补全或返回可选范围。

## 误派处理

如果任务不属于基金查询，返回 `ishandover=true` 且 `output.data=[]`。

## 输出契约

执行完成时，输出中包含 `data[0].type=fund`。
