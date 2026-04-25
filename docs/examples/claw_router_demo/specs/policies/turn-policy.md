---
spec_code: turn_policy
version: 0.1.0
---

# Turn Policy

This policy governs short follow-up messages while the harness is waiting for
slot input or confirmation.

## Machine Spec

```json
{
  "confirm_terms": ["确认", "可以", "好的", "好", "是", "执行"],
  "cancel_terms": ["取消", "不要", "算了", "不用了"]
}
```

