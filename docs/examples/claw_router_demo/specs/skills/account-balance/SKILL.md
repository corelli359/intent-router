---
skill_code: account_balance
version: 1.0.0
status: active
name: 查询余额
domain: payment
risk_level: low
executor: agent_http.account_balance
keywords: [余额, 查询余额, 账户余额, 还有多少钱]
---

# Account Balance

## Candidate Card

Account balance helps the user check available balance.

Required slots: none.

## Machine Spec

```json
{
  "required_slots": [],
  "slot_extractors": {},
  "presentation": {
    "missing_slot_prompts": {}
  }
}
```

