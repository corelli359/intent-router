# Intent Router Java SDK 集成方案

> **交付形态**: Java SDK (JAR)，内嵌 Python router 引擎  
> **部署方式**: 同容器  
> **Session**: Java 外部管理，每次请求传入/传出  
> **Long-term memory**: 外部传入  
> **SSE 事件**: Java 消费（Java 决定后续怎么推给前端）

---

## 一、整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Docker Container                              │
│                                                                  │
│  ┌─────────────────────────────────┐                             │
│  │         Java 主进程 (600 并发)    │                             │
│  │                                  │                             │
│  │  ┌────────────────────────────┐  │                             │
│  │  │  intent-router-sdk.jar     │  │                             │
│  │  │                            │  │                             │
│  │  │  IntentRouterClient        │  │   HTTP localhost:8100       │
│  │  │  ├── start() / close()     │──┼──────────────────────┐     │
│  │  │  ├── processMessage()      │  │                      ▼     │
│  │  │  ├── processAction()       │  │  ┌──────────────────────┐  │
│  │  │  └── healthCheck()         │  │  │ Python 子进程         │  │
│  │  │                            │  │  │ uvicorn               │  │
│  │  │  RouterEventListener       │  │  │                      │  │
│  │  │  └── onEvent(event)        │◀─┼──│ POST /v2/process     │  │
│  │  │                            │  │  │      ↓               │  │
│  │  └────────────────────────────┘  │  │ orchestrator 全链路   │  │
│  │                                  │  │      ↓               │  │
│  │  业务代码:                        │  │ SSE events + session │  │
│  │  router.processMessage(          │  │                      │  │
│  │    sessionState,                 │  └──────────────────────┘  │
│  │    "帮我转账",                    │                             │
│  │    longTermMemory,               │                             │
│  │    listener                      │                             │
│  │  ) → updated sessionState        │                             │
│  └─────────────────────────────────┘                             │
└─────────────────────────────────────────────────────────────────┘
```

**数据流**：

```
Java 业务代码调用 SDK
  → SDK 把 session_state + content + memory 打包成 HTTP POST
  → Python /v2/process 端点接收
  → 反序列化 GraphSessionState
  → 现有 orchestrator 全链路执行（识别→规划→槽位→Agent调度）
  → 执行过程中通过 SSE 流式推送事件给 Java
  → 最后一个 SSE event 携带更新后的 session_state
  → Java SDK 收到所有事件，回调 listener
  → 返回更新后的 session_state 给 Java 业务代码持久化
```

---

## 二、Python 侧改造

### 2.1 核心：新增无状态处理端点

当前的 `/sessions/{session_id}/messages/stream` 端点依赖 `GraphSessionStore` 内存 session。
新增一个 `/v2/process` 端点，**接收外部传入的完整 session 状态**，处理完后**返回更新后的 session**。

**关键设计**：orchestrator / message_flow / action_flow / state_sync **全部不动**。
只在 API 层做一个适配：用传入的 session 替代从 `SessionStore` 读取。

```python
# 新增文件: router_service/api/routes/sdk.py

class ProcessRequest(BaseModel):
    """SDK stateless processing request with externalized session."""
    session_state: dict[str, Any]             # GraphSessionState JSON
    content: str = ""
    cust_id: str = "cust_default"
    long_term_memory: list[str] = Field(default_factory=list)
    guided_selection: GuidedSelectionPayload | None = None
    proactive_recommendation: ProactiveRecommendationPayload | None = None
    recommendation_context: RecommendationContextPayload | None = None


class ProcessActionRequest(BaseModel):
    """SDK stateless action processing request."""
    session_state: dict[str, Any]
    action_code: str
    cust_id: str = "cust_default"
    long_term_memory: list[str] = Field(default_factory=list)
    source: str | None = None
    task_id: str | None = None
    confirm_token: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
```

**端点实现要点**：

```python
@sdk_router.post("/v2/process")
async def process_message(request: ProcessRequest, ...):
    # ① 反序列化外部 session
    session = GraphSessionState.model_validate(request.session_state)
    
    # ② 临时注入到 session_store（复用现有链路）
    orchestrator.session_store._sessions[session.session_id] = session
    
    # ③ 注入 long_term_memory
    orchestrator.session_store.long_term_memory.replace(
        session.cust_id, request.long_term_memory
    )
    
    # ④ 走现有流程（完全不改 orchestrator 内部）
    # ⑤ SSE 流式返回 events + 最终 updated session_state
    
    async def event_generator():
        queue = broker.register(session.session_id)
        task = asyncio.create_task(
            orchestrator.handle_user_message(
                session_id=session.session_id,
                cust_id=request.cust_id,
                content=request.content,
                ...
            )
        )
        try:
            while True:
                if task.done() and queue.empty():
                    await task
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                yield _encode_sse(event.event, event.model_dump(mode="json"))
        finally:
            broker.unregister(session.session_id, queue)
            # ⑥ 最终事件：返回更新后的 session
            updated = orchestrator.session_store.get(session.session_id)
            yield _encode_sse("session_state", {
                "session_state": updated.model_dump(mode="json")
            })
            # ⑦ 清理临时 session（不让它占内存）
            orchestrator.session_store._sessions.pop(session.session_id, None)
```

### 2.2 为什么这样设计

| 设计决策 | 理由 |
|---------|------|
| **临时注入 session_store** | orchestrator 内部 30+ 处直接读写 session，全部改参数传递代价太大。临时注入是最小改动方案 |
| **请求结束后清理** | Python 进程不持久保存 session，保持无状态 |
| **SSE 最后一帧返回 updated session** | Java 拿到后写回 Redis/DB |
| **long_term_memory 外部传入** | 替换 LongTermMemoryStore 内部状态，不改 orchestrator 的 recall 逻辑 |

### 2.3 LongTermMemoryStore 需要的小改造

当前 `memory_store.py` 没有"替换整个客户记忆"的方法。需要加一个：

```python
# memory_store.py 新增
def replace_facts(self, cust_id: str, facts: list[str]) -> None:
    """Replace the entire fact list for one customer (SDK integration)."""
    self._customers[cust_id] = CustomerMemory(cust_id=cust_id, facts=list(facts))
```

### 2.4 Python 侧改动清单

| 文件 | 改动 | 工作量 |
|------|------|--------|
| 新增 `api/routes/sdk.py` | 无状态处理端点 | 1 天 |
| `api/app.py` | 挂载 sdk_router | 10 分钟 |
| `core/support/memory_store.py` | 增加 `replace_facts()` | 30 分钟 |
| `core/graph/session_store.py` | 无改动 | - |
| `core/graph/orchestrator.py` | **无改动** | - |
| `core/graph/message_flow.py` | **无改动** | - |
| `core/graph/action_flow.py` | **无改动** | - |

---

## 三、Java SDK 设计

### 3.1 SDK 对外 API（Java 侧使用的接口）

```java
// ============================================
// 核心接口：IntentRouterClient
// ============================================
public interface IntentRouterClient extends AutoCloseable {

    /** 启动内嵌 Python 引擎 */
    void start() throws RouterStartupException;

    /** 处理一条用户消息（同步，内部消费SSE流） */
    RouterResult processMessage(ProcessMessageRequest request);

    /** 处理一条用户消息（异步回调方式，实时推送事件） */
    CompletableFuture<RouterResult> processMessageAsync(
        ProcessMessageRequest request,
        RouterEventListener listener
    );

    /** 处理一个显式动作（确认/取消等） */
    RouterResult processAction(ProcessActionRequest request);

    /** 处理一个显式动作（异步回调方式） */
    CompletableFuture<RouterResult> processActionAsync(
        ProcessActionRequest request,
        RouterEventListener listener
    );

    /** 健康检查 */
    boolean isHealthy();
}


// ============================================
// 请求模型
// ============================================
@Builder
public class ProcessMessageRequest {
    private String sessionState;       // GraphSessionState JSON 字符串
    private String content;            // 用户消息
    private String custId;
    private List<String> longTermMemory;
    // 可选
    private String guidedSelection;    // JSON
    private String proactiveRecommendation; // JSON
}

@Builder
public class ProcessActionRequest {
    private String sessionState;
    private String actionCode;         // "confirm_graph", "cancel_graph", etc.
    private String custId;
    private List<String> longTermMemory;
    private String taskId;
    private String confirmToken;
    private Map<String, Object> payload;
}


// ============================================
// 返回模型
// ============================================
public class RouterResult {
    private String updatedSessionState;  // 更新后的 session JSON
    private List<RouterEvent> events;    // 处理过程中产生的所有事件
    private boolean success;
    private String errorMessage;

    /** 便捷方法：直接拿 graph snapshot */
    public Optional<String> getGraphSnapshot() { ... }
}

public class RouterEvent {
    private String eventType;   // "graph.started", "node.running", "agent.delta" etc.
    private String data;        // JSON payload
    private long timestamp;
}


// ============================================
// 事件回调（异步模式使用）
// ============================================
public interface RouterEventListener {
    /** 收到一个实时事件（可用于流式推送给前端） */
    void onEvent(RouterEvent event);

    /** 处理完成 */
    default void onComplete(RouterResult result) {}

    /** 处理失败 */
    default void onError(Throwable error) {}
}
```

### 3.2 Java 侧使用示例

```java
// ① 初始化（应用启动时执行一次）
IntentRouterClient router = IntentRouterClient.builder()
    .pythonCommand("python3")         // 或 "/app/venv/bin/python"
    .routerModule("router_service.api.app:app")
    .host("127.0.0.1")
    .port(8100)
    .workers(3)                       // 3 个 uvicorn worker
    .startupTimeoutSeconds(30)
    .envVars(Map.of(
        "ROUTER_LLM_API_BASE_URL", llmUrl,
        "ROUTER_LLM_API_KEY", llmKey,
        "ROUTER_LLM_MODEL", "gpt-4o"
    ))
    .build();
router.start();  // 启动 Python 子进程，等 /health 返回 200


// ② 同步调用（简单场景）
String sessionState = redis.get("router:" + sessionId);
if (sessionState == null) {
    sessionState = "{}";  // 空 session，Python 侧会初始化
}

RouterResult result = router.processMessage(
    ProcessMessageRequest.builder()
        .sessionState(sessionState)
        .content("帮我给张三转 500 元，顺便查一下余额")
        .custId(custId)
        .longTermMemory(List.of(
            "常用收款人：张三",
            "上次转账金额 200 元"
        ))
        .build()
);

// ③ 保存更新后的 session
redis.set("router:" + sessionId, result.getUpdatedSessionState());

// ④ 拿到事件列表，自行决定推给前端
for (RouterEvent event : result.getEvents()) {
    if ("agent.delta".equals(event.getEventType())) {
        websocket.send(event.getData());  // 推给前端
    }
}


// ⑤ 异步调用（流式推送场景）
router.processMessageAsync(
    ProcessMessageRequest.builder()
        .sessionState(sessionState)
        .content("帮我查余额")
        .custId(custId)
        .longTermMemory(memories)
        .build(),
    new RouterEventListener() {
        @Override
        public void onEvent(RouterEvent event) {
            // 实时推送给前端（WebSocket / SSE / 消息队列）
            frontendPush.send(event);
        }

        @Override
        public void onComplete(RouterResult result) {
            redis.set("router:" + sessionId,
                       result.getUpdatedSessionState());
        }

        @Override
        public void onError(Throwable error) {
            log.error("Router processing failed", error);
        }
    }
);


// ⑥ 应用关闭
router.close();  // 优雅停止 Python 子进程
```

### 3.3 SDK 内部实现：单例 + 进程管理 + 守护线程

**IntentRouterClient 必须是单例**。一个 JVM 只管理一组 Python 进程。

```java
public class IntentRouterClientImpl implements IntentRouterClient {

    // ========== 单例保护 ==========
    private final AtomicBoolean started = new AtomicBoolean(false);
    private volatile Process pythonProcess;
    private Thread watchdogThread;
    private Thread logForwarderThread;
    private final WebClient webClient;

    // ========== 配置 ==========
    private final String pythonCommand;
    private final String routerModule;
    private final String host;
    private final int port;
    private final int workers;
    private final int startupTimeoutSeconds;
    private final Map<String, String> envVars;

    // ========== 启动（防重入） ==========
    @Override
    public synchronized void start() {
        if (!started.compareAndSet(false, true)) {
            log.warn("IntentRouterClient already started, skipping");
            return;
        }

        log.info("Starting Python router engine (port={}, workers={})", port, workers);

        // ① 启动 Python 进程
        launchPython();

        // ② 等待健康检查通过
        waitForHealthy();

        // ③ 启动守护线程（进程挂了自动重启）
        startWatchdog();

        log.info("Python router engine ready on port {}", port);
    }

    private void launchPython() {
        try {
            ProcessBuilder pb = new ProcessBuilder(
                pythonCommand, "-m", "uvicorn", routerModule,
                "--host", host,
                "--port", String.valueOf(port),
                "--workers", String.valueOf(workers),
                "--log-level", "info"
            );
            pb.environment().putAll(envVars);
            pb.redirectErrorStream(true);
            pythonProcess = pb.start();

            // 后台线程收集 Python stdout/stderr → Java slf4j
            logForwarderThread = new Thread(() -> {
                try (var reader = new BufferedReader(
                        new InputStreamReader(pythonProcess.getInputStream()))) {
                    String line;
                    while ((line = reader.readLine()) != null) {
                        log.info("[python-router] {}", line);
                    }
                } catch (IOException ignored) {}
            }, "intent-router-log-forwarder");
            logForwarderThread.setDaemon(true);
            logForwarderThread.start();
        } catch (IOException e) {
            started.set(false);
            throw new RouterStartupException("Failed to start Python process", e);
        }
    }

    // ========== 健康检查轮询 ==========
    private void waitForHealthy() {
        Instant deadline = Instant.now().plusSeconds(startupTimeoutSeconds);
        while (Instant.now().isBefore(deadline)) {
            try {
                HttpResponse<String> resp = HttpClient.newHttpClient()
                    .send(HttpRequest.newBuilder()
                        .uri(URI.create("http://" + host + ":" + port + "/health"))
                        .timeout(Duration.ofSeconds(2))
                        .GET().build(),
                    HttpResponse.BodyHandlers.ofString());
                if (resp.statusCode() == 200) return;  // ✅ Ready
            } catch (Exception ignored) {}
            try { Thread.sleep(500); } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RouterStartupException("Interrupted during health check");
            }
        }
        throw new RouterStartupException(
            "Python router failed to become healthy within " + startupTimeoutSeconds + "s");
    }

    // ========== Watchdog 守护线程 ==========
    private void startWatchdog() {
        watchdogThread = new Thread(() -> {
            while (started.get() && !Thread.interrupted()) {
                try {
                    Thread.sleep(5000);  // 每 5s 检查一次
                    if (pythonProcess != null && !pythonProcess.isAlive()) {
                        log.error("Python router process died (exit={}), restarting...",
                                  pythonProcess.exitValue());
                        launchPython();
                        waitForHealthy();
                        log.info("Python router restarted successfully");
                    }
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                } catch (Exception e) {
                    log.error("Watchdog failed to restart Python router", e);
                }
            }
        }, "intent-router-watchdog");
        watchdogThread.setDaemon(true);  // 守护线程，JVM 退出时自动终止
        watchdogThread.start();
    }

    // ========== 业务调用 ==========
    @Override
    public RouterResult processMessage(ProcessMessageRequest request) {
        List<RouterEvent> events = new ArrayList<>();
        String[] updatedSession = {null};

        webClient.post()
            .uri("/v2/process")
            .bodyValue(buildPayload(request))
            .retrieve()
            .bodyToFlux(String.class)
            .doOnNext(line -> {
                RouterEvent event = parseSseFrame(line);
                if ("session_state".equals(event.getEventType())) {
                    updatedSession[0] = event.getData();
                } else {
                    events.add(event);
                }
            })
            .blockLast(Duration.ofSeconds(120));

        return new RouterResult(updatedSession[0], events, true, null);
    }

    @Override
    public CompletableFuture<RouterResult> processMessageAsync(
            ProcessMessageRequest request,
            RouterEventListener listener) {
        return CompletableFuture.supplyAsync(() -> {
            List<RouterEvent> events = new ArrayList<>();
            String[] updatedSession = {null};

            webClient.post()
                .uri("/v2/process")
                .bodyValue(buildPayload(request))
                .retrieve()
                .bodyToFlux(String.class)
                .doOnNext(line -> {
                    RouterEvent event = parseSseFrame(line);
                    if ("session_state".equals(event.getEventType())) {
                        updatedSession[0] = event.getData();
                    } else {
                        events.add(event);
                        listener.onEvent(event);  // 实时回调
                    }
                })
                .doOnComplete(() -> {
                    RouterResult result = new RouterResult(
                        updatedSession[0], events, true, null);
                    listener.onComplete(result);
                })
                .doOnError(listener::onError)
                .blockLast(Duration.ofSeconds(120));

            return new RouterResult(updatedSession[0], events, true, null);
        });
    }

    // ========== 优雅关闭 ==========
    @Override
    public synchronized void close() {
        started.set(false);
        if (watchdogThread != null) watchdogThread.interrupt();
        if (pythonProcess != null && pythonProcess.isAlive()) {
            pythonProcess.destroy();           // 发 SIGTERM
            try {
                if (!pythonProcess.waitFor(5, TimeUnit.SECONDS)) {
                    pythonProcess.destroyForcibly();  // 5s 后强制 SIGKILL
                }
            } catch (InterruptedException e) {
                pythonProcess.destroyForcibly();
            }
        }
        log.info("Python router engine stopped");
    }

    @Override
    public boolean isHealthy() {
        try {
            HttpResponse<String> resp = HttpClient.newHttpClient()
                .send(HttpRequest.newBuilder()
                    .uri(URI.create("http://" + host + ":" + port + "/health"))
                    .timeout(Duration.ofSeconds(2)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
            return resp.statusCode() == 200;
        } catch (Exception e) {
            return false;
        }
    }
}
```

### 3.4 Spring Boot 集成（推荐）

```java
@Configuration
public class IntentRouterConfig {

    /**
     * 单例 Bean — 整个 JVM 只有一个 Router Client。
     * Spring 容器管理生命周期：
     *   - initMethod="start" → Bean 初始化时启动 Python 进程
     *   - destroyMethod="close" → 容器关闭时优雅停止 Python
     */
    @Bean(initMethod = "start", destroyMethod = "close")
    public IntentRouterClient intentRouterClient(
            @Value("${intent-router.port:8100}") int port,
            @Value("${intent-router.workers:3}") int workers,
            @Value("${intent-router.python-path:python3}") String pythonPath,
            @Value("${intent-router.startup-timeout:30}") int startupTimeout
    ) {
        return IntentRouterClient.builder()
            .pythonCommand(pythonPath)
            .routerModule("router_service.api.app:app")
            .host("127.0.0.1")
            .port(port)
            .workers(workers)
            .startupTimeoutSeconds(startupTimeout)
            .envVars(buildRouterEnv())
            .build();
    }

    private Map<String, String> buildRouterEnv() {
        // 从 Java 配置传递给 Python 的环境变量
        return Map.of(
            "ROUTER_LLM_API_BASE_URL", llmApiBaseUrl,
            "ROUTER_LLM_API_KEY", llmApiKey,
            "ROUTER_LLM_MODEL", llmModel,
            "ROUTER_ENV", "production"
        );
    }
}

// 业务 Service 中注入使用
@Service
public class ChatService {

    @Autowired
    private IntentRouterClient router;     // 自动注入单例

    @Autowired
    private StringRedisTemplate redis;

    public void handleUserMessage(String sessionId, String custId, String content) {
        String sessionState = redis.opsForValue().get("router:" + sessionId);

        router.processMessageAsync(
            ProcessMessageRequest.builder()
                .sessionState(sessionState != null ? sessionState : "{}")
                .content(content)
                .custId(custId)
                .longTermMemory(loadMemories(custId))
                .build(),
            new RouterEventListener() {
                @Override
                public void onEvent(RouterEvent event) {
                    pushToFrontend(sessionId, event);
                }
                @Override
                public void onComplete(RouterResult result) {
                    redis.opsForValue().set("router:" + sessionId,
                        result.getUpdatedSessionState(),
                        Duration.ofHours(2));
                }
            }
        );
    }
}
```

### 3.5 JVM 启停时序

```
JVM 启动
  │
  ├── Spring 容器初始化
  │     │
  │     ├── 创建其他 Bean（DB / Redis / MQ ...）
  │     │
  │     ├── 创建 IntentRouterClient Bean
  │     │     │
  │     │     ├── initMethod = "start()" 触发
  │     │     │     │
  │     │     │     ├── AtomicBoolean CAS 防止重复启动
  │     │     │     ├── ProcessBuilder 启动 Python 子进程
  │     │     │     │     uvicorn --workers 3 --port 8100
  │     │     │     │
  │     │     │     ├── 后台线程转发 Python 日志 → SLF4J
  │     │     │     │
  │     │     │     ├── 轮询 GET /health（最多等 30s）
  │     │     │     │     → {"status":"ok"}  ✅
  │     │     │     │
  │     │     │     ├── 启动 Watchdog 守护线程（每 5s 检查 Python 存活）
  │     │     │     │
  │     │     │     └── start() 返回，Bean 就绪
  │     │     │
  │     │     └── Bean 注册完成
  │     │
  │     └── 所有 Bean 就绪
  │
  ├── Readiness Probe → 200 OK（此时 Python 已经 ready）
  │
  └── 开始接收流量

────────────────────── 运行中 ──────────────────────

  Watchdog 检测到 Python 进程死亡
  │
  ├── log.error("Python router died, restarting...")
  ├── launchPython()     → 重新启动 Python 子进程
  ├── waitForHealthy()   → 等待新进程 ready
  └── log.info("Restarted successfully")

────────────────────── 关闭 ──────────────────────

JVM 关闭（SIGTERM / kill）
  │
  ├── Spring shutdown hook
  │     │
  │     ├── destroyMethod = "close()" 触发
  │     │     │
  │     │     ├── started.set(false) → Watchdog 退出循环
  │     │     ├── watchdogThread.interrupt()
  │     │     ├── pythonProcess.destroy()    → SIGTERM to Python
  │     │     ├── waitFor(5s)
  │     │     │     Python uvicorn 收到 SIGTERM：
  │     │     │       → 停止接收新请求
  │     │     │       → 等待 in-flight 请求完成
  │     │     │       → lifespan shutdown（关闭 catalog refresh 等）
  │     │     │       → 进程退出
  │     │     ├── 超时 → destroyForcibly() (SIGKILL)
  │     │     └── log.info("Python router stopped")
  │     │
  │     └── 其他 Bean 销毁
  │
  └── JVM 退出
```
```

---

## 四、Session 生命周期

```
                    Java 侧                              Python 侧
                    ──────                              ──────────
用户首次请求
  │
  ├─ sessionState 为空 → 传 "{}" 给 Python
  │                                          → Python 初始化 GraphSessionState
  │                                          → 执行 orchestrator 全链路
  │                                          → 返回 filled session_state
  │
  ├─ 收到 updated session_state
  ├─ 存入 Redis: router:session_001 = {...}
  │
用户第二次请求
  │
  ├─ 从 Redis 读: router:session_001
  ├─ 传 session_state JSON 给 Python
  │                                          → 反序列化为 GraphSessionState
  │                                          → 内含 messages / current_graph / tasks
  │                                          → 执行 orchestrator（有完整上下文）
  │                                          → 返回 updated session_state
  │
  ├─ 收到 updated session_state
  ├─ 写回 Redis
  │
Session 过期
  │
  ├─ Java 侧 TTL 管理（Redis EXPIRE 或定时清理）
  ├─ 过期后传空 session → 等同新 session
```

### 空 Session 处理

当 Java 传入空 `session_state`（`{}`），Python 端点需要初始化：

```python
if not request.session_state or request.session_state == {}:
    session = GraphSessionState(
        session_id=f"sdk_{uuid4().hex[:10]}",
        cust_id=request.cust_id,
    )
else:
    session = GraphSessionState.model_validate(request.session_state)
```

---

## 五、SSE 事件协议

Python → Java 的 SSE 流中，事件类型与当前完全一致，**加一个结束帧**：

| 事件类型 | 说明 | Java 处理方式 |
|---------|------|-------------|
| `graph.proposed` | 图构建完成，待确认 | 可推前端展示确认 UI |
| `graph.started` | 图开始执行 | 可推前端显示进度 |
| `graph.progress` | 图执行进度更新 | 可推前端更新进度条 |
| `node.running` | 节点开始执行 | 可推前端显示当前步骤 |
| `node.completed` | 节点执行完成 | 可推前端标记完成 |
| `agent.delta` | Agent 流式内容块 | **推前端做打字机效果** |
| `agent.done` | Agent 执行完成 | 可推前端 |
| `session.idle` | 整轮处理完毕 | 触发 session 保存 |
| **`session_state`** | **最终的 updated session JSON** | **SDK 内部消费，不推前端** |

Java SDK 内部自动识别 `session_state` 事件，提取 `updatedSessionState`，不传给 `RouterEventListener`。

---

## 六、并发与多 Worker

### 6.1 一次请求的 CPU vs I/O 时间分布

```
一次用户请求的时间线：

├── Intent 识别 (等 LLM 响应) ─────── 1-5s    ← asyncio await，不占 CPU
├── Graph 规划 (等 LLM 响应) ──────── 1-3s    ← asyncio await，不占 CPU
├── Slot 启发式抽取 (regex) ──────── ~5ms    ← 真正占 CPU
├── Slot LLM 抽取 (等 LLM 响应) ──── 1-3s    ← asyncio await，不占 CPU
├── Agent 调度 (等 Agent SSE 流) ─── 5-60s   ← asyncio await，不占 CPU
├── JSON 序列化 / session 处理 ───── <1ms    ← 真正占 CPU
│
└── CPU 实际占用：~6ms / 请求
    I/O 等待时间：3-70s / 请求
    CPU 占比：< 0.1%
```

asyncio 的工作方式是：当一个请求在 `await` 等 LLM/Agent 返回时，事件循环可以处理其他请求的 CPU 部分。所以一个 Worker 同时 `await` 200 个 I/O 操作没有任何问题——它不需要 200 个线程，就是 200 个协程挂在那里等网络返回。

### 6.2 Worker 数量 vs 并发能力

| Worker 数 | I/O 并发能力 | 实际瓶颈 |
|-----------|-------------|----------|
| 1 | ~200 | LLM API rate limit |
| 3 | ~600 | LLM API rate limit |
| 4 | ~800 | LLM API rate limit |
| 5 | ~1000 | LLM API rate limit |

**结论：3-4 个 Worker 足够扛 600 并发。真正卡脖子的是 LLM API 的并发限额，不是 Python Worker 数量。**

> [!NOTE]
> uvicorn `--workers N` 启动 N 个独立的 Python 进程，每个进程有自己的 asyncio 事件循环。进程间不共享内存。因为我们的 session 由 Java 外部传入（无状态），所以多 Worker 之间不存在状态一致性问题。

### 6.3 Session 无状态 = 无亲和性要求

因为 session 每次由 Java 传入，Python Worker 之间**不共享任何状态**。
uvicorn 的 `--workers 3` 会自动做 round-robin 负载均衡，无需 session 亲和性。

### 6.4 如果未来需要更多并发

**方式 1**：直接加 Worker 数量（最简单）

```bash
uvicorn ... --workers 5   # 5 × 200 = 1000 并发
```

**方式 2**：SDK 配置多端口进程组（更灵活，可分开监控）

```java
// SDK 配置启动两组 Python 进程
IntentRouterClient router = IntentRouterClient.builder()
    .workers(3)
    .port(8100)          // 组1: 8100, workers=3
    // SDK 内部 round-robin 分发到 8100
    .build();
```

---

## 七、容器打包

```dockerfile
# === Stage 1: Python 依赖层 ===
FROM python:3.12-slim AS python-deps
WORKDIR /app
COPY backend/services/router-service/pyproject.toml /app/
COPY backend/services/router-service/src /app/src
RUN pip install --no-cache-dir /app

# === Stage 2: 最终镜像 ===
FROM eclipse-temurin:21-jre-jammy

# 安装 Python runtime（不需要完整开发工具链）
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

# 复制 Python 包
COPY --from=python-deps /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=python-deps /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY --from=python-deps /app /app/router-service

# 复制 Java 应用
COPY target/your-java-app.jar /app/java-app.jar
COPY intent-router-sdk.jar /app/libs/intent-router-sdk.jar

# Java 启动（Java 内部通过 SDK 启动 Python）
ENTRYPOINT ["java", "-jar", "/app/java-app.jar"]
```

---

## 八、错误处理与容错

### 8.1 Python 进程崩溃

```java
// SDK 内部实现守护线程
private void startWatchdog() {
    watchdogThread = new Thread(() -> {
        while (!shutdown) {
            if (!pythonProcess.isAlive()) {
                log.warn("Python router process died, restarting...");
                try {
                    start();  // 重启
                } catch (Exception e) {
                    log.error("Failed to restart Python router", e);
                }
            }
            Thread.sleep(5000);
        }
    });
    watchdogThread.setDaemon(true);
    watchdogThread.start();
}
```

### 8.2 请求超时

```java
// SDK 配置
@Builder.Default
private Duration processTimeout = Duration.ofSeconds(120);

// 处理中超时
webClient.post()
    .uri("/v2/process")
    .bodyValue(payload)
    .retrieve()
    .bodyToFlux(String.class)
    .timeout(processTimeout)   // 超时自动取消
    .doOnError(TimeoutException.class, e -> {
        // 返回当前已收到的事件 + 原始 session（不更新）
    });
```

### 8.3 Session 序列化失败

```python
# Python 侧防御性处理
try:
    session = GraphSessionState.model_validate(request.session_state)
except ValidationError as exc:
    # session 数据损坏，重建空 session
    logger.warning("Session deserialization failed, creating fresh: %s", exc)
    session = GraphSessionState(
        session_id=request.session_state.get("session_id", f"sdk_{uuid4().hex[:10]}"),
        cust_id=request.cust_id,
    )
```

---

## 九、实施路线图

### 第一批：Python 无状态端点（2 天）

| 任务 | 文件 | 重要度 |
|------|------|--------|
| ① `GraphSessionState` 序列化往返测试 | `tests/test_session_serialization.py` | 🔴 |
| ② 新增 `api/routes/sdk.py`（消息处理） | `api/routes/sdk.py` | 🔴 |
| ③ 新增 action 处理端点 | `api/routes/sdk.py` | 🔴 |
| ④ `memory_store.py` 增加 `replace_facts()` | `core/support/memory_store.py` | 🟡 |
| ⑤ `app.py` 挂载 sdk_router | `api/app.py` | 🟢 |
| ⑥ 手动联调 curl 测试 | - | 🟡 |

### 第二批：Java SDK JAR（3 天）

| 任务 | 说明 |
|------|------|
| ① `IntentRouterClient` 接口定义 | API 层 |
| ② `IntentRouterClientImpl` 实现 | 进程管理 + HTTP + SSE 消费 |
| ③ 请求/响应模型 | `ProcessMessageRequest`, `RouterResult` 等 |
| ④ Watchdog 守护线程 | 进程崩溃自动重启 |
| ⑤ Maven/Gradle 打包 | 发布为 JAR |

### 第三批：联调与压测（2 天）

| 任务 | 验收标准 |
|------|---------|
| ① 功能联调 | 多轮对话 session 正确传递 |
| ② 并发压测 | 300 并发无报错，P99 < 10s |
| ③ 容错测试 | Python 进程 kill 后 5s 内自动恢复 |
| ④ Session 一致性 | 100 轮对话后 session 反序列化无丢失 |

**总工作量：~7 天**

---

## 十、SDK 给 Java 团队的交付物

| 交付物 | 说明 |
|--------|------|
| `intent-router-sdk-x.y.z.jar` | Maven 坐标，Java 侧引入即用 |
| `intent-router-python.tar.gz` | Python 代码 + 依赖，放入容器 `/app/router-service/` |
| `Dockerfile.example` | 参考容器构建文件 |
| `SDK-README.md` | 接入指南 + API 文档 + 配置说明 |
| `docker-compose.yml` | 本地开发环境一键启动 |

---

## 十一、与现有部署的兼容性

SDK 模式和独立部署模式**可以共存**：

```
独立部署模式（现有）：
  uvicorn router_service.api.app:app --port 8100
  前端直接调 /api/router/v2/sessions/...

SDK 嵌入模式（新增）：
  Java SDK 内部启动 uvicorn
  Java 调 /v2/process（新端点）
  现有的 /api/router/v2/sessions/... 端点仍然可用
```

两套端点挂在同一个 FastAPI app 上，互不干扰。
