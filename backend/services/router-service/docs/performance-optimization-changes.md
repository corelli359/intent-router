# Router-Service 性能优化：问题 → 方案 → 代码改动

## 改动汇总

| # | 问题 | 影响文件 | 改动量 | 风险 | 预期收益 |
|---|------|----------|--------|------|----------|
| 1 | JSON indent=2 浪费 token | 5 files | 极小 | 极低 | 5-10% 延迟 |
| 2 | 补槽冗余 LLM 识别 | understanding_service.py | 中 | 低 | 20-30% 延迟 |
| 3 | 内部调用用流式模式 | llm_client.py | 小 | 低 | 连接开销降低 |
| 4 | Agent chunk 冗余刷新 | orchestrator.py | 小 | 低 | 5-10% 延迟 |
| 5 | 线性扫描 → 索引化 | graph_domain.py | 中 | 极低 | CPU 降低 |
| 6 | 快照深拷贝 | orchestrator.py | 极小 | 低 | 2-5% 延迟 |
| 7 | Fallback 深拷贝 | intent_catalog.py | 极小 | 极低 | 微量 |
| 8 | 日志降级 | trace_logging.py | 极小 | 极低 | I/O 降低 |
| 9 | 会话级锁 | session_store.py + orchestrator.py | 中 | 中 | 稳定性 |

---

## 1. LLM Prompt JSON 美化输出浪费 token（P0-A5）

### 问题
所有 LLM 调用的 prompt 变量都使用 `json.dumps(indent=2)` 美化输出。美化后的 JSON 比 compact JSON 多出 **~30% 的 whitespace token**，直接增加 LLM 推理时间和费用。LLM 解析 JSON 不需要缩进。

**影响文件（5 个）**：`recognizer.py`、`planner.py`、`builder.py`、`extractor.py`、`recommendation_router.py`

### 方案
移除所有 `indent=2`，改用 compact JSON。

### 代码改动

**recognizer.py** — 3 处 `indent=2` 移除：
```diff
 variables={
     "message": message,
-    "recent_messages_json": json.dumps(recent_messages, ensure_ascii=False, indent=2),
-    "long_term_memory_json": json.dumps(long_term_memory, ensure_ascii=False, indent=2),
+    "recent_messages_json": json.dumps(recent_messages, ensure_ascii=False),
+    "long_term_memory_json": json.dumps(long_term_memory, ensure_ascii=False),
     "intents_json": json.dumps(
         [recognition_intent_payload(intent) for intent in active_intents],
         ensure_ascii=False,
-        indent=2,
     ),
```

**planner.py** — 8 处 `indent=2` 移除（graph planner + turn interpreter）：
```diff
 # LLMIntentGraphPlanner.plan
-"recent_messages_json": json.dumps(recent_messages or [], ensure_ascii=False, indent=2),
-"long_term_memory_json": json.dumps(long_term_memory or [], ensure_ascii=False, indent=2),
+"recent_messages_json": json.dumps(recent_messages or [], ensure_ascii=False),
+"long_term_memory_json": json.dumps(long_term_memory or [], ensure_ascii=False),
 "matched_intents_json": json.dumps(
     [...],
     ensure_ascii=False,
-    indent=2,
 ),

 # LLMGraphTurnInterpreter.interpret_pending_graph
-pending_graph_json=json.dumps(pending_graph.model_dump(mode="json"), ensure_ascii=False, indent=2),
+pending_graph_json=json.dumps(pending_graph.model_dump(mode="json"), ensure_ascii=False),

 # LLMGraphTurnInterpreter.interpret_waiting_node
-waiting_node_json=json.dumps(waiting_node.model_dump(mode="json"), ensure_ascii=False, indent=2),
-current_graph_json=json.dumps(current_graph.model_dump(mode="json"), ensure_ascii=False, indent=2),
+waiting_node_json=json.dumps(waiting_node.model_dump(mode="json"), ensure_ascii=False),
+current_graph_json=json.dumps(current_graph.model_dump(mode="json"), ensure_ascii=False),

 # LLMGraphTurnInterpreter._interpret
 "primary_intents_json": json.dumps(
     [match.model_dump(mode="json") for match in recognition.primary],
     ensure_ascii=False,
-    indent=2,
 ),
 "candidate_intents_json": json.dumps(
     [match.model_dump(mode="json") for match in recognition.candidates],
     ensure_ascii=False,
-    indent=2,
 ),
```

**builder.py** — 4 处 `indent=2` 移除：
```diff
-"recent_messages_json": json.dumps(recent_messages, ensure_ascii=False, indent=2),
-"long_term_memory_json": json.dumps(long_term_memory, ensure_ascii=False, indent=2),
+"recent_messages_json": json.dumps(recent_messages, ensure_ascii=False),
+"long_term_memory_json": json.dumps(long_term_memory, ensure_ascii=False),
 "recognition_hint_json": json.dumps({...}, ensure_ascii=False,
-    indent=2,
 ),
 "intents_json": json.dumps([...], ensure_ascii=False,
-    indent=2,
 ),
```

**extractor.py** — 2 处 `indent=2` 移除：
```diff
 "intent_json": json.dumps(
     recognition_intent_payload(intent),
     ensure_ascii=False,
-    indent=2,
 ),
-"existing_slot_memory_json": json.dumps(existing_slot_memory, ensure_ascii=False, indent=2),
+"existing_slot_memory_json": json.dumps(existing_slot_memory, ensure_ascii=False),
```

**recommendation_router.py** — 1 处 `indent=2` 移除：
```diff
 "recommendation_items_json": json.dumps(
     [item.model_dump(mode="json", by_alias=True) for item in proactive_recommendation.items],
     ensure_ascii=False,
-    indent=2,
 ),
```

---

## 2. 补槽阶段冗余全量 LLM 识别（P0-A3）

### 问题
当节点等待用户补充 slot 信息时，`understanding_service.py` 的 `interpret_waiting_node_turn` 和 `interpret_pending_graph_turn` 会发起**完整的 LLM 意图识别**（耗时 1-3s），仅仅是为了判断用户是否切换了意图。但这个调用传入的是**空上下文**（`recent_messages=[]`, `long_term_memory=[]`），LLM 在无上下文的情况下识别质量有限，纯属浪费。

### 方案
新增 `_fast_recognize` 方法，优先使用已有的 `HeuristicIntentRecognizer`（基于关键词匹配，耗时 <1ms）。只有在没有 heuristic fallback 时才降级到完整 LLM 识别。

### 代码改动

**understanding_service.py** — 替换两处完整 LLM 识别 + 新增 `_fast_recognize`：

```diff
 # interpret_pending_graph_turn — 替换 ~20 行错误处理+LLM调用
-            try:
-                recognition = await self.recognize_message(
-                    session, content,
-                    recent_messages=[], long_term_memory=[],
-                    emit_events=False,
-                )
-            except Exception as exc:
-                if not llm_exception_is_retryable(exc):
-                    raise
-                # ... 20 行异常处理 ...
+            # Use fast-path recognition (rule-based) to avoid a full LLM call.
+            recognition = await self._fast_recognize(session, content)

 # interpret_waiting_node_turn — 同样替换
-            try:
-                recognition = await self.recognize_message(
-                    session, content,
-                    recent_messages=[], long_term_memory=[],
-                    emit_events=False,
-                )
-            except Exception as exc:
-                # ... 同上 ...
+            recognition = await self._fast_recognize(session, content)
```

**新增 `_fast_recognize` 方法**：
```python
async def _fast_recognize(
    self,
    session: GraphSessionState,
    content: str,
) -> RecognitionResult:
    """Lightweight recognition path for turn interpretation.

    Uses the recognizer's rule-based fallback (heuristic) when available,
    avoiding a full LLM call that would run with empty context anyway.
    Falls back to the full recognizer when no heuristic path exists.
    """
    fast_recognizer = getattr(self.recognizer, "fallback", None)
    if fast_recognizer is not None and hasattr(fast_recognizer, "recognize"):
        try:
            return await fast_recognizer.recognize(
                message=content,
                intents=self.intent_catalog.active_intents_by_code().values(),
                recent_messages=[],
                long_term_memory=[],
            )
        except Exception:
            logger.warning("Fast-path recognition failed, degrading to empty result", exc_info=True)
            return RecognitionResult(primary=[], candidates=[], diagnostics=[])
    # No heuristic fallback available — use the full recognizer.
    try:
        return await self.recognize_message(
            session, content, recent_messages=[], long_term_memory=[], emit_events=False,
        )
    except Exception:
        logger.warning("Turn recognition unavailable, using empty result", exc_info=True)
        return RecognitionResult(primary=[], candidates=[], diagnostics=[])
```

---

## 3. LLM 内部调用使用流式模式浪费开销（P0-A4）

### 问题
`llm_client.py` 的 `run_json` 对所有调用都使用 `astream`（流式），但 planning、slot extraction、turn interpretation 等内部调用不需要逐 token 展示，流式连接的建立/迭代/缓冲有额外开销。

### 方案
新增 `_invoke_once` 方法使用 `ainvoke`（非流式）。当 `on_delta` 回调为 `None` 时自动走非流式路径；有 `on_delta` 时保持流式。

### 代码改动

**llm_client.py** — `_stream_prompt` 分流 + 新增 `_invoke_once`：

```diff
 # _stream_prompt 方法中的分流逻辑
  for attempt in range(self.rate_limit_max_retries + 1):
      try:
-         return await self._stream_once(prompt, variables, model=model, on_delta=on_delta)
+         if on_delta is not None:
+             return await self._stream_once(prompt, variables, model=model, on_delta=on_delta)
+         return await self._invoke_once(prompt, variables, model=model)
      except Exception as exc:
```

**新增 `_invoke_once` 方法**：
```python
async def _invoke_once(
    self,
    prompt: ChatPromptTemplate,
    variables: dict[str, Any],
    *,
    model: str | None = None,
) -> str:
    """Execute one non-retried non-streaming prompt call via ainvoke.

    This avoids the overhead of opening a streaming connection and
    iterating over individual tokens when no on_delta callback is needed.
    """
    chain = prompt | self._create_model(model)
    result = await chain.ainvoke(variables)
    return self._chunk_text(result.content)
```

---

## 4. Agent chunk 中间态冗余刷新图状态 + 广播事件（P1-B3）

### 问题
`orchestrator.py` 的 `_handle_agent_chunk` 在**每收到一个 agent 流式 chunk** 后都调用：
1. `_refresh_graph_state` → 遍历所有节点边重算状态
2. `_emit_graph_progress` → 序列化完整图 → 广播 SSE 事件

中间态的 message chunk 不会改变图的拓扑结构，这些调用在中间态是无效的。一个 5 节点图的一次 agent 调用可能有 10+ 个 chunk，产生 20+ 次冗余刷新。

### 方案
仅在终端/阻塞状态才执行刷新和广播。

### 代码改动

**orchestrator.py `_handle_agent_chunk`**：
```diff
         await self.event_publisher.publish_node_runtime_event(...)
-        await self._refresh_graph_state(session, graph)
-        await self._emit_graph_progress(session)
+        # Only refresh graph state on terminal/blocking chunks; intermediate
+        # message chunks do not alter topology and the full refresh + SSE
+        # broadcast is redundant overhead.
+        if chunk.status in {
+            TaskStatus.WAITING_USER_INPUT,
+            TaskStatus.WAITING_CONFIRMATION,
+            TaskStatus.COMPLETED,
+            TaskStatus.FAILED,
+        }:
+            await self._refresh_graph_state(session, graph)
+            await self._emit_graph_progress(session)
```

---

## 5. 图状态 node/edge 查找 O(N²) 线性扫描（P1-B2）

### 问题
`graph_domain.py` 中 `node_by_id`、`incoming_edges`、`outgoing_edges` 全部是 O(N) 线性扫描。在 `refresh_node_states` 中总复杂度 **O(N × E × N)**。

### 方案
添加延迟构建的 dict 索引，按 `len(nodes)`/`len(edges)` 自动失效重建，查找降为 O(1)。

### 代码改动

**graph_domain.py `ExecutionGraphState`** — 替换 3 个方法 + 新增索引：

```diff
+    # -- Lazily-built lookup indexes for O(1) node/edge access ----------
+
+    def _ensure_node_index(self) -> dict[str, GraphNodeState]:
+        """Build or return a cached node-id to node mapping."""
+        cache = getattr(self, "_node_index_cache", None)
+        node_count = len(self.nodes)
+        if cache is not None and cache[0] == node_count:
+            return cache[1]
+        index = {node.node_id: node for node in self.nodes}
+        object.__setattr__(self, "_node_index_cache", (node_count, index))
+        return index
+
+    def _ensure_edge_indexes(self) -> tuple[...]:
+        """Build or return cached incoming/outgoing edge indexes."""
+        cache = getattr(self, "_edge_index_cache", None)
+        edge_count = len(self.edges)
+        if cache is not None and cache[0] == edge_count:
+            return cache[1], cache[2]
+        incoming: dict[str, list[GraphEdge]] = {}
+        outgoing: dict[str, list[GraphEdge]] = {}
+        for edge in self.edges:
+            incoming.setdefault(edge.target_node_id, []).append(edge)
+            outgoing.setdefault(edge.source_node_id, []).append(edge)
+        object.__setattr__(self, "_edge_index_cache", (edge_count, incoming, outgoing))
+        return incoming, outgoing
+
+    def invalidate_indexes(self) -> None:
+        """Force index rebuild on next access."""
+        object.__setattr__(self, "_node_index_cache", None)
+        object.__setattr__(self, "_edge_index_cache", None)

     def node_by_id(self, node_id: str) -> GraphNodeState:
-        for node in self.nodes:
-            if node.node_id == node_id:
-                return node
-        raise KeyError(f"node not found: {node_id}")
+        index = self._ensure_node_index()
+        node = index.get(node_id)
+        if node is None:
+            raise KeyError(f"node not found: {node_id}")
+        return node

     def incoming_edges(self, node_id: str) -> list[GraphEdge]:
-        return [edge for edge in self.edges if edge.target_node_id == node_id]
+        incoming, _ = self._ensure_edge_indexes()
+        return incoming.get(node_id, [])

     def outgoing_edges(self, node_id: str) -> list[GraphEdge]:
-        return [edge for edge in self.edges if edge.source_node_id == node_id]
+        _, outgoing = self._ensure_edge_indexes()
+        return outgoing.get(node_id, [])
```

---

## 6. API 快照不必要的 Pydantic 深拷贝（P2-C2）

### 问题
`orchestrator.py _build_session_dump` 对 graph 执行 `model_copy(deep=True)`，递归克隆 50+ 个 Pydantic 对象。快照是只读 API 返回值，FastAPI 会立即序列化为 JSON。

### 方案
改为浅拷贝 `model_copy()`。

### 代码改动

```diff
-current_graph=session.current_graph.model_copy(deep=True) if session.current_graph is not None else None,
-pending_graph=session.pending_graph.model_copy(deep=True) if session.pending_graph is not None else None,
+current_graph=session.current_graph.model_copy() if session.current_graph is not None else None,
+pending_graph=session.pending_graph.model_copy() if session.pending_graph is not None else None,
```

---

## 7. Fallback intent 不必要的深拷贝（P2-C3）

### 问题
`intent_catalog.py get_fallback_intent()` 每次调用都 `model_copy(deep=True)`。

### 方案
直接返回引用。

### 代码改动

```diff
 def get_fallback_intent(self) -> IntentDefinition | None:
-    """Return a deep-copied fallback intent when one is configured."""
-    if self._snapshot.fallback is None:
-        return None
-    return self._snapshot.fallback.model_copy(deep=True)
+    """Return the fallback intent when one is configured."""
+    return self._snapshot.fallback
```

---

## 8. `router_stage` 日志过于频繁（P2-D1）

### 问题
`router_stage` 每进出一层产生 2 条 INFO 日志。每请求 16-32 条，高并发下日志 I/O 成为瓶颈。

### 方案
started/completed 降为 `DEBUG`，failed 保持 `ERROR`。

### 代码改动

**trace_logging.py**：
```diff
-    logger.info(
+    logger.debug(
         "Router stage started (trace_id=%s, session_id=%s, stage=%s, details=%s)",
         ...
     )

-        logger.info(
+        logger.debug(
             "Router stage completed (trace_id=%s, session_id=%s, stage=%s, elapsed_ms=%.2f, details=%s)",
             ...
         )
```

---

## 9. 无会话级并发保护（P3-E1）

### 问题
`GraphSessionStore` 是纯 `dict`，无并发保护。性能测试中同一 session 的并发请求在 `await` 点交错执行导致状态竞争。

### 方案
添加 per-session `asyncio.Lock`，在 orchestrator 入口处获取锁。

### 代码改动

**session_store.py** — 新增 lock 机制：
```diff
+import asyncio
+from collections import defaultdict
+from contextlib import asynccontextmanager
+from collections.abc import AsyncIterator

 class GraphSessionStore:
     def __init__(self, long_term_memory=None):
         self._sessions: dict[str, GraphSessionState] = {}
+        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
         self.long_term_memory = long_term_memory or LongTermMemoryStore()

+    @asynccontextmanager
+    async def session_lock(self, session_id: str) -> AsyncIterator[None]:
+        """Acquire a per-session lock to prevent concurrent request interleaving."""
+        async with self._locks[session_id]:
+            yield
```

**orchestrator.py** — `handle_user_message` 和 `handle_action` 加锁：
```diff
 # handle_user_message
         with router_trace(...):
-            with router_stage(...):
-                await self.message_flow.handle_user_message(...)
-            session = self.session_store.get(session_id)
-            snapshot = ...
+            async with self.session_store.session_lock(session_id):
+                with router_stage(...):
+                    await self.message_flow.handle_user_message(...)
+                session = self.session_store.get(session_id)
+                snapshot = ...

 # handle_action
-        await self.action_flow.handle_action(...)
-        session = self.session_store.get(session_id)
-        snapshot = ...
+        async with self.session_store.session_lock(session_id):
+            await self.action_flow.handle_action(...)
+            session = self.session_store.get(session_id)
+            snapshot = ...
```
