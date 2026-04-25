---
skill_code: transfer_money
version: 1.0.0
status: active
name: 转账
domain: payment
risk_level: high
executor: agent_http.transfer_money
keywords: [转账, 转给, 打钱, 汇款, 转]
---

# Transfer Money

## Candidate Card

Transfer money helps the user send money to a receiver.

Use this skill when the user says they want to transfer money, send money, remit,
or move money to another person.

Required slots: `receiver_name`, `amount`.

## Machine Spec

```json
{
  "required_slots": ["receiver_name", "amount"],
  "slot_extractors": {
    "receiver_name": {
      "type": "after_terms",
      "terms": ["给", "向", "转给"],
      "stop_terms": ["转账", "打钱", "汇款", "转", "支付"],
      "max_chars": 12
    },
    "amount": {
      "type": "number",
      "units": ["元", "块", "块钱"]
    }
  },
  "confirmation": {
    "amount_gt": 1000,
    "message_template": "将向{receiver_name}转账{amount}元，请确认是否执行。"
  },
  "presentation": {
    "missing_slot_prompts": {
      "receiver_name": "请补充收款人。",
      "amount": "请补充转账金额。"
    }
  }
}
```

