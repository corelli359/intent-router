---
skill_id: transfer
version: 0.1.0
status: active
name: 转账
risk_level: high
keywords: [转账, 转给, 打钱, 汇款, 转, 打款]
---

# 转账 Skill

## Candidate Card

处理用户向收款人转账、打款、汇款的请求。必填参数是收款人和金额。

## Machine Spec

```json
{
  "description": "向指定收款人发起转账。",
  "allowed_capabilities": ["risk_check", "transfer"],
  "references": ["references/risk_rules.md", "references/transfer_limits.md"],
  "slots": [
    {
      "name": "recipient",
      "required": true,
      "description": "收款人姓名或收款对象",
      "prompt": "请问您要转给谁？",
      "extractor": {
        "type": "after_terms",
        "terms": ["转给", "给", "向"],
        "stop_terms": ["转账", "打钱", "汇款", "打款", "转", "支付"],
        "max_chars": 16,
        "accept_direct_reply": true
      }
    },
    {
      "name": "amount",
      "required": true,
      "description": "转账金额，单位元",
      "prompt": "请问要转多少钱？",
      "extractor": {
        "type": "number"
      }
    }
  ],
  "steps": [
    {
      "id": 1,
      "type": "collect_slots"
    },
    {
      "id": 2,
      "type": "api_call",
      "capability": "risk_check",
      "body_slots": ["recipient", "amount"]
    },
    {
      "id": 3,
      "type": "confirm",
      "message_template": "确认向{recipient}转账{amount}元？"
    },
    {
      "id": 4,
      "type": "api_call",
      "capability": "transfer",
      "body_slots": ["recipient", "amount"]
    },
    {
      "id": 5,
      "type": "final",
      "message_template": "已成功向{recipient}转账{amount}元，交易流水号：{transaction_id}"
    }
  ],
  "exception_messages": {
    "risk_check.insufficient_balance": "余额不足，当前可用余额为{available_balance}元，请调整转账金额。",
    "risk_check": "转账前风险检查未通过，请稍后再试。",
    "transfer": "转账提交失败，请稍后再试。"
  }
}
```

## 业务流程

### 步骤1：信息补全

缺少收款人或金额时追问用户，一次只问一个。

### 步骤2：风控检查

调用 `risk_check` capability。

### 步骤3：用户确认

展示收款人和金额，等待用户确认。

### 步骤4：执行转账

调用 `transfer` capability。

### 步骤5：反馈结果

返回转账结果和交易流水号。

## References

- references/risk_rules.md
- references/transfer_limits.md
