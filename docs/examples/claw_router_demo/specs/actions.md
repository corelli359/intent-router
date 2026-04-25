---
spec_code: actions
version: 0.1.0
---

# Action Contract

The decision engine may emit only these actions.

## Machine Spec

```json
{
  "actions": {
    "ask_user": {
      "required": ["skill_code", "missing_slots", "message"]
    },
    "execute_skill": {
      "required": ["skill_code", "slots"]
    },
    "final": {
      "required": ["message"]
    },
    "cancel": {
      "required": ["message"]
    }
  }
}
```

