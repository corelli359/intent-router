# Router Service LLM 架构定位说明

## 1. 文档目的

本文档回答一个经常会被混用的问题：

1. 本项目在大模型相关能力上，到底更接近 `ReAct`，还是更接近统一推理与图编排路线。
2. 当前实现状态，是不是已经完全走到统一架构终点。
3. 后续演进时，应该优先朝哪个方向继续收敛。

这里的 `react` 特指 `ReAct` 推理范式，不指前端 `React` 框架。

## 2. 结论摘要

结论先行：

1. 本项目当前的设计哲学，不属于经典 `ReAct` 风格。
2. 它明显更接近 `Unified Agentic Reasoning` / `graph-native orchestration`。
3. 但它又不是“所有语义决策都已经完全统一收敛”的纯种统一架构。
4. 更准确的描述是：

   `一个以 Router 为总控、以 Graph 为运行时载体、以受控 LLM 理解为入口的过渡态统一架构。`

换句话说：

- 不是 `LLM 自由 thought-action-observation 循环`
- 而是 `Router 先理解 -> 编译 graph -> 再由运行时推进节点与状态`

## 3. 判断基线

为了避免“名词像、实现不像”的误判，这里先把三种东西分开。

### 3.1 ReAct

典型特征：

1. 模型在一个循环里持续进行 `Thought -> Action -> Observation`。
2. 工具调用决策主要由模型在运行时即时做出。
3. 运行时状态往往围绕“当前上下文和最近观察”组织，而不是围绕显式执行图组织。
4. 多轮、多任务、恢复、取消等能力常常是外挂在 agent loop 外面补出来的。

### 3.2 Unified Agentic Reasoning

这里不限定某个特定框架，而是指一类更受控的设计：

1. 先把用户输入、上下文和能力目录收敛成结构化理解结果。
2. 把多个事项、依赖关系、等待点、恢复点表达成显式状态或图。
3. 用确定性运行时推进状态，而不是把全部控制权交给自由推理循环。
4. LLM 更像“语义起草器”或“受控决策器”，不是整个系统唯一的控制器。

### 3.3 本项目当前形态

本项目不是纯工具调用型 agent，也不是一个只做单轮分类的意图识别器，而是：

1. Router 负责跨意图理解、编图、补槽、续轮控制、调度。
2. Agent 负责具体业务语义执行。
3. 前端和调用方围绕统一快照、统一动作接口、统一 SSE 事件理解系统状态。

这已经天然更接近统一运行时，而不是 ReAct。

## 4. 为什么说它不是 ReAct

### 4.1 Router 的目标不是“万能对话模型”

`v3` 需求文档明确写到，Router 当前阶段的目标不是做一个万能对话模型，而是提供一个稳定、可治理、可演进的运行时控制层。

这意味着：

1. 系统主轴不是让 LLM 自由地持续思考和试探工具。
2. 主轴是让 Router 成为统一控制平面。

### 4.2 Router 必须先把理解结果编译成 Graph

`v3` 需求文档对 Graph 编译的要求非常明确：

1. Router 必须把识别结果编译为执行图。
2. 不能直接把意图列表丢给 Agent。

这与典型 ReAct 的最大差异在于：

- ReAct 往往是“模型此刻决定下一步要不要调工具”
- 本项目是“Router 先形成 graph，再决定节点如何推进”

### 4.3 waiting / pending 续轮是显式状态机，不是 agent loop

在 `pending_graph` 和 `waiting_node` 场景下，`v3` 明确要求先进入专门的 turn interpretation 链路。

也就是说：

1. 系统知道自己正处于什么等待态。
2. 下一轮不是重新开放式自由理解全部上下文。
3. 而是先判断用户是在继续当前节点、取消当前节点、确认待执行图，还是要求重规划。

这种做法更像显式状态机加受控语义解释，而不是 ReAct 的开放式循环。

### 4.4 Router 与 Agent 的职责边界是强分层的

`v3` 要求 Router 负责：

1. 识别与候选管理
2. Graph 编译和确认
3. Router 侧槽位提取、校验、历史预填
4. 续轮解释
5. Agent 调用协议组装与状态映射

这说明 LLM 并没有直接主宰全部执行流。真正的控制权在 Router runtime，不在自由 agent loop。

## 5. 为什么说它更接近 Unified Agentic Reasoning

### 5.1 统一入口

`v3` 把 Router 定义为所有可执行意图的统一入口，并要求动态意图目录成为唯一事实来源。

这意味着语义理解不是散落在多个模块里临时决定，而是统一进入 Router 控制面。

### 5.2 统一图编排

项目强调的不是“识别出一个 intent 就完事”，而是：

1. 单意图和多意图都要进入统一 graph 运行时。
2. 多意图或复杂单意图要支持 `pending_graph` 与图确认。
3. Graph 不是展示层临时产物，而是 Router runtime 的核心中间表示。

### 5.3 统一槽位与受控补全

`v3` 明确要求 Router 在节点分发前完成本地槽位提取，而不是完全依赖 Agent 首次追问。

这件事非常关键，因为它说明：

1. 槽位理解被提升到了 Router 的统一语义层。
2. 不同业务意图之间共享一套受控的槽位治理和证据表达方式。
3. 运行时优先保证跨意图一致性，而不是把补槽逻辑散给各个 Agent 自己处理。

### 5.4 统一续轮决策

续轮不是简单地“把新的用户输入再喂给同一个 agent”。

而是：

1. 在 `pending_graph` 下走 `interpret_pending_graph_turn`
2. 在 `waiting_node` 下走 `interpret_waiting_node_turn`
3. 再由运行时决定 `confirm`、`cancel`、`resume_current`、`replan`

这已经属于典型的 graph/state-first 设计。

### 5.5 统一观测与统一边界

`router_only`、统一 SSE 事件、统一错误包装、统一动作接口，这些都说明系统在追求：

1. 可观测
2. 可测试
3. 可治理
4. 可拆分验证

这也是统一运行时常见的工程特征。

## 6. 与 ReAct、典型 UAR 的对照

| 维度 | ReAct | 本项目当前 v3 | 典型统一推理 / Graph Runtime |
| --- | --- | --- | --- |
| 核心控制单元 | `Thought -> Action -> Observation` 循环 | `识别 -> 编图 -> 状态推进 -> Agent 调度` | `State + Graph + Policy + Runtime` |
| LLM 的职责 | 即时决定下一步动作 | 做受控识别、统一编图、续轮解释 | 做受控规划、重规划、状态决策 |
| 多意图组织方式 | 常依赖 prompt 临场拆解 | 正式进入 graph / pending graph | 原生 graph / DAG / workflow |
| 续轮机制 | 继续 agent loop | 显式 turn interpreter | 显式 state transition |
| 槽位处理 | 常混在 agent 推理里 | Router 侧统一抽取、校验、预填 | 通常在 runtime state 上结构化处理 |
| Agent 定位 | 可能就是主控制器 | 是下游执行器 | 是节点执行器或工具封装 |
| 运行时确定性 | 相对弱 | 中等到强 | 强 |
| 可观测性边界 | 视实现而定 | 强调统一快照、统一事件、统一错误 | 通常也强调统一状态面 |

## 7. 但它还不是“完全统一架构”的原因

如果只说“本项目就是 Unified Agentic Reasoning”，会有一点过满。因为从 `v3` 的正式口径看，它仍然保留明显的过渡特征。

### 7.1 unified builder 不是默认唯一主链

`v3` 功能文档已经写明：

1. `unified graph builder` 不是独立总开关。
2. 只有在 `LLM 可用 + graph_build_mode=unified + planning_policy=always + 非 hierarchical` 时才真正装配。

说明当前系统仍保留多条理解路径，而不是所有场景都统一收敛到一个 builder。

### 7.2 简单场景仍允许走轻量编译

功能文档还明确说：

1. Graph 仍然总会创建。
2. 简单单意图可以通过 fallback planner 走轻量图编译。
3. 只有复杂消息才进入高成本规划。

这是一种很务实的工程选择，但也意味着系统当前不是“统一重型推理覆盖全部场景”的形态。

### 7.3 hierarchical / flat 并存

`flat` 和 `hierarchical` 两种理解模式仍然并存，说明当前理解层是可裁剪、可切换的容器式架构，而不是一条绝对单路径。

### 7.4 structured output 口径仍大于落地

`v3` 也明确指出，`structured_output_method` 虽然已经进入配置，但当前 LLM 请求载荷还没有完全下发严格 schema。

这说明：

1. 统一结构化推理已经是方向。
2. 但底层执行约束还没有完全成为唯一基础设施。

## 8. 一个更准确的命名

综合来看，我建议把本项目当前的大模型架构定位表述为：

`受控 Router 总控 + Graph 编译运行时 + 选择性 unified reasoning`

如果需要再短一点，可以写成：

`偏 UAR 的 Router-centric Graph Runtime`

不建议把它直接称为：

1. `ReAct 系统`
2. `纯统一推理系统`
3. `纯 LangGraph 式系统`

因为这三种说法都会遮掉当前实现的关键事实。

## 9. 对后续演进的含义

如果认可上述定位，后续演进就不应该朝“补一个更强的自由 agent loop”去走，而应优先继续补齐统一运行时。

建议优先级如下：

1. 继续收敛首轮理解，把 unified builder 从可选路径逐步做成更稳定主路径。
2. 让 structured output 真正成为统一底座，而不是停留在配置声明层。
3. 逐步减少 `current_graph` / `pending_graph` 兼容写路径，把业务对象模型和 graph runtime 进一步收敛。
4. 把续轮决策从“依赖 LLM 的解释器”继续演进为“LLM + 更强确定性约束”的组合策略。
5. 让 slot / hint / graph policy 在 Admin 与 runtime 之间形成更完整的闭环治理。

## 10. 最终结论

最终结论可以用三句话概括：

1. 本项目不是经典 `ReAct` 风格系统。
2. 本项目更接近 `Unified Agentic Reasoning` 和 `graph-native orchestration`。
3. 当前实现处于“统一方向明确，但工程上仍保留 legacy / lightweight / mode-matrix 过渡层”的阶段。

因此，最稳妥也最贴近现实的表述是：

`Router Service 当前是一套偏 UAR 的、以 Graph Runtime 为核心的受控大模型编排架构。`
