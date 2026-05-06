# Skill 分层加载技术需求 v0.1

状态：技术需求草案  
更新时间：2026-05-06  
适用范围：Router V4 / Skill Runtime 设计  

## 1. 目标

Skill Runtime 需要支持可控、可审计、渐进式的分层加载机制。

核心目标：

1. 每轮请求只默认加载最小全局规则。
2. 只有业务场景入口 Skill 可以被用户输入通过 `description` 命中。
3. 第三层及更深层 Skill / Reference 不参与用户输入匹配，只能被已加载的上层 Skill 显式引用后加载。
4. Loader 必须能证明“为什么加载了某份内容”，避免模型或 runtime 因全量扫描、隐式检索、语义名称泄漏导致不可控行为。

一句话定义：

> Runtime 只自动加载 `agent.md` 和被入口描述命中的业务场景 Skill；其余知识、规则、子流程只能沿显式引用链逐层进入上下文。

## 2. 分层模型

### 2.1 L1：默认 Agent 层

文件：

```text
agent.md
```

加载规则：

1. 每个 session / request 默认加载。
2. 不需要用户输入命中。
3. 是所有 Skill Runtime 的全局行为边界。

职责：

1. 定义 Router / Skill Runtime 的角色边界。
2. 定义加载协议、权限边界和禁止行为。
3. 定义如何使用 L2 业务场景 Skill。
4. 定义上下文裁剪、冲突处理、审计要求。
5. 不写具体业务流程细节。

禁止：

1. 不承载具体业务场景执行步骤。
2. 不包含大量业务知识库内容。
3. 不写能替代 L2 Skill 匹配的业务关键词规则。

### 2.2 L2：业务场景入口 Skill 层

文件示例：

```text
skills/transfer-money/SKILL.md
skills/account-balance/SKILL.md
skills/pay-gas-bill/SKILL.md
```

加载规则：

1. L2 是唯一允许通过用户输入命中 `description` 渐进式加载的业务层。
2. Runtime 初始只读取 L2 Skill 的 index metadata，不读取完整正文。
3. 用户输入、页面上下文、推荐上下文只和 L2 的 `description` / matcher metadata 做候选召回。
4. 只有命中的 L2 Skill 才读取完整 Skill 文本。

职责：

1. 声明业务场景是什么。
2. 声明适用边界、反例、输入要求和输出契约。
3. 声明需要显式加载的 L3+ 子 Skill 或 Reference。
4. 声明执行 Agent / Tool / API capability contract。
5. 声明业务场景内的多轮策略和必要 guardrail。

必须包含：

```yaml
id: transfer_money
layer: entry
kind: skill
description: 用户表达转账、汇款、给某人转钱等资金划转诉求时使用
```

可以包含：

```yaml
loads:
  skills:
    - ./internal/slot-policy.md
    - ./internal/confirmation-flow.md
  references:
    - ./references/transfer-limits.md
    - ./references/risk-rules.md
```

禁止：

1. 不允许通过 L2 Skill 偷偷全量加载同目录下所有文件。
2. 不允许用模糊 glob 引用，例如 `./references/*.md`。
3. 不允许引用仓库外路径。
4. 不允许引用未声明 layer / kind 的文件。

### 2.3 L3+：内部 Skill / Reference 层

文件示例：

```text
skills/transfer-money/internal/step-001.md
skills/transfer-money/internal/step-002.md
skills/transfer-money/references/ref-001.md
```

加载规则：

1. 不参与用户输入匹配。
2. 不进入全局 Skill index。
3. 只能由已经加载的上层 Skill 显式引用。
4. 可以继续引用更深层文件，但必须遵守同样的显式引用规则。

命名要求：

1. 不写 `description`。
2. 不写有业务触发含义的 `name`。
3. 文件名和 `id` 应尽量使用稳定但不承载业务意图的名称，例如 `step-001`、`policy-001`、`ref-001`。
4. 可以写 `title` 供人工审计，但 `title` 不进入匹配索引。

职责：

1. 内部 Skill：承载子流程、局部策略、复杂步骤。
2. Reference：承载事实、规则、字段说明、接口说明、示例，不直接发出执行指令。

必须包含：

```yaml
id: step-001
layer: internal
kind: skill
loadable_by:
  - transfer_money
```

Reference 示例：

```yaml
id: ref-001
layer: reference
kind: reference
loadable_by:
  - transfer_money
```

禁止：

1. 禁止 `description` 字段。
2. 禁止出现在 matcher index。
3. 禁止被用户输入、embedding、关键词、LLM 自主搜索直接命中。
4. 禁止跨业务场景随意引用，除非声明为 shared reference 并通过白名单授权。

## 3. 加载流程

### 3.1 单轮基础流程

```text
1. Runtime 加载 agent.md。
2. Runtime 读取 L2 Skill index metadata。
3. Matcher 只在 L2 description / matcher metadata 上召回候选业务场景。
4. Runtime 读取命中的 L2 Skill 完整正文。
5. Runtime 校验 L2 Skill 的显式 loads。
6. Runtime 按声明顺序加载 L3+ Skill / Reference。
7. Runtime 构建最终 prompt/context。
8. Runtime 记录 load trace。
```

### 3.2 多轮流程

多轮 session 中，Runtime 不应每轮重新开放深层召回。

要求：

1. Session 绑定当前 L2 Skill 时，后续补充输入默认沿当前 L2 Skill 上下文继续。
2. 若用户明显切换业务场景，只重新执行 L2 matcher。
3. 已加载的 L3+ 只能来自当前 L2 Skill 的显式引用链。
4. 若 L2 Skill 版本变化，当前 session 继续使用进入 session 时绑定的版本，除非显式 replan。

## 4. Manifest 和 Frontmatter 要求

### 4.1 L2 Entry Skill Frontmatter

```yaml
id: transfer_money
version: 0.1.0
layer: entry
kind: skill
description: 用户表达转账、汇款、给某人转钱等资金划转诉求时使用
owner: transfer-team
allowed_capabilities:
  - transfer.risk_check
  - transfer.submit
loads:
  skills:
    - ./internal/step-001.md
    - ./internal/step-002.md
  references:
    - ./references/ref-001.md
```

### 4.2 L3 Internal Skill Frontmatter

```yaml
id: step-001
version: 0.1.0
layer: internal
kind: skill
loadable_by:
  - transfer_money
```

### 4.3 Reference Frontmatter

```yaml
id: ref-001
version: 0.1.0
layer: reference
kind: reference
loadable_by:
  - transfer_money
```

### 4.4 字段规则

| 字段 | L1 agent.md | L2 entry skill | L3 internal skill | Reference |
|---|---|---|---|---|
| `id` | 可选固定值 | 必填 | 必填 | 必填 |
| `layer` | `agent` | `entry` | `internal` | `reference` |
| `kind` | `agent` | `skill` | `skill` | `reference` |
| `description` | 禁止用于业务匹配 | 必填 | 禁止 | 禁止 |
| `name` | 可选 | 可选 | 禁止有业务含义 | 禁止有业务含义 |
| `loads` | 可选加载全局 shared policy | 可选 | 可选 | 可选但受限 |
| `loadable_by` | 不需要 | 不需要 | 必填 | 必填 |

## 5. 匹配与加载约束

### 5.1 Matcher 只能看 L2

Matcher 输入只允许包含：

1. L2 Skill `id`
2. L2 Skill `description`
3. L2 Skill 显式 matcher metadata
4. L2 Skill 版本和启停状态

Matcher 禁止读取：

1. L2 Skill 正文。
2. L3+ 文件内容。
3. Reference 文件内容。
4. 文件名中的业务词作为匹配证据。

### 5.2 Loader 必须校验引用链

加载任意非 L1 / 非 L2 文件前必须校验：

1. 引用方已加载。
2. 被引用方路径在 Skill root 内。
3. 被引用方 `layer` 和 `kind` 合法。
4. 被引用方 `loadable_by` 允许引用方或引用方所属 entry skill。
5. 未超过最大加载深度。
6. 未超过 token / 字节预算。
7. 没有循环引用。

### 5.3 显式引用语义

支持两类引用：

1. `loads.skills`：加载具备指令性的内部 Skill。
2. `loads.references`：加载只读参考材料。

要求：

1. 引用必须是显式相对路径。
2. 引用顺序必须稳定。
3. 相同文件重复引用只加载一次。
4. 引用失败必须返回可诊断错误，不能静默跳过。

## 6. Context 组装规则

最终上下文按固定顺序组装：

```text
1. agent.md
2. matched L2 Skill
3. L2 显式 loads.skills，按声明顺序
4. L2 显式 loads.references，按声明顺序
5. 深层引用的 internal skills / references，按引用拓扑顺序
6. request context / session state / tool results
```

冲突规则：

1. L1 定义全局边界，L2 不能覆盖安全边界。
2. L2 定义业务场景边界，L3 不能扩大 L2 的适用范围。
3. Internal Skill 不能新增未由 L2 授权的 capability。
4. Reference 不能包含直接执行指令；若出现指令性内容，loader 应拒绝或降级为不可执行文本。

## 7. 安全与可控性要求

### 7.1 禁止隐式检索

Runtime 禁止：

1. 对全量 skill 文件做 embedding 检索后直接加载。
2. 让 LLM 根据文件名自行选择深层文件。
3. 用用户输入直接匹配 L3+ 文件。
4. 使用 glob、目录遍历、远程 URL 动态引入 Skill。

### 7.2 加载预算

必须配置：

1. 最大加载深度。
2. 最大加载文件数。
3. 最大 token / 字节数。
4. 单个 entry skill 可加载的最大 internal skill 数。
5. 单个 entry skill 可加载的最大 reference 数。

超过预算时：

1. 不允许自动扩大范围。
2. 返回明确诊断。
3. 可按 Skill 声明的优先级裁剪 reference，但不能裁剪 `agent.md` 和 matched L2 Skill。

### 7.3 版本绑定

每次 session 进入业务场景时必须记录：

1. `entry_skill_id`
2. `entry_skill_version`
3. loaded files path
4. loaded files version / hash
5. load trace

目的：

1. 多轮可重放。
2. 问题可审计。
3. Skill 更新不污染已进行中的 session。

## 8. Load Trace

每轮必须产出可观测加载轨迹：

```json
{
  "agent": {
    "path": "agent.md",
    "version": "0.1.0",
    "reason": "default"
  },
  "matched_entry_skills": [
    {
      "id": "transfer_money",
      "path": "skills/transfer-money/SKILL.md",
      "reason": "description_match",
      "score": 0.91
    }
  ],
  "loaded_children": [
    {
      "path": "skills/transfer-money/internal/step-001.md",
      "kind": "skill",
      "reason": "explicit_load",
      "requested_by": "transfer_money"
    },
    {
      "path": "skills/transfer-money/references/ref-001.md",
      "kind": "reference",
      "reason": "explicit_reference",
      "requested_by": "transfer_money"
    }
  ]
}
```

验收要求：

1. 任意已加载文件都必须能解释加载原因。
2. 任意未命中的 L3+ 文件不能出现在 trace 中。
3. trace 可用于线上问题排查和离线评测。

## 9. 目录建议

```text
skill-root/
├── agent.md
├── skills/
│   ├── transfer-money/
│   │   ├── SKILL.md
│   │   ├── internal/
│   │   │   ├── step-001.md
│   │   │   └── step-002.md
│   │   └── references/
│   │       ├── ref-001.md
│   │       └── ref-002.md
│   └── account-balance/
│       ├── SKILL.md
│       ├── internal/
│       │   └── step-001.md
│       └── references/
│           └── ref-001.md
└── shared/
    └── references/
        └── ref-common-001.md
```

Shared reference 要求：

1. 必须声明 `shared: true`。
2. 必须声明允许哪些 entry skill 引用。
3. 不能被 matcher 直接召回。

## 10. 验收场景

### S1：默认加载

输入任意用户消息。

期望：

1. 只默认加载 `agent.md`。
2. L2 只读取 index metadata。
3. 未命中业务场景时，不加载任何 L2 正文或 L3+ 文件。

### S2：命中 L2 业务场景

用户说：“帮我给张三转 500 元”。

期望：

1. Matcher 命中 `transfer_money` 的 `description`。
2. Runtime 加载 `transfer-money/SKILL.md`。
3. Runtime 加载该 Skill 显式声明的 internal skills 和 references。
4. 不加载其他业务场景文件。

### S3：L3 不可直接命中

存在一个内部文件记录转账限额规则，但用户直接问：“转账限额是多少？”

期望：

1. Matcher 不直接命中该 reference。
2. 若没有 L2 entry skill 命中，Runtime 不加载该 reference。
3. 若 L2 `transfer_money` 命中并显式引用该 reference，才能加载。

### S4：禁止隐式目录加载

L2 Skill 写：

```yaml
loads:
  references:
    - ./references/*.md
```

期望：

1. 校验失败。
2. Runtime 不执行该 Skill。
3. 返回明确错误：禁止 glob 引用。

### S5：循环引用

`step-001.md` 引用 `step-002.md`，`step-002.md` 又引用 `step-001.md`。

期望：

1. Loader 检测循环。
2. 返回可诊断错误。
3. 不进入模型上下文。

## 11. TODO

### P0：需求定稿

| ID | TODO | 状态 |
|---|---|---|
| P0-01 | 确认 L1/L2/L3+ 分层命名和含义 | 待确认 |
| P0-02 | 确认 L3+ 是否完全禁止 `name`，还是仅禁止语义化 `name` | 待确认 |
| P0-03 | 确认 Reference 是否允许继续引用其他 Reference | 待确认 |
| P0-04 | 确认 shared reference 的授权模型 | 待确认 |

### P1：规格设计

| ID | TODO | 状态 |
|---|---|---|
| P1-01 | 定义 frontmatter JSON schema | 未开始 |
| P1-02 | 定义 Skill index 生成规则 | 未开始 |
| P1-03 | 定义 load trace schema | 未开始 |
| P1-04 | 定义 context 预算和裁剪策略 | 未开始 |
| P1-05 | 定义版本 / hash 绑定格式 | 未开始 |

### P2：运行时能力

| ID | TODO | 状态 |
|---|---|---|
| P2-01 | 实现只索引 L2 description 的 matcher | 未开始 |
| P2-02 | 实现显式引用 loader | 未开始 |
| P2-03 | 实现 layer / path / loadable_by 校验 | 未开始 |
| P2-04 | 实现循环引用检测和预算控制 | 未开始 |
| P2-05 | 实现 load trace 输出 | 未开始 |

### P3：评测与治理

| ID | TODO | 状态 |
|---|---|---|
| P3-01 | 增加 L3 不可直接命中测试 | 未开始 |
| P3-02 | 增加跨业务场景误加载测试 | 未开始 |
| P3-03 | 增加 glob / 越权路径 / 循环引用测试 | 未开始 |
| P3-04 | 增加 token 预算溢出测试 | 未开始 |
| P3-05 | 增加 load trace 快照测试 | 未开始 |

## 12. 非目标

本需求不解决：

1. 具体业务 Agent 如何执行业务。
2. 业务 API 的风控、限额、幂等。
3. 最终用户话术生成。
4. 全局知识库 RAG。
5. 让模型自由搜索和组合 Skill。

这些能力可以存在于系统其他层，但不能破坏本分层加载约束。
