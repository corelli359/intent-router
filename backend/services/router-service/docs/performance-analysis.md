# Router-Service 性能瓶颈分析与优化方案

## 问题概述

`router-service` 在性能测试中表现不佳。经过对整个代码库的深入分析，识别出 **6 大类性能瓶颈**，涵盖 LLM 调用链路、图执行循环、状态管理、事件发布、序列化开销和日志系统。以下按影响程度从高到低排列。

---

## 瓶颈分析

### 🔴 P0 — LLM 调用链路（占总延迟 70%-85%）

这是最关键的瓶颈。每个用户消息可能触发 **3-5 次串行 LLM 调用**，每次 1-5 秒。

#### 调用链路全景

```
用户消息
  → 意图识别 (LLM Call #1)
    → 图规划 (LLM Call #2)
      → Slot 提取 (LLM Call #3)
        → Turn 解释 (LLM Call #4)
          → 补槽阶段 recognize (LLM Call #5)
```

| 调用点 | 文件路径 | 条件 | 预计延迟 |
|--------|----------|------|----------|
| 意图识别 `recognize` | `core/recognition/recognizer.py` | 每次消息 | 1-3s |
| 统一建图 `build_graph_from_message` | `core/recognition/understanding_service.py` L101-149 | `planning_policy=always` + 有 graph_builder | 2-5s |
| 图规划 `LLMIntentGraphPlanner.plan` | `core/graph/planner.py` L345-456 | 非 unified 模式 | 1-3s |
| Slot 提取 `SlotExtractor._extract_with_llm` | `core/slots/extractor.py` L303-368 | 有必填 slot 未被启发式填充 | 1-2s |
| Turn 解释 `interpret_pending_graph` / `interpret_waiting_node` | `core/graph/planner.py` L479-563 | 待确认图 or 补槽阶段 | 1-2s |

> **最严重问题**：所有 LLM 调用完全串行。在 `planning_policy=always` + `unified` 模式下，recognition 和 planning 被合并为一个 LLM 调用，但仍然和 slot extraction 串行。在 `legacy` 模式下，recognition → planning → slot extraction 全部串行。

#### 具体代码问题

**问题 1：冗余 LLM 调用 — 补槽阶段重复全量识别**

当节点等待用户补充信息时，`understanding_service.py` L206-L264 会再次发起**完整的意图识别**（包含全量 intent catalog），仅仅是为了判断用户是否切换了意图。这个调用接收 `recent_messages=[]` 和 `long_term_memory=[]`，上下文为空但仍需完整 LLM 推理：

```python
# understanding_service.py L222-L229
recognition = await self.recognize_message(
    session,
    content,
    recent_messages=[],      # 空上下文！
    long_term_memory=[],     # 空上下文！
    emit_events=False,
)
```

**问题 2：JSON 序列化开销 — 每次 LLM 调用 `json.dumps(indent=2)`**

`planner.py` L365-L402 的 `LLMIntentGraphPlanner.plan` 方法中，对 `field_catalog` 和 `slot_schema` 嵌套调用 `.model_dump(mode="json")`，再用 `json.dumps(indent=2)` 格式化。这在大 catalog 时开销显著。同样的问题出现在 `LLMGraphTurnInterpreter._interpret` 中对完整 graph 做 `model_dump + json.dumps(indent=2)`：

```python
# planner.py L492 — 将完整 pending_graph 序列化
pending_graph_json=json.dumps(
    pending_graph.model_dump(mode="json"),  # 深度序列化完整图
    ensure_ascii=False,
    indent=2  # 美化输出增加 token 消耗
),
```

**问题 3：LLM 流式处理阻塞**

`llm_client.py` 中的 `_stream_once` 方法使用 LangChain `astream` 收集 token，但 `run_json` 必须等待全部 token 到达后才能解析 JSON。对于非流式展示的内部调用（planning、slot extraction、turn interpretation），流式收集实际上引入了不必要的等待和缓冲开销。

---

### 🟠 P1 — 图执行循环 `_drain_graph` 效率问题

#### 问题 4：每轮迭代重复全量刷新

`orchestrator.py` L476 中 `_drain_graph` 在**每次循环迭代**都调用 `_refresh_graph_state`，该方法级联调用 `runtime_engine.refresh_node_states`（O(N×E) 遍历所有节点和边），并可能发布多个 SSE 事件。对于 3-5 节点的图，总计可能执行 10-20 次全量刷新。

```python
# orchestrator.py L467-L497 — drain 循环核心
while True:
    iterations += 1
    await self._refresh_graph_state(session, graph)  # ← 每次迭代
    # ...
    await self._run_node(session, graph, next_node, seed_input)
```

而在 `_run_node` 内部的 `orchestrator.py` L821-L822 中，**每收到一个 agent chunk 都再次调用 `_refresh_graph_state` + `_emit_graph_progress`**：

```python
# orchestrator.py L821-L822 — 每个 agent chunk 后
await self._refresh_graph_state(session, graph)
await self._emit_graph_progress(session)
```

#### 问题 5：线性搜索数据结构

`graph_domain.py` L183-L196 中 `node_by_id`、`incoming_edges`、`outgoing_edges` 全部是 O(N) 线性扫描：

```python
def node_by_id(self, node_id: str) -> GraphNodeState:
    for node in self.nodes:             # O(N)
        if node.node_id == node_id:
            return node
    raise KeyError(...)

def incoming_edges(self, node_id: str) -> list[GraphEdge]:
    return [edge for edge in self.edges   # O(E)
            if edge.target_node_id == node_id]
```

在 `refresh_node_states` 中，每个节点都调用 `incoming_edges`（O(E)），每条边再调用 `node_by_id`（O(N)），总复杂度 **O(N × E × N)**。对于 5 节点 × 4 边的图，每次刷新执行约 100 次字符串比较。

---

### 🟠 P1 — 事件发布系统过度广播

#### 问题 6：每个状态变化都触发完整 payload 序列化

`presentation.py` L290-L325 中 `publish_graph_state` 和 `publish_node_runtime_event` 每次都要：
1. 调用 `presenter.graph_payload(graph)` → 遍历所有节点调用 `node_payload` → 每个节点的 `slot_bindings` 调用 `model_dump(mode="json")`
2. 调用 `presenter.graph_interaction(graph)` → 再次完整序列化

对于一个 5 节点的图，**一次 agent chunk 事件** → `_handle_agent_chunk` → `publish_node_runtime_event`（含 graph_payload + node_payload）→ `_refresh_graph_state` → `_emit_graph_progress` → `publish_graph_state`（含 graph_payload + graph_interaction）= **2 次完整图序列化**。

在整个 drain 循环中，一个 5 节点图可能发布 **30-50 个 SSE 事件**，每个都包含完整图序列化。

#### 问题 7：SSE broker 同步推送

`broker.py` L45-L48 中 `publish` 方法同步遍历所有 subscriber queue，如果某个 queue 满了则执行 drop + retry：

```python
async def publish(self, event: TaskEvent) -> None:
    for queue in list(self._queues[event.session_id]):
        await self._push_event(queue, event)  # 同步推送每个 subscriber
```

---

### 🟡 P2 — 深拷贝和序列化开销

#### 问题 8：API 快照的过度深拷贝

`orchestrator.py` L218-L219：每次 `_build_session_dump` 对 `current_graph` 和 `pending_graph` 执行 `model_copy(deep=True)`。Pydantic v2 的深拷贝会递归克隆所有嵌套模型。对于一个含 5 节点 + 各节点有 slot_bindings 的图，一次深拷贝涉及 50+ 个 Pydantic 对象。

```python
current_graph=session.current_graph.model_copy(deep=True) if session.current_graph is not None else None,
pending_graph=session.pending_graph.model_copy(deep=True) if session.pending_graph is not None else None,
```

该方法在 `handle_user_message` 结束处被调用，以及 `_finalize_handover_business` 中第二次调用，可能导致**每个请求 2 次深拷贝**。

#### 问题 9：Fallback intent 的不必要深拷贝

`intent_catalog.py` L103-L106：`get_fallback_intent()` 每次调用都执行 `model_copy(deep=True)`。如果每个请求都需要 fallback，这是大量冗余拷贝。

```python
def get_fallback_intent(self) -> IntentDefinition | None:
    if self._snapshot.fallback is None:
        return None
    return self._snapshot.fallback.model_copy(deep=True)  # 不必要
```

---

### 🟡 P2 — 日志系统开销

#### 问题 10：`router_stage` 嵌套过深

每个请求创建 6-12 层嵌套的 `router_stage`，每层产生 2 条 INFO 日志（started + completed），含 `time.perf_counter()` 和 `ContextVar` 访问。在高并发时，日志输出本身成为 I/O 瓶颈：

| 操作 | 产生日志条数 |
|------|-------------|
| `handle_user_message` 总 trace | 2 |
| `orchestrator.handle_user_message` stage | 2 |
| `compiler.compile_message` stage | 2 |
| `understanding.recognize_message` stage | 2 |
| `compiler.plan_graph` stage | 2 |
| `orchestrator.drain_graph` stage | 2 |
| per-node `orchestrator.run_node` stage | 2 per node |
| per-node `orchestrator.validate_node_understanding` stage | 2 per node |
| **总计（单节点图）** | **≈16 条** |
| **总计（5 节点图）** | **≈32+ 条** |

---

### 🟢 P3 — 并发模型限制

#### 问题 12：无会话级锁

`GraphSessionStore` 是简单的 `dict[str, GraphSessionState]`，没有任何并发保护。如果同一 session 的两个请求并发到达（性能测试中常见），会导致状态竞争和不可预测的行为。`FastAPI` 使用 asyncio 单线程模型，但 `await` 点可能导致交错执行。

#### 问题 13：httpx AsyncClient 共享

`agent_client.py` L169-L176 中 `StreamingAgentClient` 的连接池配置为 `max_connections=100, max_keepalive_connections=20`。在高并发场景下，如果有超过 100 个并发 agent 调用（5 intents × 20 sessions），连接池会成为瓶颈。

---

## 优化优先级与预期效果

| 优先级 | 方案 | 改动量 | 风险 | 预期延迟降低 |
|--------|------|--------|------|-------------|
| 🔴 P0 | A3: 补槽阶段轻量识别 | 小 | 低 | 20-30% |
| 🔴 P0 | A5: compact JSON | 极小 | 极低 | 5-10% |
| 🔴 P0 | A4: LLM 非流式调用 | 小 | 低 | 连接开销降低 |
| 🟠 P1 | B3: 减少冗余事件发布 | 小 | 低 | 5-10% |
| 🟠 P1 | B2: 节点/边索引化 | 小 | 极低 | 3-5% |
| 🟡 P2 | C2/C3: 移除不必要深拷贝 | 极小 | 极低 | 2-5% |
| 🟡 P2 | D1: 降低日志级别 | 极小 | 极低 | 2-5% |
| 🟢 P3 | E1: 会话级锁 | 中 | 中 | 防止竞态，稳定性提升 |
