+++
[[agents]]
agent_id = "transfer-agent"
endpoint = "local://transfer-agent"
accepted_scene_ids = ["transfer"]
task_schema = "transfer_agent_task.v1"
event_schema = "agent_task_event.v1"
supports_stream = true

[[agents]]
agent_id = "fund-agent"
endpoint = "local://fund-agent"
accepted_scene_ids = ["fund_query"]
task_schema = "fund_agent_task.v1"
event_schema = "agent_task_event.v1"
supports_stream = true

[[agents]]
agent_id = "balance-agent"
endpoint = "local://balance-agent"
accepted_scene_ids = ["balance_query"]
task_schema = "balance_agent_task.v1"
event_schema = "agent_task_event.v1"
supports_stream = true

[[agents]]
agent_id = "fallback-agent"
endpoint = "local://fallback-agent"
accepted_scene_ids = ["fallback"]
task_schema = "fallback_agent_task.v1"
event_schema = "fallback_agent_task_event.v1"
supports_stream = true
+++

# Agent Registry

Execution-agent registry for Router V4 dispatch. `local://` means the task is owned by the in-process Skill ReAct runtime for demo deployment. Production deployments should replace it with HTTP/RPC agent or tool endpoints.
