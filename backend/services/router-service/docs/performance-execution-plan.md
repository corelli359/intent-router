# Router-Service 性能优化执行方案

## 目标

本轮只做高收益、低风险、可直接改善真实压测结果的改动，不引入新的共享状态风险，不重写核心编排模型。

## 本轮必须做

1. 移除 Router 内部 LLM prompt 变量中的 `indent=2`，减少 token 和序列化开销。
2. 内部 LLM 调用在没有 `on_delta` 时改为非流式 `ainvoke`，避免无意义的流式链路开销。
3. 补槽/待确认阶段引入轻量识别快路，优先走启发式 fallback，避免空上下文下重复全量 LLM 识别。
4. agent 中间 chunk 不再触发全图刷新和完整 SSE 广播，只在终态/阻塞态刷新。
5. `router_stage` 的 started/completed 日志降到 `DEBUG`，降低高并发日志 I/O。
6. 为同一 `session_id` 增加会话级锁，避免并发请求交错修改同一会话状态。

## 本轮暂不做

1. `ExecutionGraphState` 的节点/边索引缓存。
原因：需要可靠的失效策略，长度驱动缓存失效不够安全。

2. `_build_session_dump()` 改浅拷贝。
原因：虽然有收益，但需要和更多调用方的并发语义一起验证。

3. `get_fallback_intent()` 去掉深拷贝。
原因：前提是 fallback intent 在运行期绝对只读，本轮先不放大共享引用风险。

## 验收点

1. 相关单元测试通过。
2. 原有核心行为不回退：
   - LLM 调用仍可正常重试。
   - turn interpretation 在 fast-path 不可用时仍能保守降级。
   - agent 终态 chunk 仍能正确刷新图状态。
   - 同一 session 的并发请求不会交错写状态。

## 后续 TODO

1. 设计显式失效的 graph node/edge 索引。
2. 结合 session 锁评估 API 快照浅拷贝的安全边界。
3. 在压测环境补齐 `ROUTER_AGENT_BARRIER_ENABLED=true`，让 test target 同时具备逻辑挡板和副本隔离。
4. 基于真实压测结果继续削减 blocked-turn 阶段的 LLM 依赖。
