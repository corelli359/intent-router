# router-service core 重构任务清单与分包方案

状态：待执行

更新时间：2026-04-12

关联文档：

- `docs/层级意图路由与条件槽位治理改造方案.md`
- `docs/层级路由首期开发任务与时序设计.md`
- `docs/槽位填充准确率提升调研报告.md`

## 1. 背景与目标

当前 `backend/services/router-service/src/router_service/core` 已经完成两轮关键收口：

1. 删除失活的 V1 runtime
2. 将活跃的 graph runtime 模块从 `v2_*` 收口为正式 graph 命名

但当前 `core` 目录仍然是单层平铺，且混放了以下多类职责：

- 意图识别
- 槽位抽取与校验
- graph 建图与执行
- 推荐模式分流
- LLM / Agent / memory 基础设施
- prompt 模板
- 共享运行时状态模型

这导致当前结构虽然可运行，但不适合作为后续持续开发的稳定底座。

本次重构目标不是改变业务能力，而是把 `core` 收敛成一个可长期维护的分层结构。

本次目标：

1. 明确 `core` 内部的职责边界
2. 把单层平铺结构改成按职责分包
3. 降低 `api/dependencies.py` 对内部实现的穿透依赖
4. 把超大文件继续拆细，尤其是 `graph_orchestrator.py`
5. 给后续条件治理预留独立扩展位，但本轮不落地条件专项包

本次明确不做：

1. 不改现有 API 路径约定
2. 不改现有 `ROUTER_V2_*` 环境变量兼容键
3. 不改意图注册协议
4. 不改 graph runtime 行为语义
5. 不把条件治理强行并入本轮目录重构

## 2. 当前问题盘点

## 2.1 单层平铺，职责混杂

当前 `core` 下同时存在：

- 共享状态模型：`domain.py`、`graph_domain.py`
- 识别链路：`recognizer.py`、`domain_router.py`、`leaf_intent_router.py`、`hierarchical_intent_recognizer.py`
- 槽位链路：`slot_extractor.py`、`slot_validator.py`、`slot_resolution_service.py`、`understanding_validator.py`
- graph 执行链路：`graph_builder.py`、`graph_planner.py`、`graph_compiler.py`、`graph_runtime.py`、`graph_semantics.py`、`graph_presentation.py`、`graph_orchestrator.py`
- 支撑设施：`llm_client.py`、`agent_client.py`、`memory_store.py`、`context_builder.py`、`intent_catalog.py`
- prompt：`prompt_templates.py`

这些模块并列放在同一级目录，不利于阅读、检索和分阶段迁移。

## 2.2 超大文件过多

当前大文件规模已经超过合理上限：

- `graph_orchestrator.py`: 1282 行
- `slot_extractor.py`: 648 行
- `graph_builder.py`: 505 行
- `graph_planner.py`: 484 行
- `graph_presentation.py`: 450 行
- `graph_compiler.py`: 355 行
- `prompt_templates.py`: 343 行

这说明当前文件不是“功能完整”，而是“职责聚合过度”。

## 2.3 API 装配层穿透过深

`api/dependencies.py` 当前直接装配大量内部模块：

- recognizer
- graph builder
- graph planner
- recommendation router
- slot extractor
- slot validator
- understanding validator

这意味着 `api` 层在感知太多 `core` 内部拼装细节。后续只要 `core` 内部结构调整，就会连带修改 API 装配逻辑。

## 2.4 graph orchestrator 职责过重

`graph_orchestrator.py` 当前同时承担：

- session store
- message 入口调度
- action 入口调度
- proactive recommendation 路由
- pending graph / waiting node 切换
- task 创建
- agent 流式执行
- node / graph 状态刷新
- 事件发布
- memory promote

这是本轮最需要拆分的文件。

## 2.5 条件能力暂时仍与 graph 执行混放

当前条件相关逻辑主要散落在：

- `graph_domain.py`
- `graph_semantics.py`
- `graph_runtime.py`
- `graph_builder.py`
- `graph_compiler.py`

这不是错误，但会让后续条件专项治理时缺少独立落点。因此本轮需要在目标结构中预留 `conditions/` 或 `graph/conditions.py` 的演进空间，但不先做专项拆分。

## 3. 当前模块规模与职责概览

### 3.1 共享状态与基础模型

- `domain.py`
- `graph_domain.py`

### 3.2 识别链路

- `recognizer.py`
- `domain_router.py`
- `leaf_intent_router.py`
- `hierarchical_intent_recognizer.py`
- `intent_understanding_service.py`

### 3.3 槽位链路

- `slot_grounding.py`
- `slot_extractor.py`
- `slot_validator.py`
- `slot_resolution_service.py`
- `understanding_validator.py`

### 3.4 graph 运行时链路

- `graph_builder.py`
- `graph_planner.py`
- `graph_compiler.py`
- `graph_runtime.py`
- `graph_semantics.py`
- `graph_presentation.py`
- `graph_orchestrator.py`
- `recommendation_router.py`

### 3.5 支撑设施

- `llm_client.py`
- `agent_client.py`
- `memory_store.py`
- `context_builder.py`
- `intent_catalog.py`

### 3.6 prompt

- `prompt_templates.py`

## 4. 目标目录结构

建议将 `core` 调整为如下结构：

```text
router_service/core/
  shared/
    domain.py
    graph_domain.py

  recognition/
    recognizer.py
    domain_router.py
    leaf_intent_router.py
    hierarchical_intent_recognizer.py
    understanding_service.py

  slots/
    grounding.py
    extractor.py
    validator.py
    resolution_service.py
    understanding_validator.py

  graph/
    orchestrator.py
    session_store.py
    message_flow.py
    action_flow.py
    node_execution.py
    compiler.py
    builder.py
    planner.py
    runtime.py
    semantics.py
    presentation.py
    recommendation_router.py

  support/
    llm_client.py
    agent_client.py
    memory_store.py
    context_builder.py
    intent_catalog.py

  prompts/
    recognizer_prompts.py
    graph_prompts.py
    slot_prompts.py
    recommendation_prompts.py
```

说明：

1. `shared/` 只放运行时共享状态模型，不放逻辑
2. `recognition/` 只负责大类、小类识别与理解入口，不直接执行 graph
3. `slots/` 只负责槽位抽取、规范化、校验与 pre-dispatch gate
4. `graph/` 只负责建图、执行图推进、动作处理、事件投影
5. `support/` 放基础设施，不承载业务状态推进
6. `prompts/` 从单一 `prompt_templates.py` 拆为按能力分文件

## 5. 分层依赖规则

重构后必须遵守以下依赖方向：

1. `shared/` 不能依赖 `recognition/`、`slots/`、`graph/`
2. `prompts/` 不能依赖运行时模块
3. `support/` 可以依赖 `shared/`，但不能依赖 `graph/` 内部执行模块
4. `recognition/` 可以依赖 `shared/`、`support/`、`prompts/`
5. `slots/` 可以依赖 `shared/`、`support/`、`prompts/`
6. `graph/` 可以依赖 `shared/`、`support/`、`recognition/`、`slots/`
7. `api/` 不应直接感知 `recognition/slots/graph` 的细粒度实现，优先只依赖一个 runtime builder / facade

建议新增一条约束：

- `api/dependencies.py` 最终只允许直接 import：
  - `core.graph.runtime_factory`
  - `core.support.intent_catalog`
  - `core.support.llm_client`

## 6. 分阶段改造任务清单

## T1 建立目标分包骨架

目标：

- 先建立目录边界，再做模块迁移，避免继续在平铺层新增文件。

交付项：

- 新建：
  - `core/shared/`
  - `core/recognition/`
  - `core/slots/`
  - `core/graph/`
  - `core/support/`
  - `core/prompts/`
- 每个目录补 `__init__.py`

验收标准：

- 不移动业务代码前，目录骨架先到位
- 新增模块不再落到 `core/` 根层

## T2 迁移共享状态模型

目标：

- 把纯状态模型和运行逻辑分离。

交付项：

- `domain.py` -> `shared/domain.py`
- `graph_domain.py` -> `shared/graph_domain.py`
- 所有 import 改为走 `shared`

验收标准：

- `shared/` 下不出现服务逻辑函数
- 只保留枚举、Pydantic model、轻量 helper

## T3 迁移识别链路

目标：

- 将 domain route / leaf route / hierarchical route 收口到一个清晰分包。

交付项：

- `recognizer.py`
- `domain_router.py`
- `leaf_intent_router.py`
- `hierarchical_intent_recognizer.py`
- `intent_understanding_service.py`

验收标准：

- `recognition/` 内部不直接操作 graph 执行状态
- 识别链路只输出 recognition / understanding 结果

## T4 迁移槽位链路

目标：

- 将槽位治理能力收口，形成独立演进面。

交付项：

- `slot_grounding.py`
- `slot_extractor.py`
- `slot_validator.py`
- `slot_resolution_service.py`
- `understanding_validator.py`

额外交付项：

- 将 `slot_extractor.py` 再细拆为：
  - `extractor.py`
  - `heuristics.py`
  - `llm_extractor.py`
  - `payloads.py`
  - `merge_policy.py`

验收标准：

- graph 层不直接实现槽位抽取细节
- 槽位相关 prompt、规则、merge 策略可独立测试

## T5 拆分 graph orchestrator

目标：

- 将 `graph_orchestrator.py` 从“上帝文件”拆成可维护的 orchestrator 包内协作结构。

建议拆分：

- `graph/session_store.py`
- `graph/message_flow.py`
- `graph/action_flow.py`
- `graph/node_execution.py`
- `graph/state_sync.py`
- `graph/orchestrator.py`

建议职责：

- `session_store.py`
  - session create / get / expire / memory promote
- `message_flow.py`
  - `handle_user_message`
  - pending graph / waiting node / guided selection / proactive recommendation 入口分流
- `action_flow.py`
  - `handle_action`
  - `confirm_graph` / `cancel_graph` / `cancel_node`
- `node_execution.py`
  - task 创建
  - agent stream 消费
  - node fail / complete / wait 处理
- `state_sync.py`
  - graph 状态刷新
  - pending/current graph 切换
  - progress 事件投影
- `orchestrator.py`
  - 只保留 façade 与协作编排，不再承载大段具体逻辑

验收标准：

- `orchestrator.py` 控制在 300 行以内
- 单个子文件原则上不超过 400 行
- message/action/node execution 三条主链路可独立读懂

## T6 graph 其他模块分包迁移

目标：

- 让 graph 相关能力在目录层面形成闭环。

交付项：

- `graph_builder.py` -> `graph/builder.py`
- `graph_planner.py` -> `graph/planner.py`
- `graph_compiler.py` -> `graph/compiler.py`
- `graph_runtime.py` -> `graph/runtime.py`
- `graph_semantics.py` -> `graph/semantics.py`
- `graph_presentation.py` -> `graph/presentation.py`
- `recommendation_router.py` -> `graph/recommendation_router.py`

验收标准：

- graph 运行时逻辑优先只在 `graph/` 目录内闭环
- `graph/` 之外不再散落 graph 运行推进代码

## T7 prompt 分拆

目标：

- 将 300+ 行的 prompt 模板按能力拆开。

交付项：

- `prompts/recognizer_prompts.py`
- `prompts/graph_prompts.py`
- `prompts/slot_prompts.py`
- `prompts/recommendation_prompts.py`

验收标准：

- graph planner / builder / turn interpreter prompt 在同一文件
- slot extractor prompt 单独成文件
- recognizer 相关 prompt 与 domain/leaf router prompt 同组

## T8 API 装配收口

目标：

- 让 `api/dependencies.py` 只依赖一个稳定的 runtime builder，而不是一堆细粒度实现。

建议新增：

- `core/graph/runtime_factory.py`

交付项：

- `build_router_runtime()` 内部迁移到 runtime factory
- `api/dependencies.py` 只调用 factory

验收标准：

- `dependencies.py` 不再直接 import 大量内部模块
- core 内部重构不会持续反噬 API 层

## T9 测试与兼容收口

目标：

- 迁移过程中避免一次性改炸，同时把旧路径逐步下线。

交付项：

- 第一阶段保留 shim re-export
- 测试先改 import，不先改测试文件名
- 第二阶段再删除 shim

建议顺序：

1. 先迁移运行时代码与 import
2. 再迁移测试 import
3. 最后按需重命名 `test_v2_*`

说明：

- 测试文件名里保留 `v2` 一段时间可以接受，因为它表达的是历史覆盖范围，不是运行时结构
- 真正需要先收口的是生产代码路径，而不是测试文件名

## T10 条件治理预留位

目标：

- 不在本轮落地条件专项拆包，但提前保留未来结构位。

建议后续方向：

- 方案 A：`core/graph/conditions.py`
- 方案 B：`core/conditions/`

当前建议：

- 在本轮先保留在 `graph/` 域内
- 等条件治理真的开始落地时，再决定是否升格成 `conditions/` 独立分包

## 7. 推荐执行顺序

建议不要直接大爆炸搬目录，推荐按以下顺序执行：

1. 建目录骨架
2. 迁移 `shared/` 与 `support/`
3. 迁移 `recognition/`
4. 迁移 `slots/`
5. 拆 `graph_orchestrator.py`
6. 收口 `graph/` 其它模块
7. 分拆 `prompts/`
8. 新增 runtime factory
9. 清理 shim
10. 最后视情况调整测试文件名

## 8. 风险与控制策略

### 8.1 import 雪崩

风险：

- 一次性改目录会导致 import 大面积断裂。

控制策略：

- 第一阶段保留 shim 文件，仅做 re-export
- 每一批迁移后立即跑对应子集测试

### 8.2 graph_orchestrator 拆分后行为漂移

风险：

- message / action / node execution 逻辑拆散后容易引入状态同步偏差。

控制策略：

- 先抽纯 helper，再抽流程函数
- 不先改变状态机语义，只改变文件归属
- 每次拆分后必须跑：
  - `test_router_api_v2.py`
  - `test_v2_graph_runtime.py`
  - `test_v2_presentation.py`

### 8.3 prompt 拆分导致语义漏改

风险：

- prompt 常量重命名和文件拆分后容易遗漏调用方。

控制策略：

- 先只搬文件，不改文案
- prompt 常量分批重命名，不和目录迁移叠加

### 8.4 外部兼容被误删

风险：

- `ROUTER_V2_*` 环境变量和 `/api/router/v2` 路径仍是对外兼容约定。

控制策略：

- 本轮结构重构不删配置兼容键
- 本轮结构重构不删 `/api/router/v2`

## 9. 本轮建议验收口径

完成本轮重构后，应达到以下状态：

1. `core/` 根层不再继续堆业务模块
2. graph / recognition / slots / support / prompts 边界清楚
3. `graph_orchestrator.py` 被拆成多个协作模块
4. `api/dependencies.py` 收口为 runtime factory 装配
5. 当前已有回归测试全部通过
6. 对外 API、环境变量与 K8s 部署方式保持兼容

## 10. 最终结论

当前 `core` 单层平铺结构确实已经过乱，不适合作为后续开发底座。

下一阶段不应继续在 `core/` 根层补文件，而应按本方案直接进入“分包 + 大文件拆分 + 装配收口”三件事并行推进。

建议把这份文档作为后续 `router-service` 内部结构重构的执行基线。
