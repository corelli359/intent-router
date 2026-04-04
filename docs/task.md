# 执行进度

## 已完成

### [x] T01 · 意图切换检测（三子场景中的 Router 核心链路）
设计：把“等待补充信息”和“用户想切换新意图”拆成两个显式分支，避免历史实现里只要有 `WAITING_USER_INPUT` 就一律恢复原任务。恢复分支增加“冲突槽位清理”，让同意图下的新目标覆盖旧目标；切换分支则显式取消当前等待任务和队列中的待执行任务。
实现：在 `RouterOrchestrator` 增加 `RouterOrchestratorConfig(intent_switch_threshold=0.80)`；`handle_user_message()` 在存在 waiting task 时先做一次切换识别；命中“取消/算了/不需要了”等快速语义或识别到高置信度异意图时，调用 `_cancel_waiting_and_queued_tasks()`，推送 `task.cancelled`，再对新消息继续分发；恢复原任务时通过 `_prepare_resuming_task()` 清理 `recipient_* / card_* / phone_* / amount` 等易变槽位，避免旧收款人和新输入混用。

### [x] T02 · SimpleIntentRecognizer 重新定位为最终降级兜底
设计：生产默认走 LLM 识别，Simple 只保留为“最后兜底”。这样即使 LLM 暂时不可用，也还能保底路由，但不会把规则识别误当作正式生产方案。
实现：`Settings.recognizer_backend` 默认值改为 `llm`；`build_router_runtime()` 中优先构造 `LLMIntentRecognizer`，失败或显式配置非 llm 时打印 warning 并回退到 `SimpleIntentRecognizer`；`LLMIntentRecognizer.recognize()` 在异常时记录降级日志；`SimpleIntentRecognizer` 的类注释更新为“last-resort fallback only”。

### [x] T03 · intent_catalog 刷新阻塞事件循环
设计：catalog 刷新是同步仓储读取，不应该卡住 asyncio 主循环；把刷新动作放到线程池，主 loop 只负责调度和睡眠。
实现：`run_intent_catalog_refresh()` 中将 `catalog.refresh_now()` 改成 `await asyncio.to_thread(catalog.refresh_now)`，并且保留 stop event 的异步等待。

### [x] T04 · intent_catalog 缓存改为原子替换（Copy-on-Write）
设计：把 active/fallback/priorities/patterns 收敛成单一快照对象，刷新时一次性替换引用，避免读侧看到半旧半新的状态。
实现：新增 `CatalogSnapshot` dataclass，`RepositoryIntentCatalog` 只持有 `self._snapshot`；`refresh_now()` 先完成全部 IO、fallback 选择、priority 计算和 pattern 预计算，最后统一替换 snapshot；读方法全部改为从 snapshot 读取；移除了原先的 `RLock` 读锁。

### [x] T05 · Agent 调用超时保护
设计：Router 不能无限等待 Agent；超时需要转成明确的任务失败事件，而不是让 SSE 或 HTTP 请求一直挂住。
实现：`RouterOrchestrator._run_task()` 使用 `asyncio.timeout(self.config.agent_timeout_seconds)` 包裹 agent stream；超时后统一走 `_fail_task()`，把任务置为 `FAILED` 并推送带超时说明的 `task.failed`。

### [x] T06 · EventBroker 心跳 + Agent 级联取消
设计：SSE 长连接需要心跳和最大空闲时间，避免无限阻塞；一旦前端断流，Router 侧要把当前 waiting task 显式取消，不能让 agent 继续悬挂。
实现：`EventBroker` 新增 `heartbeat_interval_seconds` 和 `max_idle_seconds`，`subscribe()` 超时后发 `heartbeat`，超过 idle 时自动退出并 `unregister`；`AgentClient` Protocol 增加 `cancel()` 和 `close()`；`StreamingAgentClient` 实现 `/cancel` 调用；各 intent agent app 增加 `/api/agent/cancel` 端点；`RouterOrchestrator` 增加 `_cancel_task()` / `cancel_waiting_tasks()`；`/messages/stream` 和 `/events` 两条 SSE 路由在 `finally` 中都会取消当前 waiting task。

### [x] T07 · catalog refresh 异常可观测化
设计：刷新失败必须留痕，而且连续失败要明显升级，便于线上定位。
实现：`run_intent_catalog_refresh()` 从“吞异常”改为 `logger.warning(...)`；增加连续失败计数，连续 3 次及以上额外打 `logger.error(...)`。

### [x] T08 · K8s Ingress Sticky Session
设计：让 router-api 的 ingress cookie 亲和显式、稳定、可预期，同时给 router/agent pod 一个优雅摘流时间，避免缩容时直接打断会话。
实现：`k8s/intent/ingress.yaml` 统一使用 `nginx.ingress.kubernetes.io/session-cookie-name: "ROUTER_SESSION"` 和 `session-cookie-max-age: "1800"`；`router-api.yaml`、`appointment-agent.yaml`、`order-agent.yaml` 增加 `terminationGracePeriodSeconds: 30`。

### [x] T09 · `@lru_cache` 单例改为 FastAPI app.state 管理
设计：router 侧对象不再跨测试/跨 app 复用，而是以 app 为边界维护运行时实例，解决旧实现对 event loop 生命周期不友好、测试间污染的问题。
实现：新增 `RouterRuntime`，由 `build_router_runtime()` 组装 `EventBroker`、`RepositoryIntentCatalog`、`StreamingAgentClient`、`RouterOrchestrator`；`create_router_app()` 和平台根 `create_app()` 都在 `app.state.router_runtime` 持有该对象；`get_event_broker()` / `get_intent_catalog()` / `get_orchestrator()` 改为从 `Request.app.state` 读取；移除了 router_api 里的 `@lru_cache`。

### [x] T10 · `_drain_queue` 状态显式控制
设计：队列消费要根据终态明确分支，而不是依赖“非 waiting 就默认继续”的隐式副作用。
实现：`_drain_queue()` 现在对 `COMPLETED`、`FAILED`、`CANCELLED`、`WAITING_USER_INPUT` 分别显式处理，并对异常状态打 warning。

### [x] T11 · `StreamingAgentClient` 共享 httpx 连接池
设计：每个 app 生命周期内复用一个 `httpx.AsyncClient`，降低频繁建连成本，并在 shutdown 时统一关闭。
实现：`StreamingAgentClient.__init__()` 默认创建带 `Limits` 和 `Timeout` 的共享 `AsyncClient`；去掉 `_stream_via_http()` 里“临时创建再关闭”的逻辑；新增 `close()`，由 router app lifespan 调用。

### [x] T12 · `extract_patterns` 改为 refresh 时预计算
设计：把 Simple 识别器里反复执行的 pattern 提取挪到 catalog refresh 阶段，运行时只读 snapshot，降低降级路径的每次识别开销。
实现：`CatalogSnapshot` 增加 `patterns` 字段，`RepositoryIntentCatalog.refresh_now()` 预计算每个 active intent 的 patterns；`SimpleIntentRecognizer` 支持从 catalog 读取预计算结果，拿不到时才回退到现场 `extract_patterns()`。

## 待执行

### [ ] T01c · 意图确认面板
说明：这项需要 Router 新增 `PENDING_SWITCH_CONFIRM`、确认 API，以及前端确认组件联动，属于跨后端 + 前端的交互功能。本轮先把生产阻塞的“自动取消并切换”链路做稳，确认面板留作下一轮实现。

## 附加 Backlog（当前未动）

### [ ] T13 · 熔断与退避
### [ ] T14 · 测试 lru_cache 污染
说明：router_api 侧 `lru_cache` 已经去掉，这一项需要重新定义为“多 app / 多 loop 生命周期回归测试”。

### [ ] T15 · MockStreamingAgentClient 移出
### [ ] T16 · demo_intents 懒加载
