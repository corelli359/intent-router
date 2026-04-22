# Router Service 用户旅程文档 v0.2

状态：设计对齐稿  
更新时间：2026-04-19  
适用分支：`test/v3-concurrency-test`

## 1. 文档目标

本文档用旅程和泳道方式呈现 Router v0.2 的关键体验路径，避免只停留在文字描述。

## 2. 旅程总览

```mermaid
journey
    title Router v0.2 关键旅程总览
    section 进入会话
      创建或恢复 session: 5: 用户,前端,Router
      预热最近 20 条长期记忆: 4: Router,Memory
    section 当前轮处理
      识别意图并决定 graph: 5: Router
      有槽位则补槽: 4: Router,Memory
      无槽位则直接路由: 5: Router
    section 多业务
      当前业务可被挂起: 4: Router
      新业务可插入执行: 4: 用户,Router
      旧业务可恢复: 4: Router
    section 收口
      business 摘要写入短期记忆: 5: Router,Memory
      session 过期 dump 到长期记忆: 5: Router,Memory
```

## 3. 旅程一：新 Session 启动并复用长期记忆

### 3.1 泳道图

```mermaid
flowchart LR
    subgraph User["用户"]
        u1["发起新会话"]
        u2["发送第一条消息"]
    end

    subgraph Frontend["前端"]
        f1["创建 session"]
        f2["发送 message"]
    end

    subgraph Router["Router"]
        r1["创建 GraphSessionState"]
        r2["向 memory sidecar 请求 warmup"]
        r3["构建短期工作集"]
        r4["开始识别/编图"]
    end

    subgraph Memory["Memory"]
        m1["召回最近 20 条长期记忆"]
        m2["初始化 session 短期记忆"]
    end

    u1 --> f1 --> r1 --> r2 --> m1 --> m2 --> r3
    u2 --> f2 --> r4
    r3 --> r4
```

### 3.2 关键体验点

1. 用户无需重新输入稳定事实。
2. 历史记忆只做辅助，不应覆盖当前明确表达。

## 4. 旅程二：有槽位意图，先补槽再执行

### 4.1 时序图

```mermaid
sequenceDiagram
    participant U as User
    participant R as Router
    participant M as Memory
    participant A as Agent

    U->>R: 我想给王芳转账
    R->>M: 读取短期公共槽位
    R->>R: 识别 AG_TRANS + 缺少 amount
    R-->>U: 请提供金额
    U->>R: 500 元
    R->>M: 读取共享槽位 + 更新 amount
    R->>A: 派发转账任务
    A-->>R: 完成
    R->>M: 写入业务摘要和共享槽位
```

### 4.2 旅程节点

| 阶段 | 用户感知 | Router 动作 |
| --- | --- | --- |
| 首轮识别 | 识别到要转账 | 建图并判断缺槽 |
| 补槽追问 | 请求补充金额 | waiting node |
| 二轮补槽 | 补齐金额 | 历史槽位合并 |
| 派发执行 | 进入执行 | 创建 task 并 dispatch |
| 收口沉淀 | 结果可复用 | handover 写短期记忆 |

## 5. 旅程三：无槽位意图直接路由

### 5.1 最短路径图

```mermaid
flowchart LR
    msg["用户消息"] --> recognize["识别出 intent"]
    recognize --> noslot{"slot_schema 为空?"}
    noslot -->|是| direct["直接创建 task"]
    direct --> dispatch["派发到 Agent / READY_FOR_DISPATCH"]
    noslot -->|否| slotfill["进入补槽链"]
```

### 5.2 关键要求

1. 用户不应感知额外补槽等待。
2. 响应时间应显著短于有槽位路径。

## 6. 旅程四：穿插意图与恢复

### 6.1 泳道图

```mermaid
sequenceDiagram
    participant U as User
    participant R as Router
    participant M as Memory
    participant A as Agent

    U->>R: 帮我查余额
    R-->>U: 请提供卡号
    U->>R: 先帮我交燃气费
    R->>R: suspend 当前查余额业务
    R->>R: 创建燃气费 business
    R-->>U: 请提供燃气户号
    U->>R: 户号 88001234，金额 88
    R->>A: 派发燃气费任务
    A-->>R: 完成
    R->>M: 记录业务摘要
    R->>R: restore 最新挂起业务
    R-->>U: 已回到查余额流程，请提供卡号
```

### 6.2 关键体验点

1. 当前业务状态不能丢。
2. 新业务结束后能回到原业务。
3. session 内业务切换要明确。

## 7. 旅程五：业务结束后的记忆闭环

### 7.1 记忆闭环图

```mermaid
flowchart TD
    biz["业务结束 / handover"] --> digest["生成业务摘要"]
    digest --> shared["合并公共槽位"]
    shared --> stm["写入短期记忆"]
    stm --> expire{"session 过期?"}
    expire -->|否| reuse["后续补槽继续复用"]
    expire -->|是| dump["dump 到长期记忆"]
    dump --> warm["下次新 session warmup"]
```

### 7.2 关键体验点

1. 用户已提供过的信息在后续能继续用。
2. Router 释放 live graph/task，但不丢可复用事实。

## 8. 旅程六：Session 过期与重新进入

### 8.1 时序图

```mermaid
sequenceDiagram
    participant Cleanup as Cleanup Loop
    participant R as Router
    participant M as Memory
    participant L as Long-term Memory
    participant U as User

    Cleanup->>R: session expired
    R->>M: dump_session
    M->>L: persist memories
    L-->>M: ack
    M-->>R: ack
    Note over R: 删除本地 session
    U->>R: 新开 session
    R->>M: warmup_session(limit=20)
    M->>L: recall
    L-->>M: recent memories
    M-->>R: warmup payload
```

## 9. 旅程七：多进程场景下的 session 绑定

### 9.1 平台旅程图

```mermaid
flowchart LR
    req1["session_id=A 的请求 1"] --> ingress["Ingress hash(session_id)"]
    req2["session_id=A 的请求 2"] --> ingress
    req3["session_id=B 的请求 1"] --> ingress

    ingress --> podA["Pod A"]
    ingress --> podB["Pod B"]

    req1 -. same hash .-> podA
    req2 -. same hash .-> podA
    req3 -. different hash .-> podB
```

### 9.2 关键平台要求

1. 同一 session 尽量落同一 Pod。
2. session lock 只在 sticky 成立时才可靠。
3. Sidecar 记忆必须支持 Pod 重建后的恢复。

## 10. 失败旅程

### 10.1 无法识别

```mermaid
flowchart LR
    msg["用户消息"] --> recognize["识别"]
    recognize --> nomatch["no-match"]
    nomatch --> explicit["显式返回未识别/待澄清"]
```

要求：

1. 不允许 regex 偷偷猜一个意图。
2. 不允许默认猜槽位值。

### 10.2 达到 session 上限

```mermaid
flowchart LR
    newbiz["新业务进入"] --> limit{"business/task > 5?"}
    limit -->|否| keep["正常进入"]
    limit -->|是| trim["裁剪最老已完成/挂起对象"]
    trim --> keep
```

要求：

1. 当前焦点业务不能被自动裁掉。
2. 需要在日志和诊断里可见。

## 11. 旅程与测试映射

| 旅程 | 对应用例方向 |
| --- | --- |
| 新 session warmup | 长期记忆召回 20 条 |
| 有槽位补槽 | shared slot/history slot 复用 |
| 无槽位直达 | no-slot direct dispatch |
| 穿插意图恢复 | suspend + restore |
| 业务 handover | digest + shared slot persist |
| session 过期 | purge + dump |
| 多进程绑定 | sticky session 设计验证 |
