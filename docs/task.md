# 执行进度

## 2026-04-05 补充执行情况

## 2026-04-07 V2 Graph Runtime

### [x] T21 · V2 动态执行图运行时
设计：在不替换 V1 的前提下，为 Router 增加第二套路由内核。V2 必须先做多意图识别，再做意图关系推断和 graph factory，最后才进入节点调度。
实现：新增 `router_core/v2_domain.py`、`router_core/v2_planner.py`、`router_core/v2_orchestrator.py`，并通过 `router_api/routes/sessions_v2.py` 暴露 `/api/router/v2/*`。V2 运行时支持 graph 确认、节点 waiting/resume、取消当前 node、等待态意图切换后重规划。

### [x] T22 · `/chat/v2` 前端入口
设计：V2 前端必须独立于 V1 `/chat` 页面，避免影响已有会话页，同时要能直观看到 graph、node、edge、当前活跃节点和事件时间线。
实现：新增 `frontend/apps/chat-web/app/v2/page.tsx`，使用现有 chat-web 的视觉语言，直接对接 `/api/router/v2`，支持发送消息、确认/取消执行图、取消当前节点，并展示 graph 状态和 SSE 时间线。

### [x] T23 · V2 文档与部署说明
设计：V2 是版本化入口，不是简单 demo。文档必须明确它与 LLMCompiler/LangGraph 的关系、与 V1 的边界、以及部署时的内存策略。
实现：新增 `docs/dynamic-intent-graph-v2-design.md`，并更新 `README.md`、`docs/llmcompiler-intent-routing-report.md`、`k8s/intent/README.md`。当前结论是 V2 先以内嵌在现有 chat-web/router-api 的方式发布，使用 `/chat/v2` 与 `/api/router/v2` 暴露能力，以最小化额外常驻内存。

## 2026-04-08 Runtime Cleanup

### [x] T28 · V2.1 Unified Graph Builder 设计
设计：V2 当前首轮新消息要经过 `recognizer + planner` 两次 LLM，语义边界被拆开。V2.1 需要把“意图识别 + graph factory”合并成一次 LLM 输出，并且明确不新增 `/chat/v2.1`，而是在 `/chat/v2` 内通过内部配置切换。
实现：新增 `docs/v2.1-unified-graph-builder-design.md`，明确 unified builder 的目标、为什么保留独立 `TurnInterpreter`、为什么不新增 V2.1 路由、注册期 `slot_schema / graph_build_hints` 约束，以及 deterministic normalization 与迁移策略。

### [x] T29 · 意图注册期要素约束入模
设计：slot 约束和建图提示必须是正式的 intent schema，而不是散落在 prompt 里的临时文本。只有这样，LLM 才能在识别阶段就严格区分“槽位”和“新意图”。
实现：`models.intent`、`admin_api.schemas`、`router_core.domain`、`router_core.intent_catalog`、`router_core.recognizer` 已补充 `slot_schema / graph_build_hints`；`sql_intent_repository` 增加对应 JSON 列与旧表自动补列逻辑，为 V2.1 unified builder 提供正式输入。

### [x] T30 · Unified Builder 骨架接入
设计：首轮新消息需要可选地走 “一次 LLM 直接产出 GraphDraft”，但等待态解释和重规划先保持现有 `recognizer + turn interpreter + planner` 路径，避免一次性耦死。
实现：新增 `router_core/v2_graph_builder.py`，包含 `LLMIntentGraphBuilder`、`GraphDraftNormalizer` 和统一 draft schema；`GraphRouterOrchestrator` 已支持 `graph_builder` 可选路径；`router_api/dependencies.py` 新增 `ROUTER_V2_GRAPH_BUILD_MODE=legacy|unified` 装配开关。

### [x] T31 · V2 历史槽位复用确认与条件跳过语义修正
设计：如果 unified builder 或 graph runtime 复用了历史里的敏感槽位，不能直接执行，必须先进入 `waiting_confirmation`；同时 graph 的终态语义要从“只要有 skipped 就部分完成”收紧为“条件未满足导致的 skipped 仍算 completed，只有本应执行却失败/取消/异常跳过时才算 partially_completed”。
实现：新增 `router_core/slot_grounding.py` 做 deterministic slot grounding；`GraphDraftNormalizer` 与 `GraphRouterOrchestrator` 会把历史复用槽位标记到 `history_slot_keys` 并强制确认；除了识别 LLM 已写出的历史槽位，runtime 现在还会从 session 已确认 `task.slot_memory` 与 long-term memory 的结构化 `key=value` 条目里做受控预填充，再统一进入确认态；`v2_orchestrator` 现在会暴露 `skip_reason_code`，并在条件未满足时输出明确的 graph 终态提示；相关回归测试已补到 `test_v2_graph_builder.py` 与 `test_router_api_v2.py`。

### [x] T32 · 金融扩展意图与 Agent 拆分部署
设计：为 V2 扩展信用卡还款信息查询、天然气缴费、换外汇三个 intent，并把它们做成真实可见的独立 agent 部署，而不是继续隐藏在已有 balance/transfer agent 后面。原 `intent-order-agent` 与 `intent-appointment-agent` 收敛回单意图职责，避免运行拓扑和注册表拓扑不一致。
实现：新增 `credit_card_repayment_service.py`、`gas_bill_payment_service.py`、`forex_exchange_service.py` 以及对应的 `*_app.py` 独立入口；K8s 新增 `intent-credit-card-agent`、`intent-gas-bill-agent`、`intent-forex-agent` 三个 deployment/service；`scripts/register_financial_intents.py` 已改为注册到独立 service；`order_status_app.py` / `cancel_appointment_app.py` 退回到余额查询与转账的兼容别名；补充了对应 service/app 回归测试，并将 Minikube 部署脚本改为逐个 apply/rollout。

### [x] T24 · 运行时 `mock://` 清理
设计：`MockStreamingAgentClient` 只能存在于测试支撑层，生产 `StreamingAgentClient` 必须严格限制为 `http://` / `https://`，对非法 scheme fail-closed，而不是偷偷执行 mock。
实现：`router_core/agent_client.py` 已移除内置 `MockStreamingAgentClient` 和 `mock://` 分流；测试专用 mock client 已迁移到 `backend/tests/support/mock_agent_client.py`；补充了 runtime fail-closed 回归测试，覆盖 V1、V2 和 `StreamingAgentClient` 本身。

### [x] T25 · intent_catalog 收敛为只读快照
设计：catalog 刷新只能发生在应用启动和后台 refresh task，读路径只读当前 snapshot，不能再在请求链路同步打仓储；同时既然生产已不再使用规则识别，就不应继续维护 `patterns` 这类死数据。
实现：删除 `rule_recognizer.py`、`CatalogSnapshot.patterns` 和相关测试；`RepositoryIntentCatalog` 现在只持有 `active/fallback/priorities` 三类快照，`list_active()/priorities()/get_fallback_intent()` 都只读取当前缓存。

### [x] T26 · Runtime 装配与 SSE 限流优化
设计：V1/V2 runtime 组装应共享一份 recognizer，避免重复构造；SSE broker 必须使用有界队列，慢订阅不能无限堆积事件占内存。
实现：`router_api/dependencies.py` 已收敛为单一 recognizer 构造路径，V1/V2 共用同一实例；`router_api/sse/broker.py` 改为有界队列并在满载时丢弃最旧事件，避免发布路径被慢消费者拖垮；新增对应回归测试。

### [x] T27 · V2 条件与多轮 seed_input 收敛
设计：执行图条件只能暴露后端真正可执行的结构化字段，不能再保留 `expression` 这类“模型能写、运行时不执行”的伪能力；同时多轮恢复节点后，后续节点不能继续吃首轮原始消息。
实现：删除 `GraphCondition.expression`、相关 prompt 字段和前端展示分支；V2 在 waiting node 恢复或取消后继续 drain graph 时，改为传递本轮最新用户输入，避免后续节点误用过时 `graph.source_message`。

### [x] T13 · 管理面与运行面部署解耦
设计：`admin-api` 负责意图治理与配置发布，`router-api` 负责识别、状态机与分发，二者必须独立 Deployment，不能继续共用一个 `backend` 部署。
实现：新增 `k8s/intent/admin-api.yaml` 与 `k8s/intent/router-api.yaml`，删除 `k8s/intent/backend.yaml`；统一入口固定为 `/admin`、`/chat`、`/api/admin/*`、`/api/router/*`；相关约束已补充到 `docs/intent-router-prd.md` 与 `docs/deerflow-inspired-architecture.md`。

### [x] T14 · Ingress 路由收敛
设计：管理端和对话端必须使用明确前缀，避免根路径混用带来的路由歧义。
实现：`k8s/intent/ingress.yaml` 已固定为 `intent-router.kkrrc-359.top` 下的四类入口：`/admin` -> `intent-admin-web`、`/chat` -> `intent-chat-web`、`/api/admin` -> `intent-admin-api`、`/api/router` -> `intent-router-api`；`app-root` 指向 `/chat`。

### [x] T15 · Deployment requests 补齐
设计：当前运行环境是 4c8g 的 minikube，必须给各 Deployment 显式声明 `resources.requests`，让调度可控，避免一次性拉起所有 Pod 造成内存争抢。
实现：`admin-api`、`router-api`、`admin-web`、`chat-web`、两个 intent agent 对应 Deployment 都已补齐 `resources.requests.cpu` 与 `resources.requests.memory`；后续部署策略改为逐个滚动恢复，不再一次性重拉全部 Deployment。

### [x] T16 · Chat Web 中文化与主界面收敛
设计：对话页主界面只保留会话、当前任务和发送动作，诊断信息下沉，避免“一个页面全装下”的拥挤感。
实现：`frontend/apps/chat-web` 已完成中文化，主界面以会话输入与任务状态为主，诊断信息折叠到次级区域；管理端与对话端入口已按 `/admin` 和 `/chat` 分开。

### [x] T17 · 示例意图 Agent 重建为余额查询与转账
设计：Router 只做识别与分发，不直接执行意图；两个示意 intent agent 负责通过语义补齐槽位，不在 Router 里写业务正则。Agent 当前只做演示，不追求完整银行能力。
实现：原示意场景替换为 `query_account_balance` 与 `transfer_money`：余额查询在拿到卡号与手机号后 4 位后固定返回 `8000` 元；转账要求收款人姓名、卡号、手机号后 4 位和金额，金额大于 `8000` 返回余额不足，否则返回转账成功。当前 K8s Service 名仍沿用历史命名 `intent-order-agent` 与 `intent-appointment-agent` 承载这两个新 agent。

### [x] T18 · Router 主链路 SSE 线上验证
设计：对话主链路必须走标准 SSE，不使用额外 snapshot 补包；浏览器与脚本调用都以 `/messages/stream` 为准。
实现：线上已验证 `POST /api/router/sessions` + `POST /api/router/sessions/{session_id}/messages/stream` 主链路可用；等待补充信息、任务切换、余额查询、转账成功/失败路径都已串通；本轮另外补了 `/events` 初始 `heartbeat` 输出，便于前端尽快确认订阅建立。

### [x] T19 · 仓库内 kubectl 包装器移除
设计：`kubectl` 属于系统级工具，不应放在工程脚本目录内伪装为项目依赖。
实现：仓库内 `scripts/kubectl` 包装器已删除；当前约定改为直接使用系统级 `kubectl`，用户本机实际已放置到 `/root/kubectl`。

## 当前残留问题

### [ ] T20 · `/events` 断开后的 waiting task 在线上仍未稳定取消
说明：`/messages/stream` 断开后取消 waiting task 的主链路已在线上验证通过；但 `GET /api/router/sessions/{session_id}/events` 在 ingress 后的断连感知仍不稳定。本轮已补 initial heartbeat 和对应测试，但线上观察流仍不应被当作“交互主链路”，当前以 `/messages/stream` 作为权威交互 SSE 入口。

## 已完成

### [x] T01 · 意图切换检测（三子场景中的 Router 核心链路）
设计：把“等待补充信息”和“用户想切换新意图”拆成两个显式分支，避免历史实现里只要有 `WAITING_USER_INPUT` 就一律恢复原任务。恢复分支增加“冲突槽位清理”，让同意图下的新目标覆盖旧目标；切换分支则显式取消当前等待任务和队列中的待执行任务。
实现：在 `RouterOrchestrator` 增加 `RouterOrchestratorConfig(intent_switch_threshold=0.80)`；`handle_user_message()` 在存在 waiting task 时先做一次切换识别；命中“取消/算了/不需要了”等快速语义或识别到高置信度异意图时，调用 `_cancel_waiting_and_queued_tasks()`，推送 `task.cancelled`，再对新消息继续分发；恢复原任务时通过 `_prepare_resuming_task()` 清理 `recipient_* / card_* / phone_* / amount` 等易变槽位，避免旧收款人和新输入混用。

### [x] T02 · SimpleIntentRecognizer 重新定位为最终降级兜底
设计：生产默认走 LLM 语义识别；当 LLM 不可用时，识别层应该 fail-closed 到 `NullIntentRecognizer`，由 fallback intent/agent 兜底，而不是回到规则/正则识别。
实现：runtime 现在只会装配 `LLMIntentRecognizer` 或 `NullIntentRecognizer`；规则识别链路已从生产代码删除，避免再出现“正则也算正式识别能力”的歧义。

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
设计：既然运行时已经不再保留规则识别，就不应该继续为不存在的识别器维护 `patterns` 快照。
实现：`rule_recognizer.py`、`CatalogSnapshot.patterns` 和对应测试已删除；catalog 当前只保留生产运行时实际会消费的快照数据。

## 待执行

### [ ] T01c · 意图确认面板
说明：这项需要 Router 新增 `PENDING_SWITCH_CONFIRM`、确认 API，以及前端确认组件联动，属于跨后端 + 前端的交互功能。本轮先把生产阻塞的“自动取消并切换”链路做稳，确认面板留作下一轮实现。

## 附加 Backlog（当前未动）

### [ ] T13 · 熔断与退避
### [ ] T14 · 测试 lru_cache 污染
说明：router_api 侧 `lru_cache` 已经去掉，这一项需要重新定义为“多 app / 多 loop 生命周期回归测试”。

### [x] T15 · MockStreamingAgentClient 移出
说明：已在 2026-04-08 完成，测试 mock client 现位于 `backend/tests/support/mock_agent_client.py`，生产运行时不再内置。
### [ ] T16 · demo_intents 懒加载
