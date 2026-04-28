# DeepAgent 与 router-service 对比调研

状态：完成  
调研日期：2026-04-24  
适用分支：`test/v3-concurrency-test`

## 1. 结论摘要

先给结论：

- `DeepAgent` 和 `router-service` 有交集，但**不是同一层的问题**。
- `DeepAgent` 更像一个**通用深度推理 agent 框架**，强调开放工具集、长链推理、工具搜索、记忆折叠和 RL 训练。
- `router-service` 更像一个**面向生产集成的意图路由与图运行时服务**，强调动态目录、显式 graph、session 状态、补槽、动作接口、SSE 输出和下游 agent 调度。
- 两者都不是传统的“纯分类器”或“纯 ReAct loop”，都试图把 LLM 放进一个更可控的执行体系里。
- 但如果要给本仓库找参考系，`DeepAgent` 最值得吸收的是**开放工具检索**和**长链上下文压缩**，而不是直接把 `router-service` 改造成“单流式自由推理 agent”。

一句话判断：

| 项目 | 一句话定位 |
| --- | --- |
| DeepAgent | 面向开放工具集和长链任务的通用推理 Agent 框架 |
| router-service | 面向业务意图治理和多轮执行的 Router-centric Graph Runtime |

## 2. 调研基线

本次对比使用的基线如下：

| 对象 | 版本/时间 | 主要依据 |
| --- | --- | --- |
| DeepAgent | GitHub README 可见更新时间到 2026-01-14；arXiv `2510.21618v3` 更新时间为 2026-02-05 | 官方 GitHub README、官方 arXiv 论文、源码入口 `src/run_deep_agent.py`、`src/tools/tool_manager.py`、`src/run_tool_search_server.py`、`config/base_config.yaml` |
| router-service | 本仓库 `test/v3-concurrency-test` 分支；核心 v3 文档更新时间主要为 2026-04-18，通信协议规范含 2026-04-22 更新 | 本地 `docs/v3/*`、`backend/services/router-service/README.md`、`api/dependencies.py`、`api/routes/sessions.py`、`core/graph/orchestrator.py`、`core/support/memory_store.py`、`settings.py` |

这里需要先强调一个边界：

- `DeepAgent` 是“一个 agent 框架 + 训练/评测体系”。
- `router-service` 是“一个对外提供 HTTP/SSE 协议的运行时服务”。

所以这次对比更适合回答“哪些能力可借鉴、哪些设计边界不同”，不适合简单回答“谁更先进”。

## 3. 总览对比表

| 维度 | DeepAgent | router-service | 判断 |
| --- | --- | --- | --- |
| 核心定位 | 通用深度推理 agent，面向开放工具集和长时任务 | 意图路由与 graph 运行时服务，面向业务 Agent 编排 | 不同层 |
| 主执行单元 | 单个任务上的连续推理流 + 工具搜索/调用 | Session + Business Object + Execution Graph + Node | `router-service` 更强状态化 |
| 控制中心 | 主推理模型在单一 reasoning process 中自主决定工具发现和调用 | Router 先理解、编图，再由确定性运行时推进状态并调度下游 Agent | 控制权分布不同 |
| 工具接入方式 | 开放/半开放工具集，支持检索 16,000+ APIs，带独立 retrieval server | 受控 intent catalog + field mapping + 下游 agent HTTP 协议 | 开放性 vs 治理性 |
| 记忆机制 | Autonomous Memory Folding，分 episodic/working/tool memory | 会话消息、shared slot memory、business digest、长期记忆 fact store | 两者都重视长链上下文，但抽象不同 |
| 训练方式 | 提供 ToolPO 端到端 RL 训练范式 | 以推理时编排为主，无训练闭环 | 差异显著 |
| 主要入口 | CLI 运行、benchmark/eval 驱动，可选 tool search server | FastAPI + REST + SSE + session/action 接口 | 产品形态不同 |
| 典型任务 | ToolBench、API-Bank、GAIA、HLE、ALFWorld、WebShop 等开放任务 | 转账、缴费、查余额、外汇等受控业务 Agent 场景 | 任务域不同 |
| 可观测性 | 偏实验与评测输出 | 快照、事件流、统一动作、错误包装 | `router-service` 更偏生产集成 |
| 替代关系 | 不能直接替代 router-service | 不能直接替代 DeepAgent | 更适合互补而非替换 |

## 4. 共同点表

| 共同点 | DeepAgent 体现 | router-service 体现 | 说明 |
| --- | --- | --- | --- |
| 都不是纯分类器 | 不只做 tool selection，还做连续推理、调用、回顾 | 不只做 intent recognition，还做 graph 编译、补槽、调度、续轮 | 两者都超出“识别器”范畴 |
| 都把 LLM 放进更大运行时 | LLM 负责 reasoning、工具发现、工具调用决策 | LLM 负责识别、层级路由、建图、turn interpretation、recommendation 决策 | 都不是“模型直接包打天下” |
| 都追求长链任务处理 | 通过 thought folding 管理长 horizon 任务 | 通过 session、business object、pending/waiting 状态管理多轮过程 | 都在解决上下文衰减和任务延续 |
| 都依赖外部能力 | API、Web、VQA、代码执行、环境动作 | 下游 intent agents、LLM provider、catalog repo | 都是 orchestration 系统 |
| 都在做结构化控制 | 用工具搜索、工具 schema、记忆结构约束 agent | 用 intent catalog、slot schema、graph state、action protocol 约束运行时 | 都偏“受控 agentic system” |
| 都强调多步骤执行 | 长链工具调用、多次工具搜索与调用 | 多节点 graph、pending graph、waiting node、confirm/cancel/replan | 都不是单轮完成模型 |
| 都可以被看作 UAR 风格 | README 明确强调 single coherent reasoning process | 本地文档明确将其定位为 Router-centric graph runtime，而非传统 ReAct | 设计哲学存在交集 |

## 5. 差异表

| 差异点 | DeepAgent | router-service | 对本仓库的意义 |
| --- | --- | --- | --- |
| 问题分层 | 解决“开放世界下如何让 agent 自主找工具并完成长链任务” | 解决“受控业务域内如何做意图治理、状态推进和 agent 编排” | 不是同类替换关系 |
| 执行范式 | 单一 reasoning stream 中动态决定搜索、调用、折叠记忆 | 先结构化理解，再编译为显式 graph，由运行时推进 | `router-service` 更可预测、更易治理 |
| 工具/能力发现 | 支持 open-set tool retrieval，可单独部署 `run_tool_search_server.py` | 依赖 catalog 中已注册 intent 与 agent 配置 | DeepAgent 更开放，router-service 更稳态 |
| 对象模型 | 任务中心，重点是推理轨迹、工具调用、记忆折叠 | session/business/workflow/graph/node/task 多层对象 | `router-service` 更适合多轮业务对账和回放 |
| 记忆设计 | episodic + working + tool memory，主动触发 folding | in-memory long-term fact store + shared slot memory + business digests | DeepAgent 的记忆抽象更“agent-native” |
| 训练闭环 | 有 ToolPO、LLM-simulated APIs、工具调用 credit assignment | 当前没有训练环，主要靠规则、配置、测试与运行时约束 | 可借鉴，但接入成本高 |
| 对外接口 | 主要是 CLI、配置文件、评测流程 | 标准 HTTP API、SSE、session/action 语义协议 | `router-service` 已是可集成服务 |
| 任务边界 | 一个 agent 直接拥有工具使用权 | Router 调度下游专用 agent，业务执行下沉 | 当前架构更适合金融/客服类受控业务 |
| 失败处理 | 更多依赖推理自修正、再搜索、再调用、折叠后重试 | 明确有 waiting/pending/cancel/confirm/replan/action 接口 | `router-service` 更适合人机协同 |
| 评估方式 | 基于公开 benchmark，强调跨任务泛化 | 基于本地测试矩阵和业务协议回归 | 指标体系不同，不能直接横比 |
| 部署依赖 | 主模型、辅助模型、VQA 模型、tool retriever、数据集/环境 | Router API、LLM provider、catalog、admin-service、downstream agents | DeepAgent 更像研究/平台系统，router-service 更像业务中台 |

## 6. 关键判断

### 6.1 DeepAgent 不是 router-service 的直接上位替代

原因很直接：

1. `router-service` 当前是一个有明确会话协议、SSE 协议、动作接口、状态快照的服务。
2. `DeepAgent` 当前更像一个以任务求解和 benchmark 评测为中心的通用 agent 框架。
3. 如果直接用 `DeepAgent` 风格替换当前 Router，会削弱目录治理、显式图状态、业务回放、协议稳定性这些现有优势。

### 6.2 DeepAgent 最值得吸收的不是“自由推理”，而是两类能力

| 可吸收点 | 为什么值得吸收 | 对 router-service 的可能落点 |
| --- | --- | --- |
| 开放工具检索 | 当前 Router 的能力发现主要依赖静态 intent catalog，扩展性偏受控 | 在 `catalog` 之外增加“候选工具/候选 agent 检索层”，用于新意图探索或 recommendation |
| 记忆折叠 | 当前长期记忆以 fact store 为主，偏轻量 | 在 `session` 或 `business digest` 层加入结构化摘要，将消息/槽位/执行结果压缩为更可复用的多层记忆 |
| 辅助模型分工 | DeepAgent 把主推理模型与辅助模型分开使用 | Router 已有 recognizer model 与主模型区分，可继续细化为“识别/补槽/摘要”专模 |
| 开放评测集思路 | DeepAgent 用 benchmark 衡量开放任务能力 | Router 可以新增针对多意图、补槽、重规划、等待态恢复的系统级评测集 |

### 6.3 当前 router-service 反而在生产可治理性上更成熟

这个判断主要基于以下事实：

- `router-service` 已经有统一的 session/action/SSE 协议。
- 运行时对象已经显式化为 `session + business object + graph + node`。
- 入口是 HTTP 服务，适合前端和上游系统稳定接入。
- 代码和文档里已经有较完整的行为开关、错误包装、测试矩阵和协议约束。

所以，`DeepAgent` 给本仓库的价值更像**能力增量参考**，不是**现有架构否定项**。

## 7. 建议结论

| 建议项 | 建议 |
| --- | --- |
| 是否把 router-service 改造成 DeepAgent 式单流推理框架 | 不建议 |
| 是否吸收开放工具搜索能力 | 建议分层吸收，但放在 Router 外围能力发现层，不要冲掉 intent catalog 主链 |
| 是否吸收记忆折叠能力 | 建议，尤其适合多轮长会话与 business digest 压缩 |
| 是否引入 RL 训练路线 | 暂不建议直接进入主线，成本高且与当前服务形态不匹配 |
| 是否把 DeepAgent 作为架构参照物 | 可以，但应作为“开放 agent 能力补充参照”，不是“整体替代模板” |

最终判断：

`DeepAgent` 更适合回答“如何把大模型做成一个能在开放工具世界里持续推理的 Agent”，`router-service` 更适合回答“如何把多意图业务请求稳定落到可治理、可观测、可集成的运行时服务里”。

两者最合理的关系不是二选一，而是：

- `router-service` 继续承担**受控编排与协议中台**；
- 未来有需要时，按受控边界引入 `DeepAgent` 式的**开放工具检索**和**结构化记忆折叠**能力。

## 8. 参考资料

### 8.1 DeepAgent 官方资料

1. GitHub：<https://github.com/RUC-NLPIR/DeepAgent>
2. Paper：<https://arxiv.org/abs/2510.21618>
3. 关键源码入口：
   - `src/run_deep_agent.py`
   - `src/tools/tool_manager.py`
   - `src/run_tool_search_server.py`
   - `config/base_config.yaml`

### 8.2 本仓库 router-service 依据

1. `backend/services/router-service/README.md`
2. `docs/v3/router-service-架构设计文档.md`
3. `docs/v3/router-service-功能说明文档.md`
4. `docs/v3/archive/router-service-LLM架构定位说明.md`
5. `docs/v3/router-service-通信协议规范.md`
6. `backend/services/router-service/src/router_service/api/dependencies.py`
7. `backend/services/router-service/src/router_service/api/routes/sessions.py`
8. `backend/services/router-service/src/router_service/core/graph/orchestrator.py`
9. `backend/services/router-service/src/router_service/core/support/memory_store.py`
10. `backend/services/router-service/src/router_service/settings.py`
