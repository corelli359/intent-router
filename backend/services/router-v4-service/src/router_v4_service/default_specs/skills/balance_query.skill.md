# Skill: balance_query

## Metadata

- skill_id: balance_query
- version: 0.1.0
- owner_agent: balance-agent
- task_type: balance_query

## Boundary

This Skill only handles balance query execution. Router passes `skill_ref` and raw context but does not load this Skill body during intent recognition.

## Inputs

- router_session_id
- task_id
- intent_id
- scene_id
- raw_message
- context_refs

## State

- account_scope
- auth_status
- query_status

## Steps

1. Verify the task belongs to balance query.
2. Resolve account scope from user expression and available context.
3. Perform authentication and permission checks.
4. Query balance service.
5. Return structured balance data.

## Slot Policy

Account scope is owned by balance-agent. Router must not extract account scope.

## Handover

If the task does not belong to balance query, return `ishandover=true` and `output.data=[]`.

## Output Contract

Completed output contains `data[0].type=balance`.
