# Graph Runtime 现状与规划

## 1. 文档目的

这份文档只讨论 `graph runtime` 本身，不重复展开 V1 串行队列、Admin 管理面或历史调研报告。

目标是回答 3 个问题：

1. 当前 graph runtime 已经实现到了什么程度
2. 当前 graph runtime 的真实边界和限制是什么
3. 下一阶段应该按什么顺序继续演进

这份文档默认对应当前分支 `feature/dynamic-intent-graph-runtime` 上的最新实现。

如果你要单独看“当前 V2 一条消息会经过几次 LLM、每次 LLM 分别负责什么”，直接看：

- `docs/v2-llm-call-flow.md`

如果你要看下一阶段把“意图识别 + graph factory”合并成一次 LLM 的 V2.1 设计，直接看：

- `docs/v2.1-unified-graph-builder-design.md`

如果你要看“主动推荐事项 + 原样执行 / 修改后进 graph + 执行管理服务”的专项设计，直接看：

- `docs/proactive-recommendation-execution-design.md`

## 2. 当前目标边界

当前 V2 graph runtime 的定位不是“全自动通用工作流引擎”，而是：

- 基于大模型做多意图识别
- 把一次用户输入规划成一个可执行 graph
- 支持节点级多轮补充
- 支持用户中途取消、修改、重规划
- 在不复制整套部署的前提下，把 `/chat/v2` 和 `/api/router/v2` 发布出来

当前版本的设计重点是：

- 先把“动态 graph + 多轮对话 + 条件依赖 + 可部署”跑通
- 再逐步提高 graph 的解释性、可控性、重规划能力和并行能力

## 3. 当前架构

### 3.1 主链路

当前 V2 主链路有两种首轮建图模式：

1. 用户消息进入 `GraphRouterOrchestrator`
2. 如果 `ROUTER_V2_GRAPH_BUILD_MODE=unified`，首轮直接走 `LLMIntentGraphBuilder`
3. 如果是 `legacy`，则先走 `LLMIntentRecognizer`，再走 `LLMIntentGraphPlanner`
4. 后端把 draft/payload 归一化成 `ExecutionGraphState`
5. 如果发现复用了历史槽位或本身需要确认，graph 进入 `pending_graph`
6. 确认后 graph 转成 `current_graph`
7. orchestrator 根据边依赖和节点状态选择下一个可执行 node
8. node 复用现有 `Task + StreamingAgentClient + Agent HTTP/SSE 协议`
9. agent 返回 `waiting / completed / failed`
10. orchestrator 更新 node / graph / session 状态，并通过 SSE 推给前端

对应代码主要在：

- `backend/src/router_core/v2_domain.py`
- `backend/src/router_core/v2_planner.py`
- `backend/src/router_core/v2_orchestrator.py`
- `backend/src/router_api/routes/sessions_v2.py`
- `frontend/apps/chat-web/app/v2/page-client.tsx`

### 3.2 当前是“动态 graph”，不是“固定 workflow”

当前 graph 不是预先写死的一套 DAG 模板。

graph 的节点、边、顺序、条件都来自：

- 本轮 LLM 多意图识别结果
- 本轮 LLM graph planning 结果

因此当前能力已经具备了“graph factory”形态：

- 同一套 runtime
- 对不同输入
- 生成不同 graph

这和固定 workflow 的本质区别在于：

- graph 拓扑不是代码里预先硬编码好的
- graph 是按用户当前诉求在运行时临时构建出来的

### 3.3 当前不是“并发多 agent runtime”

当前 graph 是动态的，但调度策略仍然是保守的：

- 同一时刻只允许一个前台 active node
- 当前 node 如果进入 `waiting_user_input`，后续依赖节点不会继续向前执行
- 即使 graph 中存在 `parallel` 边，当前也没有做真正并行的多节点前台交互

这是一个刻意保守的设计，而不是遗漏。

原因：

- 当前系统的用户输入是单一对话流
- 多个需要交互的 agent 同时前台等待，会争夺同一轮用户回复
- 当前阶段优先保证状态正确性和内存稳定，而不是盲目并发

## 4. 当前状态模型

### 4.1 Graph 级状态

当前 graph 主要有这些状态：

- `draft`
- `waiting_confirmation`
- `running`
- `waiting_user_input`
- `waiting_confirmation_node`
- `partially_completed`
- `completed`
- `failed`
- `cancelled`

当前语义中最关键的两点是：

- `completed` 表示所有“本应执行”的链路都结束了；如果某些后继节点只是因为条件不满足而没有执行，graph 仍然是 `completed`
- `partially_completed` 只表示“本来应该继续执行，但有节点失败、取消、或被非条件原因跳过”

### 4.2 Node 级状态

当前 node 主要有这些状态：

- `draft`
- `blocked`
- `ready`
- `running`
- `waiting_user_input`
- `waiting_confirmation`
- `completed`
- `failed`
- `cancelled`
- `skipped`

当前语义里：

- `blocked` 是依赖尚未满足
- `ready` 是依赖已满足，等待调度
- `skipped` 是依赖已经到达终态，但条件不满足或上游链路已使该节点失效

### 4.3 Session 级状态

当前 session 同时维护：

- `pending_graph`
- `current_graph`
- `active_node_id`
- `candidate_intents`
- `messages`

当前策略是：

- 待确认 graph 放在 `pending_graph`
- 确认后转成 `current_graph`
- 同一 session 同时只维护一张 active graph

这意味着当前还没有“多张 graph 并存 + 用户切换主 graph”的能力。

## 5. 当前执行语义

### 5.1 多轮补充

当前某个 node 进入 `waiting_user_input` 后：

- 用户下一轮输入优先被解释为“补充当前 node”
- 但 orchestrator 仍会先做一轮基于 LLM 的意图判断
- 如果识别到用户已经换目标，则会触发 `replan`

也就是说，当前不是“只要 waiting 就强制恢复老节点”，而是：

- 先判断用户是继续补充
- 还是取消
- 还是切换目标

### 5.2 条件依赖

当前 graph 条件依赖只支持结构化条件：

- `left_key`
- `operator`
- `right_value`

例如：

- `balance > 8000`
- `score >= 60`

当前已经删除了 `expression` 字段，原因很明确：

- 如果让 LLM 产出自由表达式
- 但运行时并不真正执行它
- 就会制造“看起来支持，实际上不支持”的假能力

因此当前后端只承认“可执行的结构化条件”。

### 5.3 跳过语义

当某个条件分支不满足时：

- node 会被标成 `skipped`
- `skip_reason_code` 会被标成 `condition_not_met`
- `blocking_reason` 会写入触发该跳过的条件标签或原因
- graph 最终仍然会汇总成 `completed`
- 同时 runtime 会额外给用户一条明确提示，说明哪个节点因为条件不满足没有执行

这一步对于未来多意图 graph 非常关键。

因为节点越多、分支越多，最终结果很可能是：

- 一部分执行完成
- 一部分被跳过
- 一部分等待补充

如果系统不能把这种结果表达清楚，用户会误以为系统在“吞步骤”。

### 5.4 取消与重规划

当前已经支持：

- 取消待确认 graph
- 取消当前 waiting node
- 当前 waiting node 上收到新目标后重规划
- 如果 graph 复用了历史槽位，先要求用户确认再执行

这里要注意一个运行时细节：

- 现在不再依赖 LLM 主动把历史槽位写进 `node.slot_memory`
- runtime 会先对 LLM 输出做 grounding
- 然后再从 session 中最近已确认的 `task.slot_memory` 以及 long-term memory 里的结构化 `key=value` 条目补齐允许复用的槽位
- 只要发生这种历史注入，node 就会记录到 `history_slot_keys`，graph 会被抬到 `waiting_confirmation`

但当前还不支持：

- 对正在运行中的任意中间节点做复杂的图内结构编辑
- 对一张大 graph 的局部子图做显式人工改写

## 6. 当前接口与前端能力

### 6.1 后端接口

当前 V2 API 已经具备：

- `POST /api/router/v2/sessions`
- `GET /api/router/v2/sessions/{session_id}`
- `POST /api/router/v2/sessions/{session_id}/messages`
- `POST /api/router/v2/sessions/{session_id}/messages/stream`
- `POST /api/router/v2/sessions/{session_id}/actions`
- `POST /api/router/v2/sessions/{session_id}/actions/stream`
- `GET /api/router/v2/sessions/{session_id}/events`

当前 action 主要是：

- `confirm_graph`
- `cancel_graph`
- `cancel_node`

此外，`POST /api/router/v2/sessions/{session_id}/messages` 现在还支持两类与推荐场景相关的可选负载：

1. `recommendationContext`
2. `guidedSelection`

其中：

- `recommendationContext` 的定位是“把前端刚展示给用户的推荐候选项作为语义上下文传给 LLM”，仍然要走正常意图识别
- `guidedSelection` 是更底层的结构化直达能力，当前仍保留在后端里，但它不再是主动推荐场景的最终目标形态

`guidedSelection` 的结构目前是：

- `selectedIntents[]`
- 每个 selected intent 的 `intentCode`
- `title`
- `sourceFragment`
- `slotMemory`
当前更贴合产品方向的是：

- 前端把推荐候选项放进对话区
- 用户继续用自然语言表达“第一个”“第一个和第三个都要”“第二个改成给弟弟转500”
- Router 结合 `recommendationContext` 做正常意图识别

### 6.2 前端能力

当前 `/chat/v2` 已经可以展示：

- graph 总状态
- graph summary
- node 列表
- edge 列表
- 当前 active node
- SSE 事件时间线
- pending graph 的确认/取消

当前前端已经能看出：

- 节点是否 `waiting`
- 节点是否 `skipped`
- 节点的 `blocking_reason`

但“跳过原因解释”仍然偏工程态，还不够面向普通用户。

当前前端已经把“推荐事项”收敛成：

- 由按钮触发的一条对话内推荐消息
- 推荐卡片展示在聊天区，而不是侧栏选择器
- 用户仍然通过自然语言来选择、组合、修改这些推荐项
- 前端只负责把推荐上下文随消息一起传给 Router
- Router 仍然要做意图识别，而不是跳过识别直接执行

## 7. 当前已经完成的关键治理

这一轮与 graph runtime 强相关的治理，已经完成：

### 7.1 运行时移除 mock

当前生产 `StreamingAgentClient` 已经只允许：

- `http://`
- `https://`

`mock://` 已经被移到测试目录，避免生产运行时再混入测试语义。

### 7.2 不再保留规则式意图识别

当前运行时意图识别只走：

- `LLMIntentRecognizer`
- 或 fail-closed 到 `NullIntentRecognizer`

不再保留“规则/正则识别”这条生产运行时链路。

### 7.3 EventBroker 改为有界队列

当前 SSE event broker 已经加了有界 queue，避免慢订阅无限堆积事件，控制内存风险。

### 7.4 intent catalog 收敛为 snapshot

当前 intent catalog 只读缓存 snapshot，不再在请求路径同步刷仓储，降低主链路抖动。

### 7.5 Graph skip 状态语义已修正

当前 graph 里只要存在被条件跳过的节点，就不再对外谎称“全部完成”。

这一步是后续多分支 graph 能否让用户信任的基础。

### 7.6 条件边已支持隐式 producer 修复

当前 runtime 不再把“planner 没显式补那一步”当作用户语义不存在。

例如：

- “我想给小明转账1000元，如果卡里余额还剩超过2000，我就换100美元”

这里“转账后卡里余额还剩多少”并不是 `transfer_money` 节点天然能产出的字段，因此 runtime 会在 graph 里自动补一个隐式 `query_account_balance` 节点，再把条件边挂到真正能提供 `balance` 的节点上。

这说明当前 graph runtime 已经不只是“照着 LLM 输出执行”，而是开始对 graph 做语义可执行性修复。

### 7.7 推荐上下文与自由对话双入口已经并存

当前 V2 已经不是单一入口：

- `free dialog`
- `recommendation context`

二者共享同一个 graph runtime、同一套 node/edge 状态机和同一套 agent 调度协议。

区别只是首轮建图来源不同：

- 自由对话由 LLM 识别/建图
- 推荐上下文由前端提供候选事项摘要，用户仍然用自然语言表达真正想要哪些事项

但这里要明确：

- 当前仓库只完成了“推荐语义入口”
- 还没有完成“原样接受推荐时，直接交给执行管理服务”的后半段执行分流

## 8. 当前真实限制

这部分必须明确，不做包装。

### 8.1 planner 质量仍然高度依赖 LLM 输出

当前 graph factory 虽然已经成型，但 planner 仍然会受限于：

- intent description 质量
- examples 质量
- prompt 表达
- 模型稳定性

因此现在的 graph runtime 还不是“给一句复杂自然语言就永远规划正确”。

### 8.2 条件表达能力仍然偏简单

当前只支持单条件比较，例如：

- `balance > 8000`

还不支持：

- `A and B`
- `A or B`
- 多层嵌套条件
- 跨多个上游节点的联合条件
- 带时间、次数、窗口语义的条件

### 8.3 当前缺少“图级结果解释层”

虽然状态已经修正，但当前最终反馈仍主要依赖：

- graph status
- node status
- blocking_reason

还缺少一层真正面向用户的结果解释，例如：

- 哪些事项完成了
- 哪些事项因为条件不满足被跳过了
- 哪些事项仍需要补充
- 哪些事项已经被取消或替换

### 8.4 当前没有 graph revision 历史

当前 graph 被确认后，如果用户修改目标，系统主要做的是：

- 取消当前 graph
- 重新规划一张新 graph

还没有：

- revision id
- graph diff
- 节点继承关系
- 局部重规划记录

### 8.5 当前并发策略仍然保守

这保证了状态简单和内存稳定，但也意味着：

- 多个互不依赖的自动化节点不会并发跑满
- 前台交互式 graph 仍然偏串行

### 8.6 当前推荐仍然是静态前端配置

当前 `/chat/v2` 里的推荐事项面板只是一个可选入口实验，推荐卡片本身仍然是前端静态配置，而不是：

- 根据用户画像动态生成
- 根据会话上下文动态排序
- 根据注册表自动下发

因此它现在验证的是“推荐上下文进入 Router 识别”的链路是否成立，而不是“推荐系统”本身。

### 8.7 当前还没有执行管理服务 / 执行服务分层

这是当前实现与目标需求之间最重要的差距。

当前 demo 里：

- intent agent 在完成要素确认后，会直接模拟业务成功

但目标架构应该是：

- Router 决定走 `direct_execute` 还是 `interactive_graph`
- Intent Agent 只负责确认与补槽
- Execution Manager 统一承接执行请求
- 各执行服务完成真正业务调用

这部分的专项设计已单独写在：

- `docs/proactive-recommendation-execution-design.md`

## 9. 未来规划设计

下面的规划不是“愿景口号”，而是建议的工程推进顺序。

### 9.1 P0：结果解释层

目标：

- 让用户清楚知道 graph 到底做了什么、没做什么、为什么没做

建议新增：

- graph 终态摘要生成
- skipped/cancelled/failed 节点的标准原因枚举
- 前端“执行结果摘要区”
- 对话消息区的自然语言总结

设计重点：

- 不要只把解释写成自由文本
- 要同时保留结构化 reason code + 面向用户的自然语言文案

### 9.2 P1：局部重规划

目标：

- 用户修改其中一个事项时，不必整张 graph 全量推翻

建议能力：

- 保留 `graph_revision`
- 标记受影响子图
- 局部节点失效
- 局部重新规划并回接原 graph

设计重点：

- 先做“单 waiting node 周围的局部重规划”
- 不要一上来就追求通用图编辑器

### 9.3 P2：条件模型升级

目标：

- 从单条件比较升级到可控的复合条件模型

建议方向：

- `all_of`
- `any_of`
- `not`
- 跨 node output 的组合条件

设计重点：

- 仍然坚持结构化条件
- 不回退到“让 LLM 输出一段 expression 然后运行时硬 eval”

### 9.4 P3：自动节点并发

目标：

- 对不需要用户交互的纯自动化节点做并行调度

建议策略：

- 交互节点仍保持单前台
- 非交互节点允许后台并发
- 用 node capability 明确声明是否会向用户要输入

设计重点：

- 并发能力要建立在事件流、取消、资源控制和幂等语义清晰之后

### 9.5 P4：graph policy 层

目标：

- 把“哪些图允许直接执行，哪些必须确认，哪些必须二次确认”从 prompt 里抽出来

建议新增：

- graph risk level
- action guardrail
- confirmation policy
- financial / destructive / external-call policy

设计重点：

- 对高风险图不能只依赖 planner 的 `needs_confirmation`
- 必须有运行时 policy 二次裁决

### 9.6 P5：观测与压测

目标：

- 让 graph runtime 从“功能可用”走向“可上线运营”

建议补齐：

- graph 规划成功率
- node 跳过率
- replan 率
- 条件分支命中率
- 平均 graph 节点数
- SSE 连接时长
- 内存随 session 数增长的曲线

## 10. 建议的近期落地顺序

如果只看最近两到三轮，建议按下面顺序推进：

1. 先补 graph 结果解释层
2. 再补局部重规划
3. 再扩展条件模型
4. 最后再评估自动节点并发

原因很简单：

- 现在最缺的是“结果可解释”
- 不是“盲目再加 graph 花活”

如果解释层不先补，后面节点和条件越多，用户越容易觉得系统“乱跳、漏做、吞步骤”。

## 11. 当前结论

当前 graph runtime 已经具备：

- 动态 graph factory
- 多意图识别
- 条件依赖
- 节点级多轮补充
- 取消与重规划
- 前后端版本化发布

但它当前仍然是一个：

- 单前台交互节点
- 条件模型较简单
- 解释层尚不完整
- revision 能力尚不完整

的第一阶段 graph runtime。

这个判断很重要。

当前版本已经不是 demo，但也还不能把它误判成“完整通用多智能体图执行引擎”。

正确的做法是：

- 先把现有 graph runtime 的状态语义、解释能力、局部重规划做扎实
- 再继续扩展条件表达和自动并发能力
