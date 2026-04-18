# Router Service 高性能业务对象运行时设计

## 1. 背景

当前 `router-service` 的核心运行时模型仍然是“一个 session 绑定一个 `current_graph`，最多再挂一个 `pending_graph`”。这套模型在功能上已经能工作，但对于高性能版场景存在几个明显问题：

1. `analyze` 形成了一条旁路能力。
   - `POST /messages/analyze`
   - `POST /messages` + `executionMode=analyze_only`
   这两条链路不落 session、不进入真实业务编排，只返回识别与提槽结果，适合调试，不适合生产高性能路径。

2. 业务对象与 session 生命周期耦合过深。
   - 当前 graph 既承担了业务对象职责，又承担了会话内唯一运行实体职责。
   - 当用户在未完成业务中插入新业务时，现有实现倾向于“取消当前 graph 并重规划”，而不是“挂起旧业务、创建新业务”。

3. 编排状态与业务对象没有分层。
   - 已完成业务对象为了支持后续流程推进，往往被迫继续驻留在 session 内。
   - 多意图、多阶段编排会逐步把 session 膨胀成重对象。

4. 对外返回依赖 `snapshot` 思维。
   - 当前已经主要在序列化 live session，但领域上仍然存在 `GraphRouterSnapshot`、`snapshot()` 之类的旧语义。
   - 高性能版更适合统一内存对象 + `dump/serialize` 输出能力，而不是再维护一套并行快照模型。

5. 压测安全阀不完整。
   - 已有 LLM barrier。
   - 对 agent 调度仍应补齐 barrier，避免误把压测流量打进真实执行链路。

## 2. 设计目标

### 2.1 总体目标

将 router runtime 重构为“session 容器 + 业务对象 + 编排状态 + 共享缓存”的统一模型，删除 `analyze` 旁路，只保留真实业务路径和 `router_only` 边界返回能力。

### 2.2 必达目标

1. 删除 `analyze` 相关 API、参数、返回模型和测试用例。
2. 保留 `router_only`。
3. `router_only` 不再视为旁路，而是统一业务对象运行时上的一种 dispatch 策略。
4. 一个 session 内允许存在多个业务对象。
5. 当当前业务未完成时，允许插入新业务对象并切换焦点。
6. 当某个业务对象 `ishandover=true` 时：
   - 将必要记忆沉淀到 session 共享缓存
   - 将业务摘要沉淀到业务缓存
   - 释放该业务对象运行态
7. 编排状态与业务对象分离。
   - 已完成业务对象释放后，后续对象仍可继续执行。
8. 增加 agent barrier。

### 2.3 非目标

1. 本次不追求替换所有 API 返回字段。
   - 外部兼容字段如 `current_graph` / `pending_graph` 可保留为 dump 视图。
2. 本次不引入外部持久化状态机。
   - 仍以进程内 session store 为主。
3. 本次不重做 LLM/graph planner 本身。
   - 重点是运行时对象模型和生命周期。

## 3. 核心概念

### 3.1 Session

`session` 是用户会话容器，不等于单次业务。

职责：

1. 维护共享消息上下文。
2. 维护共享槽位缓存。
3. 维护业务对象集合。
4. 维护编排焦点与恢复顺序。
5. 对外提供统一 dump 视图。

不再把 session 理解成“当前只有一个业务 graph 在跑”的单对象模型。

### 3.2 Business Object

业务对象是单次业务运行单元，例如：

1. 一次转账
2. 一次缴费
3. 一次余额查询
4. 一个由多个叶子节点组成、但语义上属于同一业务编排单元的 graph

业务对象持有：

1. 自身 graph/runtime 状态
2. 当前等待点
3. 当前提槽结果
4. router_only / execute 调度策略
5. handover 状态

业务对象是短生命周期对象。

### 3.3 Workflow / Orchestration State

编排状态是 session 内部的流程控制层，不等于业务对象本身。

它至少需要表达：

1. 当前聚焦业务对象
2. 挂起业务对象栈
3. 待确认业务对象
4. 已完成业务对象摘要
5. 后续可恢复的顺序信息
6. 条件判断结果或依赖摘要

核心原则：

- 流程继续推进依赖的是“编排状态 + 已完成业务摘要”
- 不是“把所有已完成业务对象完整留在内存里”

### 3.4 Shared Cache

session 级共享缓存用于承接业务对象 handover 后仍需复用的数据。

建议包含：

1. 最近若干轮对话文本
2. 公共槽位
3. 最近业务对象摘要
4. 对后续业务仍有意义的条件结果
5. 轻量诊断摘要

不应把整个 graph 对象原样塞进缓存。

## 4. 运行时模型

### 4.1 目标结构

```text
Session
├── messages
├── shared_slot_memory
├── business_memory_digests
├── business_objects
│   ├── business_A (转账, suspended)
│   └── business_B (缴费, active)
├── workflow
│   ├── focus_business_id
│   ├── pending_business_id
│   ├── suspended_business_ids
│   └── completed_business_ids
└── dump()
```

### 4.2 当前视图兼容

为了降低改造冲击，session 对外仍可保留兼容视图：

1. `current_graph`
2. `pending_graph`
3. `active_node_id`

但这些字段不再代表 session 的完整真实状态，只代表：

- 当前焦点业务对象的图视图
- 当前待确认业务对象的图视图

即：

- `current_graph` = focus business 的 graph dump
- `pending_graph` = pending business 的 graph dump

领域上的真实状态应以业务对象集合和 workflow 为准。

## 5. 生命周期设计

### 5.1 创建业务对象

当一条新消息进来：

1. 若没有活跃或挂起业务对象，则创建新业务对象。
2. 若存在等待中的业务对象：
   - 若当前消息仍属于该业务，继续该对象
   - 若识别到新的主业务，则挂起当前业务对象，创建新业务对象

### 5.2 挂起业务对象

当等待中的业务对象遇到新的主业务：

1. 不再直接取消当前 graph
2. 将其状态变为 `suspended`
3. 保留其 graph/task/waiting node 运行态
4. workflow 记录恢复顺序
5. 新业务对象成为 focus business

这样可支持：

1. 转账未完成，插入缴费
2. 缴费完成后，返回继续转账

### 5.3 router_only

`router_only` 不再是旁路接口能力，而是统一运行时的 dispatch 策略。

业务对象在 `router_only` 下：

1. 正常进行识别
2. 正常进行提槽
3. 正常建图
4. 正常推进到 `READY_FOR_DISPATCH`
5. 不调用 agent
6. 直接 dump 返回
7. 默认视为当前业务对象已完成 handover

即：

- `router_only` = “到路由边界即返回”
- 不是 “走另一套 analyze-only runtime”

### 5.4 handover

`ishandover` 应是业务对象级信号，不是 session 级信号。

含义：

1. 当前业务对象已经完成当前阶段交接
2. 必要记忆已可沉淀
3. 当前业务对象可释放运行态

默认终态：

1. `router_only + READY_FOR_DISPATCH`
2. `COMPLETED`
3. `PARTIALLY_COMPLETED`
4. `FAILED`
5. `CANCELLED`

后续若业务约束增加，不一定只有一个 `ishandover` 标签，但原理不变：

- 命中终态条件
- 生成摘要
- 写缓存
- 释放运行态

### 5.5 释放与恢复

业务对象 handover 后：

1. 提取共享槽位
2. 写入 business digest
3. 从 session 的 active business 集合移除
4. 若 workflow 中还有挂起业务对象，则恢复下一个业务对象
5. 若没有，则 session 回到空闲态

关键点：

- 删除的是业务对象运行态
- 保留的是共享缓存和编排恢复能力

## 6. 多意图统一原则

多意图不需要特殊的另一套模型。

原则如下：

1. 多意图本质上仍然是多个业务单元的编排。
2. 已完成业务对象可以释放。
3. 编排能否继续，依赖：
   - workflow state
   - business digest
   - shared cache
4. 不依赖完整已完成对象常驻内存。

即便存在条件分支：

1. 前置业务对象完成后
2. 只保留条件结果与必要输出摘要
3. 后继业务对象即可继续决策和执行

## 7. analyze 下线方案

### 7.1 删除内容

1. `POST /sessions/{session_id}/messages/analyze`
2. `executionMode=analyze_only`
3. `analysisMode`
4. `MessageAnalysisPayload`
5. `MessageAnalysisResult`
6. 前端/脚本/压测用例中对 `analysis` 结构的依赖

### 7.2 替代方式

统一改为：

```json
{
  "content": "给小明转500元",
  "executionMode": "router_only"
}
```

并从返回 dump 中读取：

1. `snapshot.current_graph.nodes[*].intent_code`
2. `snapshot.current_graph.nodes[*].slot_memory`
3. `snapshot.current_graph.status`

### 7.3 收益

1. 删除非生产旁路
2. 压测链路更接近真实生产
3. 降低代码分支复杂度

## 8. 共享缓存设计

### 8.1 共享槽位

`shared_slot_memory`

用途：

1. 业务完成后保留公共槽位
2. 新业务对象编译时参与历史预填充
3. 减少已完成业务对象常驻内存的必要性

典型内容：

1. 卡号
2. 手机尾号
3. 收款人
4. 金额
5. 币种

### 8.2 业务摘要

`business_memory_digests`

建议包含：

1. `business_id`
2. `intent_codes`
3. `status`
4. `ishandover`
5. `summary`
6. `slot_memory`
7. `created_at`
8. `finished_at`

### 8.3 对长时记忆的关系

本次新增的 session 共享缓存是“会话级运行时缓存”，与已有 `LongTermMemoryStore` 互补：

1. session 共享缓存用于本次会话内快速恢复
2. long-term memory 用于跨会话复用

## 9. Agent Barrier

### 9.1 为什么需要

即使压测主路径走 `router_only`，仍应补充 agent barrier：

1. 防误把压测流量打到真实 agent
2. 防止执行链路配置错误造成误调度
3. 避免以“连接失败/超时”这种噪声结果污染压测判断

### 9.2 行为要求

当 barrier 开启时：

1. router 若进入 agent dispatch，必须 fail-fast
2. 错误应明确指出被 barrier 阻断
3. 不应继续发真实 HTTP 请求

## 10. 落地实现方案

### 10.1 第一步：模型扩展

扩展 `GraphSessionState`，引入：

1. `business_objects`
2. `workflow`
3. `shared_slot_memory`
4. `business_memory_digests`

保留兼容视图：

1. `current_graph`
2. `pending_graph`
3. `active_node_id`

### 10.2 第二步：切换策略

修改等待态 turn handling：

1. `pending_graph + replan`
   - 不再直接丢弃旧业务
   - 改为挂起旧业务，创建新业务
2. `waiting_node + replan`
   - 不再取消当前业务
   - 改为挂起当前业务，创建新业务

### 10.3 第三步：handover 回收

当业务对象进入 handover 终态：

1. 汇总槽位到 `shared_slot_memory`
2. 生成 `business_memory_digest`
3. 从活跃业务对象集合中移除
4. 恢复下一个挂起业务对象
5. 对外 dump 仍保留本次返回需要的图内容

### 10.4 第四步：删掉 analyze

同步删除：

1. 路由入口
2. orchestrator 分支
3. 模型
4. 测试
5. 压测用例
6. 前端文案与兼容逻辑

### 10.5 第五步：压测链路切换

admin perf 默认用例切到：

1. `executionMode=router_only`
2. 期望值从 `snapshot.current_graph` 提取
3. 不再读取 `analysis`

## 11. 兼容性策略

### 11.1 对 API 调用方

短期兼容：

1. `snapshot.current_graph`
2. `snapshot.pending_graph`

不再兼容：

1. `/messages/analyze`
2. `executionMode=analyze_only`

### 11.2 对现有测试

需要更新的测试类别：

1. router API analyze 测试
2. admin perf case 测试
3. 等待态 replan 行为测试

需要新增的测试类别：

1. waiting business 被挂起后创建新业务
2. 新业务完成后恢复旧业务
3. `router_only` handover 后业务对象释放
4. 已完成业务槽位进入 shared cache
5. agent barrier 生效

## 12. 风险

1. 当前很多模块直接读写 `session.current_graph` / `session.pending_graph`
   - 需要通过兼容视图平滑过渡
2. handover 释放过早可能导致响应丢失 graph 内容
   - 需要 response dump 和运行态释放解耦
3. 等待态切换若处理不稳，会影响原有补槽流程
   - 必须增加回归测试

## 13. 验收标准

1. `analyze` 相关入口、模型、文档、测试全部下线
2. `router_only` 能直接返回意图与槽位结果，不触发 agent
3. session 内支持业务插入与恢复
4. 业务 handover 后会写共享缓存并释放运行态
5. 多意图/条件流程在业务对象释放后仍能继续推进
6. admin 性能测试改走 `router_only`
7. agent barrier 可阻断误调度
8. 所有相关测试通过
