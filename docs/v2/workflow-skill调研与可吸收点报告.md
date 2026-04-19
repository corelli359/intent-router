# workflow-skill 调研与可吸收点报告

## 1. 结论摘要

`workflow-skill` 和当前 `intent-router` 确实相关，但两者解决的问题并不相同。

- `workflow-skill` 的核心目标是把自然语言需求转成 **外部平台可直接导入的工作流文件**。
- `intent-router` 的核心目标是把自然语言请求转成 **运行时动态意图图**，并在多轮对话里持续执行、补槽、确认、取消、重规划。

因此，这个项目**不适合直接照搬 `workflow-skill` 的产品形态**，但**很值得吸收它在“知识资产组织、模板化生成、复杂场景样例、工程化交付”上的做法**。

一句话判断：

- `workflow-skill` 强在“生成稳定的外部 DSL/ZIP/JSON”。
- `intent-router` 强在“运行时语义、状态机、多轮执行、测试覆盖”。
- 最值得吸收的不是“导出某个平台 workflow 文件”，而是“把 prompt/参考资料/样例/回归场景做成可复用资产”。

## 2. 调研对象概览

本次调研对象是 `twwch/workflow-skill` 仓库，当前可见结构是一个面向工作流平台的 skill 集合：

- `coze-workflow`
- `dify-workflow`
- `comfyui-workflow`

它的整体套路比较统一：

1. `SKILL.md` 负责定义触发条件、交互策略、生成流程。
2. `references/` 负责沉淀节点规范、DSL 细节、连线和布局规则。
3. `templates/` 或 `scripts/` 负责把高风险格式约束收敛成可复用资产。
4. `examples/` 和 `test-scenarios.md` 负责提供复杂案例。
5. `.claude-plugin/` 负责分发和 marketplace 元数据。

调研时看到的几个客观指标：

- `coze-workflow`：20 个参考文件、3 个示例、2 个脚本
- `dify-workflow`：20 个参考文件、2 个示例、4 个模板
- `comfyui-workflow`：46 个参考文件、34 个模板
- 当前 `intent-router`：13 个 graph 模块、6 个 recognition 模块、63 个 backend 测试文件

这个对比很清楚地说明：

- `workflow-skill` 的投入重心在 **知识库资产化**
- `intent-router` 的投入重心在 **运行时实现与测试**

## 3. 与 intent-router 的核心差异

| 维度 | workflow-skill | intent-router |
| --- | --- | --- |
| 主目标 | 生成可导入的外部 workflow 文件 | 构建并执行运行时动态意图图 |
| 输出物 | `.zip` / `.dify.yml` / `.json` | `ExecutionGraphState`、session state、SSE 事件 |
| 核心约束 | 平台 DSL 格式、节点 schema、布局、导入兼容性 | intent/slot 语义、状态机、条件依赖、多轮补槽、取消/重规划 |
| 图的来源 | 根据用户描述生成一个静态 workflow 文件 | 根据当前会话上下文在运行时临时建图 |
| 交互模式 | 生成前最多若干轮澄清，然后一次性产出文件 | 每个 node/graph 可持续等待、补充、确认、切换目标 |
| 成功标准 | 导入成功、节点齐全、格式兼容 | 识别正确、图可执行、状态一致、用户可控 |
| 工程重心 | 参考资料、模板、格式桥接脚本 | orchestrator、runtime、compiler、builder、API、测试 |

这也解释了为什么本仓库已有文档明确强调：当前 V2 是“动态 graph”，不是“固定 workflow”。

## 4. workflow-skill 的主要优势

### 4.1 知识分层做得很扎实

它没有把所有平台规则都直接塞进一个超长主提示，而是拆成：

- `SKILL.md` 做总控
- `references/` 做细粒度规范
- `templates/` 做高频结构复用
- `scripts/` 做高风险格式桥接

这有两个直接好处：

- 主提示更短，模型更容易保持稳定
- 具体规则可以单独维护，不必每次改 prompt 主体

对比当前 `intent-router`，我们的提示词虽然业务约束很强，但仍明显集中在 `backend/services/router-service/src/router_service/core/prompts/prompt_templates.py` 一个文件里，可维护性和可复用性还可以继续提升。

### 4.2 对“模型不擅长做的事”用了脚本兜底

`workflow-skill` 最有价值的工程意识，不是多会写 prompt，而是知道**哪些事情不该交给模型裸生成**。

典型例子：

- Coze 的 ZIP 产物不是让模型直接输出二进制，而是让 skill 生成 Python 脚本，再用脚本打包
- Coze YAML 的格式细节通过 builder 函数和字符串模板固化，而不是完全依赖自由生成
- ComfyUI 的复杂工作流优先走模板，再做参数化修改

这类“模型负责结构意图，脚本负责格式正确性”的分工，非常值得借鉴。

### 4.3 模板和参考资料覆盖复杂拓扑

它不仅有简单 demo，还有针对复杂场景的压力样例，例如：

- 双层 iteration
- 多分支 classifier
- 深层 variable aggregation
- 多外部系统并行调用

这比只给简单 happy path 示例有效得多，因为真实问题几乎都死在复杂拓扑和变量作用域上。

### 4.4 分发形态比较完整

`.claude-plugin/plugin.json` 和 `marketplace.json` 说明它已经把 skill 当成一个可分发产品在做，而不是临时 prompt。

这意味着：

- 安装路径清晰
- skill 边界清晰
- 版本化意识较强

如果后续 `intent-router` 也准备给内部团队提供“设计/配置/调试”类 agent 能力，这种可分发思路可以直接借鉴。

## 5. workflow-skill 的主要局限

### 5.1 它解决的是“生成文件”，不是“执行运行时”

`workflow-skill` 的输出是静态导入文件，不负责：

- session 生命周期
- graph/node 运行时状态
- waiting / resume / cancel / replan
- 多轮补槽
- 与真实 agent 的持续交互

而这些恰恰是 `intent-router` 的核心价值。

所以如果把它的模式直接搬到 `intent-router`，很容易把系统带偏到“图生成器”而不是“图执行器”。

### 5.2 平台知识维护成本很高

它之所以强，是因为把很多平台细节写死了；但这也意味着：

- 平台 DSL 一旦升级，参考资料和模板就要同步更新
- 节点 schema 越多，维护面越大
- 多平台之间难以共享抽象

这种模式适合“目标格式稳定且导入要求很苛刻”的场景，不适合直接作为 `intent-router` 的主工程模式。

### 5.3 自动化验证能力从仓库结构上看不算强

从本次看到的仓库结构推断，它更像是：

- 依赖示例和人工导入验证
- 用复杂测试场景做生成压力测试

但没有像 `intent-router` 这样明显成体系的后端测试矩阵。

这里要说明一下：这是**基于仓库可见结构的推断**，不是对其完整 CI 现状的断言。

### 5.4 平台导向太强，可迁移性有限

它的很多知识点都绑定在 Coze / Dify / ComfyUI 的节点和字段上。

这类资产对“生成这些平台的 workflow 文件”极其有价值，但对 `intent-router` 这种自有 runtime，只能迁移工程方法，不能直接迁移内容本身。

## 6. intent-router 当前更强的地方

从当前仓库看，`intent-router` 在下面几个方面明显强于 `workflow-skill`：

### 6.1 运行时语义更完整

当前 graph runtime 已经有比较明确的分层：

- `graph/compiler.py`
- `graph/builder.py`
- `graph/planner.py`
- `graph/runtime.py`
- `graph/orchestrator.py`
- `graph/message_flow.py`
- `graph/action_flow.py`
- `graph/state_sync.py`

这说明系统不是停留在“生成图”，而是已经进入“执行图”的阶段。

### 6.2 动态图能力更贴近真实业务

当前系统支持的核心能力包括：

- 多意图识别
- 条件边
- graph 级确认
- node 级等待用户补充
- 取消和重规划
- 历史槽位复用确认
- SSE 状态推送

这类能力是工作流导入 skill 不需要也不擅长处理的。

### 6.3 测试体系明显更成熟

当前仓库已有较完整的后端测试集合，至少覆盖了：

- builder
- runtime
- router API v2
- recognition
- slot 提取与校验
- prompt 模板

这一点是 `intent-router` 很大的资产，不应该为追求“skill 化”而稀释。

## 7. 最值得吸收的部分

下面这些点我认为值得真正落地，而不是停留在“看起来不错”。

### 7.1 把 prompt 相关知识做成分层资产

当前建议把大 prompt 继续拆分，至少形成下面几类资产：

- `prompts/recognition/`
- `prompts/graph_build/`
- `prompts/turn_interpretation/`
- `prompts/proactive_recommendation/`
- `references/intent-patterns/`
- `references/graph-patterns/`
- `references/slot-binding-patterns/`

目标不是把 prompt 写得更长，而是：

- 让规则来源更清晰
- 让维护能按主题修改
- 让未来内部 skill 或调试工具可以复用这些资料

### 7.2 建立复杂场景语料库，而不是只在测试里写零散 dict

`workflow-skill` 的 `test-scenarios.md` 很值得借鉴。

建议在本仓库新增一套复杂图场景语料，例如放到：

- `backend/tests/fixtures/graph_cases/`
- 或 `docs/examples/graph-cases/`

每个场景至少包含：

- 用户原始输入
- 已注册 intent 集合
- 期望 primary intents
- 期望 graph nodes / edges
- 期望 `needs_confirmation`
- 是否涉及 history reuse
- 关键状态迁移断言

这会显著提升后续 builder/planner 的回归效率。

### 7.3 给 graph 增加可视化导出能力

`workflow-skill` 很重视“生成结果能不能被人看懂、导入验证”。

`intent-router` 也应该补这个环节，但方式不是导出 Dify/Coze，而是导出：

- Mermaid 图
- 标准化调试 JSON
- 带节点状态的 Markdown 摘要

推荐落点：

- `scripts/export_graph_case.py`
- 或 `router_service/core/graph/debug_view.py`

这样做的价值是：

- 便于排查 planner/builder 产物
- 便于文档化复杂 case
- 便于评审 multi-intent 关系是否合理

### 7.4 把示例从“概念 demo”升级为“可回放案例”

当前 `docs/examples/` 里只有少量概念示例代码，偏解释型。

建议补两类内容：

- 面向产品/算法/后端协同的中文案例文档
- 面向测试的可执行 fixture

前者帮助统一认知，后者帮助防止回归。

### 7.5 如果要做 skill，应做“intent-router 内部设计 skill”，而不是平台 workflow skill

如果未来要把本仓库经验沉淀成 skill，建议方向是：

- 自然语言生成 intent definition 草稿
- 自然语言生成 graph case fixture
- 根据 PRD 生成 slot schema 初稿
- 根据运行时 graph 快照生成问题诊断摘要

而不是去生成 Coze/Dify workflow 文件。

因为前者能直接服务本项目，后者与本项目主线关联较弱。

## 8. 不建议直接照搬的部分

### 8.1 不建议把“导出外部 workflow 文件”当成主能力方向

这会让项目重心从 runtime correctness 转向 DSL correctness，方向会跑偏。

### 8.2 不建议把大量平台字段细节直接塞进主 prompt

`workflow-skill` 之所以能这么做，是因为目标就是平台 DSL。  
`intent-router` 的核心约束是业务语义和执行语义，不是某个平台导入格式。

### 8.3 不建议只靠样例驱动而缺少程序化断言

`intent-router` 当前已有测试优势，后续应该做的是“样例资产 + 程序化断言”结合，而不是回退成以手工观察为主。

## 9. 建议落地顺序

### P0：立即可做

1. 建一批复杂 `graph case` fixture。
2. 把现有 prompt 规则按主题拆分成更清晰的参考资产。
3. 补一份“多意图图模式手册”，把顺序、并行、条件、history reuse、guided selection 等模式沉淀下来。

### P1：一轮迭代内可做

1. 增加 graph 可视化导出脚本。
2. 让测试直接消费 fixture，而不是在测试函数里堆大段 payload。
3. 为 builder/planner 增加更系统的 golden case 回归。

### P2：视团队使用方式决定

1. 做一个内部 `intent-router-design` skill。
2. 把 intent 定义、slot schema、graph fixture 生成整合成协同工具链。

## 10. 最终判断

`workflow-skill` 不是 `intent-router` 的直接实现参考，更像是一个**很好的“skill 工程化样板”**。

对本仓库最有价值的启发有三条：

1. **把知识做成资产，而不是全塞进 prompt。**
2. **把复杂案例做成语料库，而不是靠零散临时代码说明。**
3. **把结果做成可观察产物，而不是只在运行时内部流转。**

如果按这个方向吸收，它能明显提升 `intent-router` 的：

- prompt 可维护性
- graph 设计可评审性
- 回归测试稳定性
- 团队协作效率

## 11. 参考材料

外部仓库：

- https://github.com/twwch/workflow-skill
- https://raw.githubusercontent.com/twwch/workflow-skill/main/README.zh-CN.md
- https://raw.githubusercontent.com/twwch/workflow-skill/main/skills/coze-workflow/SKILL.md
- https://raw.githubusercontent.com/twwch/workflow-skill/main/skills/dify-workflow/SKILL.md
- https://raw.githubusercontent.com/twwch/workflow-skill/main/skills/comfyui-workflow/SKILL.md

本仓库对比基线：

- `backend/services/router-service/src/router_service/core/graph/`
- `backend/services/router-service/src/router_service/core/recognition/`
- `backend/services/router-service/src/router_service/core/prompts/prompt_templates.py`
- `backend/tests/test_v2_graph_builder.py`
- `backend/tests/test_v2_graph_runtime.py`
- `backend/tests/test_router_api_v2.py`
- `docs/archive/reference-notes/graph-runtime-current-state-and-roadmap.md`
