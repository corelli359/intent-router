---
spec_code: agent_http
version: 0.1.0
---

# Agent HTTP Executors

This demo uses mock agent-http executors so it can run offline.

## Machine Spec

```json
{
  "executors": {
    "agent_http.transfer_money": {
      "type": "mock_agent_http",
      "response_template": "已为你向{receiver_name}发起{amount}元转账。"
    },
    "agent_http.account_balance": {
      "type": "mock_agent_http",
      "response_template": "你的当前可用余额为 12,345.67 元。"
    }
  }
}
```

