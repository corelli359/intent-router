# 意图路由服务 (Intent Router) 介绍与架构设计

> 本文档是一份为功能汇报/技术分享准备的 PPT 结构框架，结合了 `router_api` 和 `router_core` 的代码落地现状，以及《意图设计》文档中的最新规划。每张幻灯片（Slide）的内容都已提炼为核心要点，您可以直接提取填入演示文稿中。

---

## Slide 1: 封面
* **主标题**：智能意图路由服务 (Intent Router) 架构与功能演进
* **副标题**：构建高可靠、可伸缩的 AI 意图分发与多轮任务编排中枢
* **演讲者**：Corelli
* **部门/团队**：[您的团队名称]

---

## Slide 2: 核心挑战与定位
* **面临的挑战**：
  * 大模型直接闭环全量业务不可控，缺乏原子业务的确定性。
  * 用户意图多样发散（单次发问包含多意图、中途改变主意图）。
  * 状态管理复杂：多轮对话中上下文极易污染（例如：查完余额的卡号被带入转账任务）。
* **Intent Router 的系统定位**：
  * **分离“路由”与“执行”**：作为大脑中枢，专职负责意图判定、任务编排、上下文生命周期管理。
  * **不碰底层业务语义**：业务要素（如转账给谁）交由下游具体的 Intent Agent 处理。
  * **统一展现层**：作为统一的流式出口（SSE），对前端屏蔽后端多个 Agent 的调用复杂性。

---

## Slide 3: 整体功能全景图与系统图
* **1. 动态意图路由**：支持从持久化存储（Intent Catalog）准实时热更新意图定义，即插即用。
* **2. 混合意图识别**：提供 LLM 语义识别兜底至简单关键字。
* **3. 串行与流式编排**：内置任务队列，实时推送动作。

```mermaid
graph TD
    User([多端前端界面])
    subgraph Router Service
        API[Router API - 统一网关 & SSE流]
        Orch[Orchestrator - 核心大脑]
        Recog[双引擎 Recognizer]
        Ctx[上下文分层系统]
        Broker[事件与心跳分发]
    end
    subgraph Downstream Agents
        A1[转账 Agent]
        A2[余额 Agent]
        A3[高危审批 Agent]
    end

    User -->|"1. 消息/卡片点击"| API
    API --> Orch
    Orch -->|"2. 提取特征"| Ctx
    Orch -->|"3. 识别"| Recog
    Orch -->|"4. 直播流调度"| A1
    A1 -->|"5. 状态与卡片回传"| Orch
    Orch -->|"6. 发送"| Broker
    Broker -->|"7. SSE 流式渲染"| User
```

---

## Slide 4: 核心逻辑一：多轮边界与上下文分层设计
* **痛点**：传统的“大上下文窗口”会导致旧任务残渣污染新任务。
* **解决方案：上下文严格分层 (Context Stratification)**

```mermaid
flowchart TD
    Input([新一轮用户输入])
    
    subgraph 上下文层级 
        L1[<b>Active Task Working Memory</b><br>当前任务的槽位、交互模式]
        L2[<b>Closed Task Summary</b><br>已关停任务的结果、关闭原因]
        L3[<b>Session Transcript</b><br>会话全局消息流]
        L4[<b>Long-Term Memory</b><br>长期记忆提取库]
    end

    Input --> L1
    L1 ~~~ L2
    L2 ~~~ L3
    L3 ~~~ L4
    
    style L1 fill:#d4edda,stroke:#28a745,stroke-width:2px
    style L2 fill:#f8d7da,stroke:#dc3545,stroke-width:1px
```
* **核心动作**：任务的生命周期强标记（如 `closure_reason: completed | switched_intent`）。

---

## Slide 5: 核心逻辑二：Waiting 态下意图切换的决策树 (Decision Tree)
* 当用户处于等待补充信息 (Waiting) 态时，新的一句话到底表示什么？

```mermaid
graph TD
    Input([用户输入补槽数据]) --> A{"用户是否明确<br>说「取消/算了」?"}
    A -- 是 --> Action1[Cancel</b> 结束当前任务]
    A -- 否 --> B{"是否是在补充<br>当前缺失槽位?"}
    B -- 强相关 --> Action2[Continue 派发给当前Agent]
    B -- 弱/无关联 --> C{"是否提出了明确<br>且强烈的新意图?"}
    C -- 否 --> Action2
    C -- 是 --> Action3[<b>Switch</b> 关闭当前任务<br>生成新意图任务]
    
    style Action1 fill:#ffccd5
    style Action2 fill:#c2f0c2
    style Action3 fill:#ffe6cc
```
* 核心：**默认优先继续，明确证据才切流。**

---

## Slide 6: 核心逻辑三：双态卡片体系 (Router vs Agent Cards)
* 针对复杂与高危操作，避免纯文本黑盒，引入双态结构化卡片与独立 Action 接口。
* **Planner Card (Router 规划确认卡片)**：
  * **场景**：一次识别出多个意图（如“先查余额再打给张三500”）。
  * **行为**：Router 截停执行流，发送规划清单。用户确认后开始按队列调度，随时同步子任务进度。
* **Confirm Card (Agent 业务确认卡片)**：
  * **场景**：涉及转账等高危动作，或“同意图内目标对象突变”。
  * **行为**：Agent 中断识别返回卡片，前端展示结构化表单等待用户按键授权（而非普通文本回复），Router 做 Action 路由透传。

---

## Slide 7: 系统架构设计剖析
*(内部技术架构分解)*

```mermaid
graph TD
    subgraph `router_api`
        FastAPI(FastAPI HTTP / Action)
        SSE(SSE Event Broker)
    end

    subgraph `router_core` 
        Orchestrator((Task<br>Orchestrator))
        Queue[(Priority Task Queue)]
        Context[Context Builder]
        Recognizer[Intent Recognizer<br>LLM / KeyWord]
        Catalog[(CoW Intent Catalog<br>Lock-Free)]
    end

    subgraph 下游基础设施
        LLM[大模型 API]
        AgentClient[共享 HTTPX 连接池<br>Streaming Call]
    end

    FastAPI --> Orchestrator
    Orchestrator <--> Context
    Orchestrator <--> Recognizer
    Recognizer --> LLM
    Recognizer --> Catalog
    Orchestrator --> Queue
    Queue --> AgentClient
    AgentClient --> SSE
```

---

## Slide 8: 核心执行流时序图

```mermaid
sequenceDiagram
    participant User as 用户 (Client)
    participant Router as Router大脑
    participant Agent as 意图 Agent

    User->>Router: "先查余额，再转500块"
    activate Router
    Router->>Router: 双引擎意图识别，生成任务队列
    Router-->>User: (SSE) [事件] 规划卡片弹窗 (Plan Card)
    User->>Router: "点击确认执行"
    
    Router->>Agent: 发起流式调度 (查余额)
    loop [余额查询] Agent 迭代返回
        Agent-->>Router: Chunk 数据 (status=running)
        Router-->>User: (SSE) 持续渲染 AI 气泡
    end
    Agent-->>Router: status=completed
    
    Router->>Agent: 调度下一个任务 (转账)
    Agent-->>Router: 发现缺乏核心要素卡号
    Agent-->>Router: 返回 `waiting_user_input` 状态
    Router-->>User: 画面暂停，等待用户输入...
    deactivate Router
```

---

## Slide 9: 生产级可靠性保障 (Production Resilience)
* **无状态路由扩容设计**：
  * 配合 K8s Ingress 开启 Sticky Session (Cookie Affinity) 实现单会话 Pod 极速亲和绑定。
  * 规避跨 Pod 消息投递黑洞，随时可水平扩容。
* **资源保护机制**：
  * Agent 级联超时与熔断（Timeout API & Circuit Breaker）。
  * `EventBroker` 心跳续约（Heartbeat）+ Idle 超时关停机制防内存泄漏。
* **并发控制与安全**：
  * Intent Catalog 的后台自动异步更新。
  * 后台任务隔离报错不传染主线程响应。

---

## Slide 10: 演进路线规划 (Roadmap)
* **Phase 1 (MVP 可用层) [进行中]**：
  * 完善 Waiting 态补槽/切换的判定机制、实现超时与资源安全兜底。
* **Phase 2 (上下文闭环层)**：
  * 深入落地四层上下文边界体系（Work Memory vs Summary），增加闭环原因追踪。
* **Phase 3 (全局规划层)**：
  * 实现完整的 Router 规划卡片 `session.plan.proposed` 及对应的队列暂停控制引擎。
* **Phase 4 (核心业务防线)**：
  * 落地 Agent 二次确认业务卡片，完成完整的 `/actions` 路由中转机制，解决金融级严谨性问题。

---

## Slide 11: 总结 (Summary)
* **Intent Router** = 大脑层执行引擎 + 胶水层状态机。
* 通过极强的边界设计（状态管理、上下文分层、明确的切流规则），将发散不可控的 LLM 多轮聊天，转化为了确定性的 API 工作流调度过程。
* **价值**：极大降低各个意图智能体（Agent）的开发复杂度，专注垂类业务语义理解即可。

---
## Slide 12: Q&A
* 感谢聆听！
* Open Questions / 自由讨论
