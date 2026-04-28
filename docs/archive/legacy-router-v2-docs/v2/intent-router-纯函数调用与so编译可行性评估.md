# Router-Service 纯函数调用 / .so 编译方案可行性评估

## 一、当前代码的技术约束

在评估方案前，先量化 router-service 的技术特征：

| 维度 | 数值 | 影响 |
|------|------|------|
| `async def` 函数数量 | **121 个** | 全链路异步，必须有 asyncio 事件循环 |
| 外部 C 扩展依赖 | **httpx, pydantic, langchain-openai, PyJWT** | 必须有 CPython 解释器 |
| 标准库依赖 | asyncio, json, re, logging, datetime | 必须有完整 Python runtime |
| 核心外部 I/O | LLM 调用 (httpx) + Agent 调度 (httpx SSE) | 不可能纯 CPU 运行 |

**关键事实：router-service 的 121 个 async 函数 + httpx/pydantic/langchain C 扩展，决定了它无法脱离 CPython 运行。** 任何"编译为 .so"或"纯函数调用"的方案，最终都要内嵌一个 CPython 解释器。

---

## 二、五种候选方案评估

### 方案 A：Jep（Java Embedded Python）— 纯函数调用 ✅ 可行

```
┌──────────────────────────────────┐
│           JVM 进程                │
│                                  │
│  Java Thread                     │
│  ├── Jep SharedInterpreter       │
│  │   ├── CPython 解释器（内嵌）    │
│  │   ├── asyncio event loop      │
│  │   ├── router_service 模块     │
│  │   └── httpx/pydantic/langchain│
│  │                               │
│  │  Java 直接调用:               │
│  │  interp.invoke("process",     │
│  │    sessionJson, content, ...)  │
│  │         │                     │
│  │         ▼                     │
│  │  Python 函数执行               │
│  │  loop.run_until_complete(     │
│  │    engine.process_message()   │
│  │  )                            │
│  │         │                     │
│  │         ▼                     │
│  │  返回 JSON string 给 Java     │
│  └───────────────────────────────┘
└──────────────────────────────────┘
```

**工作原理**：
- Jep 通过 JNI 在 JVM 进程内嵌入 CPython 解释器
- Java 直接调用 Python 函数，无 HTTP 开销
- 返回值通过 JNI 直接传递（字符串/数字/字典）

**asyncio 处理方式**：

```python
# router_service/sdk/engine.py — 无 HTTP 的纯函数入口

import asyncio
import json
from router_service.core.graph.orchestrator import GraphRouterOrchestrator
from router_service.core.shared.graph_domain import GraphSessionState

class RouterEngine:
    """Embeddable router engine without HTTP server."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._orchestrator = None  # lazy init

    def initialize(self, config_json: str) -> str:
        """Initialize orchestrator with given config. Called once."""
        config = json.loads(config_json)
        # 复用 dependencies.py 的构建逻辑，但不启动 FastAPI
        self._orchestrator = self._build_orchestrator(config)
        return json.dumps({"status": "ok"})

    def process_message(
        self,
        session_state_json: str,
        content: str,
        cust_id: str,
        long_term_memory_json: str,
    ) -> str:
        """Process one message synchronously, return updated session + events."""
        result = self._loop.run_until_complete(
            self._async_process_message(
                session_state_json, content, cust_id, long_term_memory_json
            )
        )
        return json.dumps(result, ensure_ascii=False)

    async def _async_process_message(self, session_json, content, cust_id, memory_json):
        """Async implementation — called within the managed event loop."""
        # ① 反序列化 session
        session_data = json.loads(session_json) if session_json else {}
        if not session_data:
            session = GraphSessionState(session_id=f"sdk_{...}", cust_id=cust_id)
        else:
            session = GraphSessionState.model_validate(session_data)

        # ② 临时注入 session（同 HTTP 方案）
        self._orchestrator.session_store._sessions[session.session_id] = session

        # ③ 注入 long_term_memory
        memories = json.loads(long_term_memory_json) if long_term_memory_json else []
        self._orchestrator.session_store.long_term_memory.replace_facts(
            cust_id, memories
        )

        # ④ 收集事件（用回调替代 SSE）
        events = []
        original_publish = self._orchestrator._publish_event
        def capture_event(event):
            events.append(event.model_dump(mode="json"))
            # 可选：在这里通过 Jep callback 推给 Java
        self._orchestrator._publish_event = capture_event

        # ⑤ 执行 orchestrator 全链路
        try:
            snapshot = await self._orchestrator.handle_user_message(
                session_id=session.session_id,
                cust_id=cust_id,
                content=content,
            )
        finally:
            self._orchestrator._publish_event = original_publish

        # ⑥ 提取更新后的 session
        updated = self._orchestrator.session_store.get(session.session_id)
        self._orchestrator.session_store._sessions.pop(session.session_id, None)

        return {
            "updated_session_state": updated.model_dump(mode="json"),
            "events": events,
            "snapshot": snapshot.model_dump(mode="json"),
        }

    def close(self):
        """Shutdown the event loop and release resources."""
        self._loop.close()


# 模块级单例，Jep 导入时创建
_engine = RouterEngine()

def initialize(config_json: str) -> str:
    return _engine.initialize(config_json)

def process_message(session_json, content, cust_id, memory_json) -> str:
    return _engine.process_message(session_json, content, cust_id, memory_json)

def close():
    _engine.close()
```

**Java 侧调用**：

```java
public class JepRouterClient implements IntentRouterClient {

    private final SharedInterpreter interp;

    public JepRouterClient() {
        // Jep 解释器必须在单个固定线程上使用
        this.interp = new SharedInterpreter();
        interp.runScript("router_service/sdk/engine.py");
    }

    @Override
    public void start() {
        String config = buildConfigJson();
        interp.invoke("initialize", config);
    }

    @Override
    public RouterResult processMessage(ProcessMessageRequest req) {
        // 直接函数调用，无 HTTP
        String resultJson = (String) interp.invoke(
            "process_message",
            req.getSessionState(),
            req.getContent(),
            req.getCustId(),
            toJson(req.getLongTermMemory())
        );
        return parseRouterResult(resultJson);
    }

    @Override
    public void close() {
        interp.invoke("close");
        interp.close();
    }
}
```

**Jep 方案评估**：

| 维度 | 评估 |
|------|------|
| **可行性** | ✅ 可行，Jep 支持 CPython 全部功能 |
| **asyncio** | ✅ `loop.run_until_complete()` 可以工作 |
| **C 扩展** | ✅ httpx/pydantic/langchain 全部可用 |
| **性能** | 省去 HTTP 开销 ~0.3ms，但 JNI 调用本身有 ~0.05ms 开销 |
| **流式事件** | ⚠️ 困难 — Jep 调用是同步阻塞的，无法中途回调 Java |
| **并发** | 🔴 **严重限制** — Jep 解释器线程绑定 + Python GIL |
| **改造量** | 新增 `sdk/engine.py`，~200 行 |

> [!CAUTION]
> **Jep 的致命限制：并发**
>
> Jep 解释器是**线程绑定**的 — 创建它的线程是唯一能调用它的线程。加上 Python GIL，这意味着：
> - **同一时刻只能处理一个请求**
> - 如果需要并发，必须创建多个 Jep 解释器（= 多个 CPython 解释器）
> - 每个 CPython 解释器的内存开销 ~50-100MB（含 langchain/pydantic 等依赖）
> - 要支持 600 并发 I/O，理论上需要的不是 600 个解释器（因为 asyncio），但 `loop.run_until_complete()` 是阻塞的，一次只能跑一个请求
>
> **解决办法：Jep 解释器池**
> ```java
> // 创建 N 个 Jep 解释器，每个独占一个线程
> ExecutorService pool = Executors.newFixedThreadPool(10);
> // 每个线程持有一个 Jep SharedInterpreter
> // 600 并发 / 10 解释器 = 每个解释器排队 60 个请求
> // 但因为每个请求等 LLM 3-5s，排队会很严重
> ```
> 这本质上**又回到了多进程方案**，只是进程变成了 JVM 内的多线程 + 多 CPython 解释器。

---

### 方案 B：GraalPy（GraalVM Python）— 真正的 JVM 原生 ❌ 不可行

GraalPy 可以在 JVM 上运行 Python 代码，无需 CPython。但：

| 依赖 | GraalPy 支持 | 状态 |
|------|-------------|------|
| pydantic v2 | ❌ 依赖 `pydantic-core`（Rust 编译的 C 扩展） | **不可用** |
| httpx | ❌ 依赖 `httpcore`（C 扩展） | **不可用** |
| langchain-openai | ❌ 依赖 `openai`（依赖 httpx） | **不可用** |
| PyJWT | ⚠️ 纯 Python 部分可用，但加密后端可能不行 | **部分可用** |

**结论：GraalPy 无法运行 router-service，因为核心依赖全是 C 扩展。**

---

### 方案 C：Nuitka 编译为 .so ⚠️ 可行但价值有限

```bash
# Nuitka 编译整个 router_service 包为 .so
python -m nuitka --module router_service \
    --follow-imports \
    --include-package=router_service \
    --output-dir=./build
# 产出: router_service.cpython-312-x86_64-linux-gnu.so
```

**编译结果**：
- Nuitka 将你的 Python 代码翻译为 C 代码，编译为 `.so`
- **但 .so 仍然需要 CPython 解释器来加载**（它是 CPython 扩展模块格式）
- httpx、pydantic、langchain 等依赖**不会被编译进去**，仍需 pip install

**实际效果**：

```
不编译（现状）：
  CPython 解释器 → 解释执行 router_service/*.py → 调用 httpx/pydantic C 扩展

Nuitka 编译后：
  CPython 解释器 → 加载 router_service.so（已编译的 C）→ 调用 httpx/pydantic C 扩展
```

| 维度 | 效果 |
|------|------|
| **启动速度** | 略快（省去 .py 解析和 bytecode 编译） |
| **运行速度** | 对 CPU 密集操作提升 10-30%，对 I/O 等待无影响 |
| **源码保护** | ✅ .so 比 .py 更难反编译 |
| **消除 Python 依赖** | ❌ **不能**。仍然需要 CPython + pip 依赖 |
| **消除 HTTP 服务** | ❌ **不能**。只是编译了代码，不改架构 |

**结论：Nuitka .so 编译的主要价值是源码保护和微小的 CPU 性能提升，不改变架构。** 如果需要保护源码交付给 Java 团队，这招有用。

---

### 方案 D：Cython 编译 ⚠️ 类似 Nuitka

与 Nuitka 类似，Cython 可以将 `.py` 编译为 `.so`。差异：

| 维度 | Nuitka | Cython |
|------|--------|--------|
| 编译单元 | 整个包 | 单个文件 |
| 类型优化 | 自动 | 需要手动加类型注解 |
| asyncio 支持 | ✅ 完整 | ⚠️ 需要注意 |
| 实际加速 | 10-30% CPU | 10-50% CPU（加类型后） |
| 仍需 CPython | ✅ 是 | ✅ 是 |

**结论：不改变架构本质，同 Nuitka。**

---

### 方案 E：Standalone 二进制 + 管道 IPC ⚠️ 可行

```bash
# PyInstaller / Nuitka 打包为独立可执行文件
python -m nuitka --standalone --onefile router_engine_cli.py
# 产出: router_engine_cli（单文件二进制，内嵌 CPython + 所有依赖）
```

```
Java ──(stdin JSON)──► router_engine_cli ──(stdout JSON)──► Java
```

**工作原理**：
1. 将 router engine 打包为独立二进制（不需要目标机器安装 Python）
2. Java 通过 stdin/stdout 用 JSON 与之通信
3. 二进制内嵌 CPython 解释器 + 所有 pip 依赖

```python
# router_engine_cli.py
import sys, json

engine = RouterEngine()
engine.initialize(os.environ.get("ROUTER_CONFIG", "{}"))

for line in sys.stdin:
    request = json.loads(line)
    result = engine.process_message_sync(
        request["session_state"],
        request["content"],
        request["cust_id"],
        request.get("long_term_memory", []),
    )
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
```

| 维度 | 评估 |
|------|------|
| **消除 Python 安装依赖** | ✅ 独立二进制，无需 pip |
| **通信开销** | ~0.1ms（管道 IPC，比 HTTP 更低） |
| **流式事件** | ⚠️ 可以用 stdout 逐行输出，但不如 SSE 规范 |
| **并发** | 🔴 同 Jep — GIL + 阻塞式调用 |
| **二进制大小** | ~200-500MB（含 CPython + 全部依赖） |
| **部署简便性** | ✅ 单文件，Java 侧只需 `ProcessBuilder` |

---

## 三、核心矛盾：asyncio + GIL + 并发

不管选哪种"纯函数调用"方案，都绕不开一个根本矛盾：

```
你的代码:                你需要的:
  121 个 async def           600 并发
  全链路 await               低延迟
  I/O 密集 (等 LLM 3-5s)    流式事件推送

asyncio 的设计:
  一个事件循环 = 一个线程
  loop.run_until_complete() 是阻塞的
  GIL 限制同一进程只有一个线程跑 Python
```

**在 HTTP 方案中**，uvicorn 天然解决了这个问题：
- 每个 worker 有自己的事件循环
- FastAPI 的 `async def` 端点与 uvicorn 事件循环协作
- 多个请求在同一个事件循环中并发 await

**在纯函数调用中**，调用方（Java）是同步阻塞的：
- `interp.invoke("process_message", ...)` → 阻塞当前 Java 线程
- 内部的 `loop.run_until_complete()` → 阻塞 Python 事件循环
- 一次只能处理一个请求
- 要并发 → 需要多个 Python 解释器实例 → 本质上就是多进程

```
HTTP 方案 (3 workers):
  Worker 1 事件循环: [req1 await LLM] [req4 await LLM] [req7 ...] ...
  Worker 2 事件循环: [req2 await LLM] [req5 await LLM] [req8 ...] ...
  Worker 3 事件循环: [req3 await LLM] [req6 await LLM] [req9 ...] ...
  → 轻松支持 600 I/O 并发

Jep 方案 (10 解释器):
  Interpreter 1: [req1 阻塞 3s] → [req11 阻塞 3s] → ...
  Interpreter 2: [req2 阻塞 3s] → [req12 阻塞 3s] → ...
  ...
  Interpreter 10: [req10 阻塞 3s] → [req20 阻塞 3s] → ...
  → 同时只能处理 10 个请求！剩下 590 排队

  除非：在 Python 侧也用 asyncio 并发（但这就需要长驻事件循环）
```

> [!IMPORTANT]
> **要让 Jep 支持并发，Python 侧必须维护一个长驻 asyncio 事件循环，Java 侧通过队列提交请求。** 这本质上就是在 JVM 内部实现了一个"微型 uvicorn"。

---

## 四、如果真的要做：Jep + 长驻事件循环方案

这是唯一能在"无 HTTP 服务"前提下支持并发的设计：

```
┌──────────────────────────────────────────────────┐
│                  JVM 进程                          │
│                                                    │
│  Java 业务线程 (600 并发)                           │
│  ├── Thread-1: submit(req1) → Future<Result>       │
│  ├── Thread-2: submit(req2) → Future<Result>       │
│  └── ...                                           │
│        │                                           │
│        ▼                                           │
│  ┌──────────────┐                                  │
│  │ RequestQueue  │  (Java ConcurrentLinkedQueue)    │
│  └──────┬───────┘                                  │
│         │                                          │
│  ┌──────▼──────────────────────────────────┐       │
│  │ Python 专用线程 (1个)                     │       │
│  │                                          │       │
│  │  Jep SharedInterpreter                   │       │
│  │  ├── CPython 解释器                       │       │
│  │  ├── asyncio event loop (长驻运行)         │       │
│  │  │   ├── await process_message(req1)      │       │
│  │  │   ├── await process_message(req2)      │       │
│  │  │   ├── await process_message(req3)      │       │
│  │  │   └── ... (并发 await 200+)            │       │
│  │  └── httpx/pydantic/langchain             │       │
│  └──────────────────────────────────────────┘       │
│         │                                           │
│         ▼                                           │
│  Java Future.complete(result) → 唤醒业务线程         │
└──────────────────────────────────────────────────────┘
```

**Python 侧**：

```python
# router_service/sdk/async_engine.py

import asyncio
import json
import threading
from concurrent.futures import Future

class AsyncRouterEngine:
    """Long-running async engine for Jep integration."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._orchestrator = None
        self._request_queue = asyncio.Queue()

    def initialize(self, config_json: str):
        """Must be called from the Jep thread."""
        asyncio.set_event_loop(self._loop)
        self._orchestrator = self._build_orchestrator(json.loads(config_json))

    def run_loop(self):
        """Block the Jep thread, run event loop forever. 
        Called once after initialize()."""
        self._loop.run_forever()

    def submit_message(self, request_json: str, callback_id: str):
        """Submit a request from Java. Non-blocking.
        Called via Jep from any Java thread? No — Jep is thread-bound.
        Must be called from the Jep thread.
        """
        # 这里有问题：Jep 是线程绑定的，
        # 但 run_loop() 已经阻塞了这个线程。
        # 无法从其他线程调用 Jep。
        pass  # ← 这就是 Jep 的根本限制
```

> [!CAUTION]
> **走到这里就会发现 Jep 方案的死结**：
> 
> 1. Jep 解释器**只能在创建它的线程上使用**
> 2. 如果这个线程被 `loop.run_forever()` 阻塞了→ Java 无法再通过 Jep 提交新请求
> 3. 如果不阻塞这个线程→ 事件循环无法运行→ async 函数无法执行
> 
> **要打破这个死结**，需要用 `asyncio.run_coroutine_threadsafe()` + Python 侧的后台线程来运行事件循环。但这又引入了 Python 后台线程 + GIL 竞争的复杂性。
> 
> **这本质上就是在 JVM 内部手工重建了 uvicorn 的事件循环管理。 省掉的 HTTP 网络层，全部变成了 JNI + 线程协调 + 事件循环管理的复杂度。**

---

## 五、诚实对比

| 维度 | HTTP 方案 (已批准) | Jep 纯函数调用 | .so 编译 |
|------|-------------------|---------------|---------|
| **延迟节省** | 基准 | ~0.3ms (约 0.01%) | ~0.5ms CPU提升 |
| **并发模型** | uvicorn 原生支持 | 极复杂（需重建事件循环管理） | 不改变 |
| **流式事件** | SSE 原生 | 需要自建回调机制 | 不改变 |
| **Python 依赖** | 需要 Python 环境 | 需要 Python 环境 | 需要 Python 环境 |
| **源码保护** | 源码可见 | 源码可见 | ✅ .so 不可读 |
| **进程管理** | ProcessBuilder | JNI 线程管理 | ProcessBuilder |
| **调试难度** | 低（HTTP 抓包） | 高（JNI + GIL 死锁排查） | 高（编译产物调试） |
| **改造量** | ~2 天 Python + 3 天 Java | ~5 天 Python + 5 天 Java | ~2 天编译配置 |
| **生产稳定性** | 高（经过充分验证的模式） | 中（Jep 在大型项目中的案例少） | 高（编译不改逻辑） |

---

## 六、实际推荐

### 如果目标是"消除 HTTP 开销"→ 不值得

LLM 调用耗时 1-5 秒，HTTP localhost 开销 0.3ms。即使完全消除 HTTP 层，性能提升 **< 0.01%**。但引入的工程复杂度是数量级的增长。

### 如果目标是"源码保护"→ Nuitka 编译

```bash
# 将 router_service 编译为 .so，交付给 Java 团队
python -m nuitka --module router_service \
    --follow-imports \
    --include-package=router_service \
    --nofollow-import-to=tests \
    --output-dir=./dist

# 交付物：
# router_service.cpython-312-x86_64-linux-gnu.so  (编译后的代码)
# + requirements.txt  (pip 依赖清单)
# + 仍然使用 HTTP 方案的架构
```

**这样既保护了源码，又不改变已批准的 HTTP 架构。**

### 如果目标是"不要额外启动服务进程"→ Jep 但降低并发预期

如果 Java 团队强烈要求"不启动子进程、不开 HTTP 端口"，Jep 可以工作，但：

1. 并发能力大幅下降（需要解释器池，每个 ~100MB 内存）
2. 流式事件需要自建回调机制
3. 调试难度显著增加
4. 不建议用于 600 并发场景

### 如果目标是"单文件交付，不要求装 Python"→ Nuitka standalone

```bash
python -m nuitka --standalone --onefile router_engine_cli.py
# 产出单个二进制文件 (~300MB)，无需安装 Python
# Java 通过 ProcessBuilder 启动，stdin/stdout 通信
```

---

## 七、最终建议

```
                选择决策树
                ──────────

目标是什么？
│
├── "不要 HTTP 开销" → 不值得，0.3ms vs LLM 3-5s
│
├── "源码保护" → Nuitka --module 编译为 .so
│                 仍用 HTTP 方案
│                 工作量 +2 天
│
├── "不安装 Python" → Nuitka --standalone 独立二进制
│                      仍用 ProcessBuilder + HTTP
│                      工作量 +2 天
│
├── "绝对不启动子进程" → Jep 方案
│                        并发降到 ~50-100
│                        工作量 +10 天
│                        ⚠️ 不推荐用于 600 并发
│
└── "兼顾所有" → HTTP 方案 + Nuitka .so 编译
                  源码保护 ✅
                  600 并发 ✅
                  流式事件 ✅
                  工作量 7+2 = 9 天
```

> [!TIP]
> **实际推荐组合**：在已批准的 HTTP 方案基础上，加一步 Nuitka 编译。交付给 Java 团队的是编译后的 `.so` 文件而非源码 `.py` 文件，既保护知识产权，又不牺牲架构优势。
