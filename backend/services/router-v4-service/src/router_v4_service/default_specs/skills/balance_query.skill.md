# Skill：balance_query

## 元数据

- skill_id: balance_query
- version: 0.1.0
- owner_agent: balance-agent
- task_type: balance_query

## 执行边界

本 Skill 只处理余额查询。Intent ReAct 第一步只读取 `intent.md` 中的 `skill_ref` 和意图说明，不加载本 Skill 正文，也不提取账户范围。

本 Skill 负责账户范围识别、鉴权、权限校验、余额服务调用和结构化输出。

## 输入

- router_session_id
- task_id
- intent_id
- scene_id
- raw_message
- context_refs

## 内部状态

- account_scope：账户范围，由 Skill ReAct 决策
- auth_status：鉴权状态
- query_status：查询状态

## 执行步骤

1. 确认任务属于余额查询。
2. 基于用户表达和可用上下文解析账户范围。
3. 执行鉴权和权限检查。
4. 调用余额查询服务。
5. 返回结构化余额数据。

## 槽位策略

- 账户范围由本 Skill 的业务说明负责。
- Router 不提取账户范围，不判断默认账户。
- 用户没有说明账户时，由本 Skill 根据业务规则决定追问或使用默认账户策略。

## 误派处理

如果任务不属于余额查询，返回 `ishandover=true` 且 `output.data=[]`。

## 输出契约

执行完成时，输出中包含 `data[0].type=balance`。
