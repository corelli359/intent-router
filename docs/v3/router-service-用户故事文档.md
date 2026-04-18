# Router Service 用户故事文档

状态：对齐草案  
更新时间：2026-04-18  
适用分支：`test/v3-concurrency-test`

## 1. 文档目标

本文档从不同角色视角描述 Router Service 的使用目标、关键故事和验收标准，帮助需求、产品、前端、后端和架构团队对“这个服务到底要帮谁解决什么问题”建立共同理解。

## 2. 角色画像

### 2.1 平台运营方

特征：

1. 关心意图是否能被注册、启停、治理。
2. 不希望每加一个意图就改 Router 代码。

### 2.2 前端 / 集成调用方

特征：

1. 负责把用户输入接入 Router。
2. 需要基于 snapshot 和 SSE 驱动页面和交互。

### 2.3 终端用户

特征：

1. 用自然语言表达诉求。
2. 不关心系统内部有多少 Agent、多少 graph。
3. 只关心“你懂没懂、什么时候问我、能不能继续、有没有完成”。

### 2.4 架构 / 运维 / 测试方

特征：

1. 关心是否可压测、可观测、可定位问题。
2. 关心是否存在清晰边界和稳定状态模型。

### 2.5 上游推荐提供方

特征：

1. 负责向 Router 传入 recommendation context 或 proactive recommendation。
2. 希望推荐信息能辅助理解，但不会无条件覆盖用户真实输入。

### 2.6 意图 / Agent 维护方

特征：

1. 负责定义 request schema、field mapping、slot schema。
2. 关心 Router 和 Agent 之间的责任边界是否稳定。

## 3. 史诗一：意图目录治理

### 用户故事 3.1

作为平台运营方，我希望能注册一个新的意图及其 Agent 地址，以便 Router 能在不改业务代码的情况下识别和调度这个能力。

验收标准：

1. 可配置 `intent_code`、`description`、`examples`、`agent_url`。
2. 可配置 `field_mapping`、`request_schema`、`slot_schema`、`graph_build_hints`。
3. Router 刷新目录后即可识别该意图。

### 用户故事 3.2

作为平台运营方，我希望能停用或启用某个意图，以便控制线上可路由能力。

验收标准：

1. inactive intent 不进入 recognition active list。
2. Router 只消费 active intent。

## 4. 史诗二：单轮单意图理解

### 用户故事 4.1

作为终端用户，我希望一句简单请求可以被直接识别并执行，而不是每次都先确认图。

验收标准：

1. 单意图简单请求可直接编译成单节点图。
2. 若图和槽位都满足执行条件，可直接进入执行。

### 用户故事 4.2

作为前端调用方，我希望收到当前识别到的主意图和候选意图，以便在必要时展示解释或调试信息。

验收标准：

1. snapshot 中有 `candidate_intents`。
2. primary intents 能进入 graph 编译。

## 5. 史诗三：多意图理解与图确认

### 用户故事 5.1

作为终端用户，当我一句话提出多个事项时，我希望系统先告诉我它理解成了哪些事项，再决定是否执行。

验收标准：

1. 多意图场景可形成 `pending_graph`。
2. 用户可通过 `confirm_graph` / `cancel_graph` 做明确决策。

### 用户故事 5.2

作为前端调用方，我希望能区分“当前是待确认图”还是“当前是等待补槽节点”，以便渲染不同的交互。

验收标准：

1. session snapshot 中同时暴露 `current_graph` 和 `pending_graph`。
2. graph / session 事件语义一致。

## 6. 史诗四：Router 侧补槽与追问

### 用户故事 6.1

作为终端用户，当我的请求信息不完整时，我希望系统准确告诉我缺什么，而不是直接报错。

验收标准：

1. Router 在 agent 前完成基础槽位提取。
2. 当缺必填槽位时，节点进入 `waiting_user_input`。
3. assistant message 能明确提示缺失字段。

### 用户故事 6.2

作为架构设计方，我希望槽位提取发生在 Router 里，而不是完全由 Agent 首次兜底追问，以便跨意图链路保持统一。

验收标准：

1. 节点分发前存在 Router 级 `UnderstandingValidator`。
2. 无法 dispatch 时不会创建或运行下游业务 task。

### 用户故事 6.3

作为意图 / Agent 维护方，我希望 Router 只负责“可执行门”之前的槽位准备，而 Agent 仍负责最终业务校验，以便契约边界稳定且职责不过载。

验收标准：

1. Router 可以基于 `slot_schema`、`field_mapping`、`request_schema` 组织标准化请求。
2. Agent 仍保留防守性校验和业务语义最终解释责任。
3. 文档和运行时状态语义不把 Router 表述成业务执行器。

## 7. 史诗五：多轮续轮与恢复

### 用户故事 7.1

作为终端用户，当系统刚问过我“请补充金额”时，我下一句“300 元”应该默认被理解为补当前事项，而不是重新开始识别新的意图。

验收标准：

1. waiting node 存在时，下一轮先进入 waiting node 解释链路。
2. 支持 `resume_current`。

### 用户故事 7.2

作为终端用户，我希望在待确认图阶段可以说“确认”“取消”或提出新需求，系统按上下文正确处理。

验收标准：

1. pending graph 存在时，下一轮先进入 pending graph 解释链路。
2. 支持 confirm / cancel / replan。

### 用户故事 7.3

作为终端用户，当我在当前事项未完成时突然插入一个新事项，我希望系统能挂起当前业务，再去处理新业务，之后还能恢复。

验收标准：

1. Session 允许多个 business object。
2. 当前业务可被 suspend。
3. 新业务成为 focus business。
4. 完成后允许恢复最近挂起业务。

当前实现说明：

1. 运行时结构已支持 suspend / restore。
2. 真实业务场景下的“同意图穿插/恢复”仍是待优化能力。

## 8. 史诗六：动作控制

### 用户故事 8.1

作为终端用户，我希望能取消当前等待中的事项，而不是被系统卡住。

验收标准：

1. 支持 `cancel_node`。
2. 若下游 Agent 支持 cancel，Router 会尝试协同取消。

### 用户故事 8.2

作为前端调用方，我希望 graph 级确认和 node 级取消都走统一动作接口，而不是需要多套协议。

验收标准：

1. confirm / cancel graph / cancel node 统一走 `/actions`。

## 9. 史诗七：Recommendation 与 Guided Selection

### 用户故事 9.1

作为前端调用方，我希望能把前端推荐上下文告诉 Router，但不强行替代用户选择，以便识别时更贴近当前场景。

验收标准：

1. 支持 `recommendationContext`。
2. 该上下文只作为 routing 辅助，不直接变成执行节点。

### 用户故事 9.2

作为前端调用方，我希望当用户已经明确选中了哪些事项时，Router 能直接据此建图，而不是再跑一次自由识别。

验收标准：

1. 支持 `guidedSelection`。
2. guided selection 直接走 deterministic graph path。

### 用户故事 9.3

作为产品方，我希望主动推荐事项既可以直接执行，也可以变成待确认的交互式执行图。

验收标准：

1. 支持 `proactiveRecommendation`。
2. 支持 `no_selection` / `direct_execute` / `interactive_graph` / `switch_to_free_dialog` 四类 route mode。

### 用户故事 9.4

作为上游推荐提供方，我希望 recommendation context 可以影响 Router 理解，但不能在用户未确认时直接篡改当前真实意图，以便推荐系统与会话系统边界清晰。

验收标准：

1. `recommendationContext` 作为 routing 辅助信息进入上下文。
2. `guidedSelection` 与 `proactiveRecommendation` 才能直接形成确定性 graph。
3. recommendation 默认值注入必须受 schema 配置控制，而不是无条件覆盖用户输入。

## 10. 史诗八：Router-Only 与调试

### 用户故事 10.1

作为测试或集成方，我希望可以在不触发真实业务 Agent 的情况下验证 Router 的理解、建图和补槽结果。

验收标准：

1. 支持 `executionMode=router_only`。
2. Router 会正常形成 graph 和 slot 结果。
3. 节点最终停在 `READY_FOR_DISPATCH`，而不是调用 Agent。

### 用户故事 10.2

作为性能测试方，我希望 Router-only 是真实运行时的一部分，而不是另一套分析旁路，以便压测数据更接近生产路径。

验收标准：

1. `router_only` 复用同一 session / graph / slot / event 运行时。
2. 不是另外一套 analyze-only 接口。

## 11. 史诗九：观测与错误治理

### 用户故事 11.1

作为前端调用方，我希望所有错误都有统一格式，而不是 FastAPI 默认错误、业务错误、内部错误混杂。

验收标准：

1. API 错误有统一 envelope。
2. 关键状态失败能返回明确 code 和 message。

### 用户故事 11.2

作为研发和测试方，我希望从 snapshot、events 和 diagnostics 就能大致判断问题发生在哪一层，而不总是进日志排查。

验收标准：

1. diagnostics 进入 snapshot。
2. graph/node/session 都有事件。

## 12. 史诗十：运行安全

### 用户故事 12.1

作为运维方，我希望异常图不会因为状态不收敛而无限循环占用资源。

验收标准：

1. graph drain 有最大迭代保护。
2. 超限后 graph 进入失败状态并对外可见。

### 用户故事 12.2

作为运维方，我希望长时间不用的 session 能自动回收，而不是一直堆在内存里。

验收标准：

1. session store 支持 purge expired。
2. 应用生命周期里有后台 cleanup loop。

## 13. 当前故事状态判定

### 已基本实现

1. 动态目录读取
2. 单意图识别与执行
3. 多意图 pending graph
4. Router 侧补槽与 waiting node
5. guided selection
6. proactive recommendation
7. router_only
8. 统一动作接口
9. diagnostics 和统一错误包装
10. session cleanup 和 drain guard

### 已有基础但待加强

1. suspend / resume 业务对象恢复
2. waiting decision 的产品规则收紧
3. structured output 严格化
4. 长期记忆结构化

### 仍属真实缺口

1. 同意图穿插/恢复在真实业务上的稳定闭环
2. 条件治理的专项能力

## 14. 结论

从用户故事视角看，Router Service 的核心价值不在“识别意图”本身，而在于：

1. 帮用户和系统把当前事项说清楚。
2. 在多轮、多事项、待确认、待补槽场景下保持一致的运行时行为。
3. 让前端、Agent、运营、测试围绕一套统一契约协作。
