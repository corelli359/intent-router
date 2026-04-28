# 新增接口参数：recommend_task 和 current_display

## 需求背景

为了增强多意图识别和对话上下文理解能力，需要在 `/v1/message` 接口中增加两个新参数：

1. **`recommend_task`**: 应用推荐的任务列表，注入到提示词中用于支持多意图的识别与规划
2. **`current_display`**: 应用展示的对话历史，用于替换提示词中的短期记忆

这两个参数是 router 独用的上下文参数，不透传给子智能体。

---

## 接口变更

### 请求参数

```json
{
  "sessionId": "xxx",
  "txt": "用户消息",
  "recommendTask": [
    {
      "intentCode": "transfer_money",
      "title": "转账",
      "slotMemory": {
        "payee_name": "张三",
        "amount": 100
      }
    }
  ],
  "currentDisplay": [
    {
      "role": "user",
      "content": "我要转账"
    },
    {
      "role": "assistant",
      "content": "好的，请提供收款人和金额"
    }
  ]
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `recommendTask` | `array` | 否 | 应用推荐的任务列表，直接注入到 LLM 提示词中 |
| `currentDisplay` | `array` | 否 | 应用端记录的对话历史，替换 router 的短期记忆 |

---

## 代码变更清单

### 1. 接口层

**文件**: `backend/services/router-service/src/router_service/api/routes/sessions.py`

- `ProtocolMessageRequest` 添加 `recommend_task` 和 `current_display` 字段
- `_assistant_message_json_response()` 添加参数传递
- `_assistant_message_stream_response()` 添加参数传递
- `post_protocol_message()` 传递新参数

### 2. Session 状态存储

**文件**: `backend/services/router-service/src/router_service/core/shared/graph_domain.py`

- `GraphSessionState` 添加私有字段：
  - `_recommend_task: list[dict[str, Any]] | None`
  - `_current_display: list[dict[str, Any]] | None`
- `set_request_context()` 添加新参数
- 添加 getter 方法：`recommend_task()`, `current_display()`

### 3. 上下文构建

**文件**: `backend/services/router-service/src/router_service/core/support/context_builder.py`

- `build_task_context()` 添加参数：
  - `recommend_task: list[dict[str, Any]] | None`
  - `current_display: list[str] | None`
- 当 `current_display` 非空时，替换默认的 `recent_messages`

### 4. 提示词模板

**文件**: `backend/services/router-service/src/router_service/core/prompts/prompt_templates.py`

- `DEFAULT_RECOGNIZER_HUMAN_PROMPT` 添加 `{recommend_task_json}` 占位符
- `DEFAULT_GRAPH_PLANNER_HUMAN_PROMPT` 添加 `{recommend_task_json}` 占位符
- `DEFAULT_UNIFIED_GRAPH_BUILDER_HUMAN_PROMPT` 添加 `{recommend_task_json}` 占位符

### 5. Orchestrator 层

**文件**: `backend/services/router-service/src/router_service/core/graph/orchestrator.py`

- `handle_user_message()` 添加参数传递
- `handle_user_message_serialized()` 添加参数传递
- `_build_session_context()` 从 session 获取新参数并传递给 ContextBuilder
- `_recognize_message()` 添加 `recommend_task` 参数
- `_build_graph_from_message()` 添加 `recommend_task` 参数

### 6. MessageFlow 层

**文件**: `backend/services/router-service/src/router_service/core/graph/message_flow.py`

- `handle_user_message()` 添加参数并传递给 `session.set_request_context()`

### 7. Understanding Service

**文件**: `backend/services/router-service/src/router_service/core/recognition/understanding_service.py`

- `recognize_message()` 添加 `recommend_task` 参数并传递给 recognizer
- `build_graph_from_message()` 添加 `recommend_task` 参数并传递给 builder

### 8. Recognizer

**文件**: `backend/services/router-service/src/router_service/core/recognition/recognizer.py`

- `LLMIntentRecognizer.recognize()` 添加 `recommend_task` 参数
- LLM 调用添加变量：`"recommend_task_json": json_dumps(recommend_task or [])`

### 9. Planner

**文件**: `backend/services/router-service/src/router_service/core/graph/planner.py`

- `LLMIntentGraphPlanner.plan()` 添加 `recommend_task` 参数
- `SequentialIntentGraphPlanner.plan()` 添加 `recommend_task` 参数（兼容性）
- `_fallback_plan()` 添加 `recommend_task` 参数
- LLM 调用添加变量：`"recommend_task_json": json_dumps(recommend_task or [])`

### 10. Builder

**文件**: `backend/services/router-service/src/router_service/core/graph/builder.py`

- `LLMIntentGraphBuilder.build()` 添加 `recommend_task` 参数
- LLM 调用添加变量：`"recommend_task_json": json_dumps(recommend_task or [])`

### 11. Compiler

**文件**: `backend/services/router-service/src/router_service/core/graph/compiler.py`

- `compile_message()` 从 context 获取 `recommend_task`
- `recognize_only()` 传递 `recommend_task` 到 understanding_service
- `_plan_graph()` 添加 `recommend_task` 参数并传递给 planner

---

## 数据流图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              参数注入流程                                     │
└─────────────────────────────────────────────────────────────────────────────┘

POST /v1/message
  { recommendTask, currentDisplay }
            │
            ▼
sessions.py: ProtocolMessageRequest
            │
            ▼
orchestrator.handle_user_message(recommend_task, current_display)
            │
            ▼
message_flow.handle_user_message()
            │
            ▼
session.set_request_context(recommend_task, current_display)
            │
            ▼
session._recommend_task, session._current_display (私有字段)
            │
            ▼
orchestrator._build_session_context()
            │
            ▼
ContextBuilder.build_task_context(
    recommend_task = session.recommend_task(),
    current_display = [格式转换后的 list]
)
            │
            ▼
context = {
    "recommend_task": [...],
    "recent_messages": current_display 或 build_recent_messages()
}
            │
            ▼
understanding_service.recognize_message(recommend_task)
            │
            ▼
recognizer.recognize(recommend_task)
            │
            ▼
llm_client.run_json(variables = {
    "recommend_task_json": json_dumps(recommend_task),
    "recent_messages_json": json_dumps(recent_messages),
    ...
})
            │
            ▼
提示词模板: {recommend_task_json}
```

---

## 向后兼容性

- 两个新参数均为可选，默认 `None`
- 未传递时使用原有逻辑：
  - `recommend_task` 传入空列表 `[]`
  - `current_display` 为 `None` 时，使用 `session.messages`
- 不影响现有功能

---

## 测试建议

1. **接口验证**: 发送带新参数的请求，确认可正确接收
2. **提示词验证**: 检查 LLM 调用时变量正确注入
3. **向后兼容**: 未传递新参数时系统正常运行
4. **端到端测试**: 验证多意图识别效果

### 测试请求示例

```bash
curl -X POST http://localhost:8000/api/v1/message \
  -H "Content-Type: application/json" \
  -d '{
    "sessionId": "test-session-001",
    "txt": "第一个和第三个都要",
    "recommendTask": [
      {"intentCode": "transfer_money", "title": "转账", "slotMemory": {"amount": 100}},
      {"intentCode": "query_balance", "title": "查询余额"},
      {"intentCode": "pay_bills", "title": "缴费", "slotMemory": {"amount": 50}}
    ],
    "currentDisplay": [
      {"role": "assistant", "content": "为您推荐以下操作：1. 转账100元 2. 查询余额 3. 缴费50元"}
    ]
  }'
```

---

## 相关文件清单

```
backend/services/router-service/src/router_service/
├── api/routes/sessions.py
├── core/graph/
│   ├── orchestrator.py
│   ├── message_flow.py
│   ├── compiler.py
│   ├── planner.py
│   └── builder.py
├── core/shared/graph_domain.py
├── core/support/context_builder.py
├── core/prompts/prompt_templates.py
└── core/recognition/
    ├── understanding_service.py
    └── recognizer.py
```

---

## 审查要点

1. 参数传递链路完整，无遗漏
2. 提示词模板占位符正确添加
3. LLM 变量构建使用 `json_dumps()` 序列化
4. 默认值处理正确（`None` 时使用空列表或原有逻辑）
5. 不影响子智能体的 `config_variables` 透传机制
