---
tags:
  - claude-memory
  - project/intent-router
  - category/pattern
created: 2026-04-18
severity: high
trigger: user-request
---

# Agent SSE Streaming Format

## 问题
子智能体需要以 SSE 流式格式返回响应给 Router，Router 需要解析多种格式。

## Router 支持的两种格式

### 格式一：扁平 JSON（推荐）
```
event:message
data:{"event":"final","content":"已向张三转账 500 CNY","ishandover":true,"status":"completed","slot_memory":{"amount":"500"},"payload":{}}

event:done
data:[DONE]
```

### 格式二：嵌套格式（旧 Air 格式）
```
event:message
data:{"content":"","additional_kwargs":{"node_id":"end","node_title":"结束","node_output":{"output":"{\"isHandOver\":true,\"data\":[{\"answer\":\"||500||\"}]}"}}}

event:done
data:[DONE]
```

## Agent 端实现

### handle_stream 实现（新格式）
```python
async def handle_stream(self, request: XxxAgentRequest) -> AsyncIterator[str]:
    response = await self.handle(request)
    output = {
        "event": response.event,
        "content": response.content,
        "ishandover": response.ishandover,
        "status": response.status,
        "slot_memory": response.slot_memory,
        "payload": response.payload,
    }
    yield f"event:message\ndata:{json.dumps(output, ensure_ascii=False)}\n\n"
    yield "event:done\ndata:[DONE]\n\n"
```

## Router 端解析逻辑

Router 的 `_payload_to_chunk` 方法会：
1. 检测 `additional_kwargs.node_output.output` 嵌套结构并提取
2. 支持 `ishandover` 和 `isHandOver` 两种字段名
3. 从 `data[].answer` 提取旧格式的 content
4. 兼容扁平 JSON 直接解析

## 关键点
1. 新 agent 使用扁平 JSON 格式
2. Router 自动兼容两种格式
3. `json.dumps(ensure_ascii=False)` 保留中文字符
4. `event:message` 后跟 `event:done` 两个事件

## 教训
Router 需要兼容多种下游 agent 格式，解析时要先检测嵌套结构再提取实际数据。
