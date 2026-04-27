+++
[[routes]]
intent_id = "transfer"
scene_id = "transfer"
description = "转账意图进入转账执行场景，由 transfer-agent 处理。"

[[routes]]
intent_id = "balance_query"
scene_id = "balance_query"
description = "余额查询意图进入余额查询执行场景。"

[[routes]]
intent_id = "fund_query"
scene_id = "fund_query"
description = "基金查询意图进入基金查询执行场景。"
+++

# Intent Routes

Router 在意图识别完成后读取本映射，把 `intent_id` 映射为执行 `scene_id`。这个文件不描述业务槽位，也不描述 Agent 的 Skill 生命周期。
