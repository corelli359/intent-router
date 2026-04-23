# Router-Service 短期记忆统一修复与回归说明 v0.4

> 状态：实施与回归说明
> 日期：2026-04-23
> 范围：当前生产入口 `POST /api/v1/message`

## 1. 目标

本轮不是做某个意图的临时特判，而是修复 Router 在多轮补槽阶段的短期记忆连续性问题。

目标约束：

1. 不能按 `transfer_money` 写死业务逻辑。
2. 不能靠正则、猜测或硬编码姓名做“伪记忆”。
3. 要从识别 / 规划 / waiting node 恢复这条主链路统一修复。
4. 未来 agent 增多后，这套机制仍然成立。

当前唯一具备稳定提槽能力的意图仍是 `transfer_money`，因此本轮回归用它做验证载体，但修复本身不绑定该意图。

## 2. 问题现象

本轮核心复现场景：

1. 第 1 轮：`我要转账`
2. 第 2 轮：`小刚`
3. 第 3 轮：`小红吧`
4. 第 4 轮：`200`

期望行为：

1. 第 2 轮沉淀 `payee_name=小刚`
2. 第 3 轮覆盖为 `payee_name=小红`
3. 第 4 轮只补 `amount=200`
4. 最终保持 `payee_name=小红, amount=200`

实际问题是：waiting node 在后续轮次会把上一轮已经成立的 user-message 槽位重新拿“当前轮文本”校验，导致旧值被错误判失效；同时提槽 prompt 没有充分拿到最近对话，模型无法稳定理解当前轮是在“续写已有业务对象”。

这说明问题本质不是“某个名字没识别出来”，而是：

1. 短期对话上下文没有统一传到位；
2. 已确认槽位在 waiting node 恢复时没有按业务连续性校验；
3. LLM 返回的 `source_text` 没有做可信约束时，可能把并不存在于本轮证据里的内容带回来。

## 3. 根因拆解

### 3.1 waiting node 校验只盯当前轮

此前 waiting node 下的 slot validation / seed binding normalization，会把上一轮已经成立的 `SlotBindingSource.USER_MESSAGE` 值，只拿当前轮文本做 grounding。

这会导致：

1. 用户上一轮明确给过 `小红`
2. 当前轮只说 `200`
3. 校验器看到当前轮不含 `小红`
4. 于是把 `payee_name=小红` 错判为失效

这与多轮补槽的业务语义相违背。

### 3.2 提槽 prompt 缺少 recent_messages

提槽阶段虽然已经有 `existing_slot_memory`，但没有把最近对话作为上下文传给 slot extractor prompt。

结果是：

1. 模型能看到当前轮
2. 能看到已有槽位
3. 但不一定知道“当前轮是在延续哪段对话”

在覆盖、续填、纠正场景下，稳定性会下降。

### 3.3 blocked-turn 识别链路也丢上下文

waiting / blocked turn 下如果走轻量识别路径，之前会丢掉 `recent_messages` 和 `long_term_memory`，导致“是否继续当前业务 / 是否重规划”的判定上下文不足。

### 3.4 source_text 缺少可信约束

如果 LLM 给了一个 `source_text`，但这段文本根本不在当前证据里，运行时如果直接相信它，就会把模型幻觉伪装成“已 grounding 的槽位”。

这不是短期记忆，而是伪证据。

## 4. 统一修复原则

本轮修复遵循以下原则：

1. `recent_messages` 是识别和提槽共用的短期上下文载体之一。
2. waiting node 已成立的槽位，不应只按当前轮文本重新判死刑。
3. 旧槽位必须在“本轮之前的最近对话 + 当前轮业务片段”这个更完整的证据面上校验。
4. 当前轮如果明确改值，应允许覆盖旧值。
5. LLM 返回的 `source_text` 只有在证据文本里确实出现时才可信。
6. 当前轮消息不能被重复注入 `recent_messages`，避免 prompt 中当前轮重复一次。

## 5. 实现变更

### 5.1 提槽与校验统一接入 `recent_messages`

变更文件：

- `backend/services/router-service/src/router_service/core/slots/extractor.py`
- `backend/services/router-service/src/router_service/core/slots/validator.py`
- `backend/services/router-service/src/router_service/core/slots/understanding_validator.py`
- `backend/services/router-service/src/router_service/core/graph/orchestrator.py`

做法：

1. `GraphRouterOrchestrator` 在 waiting node 校验前构造 `recent_messages`
2. 自动剔除当前 inflight user turn，避免重复
3. `UnderstandingValidator -> SlotExtractor -> SlotValidator` 全链路透传 `recent_messages`

这样 waiting node 的恢复逻辑看的是“当前业务对象的连续上下文”，而不是孤立一句话。

### 5.2 旧 user-message 槽位改为按 turn-history 校验

核心变化：

1. 不再把上一轮已经成立的 user-message 槽位只对照当前轮文本做 grounding
2. 改为对照 `recent_messages + graph_source_message + node.source_fragment + current_message`
3. history 来源的槽位仍按 history 规则校验，不混淆

这解决了“上一轮说了收款人，这一轮说金额，上一轮收款人被冲掉”的问题。

### 5.3 提槽 prompt 显式加入 `recent_messages_json`

变更文件：

- `backend/services/router-service/src/router_service/core/prompts/prompt_templates.py`

当前约束明确为：

1. `recent_messages` 只用于帮助模型理解上下文连续性
2. 不能仅凭历史里出现过的旧值就新增槽位
3. 只有该值已在 `existing_slot_memory` 成立，或当前轮明确表达时，才允许继续保留 / 覆盖

这避免了把最近消息错误用成“自由历史预填”。

### 5.4 对 `source_text` 增加可信约束

变更文件：

- `backend/services/router-service/src/router_service/core/slots/grounding.py`

新增 `grounded_source_text(...)`：

1. 只有当 `source_text` 真正在证据文本中出现时才返回
2. 对数字类文本额外支持 digits 归一化比对
3. 否则视为不可信，不拿它做 grounding

这使得运行时不会因为模型返回了一个“看起来很像证据”的片段，就误以为该槽位已经有证据。

### 5.5 blocked-turn 识别路径补齐上下文

变更文件：

- `backend/services/router-service/src/router_service/core/recognition/understanding_service.py`
- `backend/services/router-service/src/router_service/core/graph/message_flow.py`

waiting / blocked turn 走轻量识别时，现在也会带上：

1. `recent_messages`
2. `long_term_memory`

且不重复注入当前轮。

## 6. 本轮回归用例

本轮回归只针对当前可稳定验证的 `transfer_money` 单意图链路，不扩展新意图。

### 6.1 `/api/v1/message` 自动回归

脚本：

- `scripts/run_router_v1_regression_suite.py`

重点场景：

1. 单轮直接完成
2. 多轮先姓名后金额
3. 多轮先金额后姓名
4. 首轮泛化表达，二轮一次补全
5. 多轮中途覆盖收款人，再补金额
6. `execute` 与 `router_only` 两条链路都覆盖

### 6.2 单元 / API 回归

核心测试文件：

- `backend/tests/test_slot_extractor.py`
- `backend/tests/test_slot_validator.py`
- `backend/tests/test_understanding_validator.py`
- `backend/tests/test_graph_orchestrator.py`
- `backend/tests/test_understanding_service.py`
- `backend/tests/test_graph_message_flow.py`
- `backend/tests/test_prompt_templates.py`
- `backend/tests/test_router_api_v2.py`

重点验证：

1. `recent_messages` 已传入 extractor prompt
2. 旧 user-message 槽位不会因下一轮缺字面值而被误删
3. 伪造 `source_text` 不会被当成真实证据
4. `/api/v1/message` 的 waiting -> completed / ready_for_dispatch 行为稳定
5. 覆盖类多轮场景能保留最新值

## 7. 已执行回归结果

本轮本地已执行：

```text
./.venv/bin/pytest -q backend/tests/test_graph_message_flow.py backend/tests/test_understanding_service.py backend/tests/test_graph_orchestrator.py backend/tests/test_slot_extractor.py backend/tests/test_slot_validator.py backend/tests/test_understanding_validator.py backend/tests/test_prompt_templates.py
44 passed

./.venv/bin/pytest -q backend/tests/test_router_api_v2.py -k 'test_v1_'
9 passed
```

说明：

1. 本轮修复相关单测全部通过
2. 当前生产入口 `/api/v1/message` 的本地 API 回归通过
3. 旧的 `/api/router/v2/sessions/*` 历史接口不作为本轮生产回归口径

### 7.2 已部署 `router-api-test` 外网回归

部署动作：

```text
kubectl rollout restart deployment/router-api-test -n intent
kubectl rollout status deployment/router-api-test -n intent --timeout=20m
```

外网入口：

```text
http://intent-router.kkrrc-359.top/api/v1/message
```

已执行针对当前 test 外网链路的 `router_only` 回归：

```text
python scripts/run_router_v1_regression_suite.py \
  --base-url http://intent-router.kkrrc-359.top \
  --case-id router_only_name_then_amount \
  --case-id router_only_amount_then_name \
  --case-id router_only_fill_all_missing_on_second_turn \
  --case-id router_only_override_payee_before_amount

summary:
  total=4
  passed=4
  failed=0
```

覆盖结果：

1. `router_only_name_then_amount`：通过
2. `router_only_amount_then_name`：通过
3. `router_only_fill_all_missing_on_second_turn`：通过
4. `router_only_override_payee_before_amount`：通过

额外流式验证：

1. 使用 `scripts/mock_assistant_router_stream.py`
2. 场景：`我要转账 -> 小刚 -> 小红吧 -> 200`
3. 结果：
   - 每轮都能收到 `event: message`
   - 最终一轮返回 `status=ready_for_dispatch`
   - 最终 `slot_memory={"payee_name":"小红","amount":200}`
   - 结束帧为 `event: done / data: [DONE]`

说明：

当前外网 `router-api-test` 入口用于验证 Router 自身识别 / 提槽 / SSE / 短期记忆链路，因此本轮把它作为 `router_only` 验收口径。

### 7.3 关于 `execute` 用例

本轮也对外网入口额外跑了 `execute` 型用例。

观察到的现象是：

1. Router 能正确拿到 `slot_memory`
2. `output` 中也能看到下游返回内容
3. 但顶层状态在当前 test 链路下未作为“短期记忆修复”验收口径统一收敛到最终完成态

因此，本轮对“短期记忆统一修复”的外网验收，以 `router_only` 为准；`execute` 的完成态收口问题单独评估，不混入本次记忆修复结论。

## 8. 结论

本轮修复后的短期记忆行为可以概括为：

1. 识别链路和 waiting node 提槽链路都能看到同一份最近对话上下文
2. 已成立槽位按“业务连续性”保留，而不是按“当前一句话必须重复出现”保留
3. 当前轮若明确改值，可以覆盖旧值
4. 运行时不接受脱离证据文本的伪 `source_text`

因此，这次修复解决的是 Router 的短期记忆传递与校验方式，不是某个转账场景的临时兜底。
