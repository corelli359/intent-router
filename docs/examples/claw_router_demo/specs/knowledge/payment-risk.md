---
spec_code: payment_risk_knowledge
version: 0.1.0
domain: payment
---

# Payment Risk Knowledge

This knowledge chunk is loaded only when the current payment run needs risk
context.

## Machine Spec

```json
{
  "chunks": {
    "large_transfer_notice": "大额转账属于高风险操作，执行前必须获得用户明确确认。"
  }
}
```

