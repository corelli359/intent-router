# 子智能体调用重构改动总结

## 改动范围

本次改动围绕 Router 调用子智能体的**请求报文格式**和**返回解析**两方面进行重构。

---

## 一、请求报文重构：config_variables 格式

### 旧格式（已废弃）

槽位分散在多个嵌套对象中，每个业务场景需要定义不同的嵌套模型类：

```json
{
  "sessionId": "xxx",
  "input": "给张三转账500元",
  "payer": {"cardNo": "6222...", "cardRemark": "工资卡"},
  "payee": {"name": "张三", "cardNo": "6222..."},
  "transfer": {"amount": "500", "ccy": "CNY"}
}
```

### 新格式（当前使用）

统一为 `config_variables` 数组，槽位聚合到 `slots_data`：

```json
{
  "session_id": "xxx",
  "txt": "给张三转账500元",
  "stream": true,
  "config_variables": [
    {"name": "custID", "value": "1631102265490929"},
    {"name": "sessionID", "value": "xxx"},
    {"name": "slots_data", "value": "{\"amount\":\"500\",\"payee_name\":\"张三\",\"payee_card_no\":\"6222...\"}"}
  ]
}
```

### 改动文件

#### Router 端

**`backend/services/router-service/src/router_service/core/support/agent_client.py`**

- 新增 `_default_payload()`：无 field_mapping 时生成默认报文
- 重写 `build()`：处理三种 field_mapping 路径（顶层字段 / config_variables / slots_data）
- 请求 Headers 补充 `Content-Type: application/json`

**`k8s/intent/router-intent-catalog/intents.json`**

- 所有 35 个意图的 `field_mapping` 更新为新格式
- field_mapping 三级前缀约定：
  - 无前缀 → 顶层字段（如 `session_id`）
  - `config_variables.xxx` → 独立变量条目
  - `config_variables.slots_data.xxx` → 聚合到 slots_data

#### Agent 端（6个 Agent）

**`backend/services/agents/*/src/*/support.py`**

- 新增 `ConfigVariablesRequest` 基类，提供 `get_config_value()` 和 `get_slots_data()` 方法
- 所有 6 个 Agent 的 `support.py` 统一添加该基类

**`backend/services/agents/*/src/*/service.py`**

- 请求模型继承 `ConfigVariablesRequest`
- 删除旧嵌套模型类（`TransferPayer`、`TransferPayee`、`TransferDetails` 等）
- 字段访问方式变更：

| 旧方式 | 新方式 |
|---|---|
| `request.input` | `request.txt` |
| `request.transfer.amount` | `request.get_slots_data().get("amount")` |
| `request.payee.name` | `request.get_slots_data().get("payee_name")` |

### 涉及的 Agent

- `transfer-money-agent`
- `account-balance-agent`
- `credit-card-repayment-agent`
- `forex-agent`
- `gas-bill-agent`
- `fallback-agent`

---

## 二、返回解析重构：SSE 流式格式兼容

### 支持的两种返回格式

#### 格式一：扁平 JSON（推荐，新 Agent 使用）

```
event:message
data:{"event":"final","content":"已向张三转账 500 CNY","ishandover":true,"status":"completed","slot_memory":{"amount":"500"}}

event:done
data:[DONE]
```

#### 格式二：嵌套格式（旧 Air Agent）

```
event:message
data:{"content":"","additional_kwargs":{"node_id":"end","node_output":{"output":"{\"isHandOver\":true,\"data\":[{\"answer\":\"||500||\"}]}"}}}
```

### 改动文件

**`backend/services/router-service/src/router_service/core/support/agent_client.py`**

1. **嵌套解析**：新增 `_extract_nested_output()` 方法，自动提取 `additional_kwargs.node_output.output` 嵌套结构
2. **字段兼容**：同时支持 `isHandOver` 和 `ishandover` 两种大小写
3. **内容提取**：新增 `_extract_content()` 方法，支持 `content` / `message` / `data[].answer` 多种来源
4. **响应增强**：空响应体检测、JSON 解析失败明确报错
5. **日志优化**：`print(flush=True)` 全部替换为 `logger.debug()`，避免阻塞事件循环

**`backend/services/router-service/src/router_service/core/graph/orchestrator.py`**

1. **SSE 流完整消费**（核心修复）：移除收到第一个终态 chunk 后的 `break`，改为消费完整 SSE 流
   - 原因：子智能体发送多个事件（每个节点一个），旧逻辑在第一个事件就退出
2. **日志降级**：调试日志从 `logger.info()` 降为 `logger.debug()`

---

## 三、E2E 验证

转账多轮对话测试通过：

```
python scripts/test_router_with_agents.py --base-url http://127.0.0.1:8000 \
  -m "帮我转账" -m "给小红" -m "转500"
```

结果：

```
[Message 1] "帮我转账"  → 识别 AG_TRANS，提示"请提供金额、收款人姓名"
[Message 2] "给小红"    → 提取 payee_name=小红，提示"请提供金额"
[Message 3] "转500"     → 提取 amount=500，返回"已向小红转账 500 CNY，转账成功"
```

---

## 四、合并注意事项

1. **`intents.json`** field_mapping 格式不兼容旧版，需全部更新
2. 所有 Agent 的 `service.py` 需同步更新请求模型
3. 调试时设置日志级别：`logging.getLogger("router_service").setLevel(logging.DEBUG)`
4. Python 版本要求 `>=3.12`
