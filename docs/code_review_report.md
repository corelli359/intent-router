# Intent Router 代码审查报告

> 基于 `router_core` + `router_api` 全量代码的逐行审查，聚焦三个问题：逻辑合理性与冗余 / 事件精简 / 性能与扩展改进空间。

---

## 一、代码逻辑合理性：冗余与过度设计

### 1.1 死代码：`rule_recognizer.py` 整个文件不再被运行时引用

[rule_recognizer.py](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_core/rule_recognizer.py) 包含完整的 `SimpleIntentRecognizer` 及 CJK 分词逻辑。但在当前代码中：

- `recognizer.py` 已经删除了 `SimpleIntentRecognizer`，替换为 `NullIntentRecognizer`
- `dependencies.py` 不再 import `SimpleIntentRecognizer`
- **唯一引用方**：`intent_catalog.py:10` 仍然 `from router_core.rule_recognizer import extract_patterns`，用于在 `CatalogSnapshot` 中预计算 patterns

> [!WARNING]
> `extract_patterns` 计算出的 patterns 在当前系统中 **无人消费**。V1 orchestrator 不再使用它，V2 也不用。`CatalogSnapshot.patterns` 字段每次刷新都在做无用计算。

**建议**：删除 `rule_recognizer.py` 整个文件，同时删除 `CatalogSnapshot.patterns` 字段和 `intent_catalog.py` 中的 `patterns()` 方法。

---

### 1.2 V1 冗余：`task_queue.py` 中的 `waiting_task()` 未被使用

[task_queue.py:28-32](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_core/task_queue.py#L28-L32) 定义了 `waiting_task()` 函数，但 V1 orchestrator 自己内联了 `_get_waiting_task()`，从未调用这个函数。

**建议**：删除 `waiting_task()` 函数。

---

### 1.3 V1 的槽位冲突检测过于特化

[orchestrator.py:791-873](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_core/orchestrator.py#L791-L873) 中的 `_conflicting_slot_keys()`、`_standalone_digits_role_for_conflict()`、`_looks_like_slot_supplement()` 三个方法高度特化于"转账"场景的正则模式（`CARD_NUMBER_RE`、`PHONE_LAST4_RE`、`NAME_CUE_RE` 等）。

这些正则同时出现在两个地方：
1. `orchestrator.py` 模块顶部（用于多轮决策）
2. `agent_client.py` 的 `MockStreamingAgentClient`（用于模拟 Agent）

> [!IMPORTANT]
> 在 V2 中，`TurnInterpreter` 已经用 LLM 语义来判定"是继续补槽还是切换意图"，不再依赖这些正则。迁移到 V2 后，V1 orchestrator 中这套正则逻辑可以随之移除。但 `agent_client.py` 中的正则仅用于 Mock Agent，保留是合理的。

**建议**：迁移完成后，随 V1 orchestrator 一起清理。当前阶段标记为不紧急。

---

### 1.4 `ContextBuilder` 过于简单，且 V2 绕过了它的类型签名

[context_builder.py](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_core/context_builder.py) 只有 31 行，`build_task_context` 方法的 `session` 参数类型标注为 `SessionState`（V1 域模型）。但 V2 的 `GraphRouterOrchestrator._build_session_context()` 传入的是 `GraphSessionState`。

这能 **行得通**是因为 `GraphSessionState` 恰好也有 `messages`、`session_id`、`cust_id` 等字段（鸭子类型），但类型检查工具会报错。

**建议**：将 `build_task_context()` 的 `session` 参数改为 Protocol 或泛型，或者让两个 session 模型继承共同基类。

---

### 1.5 V1 和 V2 的 `_FallbackCatalog` 内联类重复

`RouterOrchestrator.__init__()` 和 `GraphRouterOrchestrator.__init__()` 各自定义了一个几乎相同的 `_FallbackCatalog` 内联类。

**建议**：提取一个共享的 `NullIntentCatalog` 类。

---

### 1.6 V1 `plan` 事件 payload 中 `items` 重复序列化

观察 [orchestrator.py:910-933](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_core/orchestrator.py#L910-L933) 中 `_propose_plan`：payload 顶层有 `items`，`interaction` 内部又有 `items`，完全相同的数据序列化了两次。在 `_publish_plan_waiting_hint`、`_confirm_pending_plan`、`_cancel_pending_plan`、`_emit_plan_progress_if_needed` 中全部如此。

**建议**：保留 `interaction.items` 即可，顶层 `items` 去掉或改为引用。

---

### 1.7 V2 中 `_warn_null_recognizer` 和 `_warn_v2_null_recognizer` 重复

[dependencies.py:41-60](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_api/dependencies.py#L41-L60) 两个函数逻辑完全一样，只是日志消息略不同。

**建议**：合并为一个函数，增加一个 `version` 参数区分日志。

---

## 二、事件精简：换位思考，前端到底需要知道什么？

> 完全同意您的观点。**如果我们跳出 Router 本身的内部实现，站在前端视角审视**，前端本质上是一个 "Chat UI + 卡片渲染器"。它根本不关心后端是在跑 V1 的线性队列，还是在跑 V2 的动态 DAG 图，它更不关心节点的状态流转（draft -> ready -> running）。
>
> 暴露这些底层架构的副产物（`graph.*`、`node.*`），不仅加重了前端的认知负担，而且导致前后端严重耦合（如果哪天出了 V3，事件不是又得换一套？）。

### 2.1 剔除无意义的后端内部状态

在当前的 21 个 V2 专属事件中：
- ❌ **纯调度过渡态**（如 `node.created/dispatching/running/resuming`，`graph.created/updated/completed`）：对前端渲染毫无意义。甚至不用讲降级，应该直接**完全剥离**，不出网，只留在后端日志中！
- ❌ **意图识别内部细节**（`recognition.started/delta/completed`、`graph.unrecognized`）：意图识别只是第一步，就算识别到了，它最终的归宿也是生成卡片或是调用 Agent。在这之前的过程是黑盒，前端只需展示 "思考中..." 即可。

### 2.2 重构：以前端交互语义为中心的极简事件集（核心仅 5 类）

基于 "对前端到底有什么渲染意义"，Router 给前端推送的 SSE 流应当是一套 **纯展现层（Presentation Layer）** 的协议：

| 前端渲染语义 | 对应目前的 V2 事件 | 前端实际做什么？ |
|-------------|----------------------------|-----------------|
| **1. `message` (气泡文本)** | `node.message`<br>`graph.failed/...`的错误信息提示 | 接收文本，在聊天气泡内渲染打字机效果。 |
| **2. `require_card` (渲染交互卡片)** | `graph.proposed`<br>`node.waiting_confirmation` | 需要用户做一个结构化决策。前端直接拿着 payload 里的 `interaction` 字段渲染卡片（Plan 卡片 or 确认卡片），渲染完锁死输入框，要求用户只能点按钮。 |
| **3. `input.request` (解锁输入框)** | `node.waiting_user_input`<br>`session.idle` | Agent 或会话需要补充信息。告诉前端：结束思考状态，解锁下方的文本输入框，允许用户自由打字补槽。 |
| **4. `input.lock` (进入思考/执行态)** | （当收到用户请求、确认卡片后） | 告诉前端锁死输入框，展示“思考中... / 执行中...” |
| **5. `error` / `system_notice`** | `node.failed`<br>`graph.cancelled`（异常中止时） | 弹出一个轻提示 Toast 或者特殊的系统错误气泡。 |

**落地建议：**
我们不一定需要去大改底层 `v2_domain.py` 里的 `TaskEvent` 数据结构。而是可以在 `sessions_v2.py` 的流式端点（SSE Encoder 环节）增加一个映射过滤层（**Adapter 模式**）：把后端复杂的 `event: node.waiting_confirmation` 翻译映射成标准、收敛的外网极简事件 `event: render_card` 或 `event: chat_message` 给前端。

这样一来，**前端完全解耦了业务逻辑的具体实现，只需要安分守己地当好一个渲染器！**

---

## 三、性能与扩展改进空间

### 3.1 `_refresh_node_states()` 每次调用遍历全部节点和全部边

[v2_orchestrator.py:907-951](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_core/v2_orchestrator.py#L907-L951) 中 `_refresh_node_states()` 在 `_drain_graph` 循环中被**每次迭代**调用，还在 `_handle_agent_chunk`、`_fail_node`、`_cancel_current_node` 等多处调用。

当前是 O(nodes × edges) 的全量扫描。当图的节点数较少时没问题，但如果未来支持更复杂的工作流（10+ 节点），该方法的调用频率 × 复杂度会成为热点。

**建议**：
1. 短期：在 `ExecutionGraphState` 上缓存 `_incoming_edges_by_node`（dict），避免每次 list comprehension
2. 中期：引入脏标记（dirty flag），只在有节点状态变化时重算

---

### 3.2 `EventBroker` 无界队列 + 内存泄漏风险

[broker.py](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_api/sse/broker.py) 的 `register()` 创建无界 `asyncio.Queue`。如果前端订阅了但消费很慢（或完全不消费），队列会无限增长。

**建议**：
1. 使用 `asyncio.Queue(maxsize=500)` 限制上限
2. 在 `publish()` 中 catch `asyncio.QueueFull` 并丢弃最老事件（或断开该消费者）

---

### 3.3 `SessionStore` / `GraphSessionStore` 纯内存，无清理机制

两个 session store 都是 `dict` 存储。已过期的 session 只在 `get_or_create()` 被访问时才清理。如果用户创建了会话但不再访问（如浏览器关闭），session 对象永远留在内存。

**建议**：增加一个后台定时器（类似 `run_intent_catalog_refresh`），定期扫描并清理过期 session。

---

### 3.4 V1 和 V2 共享同一个 `StreamingAgentClient`，但创建了两个 `EventBroker`

[dependencies.py](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_api/dependencies.py) 中 `agent_client` 由 V1/V2 共享（合理），但 `LLMIntentRecognizer` 被创建了两次（V1 一个，V2 一个），各自持有独立的 prompt 构建。

> [!NOTE]
> 虽然 V1 和 V2 的 recognizer 配置完全一样，但由于 `LLMIntentRecognizer` 是无状态的（没有缓存或连接），重复创建的开销可以忽略。但代码可读性上明显是冗余。

**建议**：V1 和 V2 共享同一个 `recognizer` 实例。

---

### 3.5 `_condition_matches_from_condition()` 的表达式求值缺失

[v2_orchestrator.py:953-978](file:///Users/corelli/Desktop/PROJECT_SPACE/intent_router/backend/src/router_core/v2_orchestrator.py#L953-L978) 中 `GraphCondition.expression` 字段存在但从未被求值。`_condition_matches_from_condition` 只处理了 `left_key + operator + right_value` 的简单比较，忽略了 `expression`。

`GraphPlanConditionPayload` 和 `GraphCondition` 都定义了 `expression: str | None`，LLM 的 prompt 也提到了它，但后端没有实现。

**建议**：
- 短期：从 `GraphCondition` 和 prompt 中移除 `expression` 字段，避免 LLM 生成无法执行的条件
- 中期：如果确实需要表达式求值，使用安全沙箱（如 `simpleeval`）

---

### 3.6 `intent_catalog.py` 在主线程 `_refresh_if_needed()` 中同步调用 `refresh_now()`

虽然 `dependencies.py` 中的 `run_intent_catalog_refresh` 使用了 `asyncio.to_thread`，但 `RepositoryIntentCatalog._refresh_if_needed()` 本身是同步方法，在 `list_active()` 等调用中会阻塞事件循环。

> [!WARNING]
> 如果后台刷新间隔 > `refresh_interval_seconds`，`list_active()` 的调用者——也就是请求处理路径——会触发同步的 DB 查询。

**建议**：`_refresh_if_needed()` 中跳过自动刷新（只使用最后一次缓存），让后台任务独占刷新职责。也就是去掉 `_refresh_if_needed` 里的 `self.refresh_now()` 调用，把它改成纯返回缓存数据。

---

### 3.7 V2 的 `_drain_graph` 中 `seed_input` 参数传递不精确

`_drain_graph` 的 `seed_input` 在整个循环中始终使用 `graph.source_message`，但当第二个节点开始执行时，真正需要的输入可能是节点自己的 `source_fragment`。

查看 `_run_node` 的逻辑（L393）：
```python
effective_user_input = (node.source_fragment or user_input) if created_new_task else user_input
```

新建任务时优先用 `source_fragment`，所以 `seed_input` 其实只是 fallback。但在 `_resume_waiting_node` → `_drain_graph` 这条路径上，`seed_input = graph.source_message` 是原始的整句话（如"先查余额再转500"），而不是用户的补充输入。所以续办完一个节点后，下一个节点的 `user_input` 拿到的是原始整句话而非最新消息。

**建议**：`_drain_graph` 应该接收当前轮的最新用户输入，而非固定使用 `graph.source_message`。

---

### 3.8 V2 缺少对 `GraphEdgeType.PARALLEL` 的实际调度支持

`GraphEdgeType.PARALLEL` 定义了，`_refresh_node_states` 也不会阻塞并行边的下游，但 `_drain_graph` 每次只取**一个** ready 节点执行。多个 ready 节点仍然是串行调度。

**建议**：当前阶段在文档中明确标注 `parallel` 是预留能力；中期增加 `asyncio.gather` 并行分发。

---

## 四、TODO List

### P0（建议立即处理）

| ID | 问题 | 改动范围 | 预估工作量 |
|----|------|----------|-----------|
| R01 | 删除 `rule_recognizer.py`，清理 `intent_catalog.py` 中的 `patterns` 相关代码 | `rule_recognizer.py`, `intent_catalog.py` | 小 |
| R02 | `intent_catalog._refresh_if_needed()` 改为只返回缓存、不触发同步刷新 | `intent_catalog.py` | 小 |
| R03 | `EventBroker.register()` 改为有界队列 `maxsize=500`，publish 时处理 `QueueFull` | `broker.py` | 小 |
| R04 | V1/V2 共享同一个 `LLMIntentRecognizer` 实例 | `dependencies.py` | 小 |
| R05 | 合并 `_warn_null_recognizer` 和 `_warn_v2_null_recognizer` | `dependencies.py` | 微小 |

### P1（功能完善）

| ID | 问题 | 改动范围 | 预估工作量 |
|----|------|----------|-----------|
| R06 | `ContextBuilder.build_task_context()` 的 session 参数类型改为 Protocol | `context_builder.py` | 小 |
| R07 | V1 plan 事件 payload 中 `items` 去重（保留 `interaction.items`） | `orchestrator.py` | 中 |
| R08 | `_drain_graph` 的 `seed_input` 改为传递当前轮最新用户输入 | `v2_orchestrator.py` | 小 |
| R09 | 从 `GraphCondition` 和 prompt 中移除 `expression` 字段 | `v2_domain.py`, `v2_planner.py`, `prompt_templates.py` | 小 |
| R10 | 提取共享的 `NullIntentCatalog` 替代两处内联 `_FallbackCatalog` | `orchestrator.py`, `v2_orchestrator.py` | 小 |

### P2（性能与扩展）

| ID | 问题 | 改动范围 | 预估工作量 |
|----|------|----------|-----------|
| R11 | `ExecutionGraphState` 增加 `_incoming_edges_by_node` 缓存，避免 `_refresh_node_states` 每次全扫 | `v2_domain.py`, `v2_orchestrator.py` | 中 |
| R12 | Session Store 增加后台过期清理任务 | `orchestrator.py`, `v2_orchestrator.py`, `dependencies.py` | 中 |
| R13 | SSE 推送层增加事件过滤能力（默认推核心事件，`verbose=true` 推全量） | `broker.py`, `sessions_v2.py` | 中 |
| R14 | 删除 `task_queue.py` 中未使用的 `waiting_task()` 函数 | `task_queue.py` | 微小 |

### P3（迁移后清理）

| ID | 问题 | 改动范围 | 预估工作量 |
|----|------|----------|-----------|
| R15 | V1→V2 迁移完成后，删除 `orchestrator.py`、`task_queue.py`、`domain.py` 中 V1 专属模型 | 多个文件 | 大 |
| R16 | 迁移完成后，清理 `sessions.py`（V1 路由）和 V1 相关依赖 | `routes/sessions.py`, `dependencies.py` | 中 |
