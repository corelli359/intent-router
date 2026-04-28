# Router-Service 性能压测问题修复方案

这个方案解决了在挡板模式下压测“效果不理想”的两个核心问题。压测效果不好并非完全是架构的性能瓶颈，而是因为压测脚本和路由自身对挡板降级的异常处理存在逻辑缺陷。

## 1. 目标

修复导致压测端因为客户端连接池瓶颈而产生错误延迟的问题，同时修复 Router 在挡板模式下遇到 LLM 拦截时未能优雅降级，而是直接抛出 503 错误导致压测立马失败的问题。

## 2. 发现的问题

### A. 压测端（向外发请求）的连接池阻塞瓶颈
**问题所在**：在 `admin_service/perf/service.py` 和测试脚本 `scripts/run_router_perf_ladder.py` 中，都直接使用了默认配置的 `httpx.AsyncClient`。
`httpx` 客户端默认的安全限制是最大连接数 100（`max_connections=100`），长连接 20（`max_keepalive_connections=20`）。由于阶梯压测配置的最大并发可能达到 120 甚至更高，所有超出 100 的请求会被**直接在本地生成脚本的队列里阻塞**，这导致测出来的 `latency_ms` 包含了大量无意义的“本地排队时间”。此时 QPS 到达瓶颈完全是本地客户端的问题。

### B. 挡板模式下的 503 错误 (Router 逻辑)
**问题所在**：在之前的重构中，移除了轻量级的 `_fast_recognize` 快路。取而代之的是，不管什么上下文都会走到 `recognize_message` 甚至 `extractor`。
而在挡板模式下（`ROUTER_LLM_BARRIER_ENABLED=true`），对大模型的访问会被拦截并抛出 `LLMBarrierTriggeredError`。目前的异常捕获逻辑存在缺陷：
1. `understanding_service.py` 内部检测到异常时，使用的是 `llm_exception_is_retryable(exc)` 进行评估，但这只会识别 429 错误。因为没有包含对挡板错误的捕获，导致异常被 `raise` 抛到了应用层，转成 `HTTP 503`。
2. `extractor.py` 中更是直接写死了 `if llm_barrier_triggered(exc): raise`。
由于产生了 503 错误，压测阶梯脚本一发现失败数 > 0 就会立马终止该轮次的测试，导致结果极差（成功率 0% 或中断）。

## 3. 执行修改（已完成）

### [MODIFY] `scripts/run_router_perf_ladder.py`
为负载生成器解除 `httpx.Limits`：
```python
limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=timeout, limits=limits) as client:
```

### [MODIFY] `backend/services/admin-service/src/admin_service/perf/service.py`
为 Admin 服务的压测后台解除限制：
```python
def _default_client_factory(self) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=self._settings.perf_test_target_base_url,
        timeout=self._settings.perf_test_request_timeout_seconds,
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=None)
    )
```

### [MODIFY] `backend/services/router-service/src/router_service/core/recognition/understanding_service.py`
将挡板拦截错误纳入可接受的优雅降级范围内：
```diff
-  if not llm_exception_is_retryable(exc):
+  if not (llm_exception_is_retryable(exc) or llm_barrier_triggered(exc)):
       raise
```

### [MODIFY] `backend/services/router-service/src/router_service/core/slots/extractor.py`
在槽位提取时，把挡板错误视为服务暂不可用，进行启发式降级提取：
```diff
-  if llm_barrier_triggered(exc):
-      raise
-  if llm_exception_is_retryable(exc):
+  if llm_exception_is_retryable(exc) or llm_barrier_triggered(exc):
```
