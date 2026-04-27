# Skill: transfer

## Metadata

- skill_id: transfer
- version: 0.1.0
- owner_agent: transfer-agent
- task_type: transfer

## Boundary

This Skill only handles transfer execution. Router may pass `skill_ref` and the raw user message, but Router must not load this Skill body during intent recognition.

The Skill owns recipient extraction, amount extraction, missing-field prompts, confirmation, risk check, limit check, transfer API invocation, and structured output.

## Inputs

- router_session_id
- task_id
- intent_id
- scene_id
- raw_message
- source
- push_context
- context_refs

## State

- recipient: collected by transfer-agent
- amount: collected by transfer-agent
- currency: default CNY
- skill_step: start | collecting_transfer_fields | waiting_confirmation | completed | cancelled | handover

## Steps

1. Load Router task snapshot and verify `scene_id=transfer`.
2. If the task is not transfer, return `ishandover=true` and `output.data=[]`.
3. Read the current user expression as free text.
4. Extract all available transfer fields in one pass.
5. If recipient or amount is missing, ask for all missing fields in one response.
6. If recipient and amount are complete, ask for user confirmation.
7. After confirmation, run risk check, limit check, and transfer API.
8. Submit structured output to Router by `agent-output`.

## Slot Policy

- recipient: payee name, alias, or resolvable payee object.
- amount: explicit transfer amount from the user expression.
- If the user says only "我要转账", ask: "可以，请告诉我转给谁、转账金额是多少？"
- If the user says "我要转账300给小红", collect both fields and enter confirmation.

## Handover

When the task does not belong to this Skill:

```json
{
  "ishandover": true,
  "output": {"data": []}
}
```

## Output Contract

Running:

```json
{
  "status": "running",
  "assistant_message": "string"
}
```

Completed:

```json
{
  "status": "completed",
  "output": {
    "data": [{"type": "transfer_result", "status": "success"}],
    "risk": {"status": "passed"},
    "limit": {"status": "passed"},
    "business_api": {"name": "transfer.submit", "status": "success"}
  }
}
```
