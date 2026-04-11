# 主动推荐执行分流设计

## 1. 文档目的

这份文档只回答一个问题：

- 当系统主动给用户推荐若干“已预填完整要素”的事项后，Router、Intent Agent、执行管理服务、执行服务之间应该如何分工？

这份设计基于当前仓库的实际前提：

- 当前仓库已经有 `Router + Graph Runtime + Intent Agent`
- 当前仓库**没有**独立的“执行管理服务”和“各意图执行服务”
- 当前内置 agent 目前把“要素确认 + 执行模拟”合并在一起

因此本文会同时给出：

1. **目标架构**
2. **当前简化实现与目标架构的差距**
3. **在当前仓库里如何逐步演进，而不是一次性把全部服务补齐**

## 2. 需求澄清后的标准流程

这里把需求收敛成三个模式。

### 2.1 模式 A：自由对话

用户没有先看到主动推荐卡片，而是直接输入自然语言。

这时仍然走当前 V2 的主链路：

1. LLM 意图识别
2. graph factory / graph planner
3. 如有必要进入 intent agent 多轮补充
4. 最终进入执行

### 2.2 模式 B：主动推荐，用户无修改

前端主动向用户展示若干推荐事项。

这些推荐事项在到达 Router 时，默认已经具备：

- `intent_code`
- 完整 `slot_memory`
- 完整 `execution_payload`

用户随后通过自然语言表达：

- “第一个”
- “第一和第三个都要”
- “就按这个来”

且**没有提出任何修改要求**。

这时正确流程不是建 graph，也不是先进入 intent agent，而是：

1. Router 仍然用 LLM 做意图识别 / 选择解析
2. Router 判断这是“基于推荐事项的原样接受”
3. Router 把对应推荐项的业务报文**原封不动**交给执行管理服务
4. 执行管理服务调用各意图下游的执行服务
5. 执行结果回到 Router / 前端

关键点：

- 这里**仍然有意图识别**
- 但识别的结果不是 graph，而是“选中了哪些推荐项，且用户没有修改”

### 2.3 模式 C：主动推荐，用户有修改

用户虽然基于推荐事项回复，但表达了修改、补充、组合、条件、顺序等语义，例如：

- “第一个，但是金额改成 500”
- “第二个和第四个都要，先执行第二个”
- “还是给我弟弟，不是小明”
- “如果余额还够，再执行第三个”

这时正确流程是：

1. Router 用 LLM 识别“用户选中了哪些推荐事项”
2. Router 同时识别“用户是否修改了要素 / 增加了条件 / 改了依赖关系”
3. 只要发生修改，就不允许原样透传执行报文
4. Router 建立 graph
5. graph 节点交给 intent agent 做多轮确认 / 槽位补齐
6. 节点确认完毕后，再交给执行管理服务
7. 执行管理服务调用真实执行服务

关键点：

- “主动推荐”不会取消 graph 能力
- 它只是提供了一个**额外的语义入口**
- 一旦用户改了任何关键数据，就必须回到 graph + agent

## 3. 目标架构分层

目标架构里，职责必须拆清楚。

### 3.1 Frontend

前端负责：

- 向用户展示推荐事项卡片
- 保留推荐项的结构化上下文
- 接收用户自然语言回复
- 把“推荐上下文 + 用户文本”一起发给 Router

前端不负责：

- 直接决定执行哪项业务
- 直接决定是否跳过 intent agent

### 3.2 Intent Router

Router 负责：

- 基于 LLM 识别用户当前真正想要哪些事项
- 判断用户是在“原样接受推荐”还是“修改推荐”
- 决定走：
  - `direct_execute`
  - `interactive_graph`
  - `free_dialog_graph`

Router 不负责：

- 直接执行业务
- 直接调用具体银行/缴费/换汇执行接口

### 3.3 Intent Agent

Intent Agent 的目标职责应该是：

- 槽位补齐
- 参数确认
- 意图内语义约束校验

Intent Agent 不应该承担：

- 最终业务执行

也就是说，目标架构里 agent 应该产出的是：

- `ready_for_execution`

而不是像当前 demo 一样直接产出“转账成功”“换汇成功”。

### 3.4 Execution Manager

执行管理服务负责：

- 接收 Router 或 Agent 输出的标准执行请求
- 统一做幂等、审计、重试、超时、状态归档
- 决定调用哪个执行服务
- 汇总执行结果

它是 Router 和具体执行服务之间的中间层。

### 3.5 Execution Service

每个意图对应的执行服务负责：

- 执行真正的业务动作
- 返回标准化业务结果

例如：

- 转账执行服务
- 余额查询执行服务
- 信用卡账单查询服务
- 燃气缴费执行服务
- 换汇执行服务

## 4. Router 需要新增的判定结果

为了支持上面的分流，Router 首轮输出不能只停留在：

- `primary_intents`
- `candidate_intents`

还需要新增一个更高层的路由决策结果。

建议增加如下结构：

```json
{
  "route_mode": "direct_execute | interactive_graph | free_dialog_graph | no_match",
  "selected_recommendation_ids": ["rec_item_1", "rec_item_3"],
  "selected_intents": ["transfer_money", "exchange_forex"],
  "has_user_modification": true,
  "modification_reasons": [
    "slot_value_changed",
    "condition_added"
  ],
  "reason": "用户引用了推荐事项，并修改了金额与依赖关系"
}
```

其中最关键的是：

- `route_mode`
- `has_user_modification`

### 4.1 什么时候可以 `direct_execute`

必须同时满足：

1. 当前消息明确选择了推荐项
2. 选中的推荐项已经带完整 `execution_payload`
3. 用户没有修改任何关键槽位
4. 用户没有新增条件、顺序、并行、附加意图
5. 用户没有表达“先确认一下”“再问我一下”之类的人机确认要求

只有满足这 5 条，才能不建 graph。

### 4.2 什么时候必须 `interactive_graph`

只要出现以下任一情况，就必须转 graph：

- 改金额
- 改收款人
- 改卡号
- 改币种
- 新增条件
- 新增顺序 / 并行关系
- 新增一个原推荐中不存在的意图
- 删除原推荐中的一部分意图后又补了额外说明
- 参数不完整，需要 agent 补齐

## 5. 推荐事项的数据契约

主动推荐不能只给前端一组“标题文案”，而要带标准结构。

建议推荐项至少包含：

```json
{
  "recommendation_item_id": "rec_xxx",
  "intent_code": "transfer_money",
  "title": "给小明转账 1000 元",
  "slot_memory": {
    "recipient_name": "小明",
    "amount": "1000"
  },
  "execution_payload": {
    "recipientName": "小明",
    "amount": "1000",
    "accountId": "acct_001"
  },
  "execution_service": "transfer-executor",
  "prefill_complete": true,
  "allow_direct_execute": true
}
```

关键点：

- `slot_memory` 给 Router / Agent 看
- `execution_payload` 给执行管理服务和执行服务看
- 二者不能混为一个字段

## 6. 目标链路设计

### 6.1 原样接受链路

```text
Frontend 推荐卡片
  -> 用户自然语言选择
  -> Router 识别“选中了哪些卡片，且无修改”
  -> Router 生成 DirectExecutionDispatch
  -> Execution Manager
  -> 各执行服务
```

这里 Router 不建 graph，也不进 intent agent。

### 6.2 修改后确认链路

```text
Frontend 推荐卡片
  -> 用户自然语言选择并修改
  -> Router 识别“选中了哪些卡片 + 改了什么”
  -> Router 建 graph
  -> Intent Agent 多轮确认 / 补槽
  -> 节点 ready_for_execution
  -> Execution Manager
  -> 各执行服务
```

这里 graph 是必须的。

### 6.3 自由输入链路

```text
用户自由输入
  -> Router 识别
  -> Router 建 graph
  -> Intent Agent
  -> Execution Manager
  -> 执行服务
```

## 7. 当前仓库与目标架构的差距

当前仓库已经有：

- LLM 意图识别
- graph runtime
- 节点级多轮补充
- pending / cancel / replan
- 推荐上下文进入 Router 识别链路

当前仓库还没有：

- 独立的 `Execution Manager`
- 独立的 `Execution Service`
- `Agent -> ready_for_execution -> 执行` 的分层
- `direct_execute` 与 `interactive_graph` 的正式分流结果模型

当前仓库的简化实现是：

- intent agent 识别完就直接模拟业务完成

这对当前验证“意图识别 + graph runtime”是足够的，但它不是最终目标架构。

## 8. 当前仓库里的正确演进方式

这里的重点不是“立刻补一堆服务”，而是**先把边界抽象对**。

### 8.1 第一阶段：把执行能力抽象出来

新增统一抽象：

- `ExecutionManagerClient`
- `ExecutionRequestEnvelope`
- `ExecutionResult`

然后让当前 demo 先接一个本地 mock manager。

这样即使现在没有真实执行服务，Router 和 Agent 的接口也先对齐。

### 8.2 第二阶段：把 Agent 从“执行者”改成“确认者”

当前 agent 的返回值建议分裂成两类：

- `waiting_user_input`
- `ready_for_execution`

不要继续把“转账成功”直接当 agent 的终态。

### 8.3 第三阶段：补 Router 的双分流

Router 首轮收到：

- `content`
- `recommendation_context`

后，LLM 不只要识别 intent，还要判断：

- 用户是否只是选了推荐项
- 还是改了推荐项

然后分流到：

- `direct_execute`
- `interactive_graph`

### 8.4 第四阶段：把 graph 节点终点改成执行请求

graph node 完成后，不直接写“业务成功”，而是：

1. 产出标准执行报文
2. 提交给 execution manager
3. 由 manager 返回执行结果

### 8.5 第五阶段：再补真实执行服务

等上面接口稳定后，再把当前 demo 里的模拟执行逻辑拆到：

- 转账执行服务
- 查询余额执行服务
- 换汇执行服务
- 燃气缴费执行服务

## 9. 建议的开发顺序

建议顺序如下。

### 9.1 P0：数据契约落模

先补这些正式模型：

- `RecommendationContextPayload`
- `RecommendationItem`
- `ExecutionRequestEnvelope`
- `ExecutionTarget`
- `RouteModeDecision`

### 9.2 P1：Router 分流决策

新增一层“推荐场景分流判定”：

- 识别选择了哪些推荐项
- 识别是否发生修改
- 输出 `direct_execute / interactive_graph`

### 9.3 P2：Execution Manager 抽象

哪怕先只有 mock 实现，也要先把接口立住。

### 9.4 P3：Agent 终态重构

Agent 的“完成”要区分：

- `semantic_completed`
- `execution_completed`

在目标架构里，agent 应该只负责前者。

### 9.5 P4：E2E 集成测试

重点不是单测，而是端到端场景：

- 推荐项原样接受
- 推荐项改金额
- 推荐项改收款人
- 推荐项增加条件
- 推荐项组合多个事项
- 推荐项之外再新增一个新意图

## 10. 对当前实现的结论

当前这套“推荐上下文进对话、仍然走 LLM 意图识别”的实现方向是对的，因为它保住了：

- 推荐只是上下文
- 用户仍然用自然语言表达
- Router 仍然做意图识别

但它还只完成了**前半段语义入口**，没有完成你真正要的**后半段执行分流架构**。

也就是说，下一阶段真正该补的不是更多前端花样，而是：

- `Router 分流`
- `Execution Manager`
- `Agent / Executor 职责拆分`

这三件事补齐后，整条链路才算真正贴合你的目标需求。

## 11. 当前分支落地状态（2026-04-09）

当前分支已经把“主动推荐模式”和“自由对话模式”拆成了两条不同的入口语义，但执行层仍然保持 demo 级简化。

### 11.1 已落地

- `POST /api/router/v2/sessions/{session_id}/messages` 已支持 `proactiveRecommendation`
- Router 新增独立的 `LLMProactiveRecommendationRouter`
- 当前支持 4 种推荐模式分流结果：
  - `no_selection`
  - `direct_execute`
  - `interactive_graph`
  - `switch_to_free_dialog`
- `/chat/v2` 的推荐卡片现在会把完整预填数据一起发给 Router：
  - `recommendationItemId`
  - `intentCode`
  - `title`
  - `description`
  - `slotMemory`
  - `executionPayload`
  - `allowDirectExecute`

### 11.2 当前运行时语义

- `direct_execute`
  - 对外入口已经是 `proactiveRecommendation`
  - 当前仓库里内部仍暂时复用 `guidedSelection` 直达执行图
  - 如果某个推荐项 `allowDirectExecute=false`，运行时会强制降级到 `interactive_graph`
- `interactive_graph`
  - 上游选中的推荐项先转成 recognition hint 和默认槽位种子
  - graph builder / planner 仍然基于用户当前自然语言做图规划
  - 推荐项的 `slotMemory` 会作为节点默认要素并回灌给 graph node
  - 用户对金额、对象、条件、顺序的修改仍然在 graph runtime 内处理
- `switch_to_free_dialog`
  - 会完整退回原有自由对话识别链路
  - 不继承推荐项默认要素

### 11.3 仍未落地

- 还没有独立 `ExecutionManager`
- 还没有各意图独立的真实 `ExecutionService`
- 当前 agent 仍然是“要素确认 + 模拟执行”合一

因此，当前版本已经完成的是：

- 主动推荐模式的语义分流
- 推荐项预填要素进入 Router / graph runtime
- 不破坏原有自由对话模式

尚未完成的是：

- Router -> Execution Manager -> Execution Service 的生产级拆层
