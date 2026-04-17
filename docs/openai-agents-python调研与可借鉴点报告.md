# OpenAI Agents SDK 调研与可借鉴点报告

## 1. 结论摘要

结论先行：

- `openai-agents-python` 对当前 `intent-router` **有帮助**，但它最适合承担的是“单个业务 agent 内部的 agent runtime”，**不适合直接替换当前 `router-service` 核心**。
- 对我们最有价值的不是“整套 run loop 原样接入”，而是三类能力：
  - `guardrails + approvals`：给高风险业务动作加自动校验和人工审批边界。
  - `tracing`：把一次 agent run 的模型调用、工具调用、handoff、审批中断串成可调试链路。
  - `session / resumable state`：把“暂停后继续”做成明确的运行时边界。
- 如果要真正落地，**最佳切入点是某个独立业务 agent 服务内部**，例如 `transfer-money-agent`、`credit-card-repayment-agent`、`forex-agent`，而不是 `router-service`。
- 上游仓库是 **MIT License**，所以从许可证角度，**可以依赖、复制或改写其代码模块**，前提是保留许可证声明。

一句话判断：

- **架构借鉴：强烈建议**
- **业务 agent 内部试点：建议**
- **直接替换 router 核心：不建议**

## 2. 调研范围

本次调研覆盖四部分：

1. OpenAI 官方 Agents 指南：
   - <https://developers.openai.com/api/docs/guides/agents>
   - <https://developers.openai.com/api/docs/guides/agents/running-agents>
   - <https://developers.openai.com/api/docs/guides/agents/orchestration>
   - <https://developers.openai.com/api/docs/guides/agents/guardrails-approvals>
   - <https://developers.openai.com/api/docs/guides/agents/results>
   - <https://developers.openai.com/api/docs/guides/agents/integrations-observability>
2. OpenAI Python SDK 仓库：
   - <https://github.com/openai/openai-agents-python>
3. 上游源码的关键模块：
   - `src/agents/agent.py`
   - `src/agents/run.py`
   - `src/agents/handoffs/__init__.py`
   - `src/agents/guardrail.py`
   - `src/agents/memory/sqlite_session.py`
   - `src/agents/run_state.py`
   - `src/agents/tracing/processors.py`
   - `src/agents/function_schema.py`
4. 当前仓库的相关实现：
   - [`backend/services/router-service/src/router_service/api/dependencies.py`](../backend/services/router-service/src/router_service/api/dependencies.py)
   - [`backend/services/router-service/src/router_service/core/support/agent_client.py`](../backend/services/router-service/src/router_service/core/support/agent_client.py)
   - [`backend/services/router-service/src/router_service/core/graph/session_store.py`](../backend/services/router-service/src/router_service/core/graph/session_store.py)
   - [`backend/services/router-service/src/router_service/core/shared/domain.py`](../backend/services/router-service/src/router_service/core/shared/domain.py)
   - [`backend/services/agents/transfer-money-agent/src/transfer_money_agent/service.py`](../backend/services/agents/transfer-money-agent/src/transfer_money_agent/service.py)
   - [`backend/services/agents/forex-agent/src/forex_agent/service.py`](../backend/services/agents/forex-agent/src/forex_agent/service.py)
   - [`docs/router-service开发架构与代码导读.md`](./router-service开发架构与代码导读.md)

调研时间点：

- 当前报告基于 **2026-04-17** 的官方文档访问结果。
- 本地拉取的上游仓库 HEAD 为 `3ad12bc27308278fc875144a04c60a068d399b1a`，提交时间 **2026-04-16**。
- 上游 `pyproject.toml` 当前包版本为 **`0.14.1`**。

## 3. 上游项目到底提供什么

### 3.1 官方定位

OpenAI 官方文档对 Agents SDK 的定位非常明确：

- Agents SDK 适合“**你的服务端自己拥有 orchestration、tool execution、state、approvals**”的场景。
- 官方建议先从 **单个 agent** 起步，再在“职责、工具、策略边界确实变化”时拆多 agent。
- 一次 SDK run 的核心循环是：
  - 调模型
  - 看输出
  - 有 tool call 就执行
  - 有 handoff 就切换 agent
  - 直到得到 final output 或进入 interruption

这说明它本质上是一个 **通用 agent runtime**，而不是一个面向“外部 HTTP 业务 agent 编排”的专用 router。

### 3.2 上游核心能力

从官方文档和源码结构看，上游主要提供这些能力：

- `Agent` / `Runner`
  - 定义 agent 和运行主循环。
- `handoff` / `agent.as_tool`
  - 在多个 specialist 之间做所有权切换，或把 specialist 当工具调用。
- `guardrails`
  - 对输入、输出、工具调用做自动校验。
- `approvals`
  - 在有副作用动作前暂停，等待人工审批。
- `session`
  - 托管对话历史，支持持久化和恢复。
- `result / run_state`
  - 把一次未完成 run 的“可恢复状态”显式暴露出来。
- `tracing`
  - 记录模型调用、工具调用、handoff、guardrail、custom spans。
- `MCP`
  - 连接 hosted MCP 或 runtime-owned MCP server。
- `sandbox`
  - 面向文件/命令/补丁/长周期工作流的容器化 agent 环境。

### 3.3 上游源码的工程特点

这套 SDK 的几个工程点值得特别注意：

- `tool`、`guardrail`、`run_state` 都是高度类型化和结构化的，不是简单 prompt wrapper。
- `run_state.py` 明确把“暂停后恢复”作为一等能力建模。
- `function_schema.py` 会把 Python 函数签名和 docstring 转成严格 JSON schema，这对工具暴露很有价值。
- `tracing/processors.py` 里已经带了一个可发到 OpenAI tracing ingest 的 exporter。
- `memory/sqlite_session.py` 提供了一个线程安全、支持持久化的 SQLite session 实现。

## 4. 与当前 intent-router 的架构对照

### 4.1 当前项目的核心边界

从当前仓库实现看，`intent-router` 的边界已经非常清楚：

- `router-service` 负责：
  - 意图识别
  - graph 构建 / 规划
  - session / task / graph 状态管理
  - SSE 事件发布
  - 对外部业务 agent 的 HTTP/SSE 调度
- 业务 agent 负责：
  - 单个业务能力内部的槽位补充与执行
  - 通过当前既定协议返回 `waiting/completed/failed`

当前事实上的运行时中心是：

- [`backend/services/router-service/src/router_service/api/dependencies.py`](../backend/services/router-service/src/router_service/api/dependencies.py)
- [`backend/services/router-service/src/router_service/core/support/agent_client.py`](../backend/services/router-service/src/router_service/core/support/agent_client.py)
- [`backend/services/router-service/src/router_service/core/graph/session_store.py`](../backend/services/router-service/src/router_service/core/graph/session_store.py)

这套结构与 OpenAI Agents SDK 的默认假设并不一致。

### 4.2 最大的不匹配点

#### A. 我们已经有一个 router-runtime 了

OpenAI Agents SDK 假设“agent runtime 是你的编排核心”。  
但当前项目里，**编排核心已经是 `GraphRouterOrchestrator + EventBroker + GraphSessionStore`**。

如果把 SDK 直接塞进 `router-service`，结果会变成：

- 外层是我们的 graph runtime
- 内层又嵌一个 SDK run loop

这会导致“谁才是权威 orchestration 层”变得不清晰。

#### B. 我们的 agent 是远程 HTTP/SSE 服务，不是进程内 tool

上游 SDK 的默认抽象是：

- tool
- handoff
- agent as tool
- MCP

而当前项目的下游 agent 抽象是：

- 一个外部 URL
- 一个标准请求 payload
- 一条流式响应协议
- 一个取消接口

这一点在 [`backend/services/router-service/src/router_service/core/support/agent_client.py`](../backend/services/router-service/src/router_service/core/support/agent_client.py) 中体现得很明显。  
所以 SDK 的 `tool/handoff` 与我们当前的 `remote intent agent` **不是一一对应关系**。

#### C. 我们的 session 不只是 transcript

上游 `session` 主要存的是 conversation items。  
而当前 [`backend/services/router-service/src/router_service/core/graph/session_store.py`](../backend/services/router-service/src/router_service/core/graph/session_store.py) 管的是：

- messages
- tasks
- graph/session 生命周期
- 过期提升到 long-term memory

所以它不是一个“只存聊天历史”的 session，而是一个 **graph runtime state store**。

#### D. 官方文档明确提醒“每段会话尽量只选一种 state strategy”

官方 `Running agents` 文档明确说：

- 本地 replay history
- session
- `conversationId`
- `previousResponseId`

通常一段会话只选一种策略，不要混用。  
当前 `intent-router` 已经有自己本地权威状态，如果在 router 层再引入 SDK session 或 server-managed continuation，就容易出现 **双份上下文、重复历史、状态分叉**。

## 5. 适配度判断

| 场景 | 判断 | 原因 |
| --- | --- | --- |
| 用 OpenAI Agents SDK 替换 `router-service` 核心 | 不建议 | 现有 router 已经承担 orchestration、graph state、SSE、remote-agent dispatch；直接替换收益低、改动大 |
| 在单个业务 agent 服务内部使用 SDK | 建议 | 不破坏现有 router 对外协议，可以把 SDK 能力包在 agent 内部 |
| 借鉴 `guardrails + approvals` | 强烈建议 | 转账、还款、换汇类场景天然需要敏感动作前暂停和审批 |
| 借鉴 `tracing` 设计 | 建议 | 很适合补齐当前 router -> agent -> result 的端到端可观测链路 |
| 直接复用 `session` 实现 | 局部可用 | 可用于 agent 内部 transcript；不适合直接替换 graph session store |
| 直接引入 `sandbox` / `realtime` | 暂不建议 | 当前项目不是 coding agent、不是语音 agent，目标不匹配 |
| 直接把 `run_state.py` 接到 router 协议上 | 不建议 | 耦合了 SDK 自己的 item/tool/approval 数据模型，迁移成本高 |

## 6. 哪些地方对我们最有帮助

### 6.1 高风险业务动作的审批边界

这部分是我认为最值得吸收的点。

官方文档把“人审”定义成：

- 模型可以判断“需要做这个动作”
- 但真正执行前，run 要暂停
- 等审批后再继续

这和当前项目里的业务场景高度匹配，尤其是：

- 转账
- 信用卡还款
- 换汇
- 未来任何有真实资金变动或不可逆副作用的操作

对我们来说，最合理的落地方式不是把审批做在 router 大中枢里，而是：

- router 仍负责识别、建图、调度
- 具体业务 agent 内部用 SDK `needs_approval` 或 guardrail 机制来决定是否暂停
- 然后再通过我们现有的 agent 协议，把“需要确认/审批”的状态转换回 `waiting_confirmation` 或类似业务态

### 6.2 单个业务 agent 内部的 specialist 拆分

当前一些业务 agent，例如：

- [`backend/services/agents/transfer-money-agent/src/transfer_money_agent/service.py`](../backend/services/agents/transfer-money-agent/src/transfer_money_agent/service.py)
- [`backend/services/agents/forex-agent/src/forex_agent/service.py`](../backend/services/agents/forex-agent/src/forex_agent/service.py)

本质上还是“单服务里一大段 prompt + heuristic + slot merge 逻辑”。

这些 agent 的内部，其实很适合尝试用 SDK 做以下拆分：

- `triage/理解 agent`
- `slot extraction specialist`
- `policy/风险校验 specialist`
- `execution specialist`

但这个拆分应该发生在 **agent 服务内部**，不应该上推到 `router-service`。

也就是说：

- router 仍然只看到一个 `transfer-money-agent`
- 这个 agent 服务内部再决定是否 handoff 给自己的 specialist

这样做的好处是：

- 不破坏现有 router 对下游的 URL/协议抽象
- 可以渐进引入，而不是大迁移
- 容易做 A/B 或单 agent 试点

### 6.3 可观测性

当前项目已经有：

- session/task/graph 状态
- SSE 事件

但缺一条更像“工程诊断视角”的 trace。

上游 SDK 的 tracing 思路很适合借鉴成下面这条链：

- router message ingress
- intent recognition
- graph build / planner decision
- node dispatch
- remote agent stream
- waiting/approval
- final completion/failure

我不建议为了 tracing 把整个 SDK 引进 `router-service`。  
更合理的是：

- 参考它的 span 分类和 trace 组织方式
- 在我们自己的 runtime 上做一层轻量 trace adapter

### 6.4 函数工具 schema 生成

如果后续你们希望：

- 给某些业务 agent 增加“内部工具层”
- 或把内部工具做成统一 schema 暴露
- 或把若干策略函数封装成 tool catalog

那么上游的 `function_schema.py` 很值得参考。

它的价值不在“函数能不能转 JSON schema”，而在于它把这些事情做成了稳定工程能力：

- 读取签名
- 读取注释
- 生成严格 schema
- 处理上下文参数

这部分**可以借鉴，甚至可以局部改写后复用**。

## 7. 哪些代码模块可以考虑直接用

这里分三类说。

### 7.1 适合“作为依赖直接用”的模块

前提：放在 **某个业务 agent 服务** 里，不建议先放进 `router-service`。

可直接依赖的优先项：

- `Agent`
- `Runner`
- `function_tool`
- `handoff`
- `input_guardrail` / `output_guardrail`
- `SQLiteSession`

适合的试点方式：

- 在 `transfer-money-agent` 内部把“提槽、风险检查、执行确认”拆成 2-3 个 specialist
- 用 SDK 管内部 handoff / approvals
- 最终仍然输出当前项目既定的 `AgentExecutionResponse`

### 7.2 适合“参考实现或局部摘取”的模块

这些模块有价值，但不建议直接整块搬进 `router-service`：

- `src/agents/function_schema.py`
  - 很适合做内部 tool schema 生成器的参考实现。
- `src/agents/tracing/*`
  - 很适合拿来设计 router 的 trace model。
- `src/agents/memory/sqlite_session.py`
  - 适合参考 transcript persistence 的并发处理方式。
- `src/agents/handoffs/__init__.py`
  - 里面的 input filter / history filter 设计值得借鉴，尤其适合多 specialist 传递上下文时做“最小必要历史”控制。
- `src/agents/run_state.py`
  - 适合学习“如何把 interruption/resume 做成显式状态对象”，但不适合直接接入我们当前 runtime。

### 7.3 不建议直接引入的模块

这些模块太重、太耦合，或者目标不匹配：

- `src/agents/run_internal/*`
  - 属于 SDK 内核，和它自己的 item/tool/trace 模型深度耦合。
- `src/agents/tool.py`
  - 功能很全，但对当前项目来说抽象层级不对。
- `src/agents/sandbox/*`
  - 当前项目不是面向代码工作台或容器化操作型 agent。
- `src/agents/realtime/*`
  - 当前项目不是语音实时 agent。

## 8. 如果真要引入，兼容性和成本是什么

### 8.1 依赖层面的直接成本

上游 `openai-agents` 当前依赖要求包括：

- `openai>=2.26.0,<3`
- `pydantic>=2.12.2,<3`
- `griffelib>=2,<3`
- `requests>=2,<3`
- `websockets>=15,<16`
- `mcp>=1.19,<2`

而当前 [`backend/services/router-service/pyproject.toml`](../backend/services/router-service/pyproject.toml) 里是：

- `openai==2.30.0`
- `pydantic>=2.8,<3.0`

这意味着：

- `openai` 版本大概率没问题。
- **`pydantic` 版本下限需要上调到 `2.12.2+`**，这是最直接的兼容性改动。
- 还要额外引入 `griffelib`、`websockets`、`mcp` 等依赖。

我对这一点的判断是：

- 在单个业务 agent 里单独加依赖，成本可控。
- 在 `router-service` 核心直接加，会增加整条主链路的依赖面和升级压力。

### 8.2 模型接入层面的约束

上游 SDK 本身支持：

- OpenAI provider
- 自定义 `base_url`
- LiteLLM provider

所以它**理论上可以接 OpenAI-compatible endpoint**。  
但要注意一个边界：

- 如果只用基础 chat / function tool 模式，兼容空间较大。
- 如果想用 `Responses API`、`previous_response_id`、hosted tools、部分 tracing/registration 能力，就要求底层服务真的支持相应 OpenAI 语义。

这里我做一个明确判断：

- **在业务 agent 内部把它当“本地 agent runtime”使用，可行性高。**
- **在 router 核心里依赖它的完整 OpenAI runtime 语义，可行性不如前者高。**

这是基于源码和官方文档的推断，不是对你们当前模型网关能力的断言。

### 8.3 数据与合规风险

这是本次调研里一个必须明确写出来的点。

官方文档说明：

- tracing 在 server-side SDK 路径里默认开启
- trace 里可以包含 model call、tool call、handoff、guardrail、custom spans

如果把它直接用到金融类业务 agent：

- 用户输入
- 槽位值
- 审批动作
- 工具参数

都有可能进入 trace 面。

因此，生产引入前必须明确：

- tracing 是否默认关闭
- 哪些字段需要脱敏
- trace 是否允许发往 OpenAI tracing ingest
- 内部审计与隐私策略是否允许

对当前项目，我建议：

- 试点阶段默认关闭上游默认 trace export
- 先做本地或自建 trace sink
- 等字段脱敏策略明确后，再评估是否接官方 tracing

## 9. 推荐落地路径

### 9.1 第一阶段：只做单 agent 试点

推荐在一个业务 agent 内试点，不动 router 核心。

优先级建议：

1. `transfer-money-agent`
2. `credit-card-repayment-agent`
3. `forex-agent`

原因：

- 都有明显的高风险动作边界
- 都适合“理解 -> 校验 -> 执行确认”的 specialist 拆分
- 即使失败，也只影响一个下游 agent，不会冲击 router 主链路

### 9.2 第二阶段：保留现有协议，只替换 agent 内核

建议保持现有下游协议不变：

- router 发标准 payload
- agent 仍返回当前项目定义的 `waiting/completed/failed`

也就是说：

- router 无感
- SDK 只存在于 agent 内部

这是最稳妥的演进方式。

### 9.3 第三阶段：引入审批和轻量 tracing

当单 agent 试点跑通后，再引入：

- `needs_approval`
- input/output/tool guardrails
- 轻量 trace/span

不要一开始就同时引入：

- handoff
- MCP
- tracing export
- session persistence

这样很容易把试点复杂度拉高。

## 10. 最终判断

最终判断可以浓缩成三句话：

1. `openai-agents-python` **值得研究，也值得试点**。
2. 它对我们最合适的位置是：**业务 agent 内部 runtime**，不是 `router-service` 核心。
3. 最该吸收的是：**审批边界、可恢复状态、trace 设计、tool schema 工程化**，而不是把整个 SDK run loop 原样塞进现有 router。

如果只给一个执行建议，我会建议：

- **下一步做一个 `transfer-money-agent` 的最小试点版本**：
  - 内部使用 OpenAI Agents SDK
  - 外部仍然兼容现有 agent HTTP/SSE 协议
  - 先只启用 `guardrail + approval`
  - 不改 router 核心

## 11. 参考链接

### 官方文档

- Agents SDK 总览：<https://developers.openai.com/api/docs/guides/agents>
- Running agents：<https://developers.openai.com/api/docs/guides/agents/running-agents>
- Orchestration and handoffs：<https://developers.openai.com/api/docs/guides/agents/orchestration>
- Guardrails and human review：<https://developers.openai.com/api/docs/guides/agents/guardrails-approvals>
- Results and state：<https://developers.openai.com/api/docs/guides/agents/results>
- Integrations and observability：<https://developers.openai.com/api/docs/guides/agents/integrations-observability>

### 上游代码

- 仓库首页：<https://github.com/openai/openai-agents-python>
- `agent.py`：<https://github.com/openai/openai-agents-python/blob/main/src/agents/agent.py>
- `run.py`：<https://github.com/openai/openai-agents-python/blob/main/src/agents/run.py>
- `guardrail.py`：<https://github.com/openai/openai-agents-python/blob/main/src/agents/guardrail.py>
- `handoffs/__init__.py`：<https://github.com/openai/openai-agents-python/blob/main/src/agents/handoffs/__init__.py>
- `memory/sqlite_session.py`：<https://github.com/openai/openai-agents-python/blob/main/src/agents/memory/sqlite_session.py>
- `run_state.py`：<https://github.com/openai/openai-agents-python/blob/main/src/agents/run_state.py>
- `tracing/processors.py`：<https://github.com/openai/openai-agents-python/blob/main/src/agents/tracing/processors.py>
- `function_schema.py`：<https://github.com/openai/openai-agents-python/blob/main/src/agents/function_schema.py>
