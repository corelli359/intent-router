# router-service 代码评审改造方案设计

状态：待实施

更新时间：2026-04-13

关联文档：

- `docs/router_service_code_review.md`
- `docs/router-service开发架构与代码导读.md`
- `docs/层级意图路由与条件槽位治理改造方案.md`

## 1. 文档目的

这份文档用于把 `router_service_code_review.md` 中有价值的评审建议，转化为一份可直接指导后续开发的正式改造方案。

这份方案不照单全收 review 结论，而是基于当前真实代码状态重新判断：

1. 哪些问题已经存在且应立即治理
2. 哪些建议方向对，但优先级不高
3. 哪些建议前提不准确，需要纠偏后再落地
4. 哪些改造适合先做，哪些改造应后置

本方案的目标不是大范围重写，而是在不破坏当前主链路的前提下，优先增强：

- graph 运行时安全性
- session 生命周期治理
- 热路径性能
- 重复实现收敛
- 测试覆盖
- 运行期可观测性

## 2. 当前主链路边界

在进入改造项之前，需要先明确当前系统的主边界不变：

1. 用户消息先进入 router 层
2. router 先完成意图识别
3. 再完成对应意图节点的槽位抽取与校验
4. 然后由 graph 编译与运行时驱动节点执行
5. 每个意图背后仍由独立 agent 执行
6. agent 不负责意图路由，只负责槽位检查、补充交互和后续执行

本次改造不改变这条主链路，也不把条件能力和槽位能力混做一体化重构。

## 3. 对 review 结论的采纳判断

## 3.1 直接采纳

以下建议结论明确成立，应进入实施清单：

1. `_drain_graph` 需要最大迭代保护
2. `TERMINAL_NODE_STATUSES` 等常量/辅助函数存在重复定义
3. `intent_catalog.list_active()` 热路径存在重复遍历和重复建索引
4. `GraphMessageFlow`、`GraphActionFlow`、`GraphStateSync` 缺少隔离测试
5. `condition_matches` 的类型不匹配路径缺少可观测性
6. `LongTermMemoryStore` 缺少容量上限
7. orchestrator 存在过多 delegate wrapper，后续应收敛
8. `extractor.py`、`planner.py` 文件规模偏大，后续应按职责拆分

## 3.2 部分采纳，需要纠偏

以下建议方向基本正确，但原 review 的表述不够准确：

### 3.2.1 session 过期

review 中说“session 过期无主动清理”，这个结论只对一半。

当前代码在 `GraphSessionStore.get_or_create()` 中已经做了懒过期处理：

- 当 session 已过期时，会先执行 `long_term_memory.promote_session(...)`
- 然后重建一个新的 `GraphSessionState`

因此，当前真正缺少的不是“过期判定”，而是：

1. 过期 session 的后台主动清理
2. `_sessions` 字典中的存量过期对象清除
3. session 生命周期指标和治理开关

### 3.2.2 StreamingAgentClient 测试

review 中说 `StreamingAgentClient` 无专项测试，这个结论不准确。

当前 `backend/tests/test_llm_integration.py` 已经覆盖了以下路径：

1. HTTP payload mapping
2. cancel 接口路径拼接
3. mock scheme 拒绝
4. client close 行为

因此这里真正缺失的是“边界场景专项测试”，包括：

1. SSE 空流
2. 非法 JSON
3. HTTP 4xx/5xx
4. 半结构化 chunk
5. 取消失败路径

## 3.3 暂不作为主线

以下建议可以保留，但不进入当前第一批开发主线：

1. `GraphSessionState.tasks` 增加 `_tasks_by_id` 私有索引
2. `ExecutionGraphState` 增加节点/边懒索引缓存
3. `LangChainLLMClient` 缓存 `ChatOpenAI` 实例
4. 直接让 `MessageFlow/ActionFlow` 持有 orchestrator 引用

原因：

1. 这些问题成立，但当前收益不如主链路安全性和热路径优化高
2. 其中部分改造会把缓存语义混进 Pydantic 状态模型，增加一致性风险
3. 当前主要性能瓶颈仍是远端 LLM 调用，而不是本地对象创建
4. flow 直接持有 orchestrator 会加重循环依赖，不如引入 typed callbacks 收敛

## 4. 总体改造原则

本轮改造遵循以下原则：

1. 先补安全护栏，再做结构瘦身
2. 先优化热路径重复工作，再处理低收益局部优化
3. 先补 isolation test，再做中型结构调整
4. 条件能力单独治理，不与槽位治理混改
5. 不为了“提前兼容”保留多余结构，代码以当前开发态为准

## 5. 分期实施方案

## 5.1 P0：运行时安全与 session 生命周期治理

P0 是必须优先处理的一批，目标是先消除潜在失控路径。

### 5.1.1 `_drain_graph` 最大迭代保护

影响模块：

- `backend/services/router-service/src/router_service/core/graph/orchestrator.py`

当前问题：

- `_drain_graph()` 使用无界 `while True`
- 如果某个节点因为 bug 长期不能进入 terminal 或 waiting 状态，循环可能无法自然收敛
- 单节点执行有 timeout，但整个 graph drain 没有总迭代护栏

设计方案：

1. 在 `GraphRouterOrchestratorConfig` 中新增配置项
2. 推荐新增两种控制方式中的一种：
   - `max_drain_iterations: int | None`
   - `drain_iteration_multiplier: int`
3. 默认策略采用图规模自适应：
   - `max(8, len(graph.nodes) * 3)`
4. `_drain_graph()` 每轮循环递增计数
5. 超过阈值后：
   - 记录 error log
   - 将 graph 标记为 `FAILED`
   - 写入失败原因到 graph 级 payload 或 message
   - 发出 `graph.failed`
   - 发出 `session.idle`
   - 退出 drain

不建议的处理方式：

1. 直接 `break` 而不改 graph 状态
2. 只写 warning 不落用户可见状态
3. 把这类异常伪装成正常完成

验收标准：

1. 构造异常图时不会无限循环
2. 会产生可见的 graph 失败状态
3. SSE 与 snapshot 状态一致

测试设计：

1. 增加专门单测，构造无法收敛的假节点状态迁移
2. 验证迭代超限后 graph 进入 `FAILED`
3. 验证 session 最终回到 `idle`

### 5.1.2 session 主动清理机制

影响模块：

- `backend/services/router-service/src/router_service/core/graph/session_store.py`
- `backend/services/router-service/src/router_service/api/dependencies.py`
- `backend/services/router-service/src/router_service/settings.py`

当前问题：

1. 已有懒过期重建，但没有后台主动清理
2. `_sessions` 字典可能长期持有已过期对象
3. 当前生命周期治理依赖“下次再访问这个 session”，这在长时间无访问场景下不够

设计方案：

1. 保留现有 `get_or_create()` 中的懒过期逻辑
2. 在 `GraphSessionStore` 中新增：
   - `purge_expired(now: datetime | None = None) -> int`
3. `purge_expired()` 行为：
   - 遍历 `_sessions`
   - 找出 `is_expired()` 的 session
   - 对未清理过的 session 执行 `promote_session(...)`
   - 删除 session
   - 返回清理数量
4. 新增后台循环，与 intent catalog refresh 同级运行
5. 新增配置项：
   - `ROUTER_SESSION_CLEANUP_INTERVAL_SECONDS`
6. 可选增加日志：
   - 本轮清理数量
   - 当前 session 总量

实现注意点：

1. 避免同一个 session 在懒过期和后台清理中重复 promote
2. 可通过“删除前 promote”保证只执行一次
3. `get()` 的调用方若读取一个已被后台清理的 session，应保持当前按不存在处理的语义

验收标准：

1. 后台循环可定期回收过期 session
2. 过期 session 不会无限积压在内存中
3. 长期记忆仍可被提升，不丢历史信息

测试设计：

1. 为 `GraphSessionStore` 增加过期清理单测
2. 验证清理前会 promote，清理后 `_sessions` 不再持有该 session
3. 验证不同 cust_id 复用同 session_id 的现有语义不被破坏

## 5.2 P1：热路径优化、重复实现收敛、核心隔离测试补齐

P1 解决的是“跑得久了会烦”和“改起来不安心”的问题。

### 5.2.1 intent catalog 增加按 code 的只读索引视图

影响模块：

- `backend/services/router-service/src/router_service/core/support/intent_catalog.py`
- `backend/services/router-service/src/router_service/core/graph/compiler.py`
- `backend/services/router-service/src/router_service/core/graph/orchestrator.py`
- `backend/services/router-service/src/router_service/core/recognition/understanding_service.py`

当前问题：

当前多个热路径都会执行：

```python
{intent.intent_code: intent for intent in self.intent_catalog.list_active()}
```

这意味着：

1. 同一次请求中多次重复遍历 active intents
2. 多次重复构造字典
3. 代码分散，后续无法统一优化

设计方案：

1. 扩展 `CatalogSnapshot`，增加：
   - `active_by_code: dict[str, IntentDefinition]`
2. 在 `refresh_now()` 中一次性构建：
   - `active`
   - `active_by_code`
   - `domains`
   - `priorities`
3. 对外新增接口：
   - `active_intents_by_code() -> dict[str, IntentDefinition]`
   - `get_active_intent(intent_code: str) -> IntentDefinition | None`
4. compiler / orchestrator / understanding service 一律改为读取快照索引

实现注意点：

1. 对外仍返回副本，避免运行期直接修改 snapshot 内部数据
2. `get_fallback_intent()` 的深拷贝语义保持不变

验收标准：

1. 热路径不再散落重复建字典
2. intent catalog 的索引语义集中在一个模块里
3. 现有功能与排序语义不变

测试设计：

1. 扩充 `test_intent_catalog.py`
2. 验证 refresh 后索引内容和 active 列表一致
3. 验证 fallback 与 domain 视图不受影响

### 5.2.2 图与槽位层的重复常量/辅助函数收敛

影响模块：

- `backend/services/router-service/src/router_service/core/graph/runtime.py`
- `backend/services/router-service/src/router_service/core/graph/orchestrator.py`
- `backend/services/router-service/src/router_service/core/graph/action_flow.py`
- `backend/services/router-service/src/router_service/core/slots/extractor.py`
- `backend/services/router-service/src/router_service/core/slots/validator.py`
- `backend/services/router-service/src/router_service/core/slots/grounding.py`

当前问题：

存在多类重复实现：

1. `TERMINAL_NODE_STATUSES`
2. `_CURRENCY_TOKENS`
3. `_combined_text`
4. `_value_is_grounded`
5. `slot_signature` 拼接逻辑

设计方案：

1. graph 相关常量统一收敛到 graph 公共常量位置
2. slot 相关公共能力统一收敛到 `slots/grounding.py`
3. 建议新增以下 helper：
   - `combine_distinct_text(*parts: str | None) -> str`
   - `slot_semantic_signature(slot_def: IntentSlotDefinition) -> str`
   - `currency_aliases(currency_code: str) -> tuple[str, ...] | None`
   - `slot_value_grounded_with_currency_fallback(...) -> bool`

不建议第一步就做的事：

1. 直接把 `cached_property` 塞进 `IntentSlotDefinition`
2. 一上来重写 extractor/validator 主体结构

原因：

1. 先收公共 helper，最小化行为变化
2. 避免把缓存语义过早耦合进 Pydantic 模型

验收标准：

1. 重复定义消失
2. 槽位 grounding 行为不回退
3. graph terminal 状态集合只保留一份

测试设计：

1. 跑现有 `slot extractor` 与 `slot validator` 测试
2. 增加针对公共 helper 的单测
3. 验证币种 fallback 行为不变

### 5.2.3 核心 Flow/StateSync 隔离测试补齐

影响模块：

- `backend/tests/`

新增测试文件建议：

1. `test_graph_message_flow.py`
2. `test_graph_action_flow.py`
3. `test_graph_state_sync.py`
4. `test_streaming_agent_client.py`

当前问题：

1. 现有回归更多依赖 `test_router_api_v2.py` 这类 E2E
2. message/action/state 这几个核心状态机缺少低成本隔离测试
3. 一旦改 orchestrator/flow，很容易只能靠大而慢的 API 测试兜底

设计方案：

1. `GraphMessageFlow`
   - pending graph turn
   - waiting node turn
   - proactive recommendation turn
   - guided selection turn
   - free dialog 路由分支
2. `GraphActionFlow`
   - confirm pending graph
   - cancel pending graph
   - cancel current node
   - cancel current graph
3. `GraphStateSync`
   - skipped node 事件发布
   - graph progress 聚合
   - terminal message 附加逻辑
4. `StreamingAgentClient`
   - 空流
   - 非法 JSON
   - HTTP 4xx/5xx
   - 非预期 content-type
   - cancel 失败

验收标准：

1. 中等规模结构调整不再只能依赖大 E2E
2. graph message/action 主分支均有独立测试
3. client 边界场景具备明确回归用例

## 5.3 P2：可观测性、内存治理、配置硬化

### 5.3.1 `condition_matches` 类型不匹配告警

影响模块：

- `backend/services/router-service/src/router_service/core/graph/runtime.py`

当前问题：

当前 `TypeError` 会被吞掉并直接返回 `False`，这虽然安全，但排障成本高。

设计方案：

1. 在 `except TypeError` 分支增加 warning log
2. 日志字段至少包含：
   - `source_node_id`
   - `left_key`
   - `operator`
   - `left_value`
   - `left_type`
   - `right_value`
   - `right_type`

边界说明：

1. 这只是条件运行时的可观测性增强
2. 不代表条件能力要在本轮提前专项开发
3. 条件仍作为独立治理线，不与槽位治理合并

### 5.3.2 graph 取消失败汇总反馈

影响模块：

- `backend/services/router-service/src/router_service/core/graph/action_flow.py`
- `backend/services/router-service/src/router_service/core/graph/presentation.py`

当前问题：

`cancel_current_graph()` 中若多个 agent cancel 失败，只会散落 warning log，调用方拿不到汇总反馈。

设计方案：

1. 在 graph cancel 过程中收集失败项：
   - node_id
   - task_id
   - error 摘要
2. 将汇总结果挂入 graph 事件 payload
3. 不因为部分 cancel 失败阻塞 graph 进入 `CANCELLED`

验收标准：

1. 用户侧或调用侧可以看到取消失败摘要
2. graph 状态仍保持可终止

### 5.3.3 LongTermMemoryStore 容量上限与淘汰

影响模块：

- `backend/services/router-service/src/router_service/core/support/memory_store.py`
- `backend/services/router-service/src/router_service/settings.py`

当前问题：

当前长期记忆是无界追加，客户长期对话会持续膨胀。

设计方案：

1. 新增配置项：
   - `ROUTER_MEMORY_MAX_FACTS_PER_CUSTOMER`
2. `LongTermMemoryStore` 增加上限控制
3. `remember()` 或 `promote_session()` 后统一裁剪
4. 默认淘汰策略为：
   - 超限后删除最旧 facts

可选增强：

1. 对连续重复 fact 做去重
2. 后续升级为结构化 memory record，但不在本轮落地

验收标准：

1. 长期记忆空间可控
2. `recall()` 语义保持最近优先

### 5.3.4 生产环境 `.env` 扫描硬化

影响模块：

- `backend/services/router-service/src/router_service/settings.py`

当前问题：

当前 `Settings.from_env()` 会主动扫描 `.env` 和 `.env.local`，在 dev 很方便，但在容器环境中过于激进。

设计方案：

1. 仅在以下场景启用本地 env 文件加载：
   - `env == "dev"`
   - 或显式设置 `ROUTER_LOAD_DOTENV=1`
2. 非 dev 环境默认只读取进程环境变量

验收标准：

1. k8s/生产环境不再依赖目录扫描
2. 本地开发体验保留

## 5.4 P3：结构收敛与可维护性重构

这批改造是合理的，但不应早于 P0/P1/P2。

### 5.4.1 orchestrator delegate wrappers 收敛

影响模块：

- `backend/services/router-service/src/router_service/core/graph/orchestrator.py`
- `backend/services/router-service/src/router_service/core/graph/message_flow.py`
- `backend/services/router-service/src/router_service/core/graph/action_flow.py`

当前问题：

orchestrator 中存在较多“一行转发型 wrapper”，使得文件体积继续膨胀。

设计方案：

1. 不建议让 flow 直接持有 orchestrator
2. 建议引入 `GraphFlowCallbacks` dataclass 或 Protocol
3. 将当前散落的十余个 callback 收敛为一个 typed bundle 注入给 `message_flow` 和 `action_flow`

好处：

1. 降低 orchestrator 文件噪音
2. callback 输入面统一
3. 后续新增 flow 依赖时，不必在多个构造参数中继续平铺扩张

落地顺序要求：

1. 先补 tests
2. 再做 callbacks 收敛

### 5.4.2 `extractor.py`、`planner.py` 拆分

影响模块：

- `backend/services/router-service/src/router_service/core/slots/`
- `backend/services/router-service/src/router_service/core/graph/`

设计方向：

1. `extractor.py`
   - `heuristics.py`
   - `llm_extractor.py`
   - `merge.py`
   - `extractor.py` 作为 facade
2. `planner.py`
   - `planning_models.py`
   - `turn_interpreter.py`
   - `planner.py`

为什么后置：

1. 这是可维护性重构，不是当前主风险
2. 在 tests 不足时拆大文件，回归风险偏高

### 5.4.3 可选的局部索引优化

包括：

1. session task 查找优化
2. graph node/edge 查找索引

当前判断：

1. 问题成立
2. 但当前 graph 节点规模仍偏小
3. 优先级低于主链路安全性、重复实现收敛和测试补齐

如果后续实施，建议：

1. 先做 runtime 层临时索引
2. 再评估是否真的需要把索引语义放进状态模型

## 6. 条件能力的独立治理说明

这里单独强调一次，避免后续开发误解：

1. 条件不等于槽位
2. 条件能力当前只做运行期安全性和可观测性增强
3. 条件专项设计、抽取、结构化建模仍作为单独任务保留
4. 本文不要求条件先做

本轮与条件相关的改造，只包括：

1. `condition_matches` 告警日志
2. graph 取消与异常场景的状态可见性增强

本轮明确不做：

1. ConditionExtractor
2. 条件 DSL
3. 条件结构化存储升级
4. 条件与槽位合仓

## 7. 不进入当前主线的事项

以下事项保留在观察列表，但不进入当前主线开发：

1. `GraphSessionState.tasks` 私有索引字段
2. `ExecutionGraphState` 内嵌索引缓存
3. `LangChainLLMClient` 复用 `ChatOpenAI` 实例
4. flow 直接引用 orchestrator
5. 结构化 long-term memory 全量升级

原因统一归纳如下：

1. 收益不如当前主线问题高
2. 可能引入额外缓存一致性问题
3. 可能造成职责边界倒退

## 8. 建议实施顺序

建议按四批执行：

### 第一批

1. `_drain_graph` 迭代保护
2. session 主动清理
3. intent catalog 按 code 索引视图

### 第二批

1. graph/slot 公共常量与 helper 收敛
2. MessageFlow / ActionFlow / StateSync / AgentClient 隔离测试补齐

### 第三批

1. condition warning log
2. cancel 失败汇总反馈
3. long-term memory 容量上限
4. `.env` 扫描硬化

### 第四批

1. orchestrator callbacks 收敛
2. extractor / planner 大文件拆分
3. 可选局部索引优化

## 9. 实施完成后的目标状态

当本方案实施完成后，`router-service` 应达到以下状态：

1. graph 执行链路具备基本防失控护栏
2. session 生命周期从“懒处理”升级为“懒处理 + 主动清理”
3. 热路径不再重复构建 active intent 索引
4. slot 与 graph 的公共逻辑不再散落复制
5. 核心 flow 层具备独立回归测试
6. 条件能力仍保持独立治理边界
7. orchestrator 的后续瘦身具备更稳定的测试基础

## 10. 结论

`router_service_code_review.md` 的整体判断方向是正确的，尤其在运行时护栏、重复实现、测试缺口和结构瘦身上，结论基本成立。

但从当前代码真实状态看，本轮不应平均用力，而应先抓住下面四件事：

1. 先补 `_drain_graph` 与 session 生命周期这两个安全问题
2. 再做 intent catalog 热路径优化和公共 helper 收敛
3. 紧接着补齐 Flow/StateSync/AgentClient 的 isolation test
4. 最后再做 callbacks 收敛和大文件拆分

这样推进，既能解决当前真实风险，也不会因为过早做结构性重排而把主链路搞乱。
