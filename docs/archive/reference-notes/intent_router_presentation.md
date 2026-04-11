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
  * 大模型直接闭环全量业务不可控，容易答非所问或产生幻觉。
  * 用户意图多样发散（一句包含多意图、中途改变主意、中途变更目标）。
  * 状态管理复杂：多轮对话中上下文极易污染（例如：查完余额的卡号被带入转账任务）。
* **Intent Router 的系统定位**：
  * **分离“路由”与“执行”**：作为大脑中枢，专职负责意图判定、任务编排、多意图规划卡片分发。
  * **不碰底层业务语义**：具体业务逻辑（如校验转账额度）交由下游具体的 Intent Agent 处理。
  * **统一展现与交互**：通过独立 Action 流和 SSE，为前端提供高度结构化的界面（按钮/表单/打字机效果）。

---

## Slide 3: 整体功能全景图与系统图
* **1. 动态意图路由**：支持从持久化存储准实时热更新意图定义，即插即用。
* **2. 意图识别引擎**：以大模型（LLM）语义识别为主，辅助 CJK 中文语法正则匹配进行极端降级兜底。
* **3. 多任务规划 (Session Planner)**：发觉多个诉求时，生成规划清单，用户打勾确认后再做。
* **4. 多轮对话流控 (Suspend, Cancel & Switch)**：
  * 精准感知 Agent 的补槽状态。支持无缝“中途切换意图”并一键中断级联任务。
* **5. 双轨流式通道**：自然语言聊天推送通道 + 独立的高稳态结构化事件流通道。

```mermaid
graph TD
    User([多端前端界面])
    subgraph Router Service
        API[Router API - 统一网关与SSE流]
        Orch[Orchestrator - 核心大脑]
        Recog[意图识别 LLM与降级规则]
        Ctx[上下文分层系统]
        Broker[事件与心跳分发]
    end
    subgraph 意图执行 Agents
        A1[转账 Agent]
        A2[余额 Agent]
        A3[高危审批 Agent]
    end

    User -->|接收消息或卡片点击| API
    API --> Orch
    Orch -->|提取特征| Ctx
    Orch -->|意图识别| Recog
    Orch -->|直播流调度| A1
    A1 -->|状态与卡片回传| Orch
    Orch -->|发送事件| Broker
    Broker -->|SSE 流式渲染| User
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

## Slide 6: 核心逻辑三：同意图下“换目标”与槽位防污染
* **痛点**：用户在转账补槽阶段改变主意：“啊不是转给张三，是转给李四，账号是 xxxx”。
* **Router 的防污染机制**：
  * 上下文识别：发现新的账号/手机号特征（正则提取）。
  * **无缝覆盖 (Override)**：当继续当前任务时，精准剔除工作记忆中原有的冲突槽位（如旧卡号），防止将“李四+旧卡号”拼接成错误指令。
  * 由 Agent 决定是否重新弹卡片让用户核对。

---

## Slide 7: 核心逻辑四：双态卡片体系与 Action 接口
* 针对复杂操作，避免大模型“自说自话”，提供 100% 确定的 GUI 交互。
* **Planner Card (Router 规划全局卡片)**：
  * **场景**：一次发现多个新意图（如“先查余额再打给张三500”）。
  * **行为**：后台将任务暂挂入等待区，前端展出计划清单。
* **Confirm Card (Agent 业务确认卡片)**：
  * **场景**：涉及金融转账、系统审批。
  * **行为**：进入核心操作前，前端渲染确认表单。
* **独立交互接口 (`/actions`)**：所有卡片按钮的点击脱离自然语言文本通道，通过高可靠后端 HTTP 验签和路由。

---

## Slide 8: 系统架构设计剖析
*(内部技术架构分解)*

```mermaid
graph TD
    subgraph 接入层 router_api
        接口路由
        SSE(SSE 事件流推送中心)
    end

    subgraph 核心引擎层 router_core
        Orchestrator((任务调度中心<br>Orchestrator))
        Queue[(优先级任务队列)]
        Context[上下文构建器]
        Recognizer[意图识别组件]
        Catalog[(意图字典缓存<br>无锁热更新)]
    end

    subgraph 下游基础设施
        LLM[大模型服务 API]
        AgentClient[请求调度客户端<br>连接池复用]
    end

    FastAPI -->|"接收消息/动作"| Orchestrator
    Orchestrator <-->|"构建记忆"| Context
    Orchestrator <-->|"发起识别"| Recognizer
    Recognizer -->|"大模型调用"| LLM
    Recognizer -->|"下发配置"| Catalog
    Orchestrator -->|"压入调度流"| Queue
    Queue -->|"流式调度分发"| AgentClient
    AgentClient -->|"转发流式事件"| SSE
```

---

## Slide 9: 核心执行流时序图

```mermaid
sequenceDiagram
    participant User as 用户
    participant API as Actions API
    participant Router as Router大脑
    participant Agent as 意图 Agent

    User->>Router: "先查余额，再转500块"
    Router->>Router: 执行意图识别，发现双诉求
    Router-->>User: (SSE) 展示多意图规划卡片 (Plan Card)
    
    User->>API: 独立接口: 点击"确认执行"
    API->>Router: 触发 Action Router
    
    Router->>Agent: 调度[余额Agent]
    Agent-->>Router: 返回金额
    
    Router->>Agent: 调度[转账Agent]
    Agent-->>Router: 返回待确认表单 (waiting_confirmation)
    Router-->>User: 画面弹出转账核对按钮
    
    User->>API: 独立接口: 点击"确认"
    API->>Router: Action透传
    Router->>Agent: 放行交易
```

---

## Slide 10: 生产级可靠性保障 (Production Resilience)
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

## Slide 11: 演进路线规划 (Roadmap)
* **Phase 1 (MVP 可用层) [进行中]**：
  * 完善 Waiting 态补槽/切换的判定机制、实现超时与资源安全兜底。
* **Phase 2 (上下文闭环层)**：
  * 深入落地四层上下文边界体系（Work Memory vs Summary），增加闭环原因追踪。
* **Phase 3 (全局规划层)**：
  * 实现完整的 Router 规划卡片 `session.plan.proposed` 及对应的队列暂停控制引擎。
* **Phase 4 (核心业务防线)**：
  * 落地 Agent 二次确认业务卡片，完成完整的 `/actions` 路由中转机制，解决金融级严谨性问题。

---

## Slide 12: 总结 (Summary)
* **Intent Router** = 大脑层执行引擎 + 胶水层状态机。
* 通过极强的边界设计（状态管理、上下文分层、明确的切流规则），将发散不可控的 LLM 多轮聊天，转化为了确定性的 API 工作流调度过程。
* **价值**：极大降低各个意图智能体（Agent）的开发复杂度，专注垂类业务语义理解即可。

---
## Slide 13: Q&A
* 感谢聆听！
* Open Questions / 自由讨论
