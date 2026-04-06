# 基于 LLMCompiler 的意图路由增强研究报告

## 1. 结论摘要

结论先行：

- `LLMCompiler` 不能直接当作本项目的“多轮多意图路由器”拿来替换现有实现。
- `LLMCompiler` 非常适合作为本项目后续“多意图执行规划器”的设计参考，尤其适合引入以下三类能力：
  - 把多个意图或多个子任务表达成显式依赖图，而不是当前的简单串行队列。
  - 识别可并行执行的后台任务，降低整体执行时延。
  - 在中间结果出现后做动态重规划，从而支持带条件依赖的执行流程。
- 对本项目最合理的方式不是“直接集成整个 LLMCompiler 仓库”，而是“借鉴其 Planner + Task Fetching Unit + Replan 思路，在现有 `router_core` 内实现一个面向外部 intent agent 的 DAG 规划与调度层”。

如果目标是“基于与用户实时多轮对话的多意图识别 + 可能带条件依赖的动态执行规划”，我的判断是：

- `LLMCompiler` 对“动态执行规划”这半个问题是强相关方案。
- `LLMCompiler` 对“实时多轮对话、waiting/resume、人机交互挂起、意图切换、会话状态管理”这半个问题不是现成答案。
- 因此它能满足你的需求的一部分，但必须与本项目现有会话状态机深度结合，不能单独使用。

## 2. 研究对象

本次判断主要基于以下一手资料：

- 论文：<https://arxiv.org/abs/2312.04511>
- 论文正式版本页面：<https://proceedings.mlr.press/v235/kim24y.html>
- 官方实现仓库：<https://github.com/SqueezeAILab/LLMCompiler>

同时对照了本项目当前实现，重点看了这些模块：

- `backend/src/router_core/orchestrator.py`
- `backend/src/router_core/task_queue.py`
- `backend/src/router_core/domain.py`
- `backend/src/router_core/recognizer.py`
- `backend/src/router_core/context_builder.py`
- `backend/tests/test_router_api.py`

## 3. LLMCompiler 到底解决了什么

根据论文和官方实现，`LLMCompiler` 的核心不是“意图识别”，而是“把一个复杂请求编译成可执行任务图并高效调度”。

它的三个关键组件如下：

- `Function Calling Planner`
  - 用 LLM 把用户请求拆成多个任务。
  - 每个任务带有依赖关系。
  - 依赖通过类似 `$1`、`$2` 这样的前序任务引用表达。
- `Task Fetching Unit`
  - 维护哪些任务已经完成。
  - 一旦某个任务的依赖满足，就立刻把它交给执行器。
  - 会把占位符变量替换成上游任务的真实结果。
- `Executor`
  - 并行执行已经 ready 的任务。

论文里特别强调的能力有三点：

- 静态 DAG 规划
  - 先把任务图规划出来，再按依赖执行。
- 流式规划
  - Planner 不必等整张图完全生成后再执行；可以边规划边下发任务。
- 重规划
  - 当中间结果决定后续分支时，可以把已有执行结果再送回 Planner，生成新图。

这套机制本质上更像“LLM 驱动的 DAG 编译器”，而不是“聊天式 agent 路由器”。

## 4. LLMCompiler 官方实现的能力边界

从官方仓库实现看，它更偏研究验证代码，而不是可直接接入本项目的生产运行时。

### 4.1 它天然擅长的部分

- 给定一组工具定义，生成任务分解计划。
- 用依赖图表示哪些步骤可以并行、哪些必须串行。
- 在依赖满足后立刻调度 ready 任务。
- 在需要时做 replanning。
- 对“工具调用型”问题有很强适配性，例如搜索、计算、分析、汇总。

### 4.2 它天然不擅长的部分

- 多轮会话状态管理。
- 用户补槽后的 waiting/resume。
- 面向外部 HTTP intent agent 的统一协议编排。
- Router 级别的计划确认卡片、业务确认卡片、SSE 事件协议。
- 意图目录治理、启停、阈值、优先级、fallback。
- 安全约束下的“只允许一个任务与用户交互，其余后台任务可并行”。

### 4.3 为什么不能直接整仓引入

主要有五个原因：

- 官方实现的“工具”是进程内函数，不是本项目的外部 `agent_url` 服务。
- 它的 Planner 输出是类 Python 的文本 DSL，研究验证可用，但生产环境更适合换成 JSON Schema 约束输出。
- 它没有本项目已经具备的会话态、任务态、SSE 事件、恢复逻辑。
- 它没有“用户输入本身也是一种异步依赖”的建模。
- 它的 join/replan 逻辑是面向最终答案生成，不是面向路由层任务生命周期管理。

因此，正确姿势是“抽象借鉴”，不是“代码级照搬”。

## 5. 本项目当前能力现状

当前项目已经具备一个比较清晰的 V1 路由内核，且基础并不弱。

### 5.1 已有能力

从 `router_core` 当前实现看，已经具备：

- 基于注册意图清单的多意图识别。
- 单轮识别出多个主意图后，先生成待确认计划。
- 计划确认后，把多个任务放入队列按顺序执行。
- 当队首任务 `waiting_user_input` 或 `waiting_confirmation` 时暂停队列。
- 用户补充输入后恢复原任务继续执行。
- 当 waiting 态下识别到明显新意图时，取消旧任务并切换。
- 长短期记忆和最近消息上下文注入。
- 统一的 SSE 任务事件协议。

这些能力主要落在：

- `orchestrator.py`
- `task_queue.py`
- `agent_client.py`
- `domain.py`

### 5.2 当前短板

但从“多意图 + 条件依赖 + 动态规划”的目标看，当前实现仍然是队列模型，不是图模型。

核心短板如下：

- 任务之间只有顺序，没有显式依赖边。
- 无法表达“B 依赖 A 的结果”。
- 无法表达“若 A 成功且余额足够，则执行 B；否则执行 C”。
- 无法表达“两个非交互后台任务先并行，汇总后再进入下一个任务”。
- 无法表达“先执行一部分，再根据中间结果重规划剩余步骤”。
- 当前多意图计划本质上是 `list[SessionPlanItem]`，不是可执行 DAG。

换句话说，项目已经解决了“多轮任务状态机”，但还没有解决“图式规划器”。

## 6. LLMCompiler 是否满足你的需求

### 6.1 结论

部分满足，但不能直接满足。

### 6.2 分维度判断

| 需求维度 | LLMCompiler 原生支持 | 对本项目是否够用 | 判断 |
| --- | --- | --- | --- |
| 单轮复杂请求拆解 | 支持 | 基本够用 | 强项 |
| 显式依赖图 | 支持 | 够用 | 强项 |
| 可并行任务发现 | 支持 | 够用 | 强项 |
| 动态重规划 | 支持 | 基本够用 | 强项 |
| 多意图识别 | 不直接支持 | 不够 | 需保留现有 recognizer |
| 多轮对话补槽 | 不支持 | 不够 | 需保留现有 orchestrator 能力 |
| waiting/resume | 不支持 | 不够 | 必须自行实现 |
| 计划确认 / 业务确认 | 不支持 | 不够 | 必须自行实现 |
| 外部 HTTP agent 调度 | 不直接支持 | 不够 | 需按本项目协议封装 |
| 持久化与可观测性 | 很弱 | 不够 | 需沿用现有基础设施 |

### 6.3 最重要的判断

如果你的目标是：

- 用户一句话里有多个诉求；
- 这些诉求之间可能有前后依赖；
- 有些步骤需要看前一步结果再决定；
- 有些步骤可以并行；
- 整个过程还要允许用户中途补信息、确认、切换；

那么 `LLMCompiler` 可以承担其中的“规划器 + 依赖调度器”角色，但不能承担整个“对话路由内核”角色。

## 7. 推荐的落地方式

推荐方案：

- 保留现有 `IntentRecognizer` 作为“候选意图收缩器”。
- 在 `RouterOrchestrator` 之上增加一层“Graph Planner”。
- 用 `LLMCompiler` 的思想生成任务图，但执行仍走本项目既有的 `Task` / `AgentClient` / SSE 机制。
- 把当前简单队列升级为“受控并行的 DAG 调度器”。

一句话概括：

- 识别仍然是本项目的事。
- 规划借鉴 `LLMCompiler`。
- 执行仍然是本项目的事。

## 8. 面向本项目的目标架构

建议把现有链路升级为下面这个分层：

### 8.1 总体链路

1. 用户输入进入 Router。
2. `IntentRecognizer` 先从注册意图清单中筛出候选意图。
3. 新增 `GraphIntentPlanner` 基于：
   - 当前消息
   - 最近对话
   - 长期记忆
   - 候选意图定义
   - 当前 open task / closed task summary
   生成一个 `ExecutionGraph`。
4. 新增 `GraphScheduler` 找出 ready 节点。
5. 可后台执行且无用户交互的节点允许并行。
6. 需要用户输入或确认的节点进入 `WAITING_*`，并冻结其下游依赖。
7. 新输入到来后：
   - 如果是补当前图中的 open node，则恢复该节点。
   - 如果改变了全局目标，则触发 replan。
8. 图全部完成或取消后，输出最终计划状态。

### 8.2 新增的核心抽象

建议在 `router_core.domain` 中增加以下概念：

- `ExecutionGraph`
  - 当前会话的一张可执行图。
- `PlanNode`
  - 一个节点可以是：
    - `intent_task`
    - `condition`
    - `join`
    - `human_gate`
    - `notify`
- `PlanEdge`
  - 表示依赖关系。
- `DependencyExpression`
  - 表示节点输入如何引用上游输出。
- `NodeRuntimeState`
  - `pending`
  - `ready`
  - `running`
  - `waiting_user_input`
  - `waiting_confirmation`
  - `completed`
  - `failed`
  - `cancelled`
  - `skipped`
- `TaskArtifact`
  - 保存节点产出的结构化结果，供下游引用。

### 8.3 为什么要把用户输入也建模成图的一部分

这是本项目和 LLMCompiler 最大的差异点。

在论文场景里，依赖通常来自工具结果。
在你的场景里，依赖既可能来自：

- 上游 agent 的输出；
- 也可能来自用户下一轮补充输入。

因此本项目需要把“等待用户输入/确认”视为一种特殊依赖屏障，而不只是普通任务状态。

推荐做法：

- `human_gate` 节点不直接调用 agent。
- 它代表“等待外部人类输入”。
- 下游节点只有在 `human_gate` 被满足后才能 ready。

这样就能把多轮对话自然地并入 DAG 执行模型。

## 9. 具体如何借鉴 LLMCompiler

### 9.1 借鉴一：Planner 思想

保留其核心优点：

- 用 LLM 直接产出任务分解和依赖关系。
- 强调“最大并行化”。
- 在计划里显式引用前序节点结果。

但不建议沿用它的文本 DSL。

本项目更适合改成结构化 JSON 输出，例如：

```json
{
  "plan_id": "plan_xxx",
  "nodes": [
    {
      "node_id": "n1",
      "type": "intent_task",
      "intent_code": "query_account_balance",
      "title": "查询账户余额",
      "depends_on": [],
      "interactive": true
    },
    {
      "node_id": "n2",
      "type": "condition",
      "title": "判断余额是否足够",
      "depends_on": ["n1"],
      "condition": {
        "kind": "expression",
        "expr": "artifacts.n1.balance >= 200"
      }
    },
    {
      "node_id": "n3",
      "type": "intent_task",
      "intent_code": "transfer_money",
      "title": "执行转账",
      "depends_on": ["n2"],
      "run_if": "artifacts.n2.result == true",
      "interactive": true
    },
    {
      "node_id": "n4",
      "type": "notify",
      "title": "余额不足提示",
      "depends_on": ["n2"],
      "run_if": "artifacts.n2.result == false"
    }
  ]
}
```

这比文本版 `$1 -> $2` 更适合生产校验、审计和测试。

### 9.2 借鉴二：Task Fetching Unit 思想

`LLMCompiler` 的 `Task Fetching Unit` 很值得引入。

对本项目可直接转化为：

- 新增 `GraphScheduler.next_runnable_nodes(...)`
- 根据依赖完成情况找出 ready 节点
- 对 ready 的后台节点并行执行
- 对 interactive 节点做互斥

建议调度规则如下：

- 同一时刻最多只有 1 个 interactive 节点处于前台。
- 非 interactive 且无互相依赖的节点可并行。
- 任何依赖 interactive 节点结果的下游节点都必须等待。
- 若某节点失败，其下游可按策略：
  - `skip`
  - `replan`
  - `fail_fast`

这正好和你在 PRD 里提到的“受控并行”方向一致。

### 9.3 借鉴三：Replan 思想

这是最值得保留的部分。

在你的场景里，replan 触发条件可以是：

- 条件节点无法静态决定。
- 某个 agent 返回失败，但失败结果允许改道。
- 用户在执行中改变目标。
- 某个任务补槽后暴露出新的依赖。
- 某个 intent 的前提条件被上游任务否决。

推荐引入：

- `PlanReplanner`
- 输入：
  - 原始用户目标
  - 当前图
  - 已完成节点结果
  - 当前 waiting 节点
  - 最近新输入
- 输出：
  - 新图，或
  - 对原图的 patch

在工程上，建议优先做“整图重算”，不要一开始就做图 patch merge。前者更简单、更稳定。

## 10. 不建议照搬的部分

有几处必须明确不建议直接照搬。

### 10.1 不建议把每个 intent agent 生硬包装成 LLMCompiler 的本地 Tool

原因：

- 本项目 intent agent 是独立服务，不是纯函数。
- 它们有自己的流式协议、取消协议、等待用户输入语义。
- 直接包成本地 tool 会丢掉当前 Router 已有的任务生命周期管理。

更合理的方式是：

- 让 Planner 规划 `intent_code` 和依赖关系；
- 真正执行节点时仍然走当前 `StreamingAgentClient`。

### 10.2 不建议把 joiner 当作 Router 的最终答复器

`LLMCompiler` 的 joiner 更像“汇总工具输出，生成最终自然语言答案”的组件。

但 Router 这一层的职责不是回答问题，而是：

- 管任务状态；
- 管执行计划；
- 管路由；
- 管事件流；
- 管等待与恢复。

所以本项目里可以保留一个 `join` 节点概念，但它更适合作为：

- 聚合多个节点结果；
- 生成下一步分支判断输入；
- 或生成 Router 级计划摘要；

而不是替代业务 agent 的回答。

### 10.3 不建议继续使用自由格式 Planner 文本输出

当前项目已经有较强的工程化约束，应直接上结构化输出：

- JSON Schema
- 节点类型白名单
- 条件表达式白名单
- 依赖合法性校验
- 最大节点数 / 最大深度 / 最大并行度约束

## 11. 本项目的最小可行升级方案

如果希望在当前项目基础上稳妥演进，建议分四期做，而不是一步到位重写。

### 第 1 期：先把“多意图列表计划”升级成“静态 DAG 计划”

目标：

- 不改现有执行协议；
- 先把 `SessionPlan.items` 从列表升级为图；
- 仍然先按串行执行，验证 Planner 质量。

本期要做的事：

- 新增 `ExecutionGraph` 数据结构。
- 新增 `GraphIntentPlanner`。
- `orchestrator` 在多主意图时不再只生成列表，而是生成图。
- 计划确认卡片展示节点和依赖关系。

收益：

- 风险最小。
- 先验证规划是否稳定。

### 第 2 期：把队列调度器升级成受控并行 DAG 调度器

目标：

- 非交互后台节点可并行。
- interactive 节点仍保持单前台。

本期要做的事：

- 用 `GraphScheduler` 替换 `task_queue.py` 的简单排序逻辑。
- 增加节点级 ready/block 状态。
- 增加最大并行数配置。

收益：

- 直接得到 latency 改善。
- 与 LLMCompiler 的核心收益最接近。

### 第 3 期：引入条件节点和重规划

目标：

- 支持“如果 A 则 B，否则 C”。
- 支持中间结果驱动的重新规划。

本期要做的事：

- 增加 `condition` 节点。
- 增加 `PlanReplanner`。
- 约定哪些失败是 `replanable`。

收益：

- 开始真正满足“条件依赖的动态执行规划”。

### 第 4 期：把 waiting/resume 正式并入图模型

目标：

- 用户补充输入本身成为图执行的一部分。

本期要做的事：

- 增加 `human_gate` 节点。
- waiting task 不再只是单任务挂起，而是图上节点阻塞。
- 下游节点按 gate 解锁。

收益：

- 最终完成“实时多轮对话 + 动态规划”的统一建模。

## 12. 建议修改的代码位置

下面是最适合落地改造的位置。

### 12.1 `backend/src/router_core/domain.py`

新增：

- `ExecutionGraph`
- `PlanNode`
- `PlanNodeType`
- `PlanNodeStatus`
- `PlanEdge`
- `TaskArtifact`
- `ReplanDecision`

### 12.2 `backend/src/router_core/orchestrator.py`

演进方向：

- 当前以“单任务队列”为中心。
- 后续应切到“图执行状态机”为中心。

建议新增职责：

- 识别后调用 `GraphIntentPlanner`
- 图确认
- 图执行
- 节点级 resume
- replan 触发与切换

### 12.3 `backend/src/router_core/task_queue.py`

建议重构为：

- `graph_scheduler.py`

职责改为：

- 解析 ready 节点
- 维护并发窗口
- 处理 blocked/running/completed 转移

### 12.4 `backend/src/router_core/context_builder.py`

建议增强上下文分层：

- 最近消息
- 当前 open node 工作记忆
- closed task summaries
- 长期记忆
- 当前 execution graph 摘要

这是提高 planner 和 replanner 稳定性的关键。

### 12.5 `backend/src/router_core/prompt_templates.py`

建议新增：

- DAG planner prompt
- replanner prompt
- condition node planning prompt

## 13. 一个更贴近你需求的示例

用户说：

“先帮我查余额，如果工资卡余额够 2000，就给张三转 2000；如果不够，就提醒我余额不足。顺便再查一下信用卡账单。”

这个需求不是简单的多意图并列，而是混合了：

- 一个条件依赖链：
  - 查余额
  - 判断余额
  - 转账或提醒
- 一个可独立并行的查询：
  - 查信用卡账单

更合理的图会是：

- `n1`: 查询工资卡余额
- `n2`: 查询信用卡账单
- `n3`: 条件判断 `n1.balance >= 2000`
- `n4`: 执行转账，依赖 `n3=true`
- `n5`: 余额不足提醒，依赖 `n3=false`
- `n6`: join 汇总执行结果

这里：

- `n1` 和 `n2` 可以并行。
- `n3` 依赖 `n1`。
- `n4` / `n5` 是条件分支。
- 如果 `n4` 需要用户确认收款信息，则 `n4` 会进入 `waiting_confirmation`，其下游暂停。

这类问题正是 `LLMCompiler` 思想能发挥价值的地方。

## 14. 风险与控制措施

### 14.1 最大风险：Planner 不稳定

LLM 规划器最常见的问题是：

- 漏节点
- 乱依赖
- 条件表达错误
- 生成未注册 intent

控制措施：

- 先用 recognizer 把候选意图缩到一个有限集合。
- Planner 只能从候选意图白名单里选。
- 强制结构化 JSON 输出。
- 对依赖图做 schema 和 runtime 校验。

### 14.2 第二风险：并行执行导致用户交互混乱

控制措施：

- interactive 节点全局互斥。
- 默认只并行 non-interactive 节点。
- agent 注册信息里新增 `interaction_mode` / `can_run_in_parallel`。

### 14.3 第三风险：replan 过于频繁

控制措施：

- 限制每轮最大重规划次数。
- 明确哪些状态允许 replan。
- 先从“失败改道”和“条件分支补图”两个场景做起。

## 15. 最终建议

最终建议很明确：

- 不建议把 `LLMCompiler` 当成现成框架直接接进来。
- 建议把它当成“规划与调度方法论”，在本项目 `router_core` 内重写一个适配 intent router 的 DAG 规划层。
- 本项目应继续保留当前已经做得不错的：
  - recognizer
  - waiting/resume
  - SSE 事件
  - 外部 agent 协议
  - 会话态与计划确认
- 在此基础上补上：
  - `ExecutionGraph`
  - `GraphIntentPlanner`
  - `GraphScheduler`
  - `PlanReplanner`
  - `human_gate`

如果只做一句判断：

- `LLMCompiler` 值得用来升级本项目的“执行规划能力”。
- 但它不能单独解决你的“实时多轮对话意图路由”问题。
- 最优路线是“保留现有 Router，会话态不动；引入 LLMCompiler 思想，升级计划表达和调度器”。

## 16. 补充：LangGraph 能否实现你的需求

结论先行：

- 可以，`LangGraph` 能实现你的需求，而且从“运行时编排框架”的角度看，它比直接引入 `LLMCompiler` 更贴近你的整体目标。
- 但 `LangGraph` 解决的是“如何运行一个长生命周期、可中断、可恢复、可分支、可并行的图工作流”，不是“如何自动识别多意图并自动生成高质量规划图”。
- 因此最合理的理解是：
  - `LangGraph` 更适合作为 orchestration runtime。
  - `LLMCompiler` 更适合作为 planner pattern。

换句话说，如果只问“能不能做实时多轮、多意图、条件依赖、动态执行规划”，答案是“能”；但如果问“装上 LangGraph 之后这些能力会不会自动出现”，答案是“不会，仍需要你自己定义识别、规划、节点协议和状态模型”。

### 16.1 为什么说 LangGraph 更贴近你的目标

根据官方文档，`LangGraph` 的定位是一个 “low-level orchestration framework and runtime for building, managing, and deploying long-running, stateful agents”，并且把以下能力作为核心收益：

- durable execution
- streaming
- human-in-the-loop
- memory
- long-running / stateful workflow

这几个关键词和你的目标高度一致，因为你的问题本质上不是“单次工具调用优化”，而是“长生命周期会话中的图执行控制”。

### 16.2 LangGraph 与你的需求逐项映射

#### 1. 实时多轮对话

这点 `LangGraph` 是支持的。

它的 `interrupt()` 机制允许在图执行中某个节点精确暂停，等待外部输入后再继续；恢复时通过 `Command(resume=...)` 把用户回答送回图中。官方文档还明确要求：

- 使用 checkpointer 持久化图状态
- 用 `thread_id` 标识要恢复的那条执行线程

这和本项目当前的 `waiting_user_input` / `waiting_confirmation` 很接近，但 `LangGraph` 的建模更原生，因为“暂停并等待人类输入”本来就是运行时一等公民，而不只是一个任务状态枚举。

#### 2. 条件依赖与动态分支

这点 `LangGraph` 也是强项。

官方 Graph API 直接提供：

- 顺序边
- 并行 fan-out / fan-in
- `add_conditional_edges`
- `Command(goto=...)`
- loops
- map-reduce / `Send`

这意味着下面这些场景都可以自然表达：

- “如果余额足够就转账，否则提醒余额不足”
- “先并行查两个系统，再合并结果”
- “根据中间结果走不同分支”
- “循环追问直到补齐必要槽位或达到上限”

这部分其实比当前本项目能力强很多，也比原始 `LLMCompiler` 更适合做复杂 runtime control flow。

#### 3. 动态执行规划

这里需要区分“运行时支持动态路径”与“自动生成规划”。

`LangGraph` 原生支持：

- 运行时根据 state 动态跳转
- 中断后恢复
- 循环
- 重放 / time travel
- 故障后从 checkpoint 恢复

但它不原生提供：

- 多意图识别器
- LLM 风格 DAG 自动规划器
- 类 `LLMCompiler` 的 plan decomposition prompt

所以如果你需要“从自然语言自动产出一张执行图”，仍然要自己实现：

- planner node
- replanner node
- graph schema validator

也就是说，`LangGraph` 可以承载动态规划执行，但不会替你生成规划。

#### 4. 多意图与状态化执行

这点 `LangGraph` 能做，但需要你自己定义状态模型。

适合本项目的 LangGraph state 建议至少包含：

- `messages`
- `candidate_intents`
- `recognized_intents`
- `execution_graph`
- `active_node_id`
- `task_artifacts`
- `waiting_for`
- `pending_user_response`
- `closed_task_summaries`
- `long_term_memory_refs`

只要状态模型设计合理，LangGraph 可以把当前 Router 的：

- session state
- task state
- pending plan
- waiting / resuming

统一收敛到一张图的 state 里。

#### 5. 持久化、可恢复、长运行

这点 `LangGraph` 明显优于直接拿 `LLMCompiler` 官方仓库改造。

官方 persistence 文档明确引入：

- `thread`
- checkpoint
- fault tolerance
- pending writes
- time travel

官方 durable execution 文档还特别强调：

- 恢复执行时不是从同一行代码继续，而是从一个合适的起点重放
- 带副作用或不确定性的操作要包进 task/node 以保证一致重放

这对你的场景非常关键，因为你的 intent agent 调用、SSE、中途恢复、取消、确认都属于长生命周期有副作用流程。

### 16.3 LangGraph 不能替你解决的部分

即使采用 LangGraph，也还有四件核心工作必须自己做：

#### 1. 多意图识别

LangGraph 不负责从注册 intent 里筛选主意图和候选意图。

你仍然需要：

- 当前项目的 `IntentRecognizer`
- 或新的结构化 recognizer node

#### 2. 自动规划器

LangGraph 本身不是 LLMCompiler。

你仍然需要自己做：

- 从自然语言生成 DAG 的 planner prompt
- 从中间状态重算 DAG 的 replanner prompt
- 节点合法性校验

#### 3. 外部 intent agent 协议

LangGraph 负责图运行，不负责定义你的业务 agent 协议。

你仍然需要保留或重写：

- `agent_url` 调用层
- 流式事件协议
- cancel 协议
- slot memory 合并逻辑

#### 4. 前端展示协议

例如：

- Router 计划卡片
- 业务确认卡片
- 任务进度流
- 节点级等待提示

这些仍然要由你自己设计。

### 16.4 LangGraph 的一个重要限制

虽然 `LangGraph` 支持图、子图和并发，但在官方 subgraph 文档里也明确提示：

- 对带 checkpointer 的 per-thread subagent，如果并行调用，可能产生 checkpoint conflicts
- 官方示例甚至用 `ToolCallLimitMiddleware(..., run_limit=1)` 来限制某些子图的并行调用

这说明两件事：

- `LangGraph` 支持并发，不代表所有架构层次都应该无约束并发。
- 对你的项目仍然应坚持“interactive 节点互斥、后台节点受控并行”的原则。

这和本报告前面给出的“受控并行 DAG 调度器”方向是一致的。

### 16.5 对本项目的实际建议

如果把 `LLMCompiler` 和 `LangGraph` 放在一起比较，我的建议是：

- 如果你要小步快跑、尽量复用当前代码：
  - 保留现有 `router_core`
  - 借鉴 `LLMCompiler` 的 planner / DAG / replan 思想
  - 先自己在当前工程里实现 `ExecutionGraph`
- 如果你准备做一次更明显的 runtime 升级：
  - 优先考虑 `LangGraph` 作为新的编排底座
  - 再把 `LLMCompiler` 风格 planner 作为 LangGraph 中的一个 planner node 或 planner subgraph

也就是说，二者不是二选一关系，更合理的组合是：

- `LangGraph = 运行时图框架`
- `LLMCompiler = 自动规划策略`

### 16.6 我对“是否采用 LangGraph”的最终判断

最终判断如下：

- `LangGraph` 能实现你的需求。
- 从能力匹配度看，它比“直接改造 LLMCompiler 官方仓库”更适合做你的主运行时框架。
- 但对于当前项目而言，直接整体迁移到 `LangGraph` 的改造面会明显大于“在现有 Router 内引入 DAG planner + scheduler”。

所以从工程策略上我建议：

- 近阶段：
  - 不直接迁移 LangGraph
  - 先在现有项目里完成图模型升级
- 中阶段：
  - 如果图模型、重规划、并行控制被证明长期有效，再评估是否把 orchestrator runtime 迁到 LangGraph
- 长阶段：
  - 可考虑形成“LangGraph runtime + 本项目 intent schema + LLMCompiler-style planner”的组合架构

这一判断的关键原因不是 LangGraph 能力不够，而是当前项目已经有一套能跑的 session/task/SSE/runtime 体系，贸然换底座的收益不一定立刻超过迁移成本。

## 17. 最终技术决策

这一节给出一个可以直接执行的结论，避免前面分析太多后落不到工程决策上。

### 17.1 关于 LLMCompiler

结论：

- 不建议把 `LLMCompiler` 作为依赖或子模块集成进本项目。
- 可以把 `LLMCompiler` 作为设计参考继续保留。
- 当前项目后续要补的能力，本质上仍然是 `LLMCompiler` 那套：
  - planner
  - DAG
  - dependency-aware scheduling
  - replanning

换句话说：

- 可以不考虑“引入 LLMCompiler 仓库”
- 不能不考虑“实现 LLMCompiler 式能力”

### 17.2 关于 LangGraph

结论：

- `LangGraph` 适合作为未来可能的运行时底座。
- 但当前阶段不建议直接迁移到 `LangGraph`。

原因不是它能力不够，而是：

- 当前仓库已经有可运行的会话态、任务态、SSE 和 agent 协议。
- 直接迁移底座会带来较大的重构成本。
- 你当前最缺的是“图规划与图调度能力”，而不是“把现有 runtime 全部推翻重写”。

### 17.3 当前推荐路线

当前最优工程路线是：

1. 保留现有 Router runtime。
2. 在当前代码库中新增图模型和图调度能力。
3. 先做静态图，再做受控并行，再做 replanner。
4. 等图模型被验证稳定后，再决定是否需要迁到 `LangGraph`。

一句话总结：

- 短期：不接 LLMCompiler，不迁 LangGraph。
- 中期：在现有 Router 上实现 LLMCompiler 式 DAG 能力。
- 长期：若运行时复杂度明显升高，再评估 LangGraph 迁移。

## 18. 功能设计

这一节回答“要怎么改”。

设计目标不是一次性做成全功能新框架，而是在当前代码结构上最小代价升级到“可规划、可依赖、可重规划”的图执行模型。

### 18.1 设计目标

本次功能设计要实现的核心能力是：

- 在一次用户输入中识别多个意图，并生成可确认的执行图，而不只是顺序列表。
- 支持显式依赖关系。
- 支持受控并行：
  - 同时只允许一个前台交互节点；
  - 后台非交互节点可以并行。
- 支持条件分支。
- 支持中间结果触发的重规划。
- 保留当前项目已有能力：
  - waiting / resume
  - intent switch
  - SSE
  - 外部 HTTP intent agent 协议
  - plan confirm

### 18.2 非目标

本轮不建议做的事：

- 不把整个 runtime 迁到 `LangGraph`
- 不引入 `LLMCompiler` 官方代码作为运行时依赖
- 不改前端协议为全新格式
- 不一次性做复杂 patch-based graph merge
- 不在第一阶段就做多节点无限并发

### 18.3 总体方案

当前系统：

- 识别结果 -> `SessionPlan.items` -> `Task` 队列 -> 顺序执行

升级后：

- 识别结果 -> `ExecutionGraph` -> `PlanNode` -> `GraphScheduler` -> 节点执行

其中：

- `Task` 不立即删除，继续作为“intent agent 调用执行单元”
- `PlanNode` 变成更高层的图节点
- 一个 `intent_task` 节点在运行时可以绑定一个现有 `Task`

也就是说，推荐演进方式不是替换 `Task`，而是让 `Task` 成为图节点的一种执行后端。

### 18.4 新增领域模型

建议在 [domain.py](/root/intent-router/backend/src/router_core/domain.py) 新增以下模型。

#### 1. 图对象

```python
class PlanNodeType(StrEnum):
    INTENT_TASK = "intent_task"
    CONDITION = "condition"
    JOIN = "join"
    HUMAN_GATE = "human_gate"
    NOTIFY = "notify"


class PlanNodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class PlanNode(BaseModel):
    node_id: str
    node_type: PlanNodeType
    title: str
    intent_code: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    run_if: str | None = None
    interactive: bool = True
    task_id: str | None = None
    status: PlanNodeStatus = PlanNodeStatus.PENDING
    output_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionGraph(BaseModel):
    graph_id: str = Field(default_factory=lambda: f"graph_{uuid4().hex[:10]}")
    source_message: str
    version: int = 1
    status: SessionPlanStatus = SessionPlanStatus.WAITING_CONFIRMATION
    nodes: list[PlanNode] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
```

#### 2. 节点产物

```python
class TaskArtifact(BaseModel):
    node_id: str
    intent_code: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
```

#### 3. 会话态扩展

在 `SessionState` 中新增：

- `execution_graph: ExecutionGraph | None = None`
- `artifacts: dict[str, TaskArtifact] = Field(default_factory=dict)`
- `active_node_id: str | None = None`

同时保留：

- `tasks`
- `pending_plan`

迁移初期建议 `pending_plan` 和 `execution_graph` 并存一段时间，用于兼容旧前端和旧测试。

### 18.5 节点类型语义

#### 1. `intent_task`

- 对应一个已注册 intent
- 真正执行时仍然走 `Task + StreamingAgentClient`
- 适用于：
  - 查余额
  - 转账
  - 查账单

#### 2. `condition`

- 不调 agent
- 读取上游 `TaskArtifact`
- 做简单布尔判断
- 输出 `{ "result": true | false }`

第一阶段建议只支持白名单表达式，不支持自由 Python。

#### 3. `human_gate`

- 不调 agent
- 代表等待用户输入或确认
- 恢复后把用户输入写入 `metadata` 或 artifact，再解锁下游

#### 4. `join`

- 聚合多个上游结果
- 主要用于：
  - 生成计划摘要
  - 生成统一完成事件
  - 给 replanner 提供汇总上下文

#### 5. `notify`

- 不调用业务 agent
- 由 Router 直接发 SSE/消息
- 适合：
  - 余额不足提示
  - 执行结果聚合提示

### 18.6 Intent 注册模型要加什么

为了支持图规划，建议在 [models/intent.py](/root/intent-router/backend/src/models/intent.py)、[schemas.py](/root/intent-router/backend/src/admin_api/schemas.py)、[sql_intent_repository.py](/root/intent-router/backend/src/persistence/sql_intent_repository.py) 增加最小必要字段。

建议新增：

- `interaction_mode: str = "foreground"`
  - 可选：
    - `foreground`
    - `background`
- `can_run_in_parallel: bool = False`
- `planner_hints: dict[str, Any] = {}`
- `result_schema: dict[str, Any] = {}`

其中：

- `interaction_mode`
  - 决定该 intent 默认是否属于前台交互型节点
- `can_run_in_parallel`
  - 决定 scheduler 是否允许并发
- `planner_hints`
  - 用于告诉 planner 这个 intent 常见前置条件、风险级别、适配场景
- `result_schema`
  - 用于约束该 intent 输出哪些结构化字段，方便 condition / downstream node 使用

这是最小集合，先不要扩太多。

### 18.7 Router 核心链路如何改

核心改造点在 [orchestrator.py](/root/intent-router/backend/src/router_core/orchestrator.py)。

建议把现有流程拆成五个阶段：

#### 阶段 1. 候选意图识别

沿用当前 recognizer：

- 输入：
  - 当前消息
  - recent messages
  - long-term memory
  - active intents
- 输出：
  - primary intents
  - candidate intents

这一层不需要推翻重做。

#### 阶段 2. 图规划

新增 `GraphIntentPlanner`：

- 输入：
  - 当前消息
  - primary intents
  - candidate intents
  - 当前 open task / graph summary
  - registered intent metadata
- 输出：
  - `ExecutionGraph`

如果只有一个主意图且没有依赖信号，可退化为单节点图。

如果有多个主意图，或文本中出现条件词，例如：

- 如果
- 若
- 不够就
- 再 / 顺便 / 同时

则进入图规划。

#### 阶段 3. 图确认

沿用现有 plan confirm 思路，但展示对象从 `SessionPlan.items` 升级为 `ExecutionGraph.nodes`。

新增 SSE 事件：

- `session.graph.proposed`
- `session.graph.confirmed`
- `session.graph.updated`
- `session.graph.completed`
- `session.graph.replanned`

迁移期可同时保留现有：

- `session.plan.*`

但建议最终统一到 `graph` 语义。

#### 阶段 4. 图执行

新增 `GraphScheduler` 替代 [task_queue.py](/root/intent-router/backend/src/router_core/task_queue.py) 当前的简单优先级队列。

调度规则建议如下：

- 规则 1：只有依赖全部完成的节点才进入 `READY`
- 规则 2：同一时刻最多 1 个 `interactive=True` 节点进入前台
- 规则 3：`interactive=False` 且 `can_run_in_parallel=True` 的节点可以并发
- 规则 4：某节点 `WAITING_*` 时，仅阻塞其下游，不必阻塞独立后台支路
- 规则 5：`FAILED` 节点默认只影响其依赖后继，不影响独立支路

#### 阶段 5. 重规划

新增 `PlanReplanner`，触发条件包括：

- 条件节点需要更多上下文
- 上游结果导致原图不可行
- 用户中途切换目标
- 某个节点失败但可改道

第一阶段建议做：

- 整图重算

不要做：

- 局部图 patch merge

### 18.8 节点执行如何复用现有 Task

为了降低改造成本，建议保留当前 `Task` 作为 `intent_task` 的执行载体。

执行方式：

1. `PlanNode(node_type=intent_task)` 进入 `READY`
2. orchestrator 为它创建现有 `Task`
3. `Task.task_id` 回写到 `node.task_id`
4. agent 返回结果后：
   - 更新 `task.status`
   - 同步更新 `node.status`
   - 生成 `TaskArtifact`

也就是说：

- `Task` 继续负责 agent 调用
- `PlanNode` 负责图级调度与依赖

这样不需要推翻 [agent_client.py](/root/intent-router/backend/src/router_core/agent_client.py)。

### 18.9 Waiting / Resume 如何并入图模型

这是最重要的运行时设计。

当前逻辑是：

- 找最新 waiting task
- 用户补充输入 -> resume 该 task

升级后建议变成：

- 找当前 `active_node_id`
- 若该 node 为 `WAITING_USER_INPUT` 或 `WAITING_CONFIRMATION`
  - 优先尝试恢复该 node
- 若新输入明显改变全局目标
  - 触发 graph-level replan

判断顺序建议保持为：

1. 是否有 waiting foreground node
2. 当前输入是否像补槽
3. 当前输入是否显式切意图
4. 若是补槽，resume 当前 node
5. 若是切换，取消当前 open branch 并 replan

这基本延续现有 waiting decision 思路，只是作用对象从 `Task` 升级到 `PlanNode`。

### 18.10 ContextBuilder 如何增强

建议增强 [context_builder.py](/root/intent-router/backend/src/router_core/context_builder.py)。

现状只有：

- recent messages
- long-term memory
- slot memory

建议增加：

- `open_graph_summary`
- `open_node_summary`
- `closed_task_summaries`
- `artifacts_summary`
- `waiting_for`

原因是 planner / replanner 的稳定性非常依赖“当前系统到底执行到哪了”这类摘要。

### 18.11 Prompt 设计建议

建议在 [prompt_templates.py](/root/intent-router/backend/src/router_core/prompt_templates.py) 新增三组 prompt。

#### 1. planner prompt

目标：

- 从多意图和用户原始目标中生成结构化图

输出强制为 JSON。

#### 2. replanner prompt

目标：

- 根据：
  - 原始目标
  - 当前图
  - 已完成节点结果
  - 当前 waiting 节点
  - 新用户输入
  生成新图

#### 3. waiting decision prompt

当前 waiting decision 还偏过程式逻辑。

后续建议单独抽出来，输出：

- `resume_current_node`
- `switch_goal_and_replan`
- `cancel_current_branch`

### 18.12 API 与 SSE 协议怎么改

建议尽量保持 [sessions.py](/root/intent-router/backend/src/router_api/routes/sessions.py) 现有接口不变：

- `POST /api/router/sessions/{session_id}/messages`
- `POST /api/router/sessions/{session_id}/messages/stream`
- `POST /api/router/sessions/{session_id}/actions`
- `POST /api/router/sessions/{session_id}/actions/stream`

这样前端不需要立刻大改。

需要新增的是 snapshot 字段和 SSE 事件类型。

#### snapshot 建议新增字段

- `execution_graph`
- `active_node_id`
- `artifacts`
- `graph_version`

#### SSE 建议新增事件

- `session.graph.proposed`
- `session.graph.confirmed`
- `session.graph.updated`
- `session.graph.completed`
- `session.graph.replanned`
- `node.ready`
- `node.running`
- `node.waiting_user_input`
- `node.waiting_confirmation`
- `node.completed`
- `node.failed`
- `node.cancelled`

迁移期可继续发旧事件，方便前端渐进兼容。

### 18.13 文件级修改清单

这一节直接回答“该改哪些文件”。

#### 必改

- [domain.py](/root/intent-router/backend/src/router_core/domain.py)
  - 新增图模型、节点模型、artifact 模型
- [orchestrator.py](/root/intent-router/backend/src/router_core/orchestrator.py)
  - 增加 graph planning / graph execution / replanning 主流程
- [task_queue.py](/root/intent-router/backend/src/router_core/task_queue.py)
  - 重构为 `graph_scheduler.py`
- [context_builder.py](/root/intent-router/backend/src/router_core/context_builder.py)
  - 增加 graph-aware context
- [prompt_templates.py](/root/intent-router/backend/src/router_core/prompt_templates.py)
  - 增加 planner / replanner 模板
- [models/intent.py](/root/intent-router/backend/src/models/intent.py)
  - 增加 intent 图执行元数据字段
- [schemas.py](/root/intent-router/backend/src/admin_api/schemas.py)
  - 暴露新增 intent 元数据
- [sql_intent_repository.py](/root/intent-router/backend/src/persistence/sql_intent_repository.py)
  - 持久化新增字段
- [sessions.py](/root/intent-router/backend/src/router_api/routes/sessions.py)
  - snapshot / SSE 兼容 graph 字段

#### 新增文件建议

- `backend/src/router_core/graph_planner.py`
- `backend/src/router_core/graph_scheduler.py`
- `backend/src/router_core/replanner.py`
- `backend/src/router_core/graph_validator.py`

### 18.14 测试设计

建议新增三类测试，先于大规模实现落地。

#### 1. 图规划测试

输入：

- “先查余额，再转账”
- “如果余额够 2000 就转账，不够就提醒”
- “同时查余额和账单”

断言：

- 输出节点数
- 依赖关系
- interactive 标记
- 条件节点正确生成

#### 2. 图调度测试

断言：

- 后台节点可并行 ready
- 前台交互节点互斥
- waiting 节点只阻塞依赖支路

#### 3. 重规划测试

断言：

- 用户补槽时恢复当前 node
- 用户换目标时触发 replan
- 节点失败时可按策略跳转

建议主要新增到：

- [test_router_api.py](/root/intent-router/backend/tests/test_router_api.py)
- 新增 `backend/tests/test_graph_planner.py`
- 新增 `backend/tests/test_graph_scheduler.py`

### 18.15 分阶段实施方案

#### Phase 1

- 落 `ExecutionGraph`
- 落 `GraphIntentPlanner`
- 保持串行执行
- 前端先继续消费旧 plan/任务语义

#### Phase 2

- 落 `GraphScheduler`
- 启用受控并行
- 增加节点级 SSE

#### Phase 3

- 落 `condition` 节点
- 落 `PlanReplanner`

#### Phase 4

- 把 waiting/resume 全面切到 node 级
- 逐步废弃只面向 `Task` 的思维模型

### 18.16 最小实现优先级

如果现在立刻开始改，我建议严格按下面顺序做：

1. `domain.py`
2. `graph_planner.py`
3. `orchestrator.py` 最小接入
4. `test_graph_planner.py`
5. `graph_scheduler.py`
6. `sessions.py` graph snapshot
7. replanner

这个顺序的原因是：

- 先把“表达能力”补上
- 再补“调度能力”
- 最后补“动态能力”

## 19. 推荐下一步

推荐按下面顺序推进：

1. 先做 `ExecutionGraph` 和 `GraphIntentPlanner`，不改执行器。
2. 用现有测试场景补 3 类新用例：
   - 条件依赖
   - 可并行后台节点
   - 中途 replan
3. 再做 `GraphScheduler`，实现受控并行。
4. 最后把 waiting/resume 接入图模型。

## 20. 示例代码说明

为了让设计更直观，仓库里额外补了两个 Python 示例文件，建议和本报告第 18 节一起看。

### 20.1 纯 Python 图模型示例

文件：

- [intent_graph_example.py](/root/intent-router/docs/examples/intent_graph_example.py)

这个示例不依赖 `LangGraph`，它展示的是“按本项目当前代码结构，建议怎么落地图模型”。

主要包含四部分：

- `ExecutionGraph`
  - 整张执行图
- `PlanNode`
  - 图中的每个节点
- `TaskArtifact`
  - 上游节点的结构化产物
- `GraphRuntime`
  - 一个最小可运行调度器

这个示例重点展示了：

- 多意图如何转成图节点
- 哪些节点可以一开始就 `ready`
- 条件节点如何读取上游结果
- 条件分支如何自动把不满足条件的节点标记为 `SKIPPED`
- 最终 join 节点如何在多个依赖完成后进入 `READY`

建议你重点看这几个函数：

- `build_demo_graph()`
  - 看一条用户请求如何被建模成一张图
- `ready_nodes()`
  - 看 runtime 如何找出当前可执行节点
- `run_condition_node()`
  - 看条件节点如何消费上游 artifact
- `complete_node()`
  - 看节点完成后如何解锁后继节点

如果你关心“当前项目以后应该怎么改”，这个文件更重要。

### 20.2 LangGraph 示例

文件：

- [langgraph_intent_graph_example.py](/root/intent-router/docs/examples/langgraph_intent_graph_example.py)

这个示例现在展示的不是“固定业务 workflow”，而是更贴近真实需求的写法：

- LangGraph 外层只保留少数固定 runtime 节点
- 真正动态的业务图存放在 `state["execution_graph"]`
- 运行时每一轮都从 `execution_graph` 里挑出 ready 节点
- 每个 ready 节点通过同一个通用 `run_node()` 执行

也就是说，这个文件演示的是：

- “静态外层控制图 + 动态内层执行图”

这个示例包含：

- `recognize_or_update_goal`
- `plan_graph`
- `pick_ready_nodes`
- `run_node`
- `finalize`

其中最关键的是四点：

#### 1. 动态规划结果不写死在 LangGraph 边上

业务 agent、条件节点、join 节点，都不是写死在 LangGraph 的 `add_edge(...)` 里，而是由：

- `mock_dynamic_planner(...)`

生成一张 `execution_graph`。

这对应的是：

- 很多个 agent、很多条件、很多依赖，都作为运行时状态存在
- 外层图不需要随着业务 agent 数量增长而无限膨胀

#### 2. 动态 fan-out

`pick_ready_nodes()` 不是返回一个固定节点名，而是根据当前 `execution_graph` 和 `artifacts` 动态找出 ready 节点，然后返回：

- `Send("run_node", {...})`

这对应的是：

- 可以动态并行派发多个 ready 节点
- 不需要提前把所有业务路径写成固定边

#### 3. 通用节点执行器

`run_node()` 是通用执行器，而不是每个 agent 一个 LangGraph 节点。

它会根据 `node_type / intent_code` 决定：

- 执行 intent task
- 执行 condition
- 执行 notify
- 执行 join

这对应的是：

- 未来真正接入时，只需要把 `run_node()` 里的 mock 逻辑替换成你当前的 `StreamingAgentClient`

#### 4. human-in-the-loop

示例里 `transfer_money` 这类节点仍然可以在通用 `run_node()` 内部使用：

- `interrupt(...)`

暂停图执行；恢复时通过：

- `Command(resume=True)`

继续执行。

如果你关心“未来迁到 LangGraph 会长什么样”，这个文件更重要。

### 20.3 两个示例该怎么看

建议按这个顺序看：

1. 先看 [intent_graph_example.py](/root/intent-router/docs/examples/intent_graph_example.py)
   - 理解“图模型长什么样”
2. 再看 [langgraph_intent_graph_example.py](/root/intent-router/docs/examples/langgraph_intent_graph_example.py)
   - 理解“如果换成图运行时框架会怎么写”

这样更容易区分两层问题：

- 图模型和调度规则
- 图运行时底座

### 20.4 运行说明

纯 Python 示例：

```bash
python docs/examples/intent_graph_example.py
```

LangGraph 示例：

```bash
pip install -U langgraph
python docs/examples/langgraph_intent_graph_example.py
```

当前仓库默认依赖里没有 `langgraph`，所以第二个示例默认不会直接跑通，这个是有意为之，因为当前项目还没有决定正式迁到 LangGraph。

## 21. 参考资料

- 论文摘要页：<https://arxiv.org/abs/2312.04511>
- PMLR 正式页面：<https://proceedings.mlr.press/v235/kim24y.html>
- 官方实现仓库：<https://github.com/SqueezeAILab/LLMCompiler>
- 官方 README：<https://github.com/SqueezeAILab/LLMCompiler/blob/main/README.md>
- Planner 实现：<https://github.com/SqueezeAILab/LLMCompiler/blob/main/src/llm_compiler/planner.py>
- Task Fetching Unit 实现：<https://github.com/SqueezeAILab/LLMCompiler/blob/main/src/llm_compiler/task_fetching_unit.py>
- 主执行器实现：<https://github.com/SqueezeAILab/LLMCompiler/blob/main/src/llm_compiler/llm_compiler.py>
- LangGraph 总览：<https://docs.langchain.com/oss/python/langgraph/overview>
- LangGraph Graph API：<https://docs.langchain.com/oss/python/langgraph/use-graph-api>
- LangGraph Interrupts：<https://docs.langchain.com/oss/python/langgraph/interrupts>
- LangGraph Persistence：<https://docs.langchain.com/oss/python/langgraph/persistence>
- LangGraph Durable Execution：<https://docs.langchain.com/oss/python/langgraph/durable-execution>
- LangGraph Subgraphs：<https://docs.langchain.com/oss/python/langgraph/use-subgraphs>
- LangGraph 官方仓库中保留的 LLMCompiler 示例入口：<https://github.com/langchain-ai/langgraph/blob/main/examples/llm-compiler/LLMCompiler.ipynb>
