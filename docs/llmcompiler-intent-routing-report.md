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
- agent 注册信息里新增 `interaction_mode` / `can_run_in_background`。

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

## 17. 推荐下一步

推荐按下面顺序推进：

1. 先做 `ExecutionGraph` 和 `GraphIntentPlanner`，不改执行器。
2. 用现有测试场景补 3 类新用例：
   - 条件依赖
   - 可并行后台节点
   - 中途 replan
3. 再做 `GraphScheduler`，实现受控并行。
4. 最后把 waiting/resume 接入图模型。

## 18. 参考资料

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
