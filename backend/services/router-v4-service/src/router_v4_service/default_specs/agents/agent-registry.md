+++
[[agents]]
agent_id = "transfer-agent"
endpoint = "mock://transfer-agent"
accepted_scene_ids = ["transfer"]
task_schema = "transfer_agent_task.v1"
event_schema = "agent_task_event.v1"
supports_stream = true

[[agents]]
agent_id = "fund-agent"
endpoint = "mock://fund-agent"
accepted_scene_ids = ["fund_query"]
task_schema = "fund_agent_task.v1"
event_schema = "agent_task_event.v1"
supports_stream = true

[[agents]]
agent_id = "balance-agent"
endpoint = "mock://balance-agent"
accepted_scene_ids = ["balance_query"]
task_schema = "balance_agent_task.v1"
event_schema = "agent_task_event.v1"
supports_stream = true

[[agents]]
agent_id = "fallback-agent"
endpoint = "mock://fallback-agent"
accepted_scene_ids = ["fallback"]
task_schema = "fallback_agent_task.v1"
event_schema = "fallback_agent_task_event.v1"
supports_stream = true
+++

# Agent Registry

Execution-agent registry for Router V4 dispatch. This markdown file is the source registry; Router parses its frontmatter at startup.

