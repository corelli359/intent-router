# Skill: fund_query

## Metadata

- skill_id: fund_query
- version: 0.1.0
- owner_agent: fund-agent
- task_type: fund_query

## Boundary

This Skill only handles fund query execution. Router passes `skill_ref` and raw context but does not load this Skill body during intent recognition.

## Inputs

- router_session_id
- task_id
- intent_id
- scene_id
- raw_message
- context_refs

## State

- fund_scope
- product_id
- query_status
- suitability_status

## Steps

1. Verify the task belongs to fund query.
2. Resolve fund product, code, or product scope.
3. Query product profile, risk level, net value, holdings, or return data.
4. Apply suitability and disclosure rules if required.
5. Return structured fund data.

## Slot Policy

Fund product resolution is owned by fund-agent. Router must not extract product fields.

## Handover

If the task does not belong to fund query, return `ishandover=true` and `output.data=[]`.

## Output Contract

Completed output contains `data[0].type=fund`.
