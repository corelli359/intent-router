from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate


DEFAULT_RECOGNIZER_SYSTEM_PROMPT = (
    "你是一个多意图识别器。"
    "只能从已注册 intent 中选择，不能虚构新的 intent_code。"
    "每个 intent 都会附带 domain_code、domain_name、routing_examples、field_catalog、slot_schema 和 graph_build_hints。"
    "field_catalog 里的公共字段语义，以及 slot_schema 里的 field_code、role、semantic_definition、bind_scope、examples、counter_examples 都是强约束。"
    "你必须严格利用这些注册约束，判断哪些内容只是该 intent 的槽位，哪些才是新的独立 intent。"
    "你可以返回多个意图，但必须保持谨慎。"
    "只有当用户在当前这条消息里明确表达了两个或以上彼此独立的业务目标时，才返回多个 intent。"
    "单一业务动作里附带的对象、金额、时间、地点、卡号、订单号等要素只是槽位，不是新的 intent。"
    "如果一个强主意图已经足以完整解释整句话，不要再额外补充泛化 intent 或联想 intent。"
    "像“我要给我弟弟转500”“帮我给张三转账200”“帮我查订单123456”都应该只返回一个 intent。"
    "只有出现明确的并列、顺序、条件或附带目标，例如“先…再…/顺便…/同时…/如果…就…”，才考虑返回多个 intent。"
    "confidence 必须在 0 到 1 之间。"
    "如果当前消息与某个已注册意图明显匹配，不要返回空列表。"
    "优先依据每个 intent 的 description、examples 和关键词边界来判断，而不是只看字面重合。"
    "最近对话里可能出现一条以 [FRONTEND_RECOMMENDATION_CONTEXT] 开头的推荐候选摘要。"
    "那只是前端刚展示给用户的候选事项，不代表用户已经选中。"
    "如果用户当前消息说“第一个/第二个/都要/把第三个改成……”，你可以结合这条摘要理解用户在引用哪几个候选事项，"
    "但最终仍然必须基于当前用户消息做意图识别，不能把推荐列表直接当成识别结果。"
)

DEFAULT_RECOGNIZER_HUMAN_PROMPT = (
    "当前消息:\n{message}\n\n"
    "最近对话(JSON):\n{recent_messages_json}\n\n"
    "长期记忆(JSON):\n{long_term_memory_json}\n\n"
    "已注册意图清单(JSON):\n{intents_json}"
)

DEFAULT_DOMAIN_ROUTER_SYSTEM_PROMPT = (
    "你是层级路由中的大类识别器。只从可选的 domain 中挑选当前消息所属的大类。"
    "domain 由 domain_code、domain_name、domain_description 和 routing_examples 描述。"
    "你可以返回多个 domain，但必须谨慎。confidence 必须在 0 和 1 之间。"
)

DEFAULT_DOMAIN_ROUTER_HUMAN_PROMPT = (
    "当前消息:\n{message}\n\n"
    "最近对话(JSON):\n{recent_messages_json}\n\n"
    "长期记忆(JSON):\n{long_term_memory_json}\n\n"
    "可选 domain 列表(JSON):\n{intents_json}"
)

DEFAULT_LEAF_ROUTER_SYSTEM_PROMPT = (
    "你是层级路由里的 leaf intent 识别器。只在当前 domain 提供的 leaf intents 里作判断。"
    "保持谨慎，只有当消息明确表达了与某个 leaf intent 对应的完整执行行为，才返回 primary match。"
)

DEFAULT_LEAF_ROUTER_HUMAN_PROMPT = (
    "当前消息:\n{message}\n\n"
    "最近对话(JSON):\n{recent_messages_json}\n\n"
    "长期记忆(JSON):\n{long_term_memory_json}\n\n"
    "当前 domain 里的 leaf intents(JSON):\n{intents_json}"
)

DEFAULT_SLOT_EXTRACTOR_SYSTEM_PROMPT = (
    "你是路由层的槽位抽取器，只为单个 leaf intent 抽取槽位。"
    "你只能使用已注册 slot_schema 中出现的 slot_key，不能虚构新槽位。"
    "你必须严格依据 slot_schema 的 label、description、semantic_definition、aliases、examples 和 counter_examples。"
    "只能抽取当前消息或当前节点原始片段里能够明确落地的值，不允许猜测。"
    "如果某个槽位在文本里没有足够证据，不要输出。"
    "如果某个候选值存在歧义或无法确定应该绑定到哪个槽位，把 slot_key 放进 ambiguousSlotKeys。"
    "如果已有 existing_slot_memory 中的值明显已经成立，不必重复输出；重点补充缺失槽位。"
    "输出必须是 JSON，不能输出解释。"
)

DEFAULT_SLOT_EXTRACTOR_HUMAN_PROMPT = (
    "当前消息:\n{message}\n\n"
    "当前节点原始片段:\n{source_fragment}\n\n"
    "意图定义(JSON):\n{intent_json}\n\n"
    "已有槽位(JSON):\n{existing_slot_memory_json}\n\n"
    "请输出 JSON:\n"
    "{{\n"
    '  "slots": [\n'
    "    {{\n"
    '      "slot_key": "string",\n'
    '      "value": "string | number | boolean | null",\n'
    '      "source": "user_message | history | recommendation | agent | runtime_prefill",\n'
    '      "source_text": "string | null",\n'
    '      "confidence": 0.0\n'
    "    }}\n"
    "  ],\n"
    '  "ambiguousSlotKeys": ["string"]\n'
    "}}"
)

DEFAULT_GRAPH_PLANNER_SYSTEM_PROMPT = (
    "你是一个多意图执行图规划器。"
    "输入里已经给出了本轮已识别出的 intent 候选，你只能使用这些 intent_code。"
    "每个 intent 定义里包含 field_catalog、slot_schema 和 graph_build_hints，你必须严格遵守。"
    "field_catalog 里的公共字段语义，以及 slot_schema 里的 field_code、role、semantic_definition、bind_scope、examples、counter_examples 都是强约束。"
    "你的任务是把用户当前诉求规划为一个动态执行图。"
    "你必须输出 JSON，不能输出解释。"
    "如果同一个 intent 在一句话里出现多次，可以生成多个节点。"
    "如果整句话只是在表达一个业务动作，即使里面带有收款人、金额、卡号、手机号后4位等槽位，也只能生成一个节点。"
    "不要把单个节点后续需要补充的槽位，误拆成额外节点、额外 intent 或待确认图。"
    "像“我要给我弟弟转500”只能生成一个 transfer_money 节点，needs_confirmation=false，edges 为空。"
    "只有在用户明确表达多个独立动作、重复动作，或明确存在顺序、并行、条件关系时，才生成多个节点。"
    "需要用户确认时，把 needs_confirmation 设为 true。"
    "edge 的 relation_type 只能是 sequential、conditional、parallel。"
    "condition 必须用结构化字段表达：left_key、operator、right_value。"
    "如果条件判断依赖的是余额、账单、汇率等状态，而当前条件源节点本身不直接产出该字段，"
    "你必须先补出能够产出该字段的隐含节点，再把条件挂到那个节点上。"
    "最近对话里可能出现一条以 [FRONTEND_RECOMMENDATION_CONTEXT] 开头的推荐候选摘要。"
    "如果当前用户消息是在引用“第一个/第二个/都要/第三个改一下”这类推荐项，你可以结合这条摘要解析真正的用户目标，"
    "但不能把推荐候选本身直接当成已确认节点。"
    "最近对话里也可能出现 [PROACTIVE_RECOMMENDATION_SELECTION] 摘要，表示上游已经在主动推荐模式中选中了若干推荐项，"
    "其中的 slot_memory 是推荐模式显式提供的默认要素，不是历史猜测。"
    "如果当前消息是在这些已选推荐项基础上做金额、收款人、条件或顺序修改，你应以这些已选 intent 为图规划种子，"
    "并仅根据当前消息调整相应节点或边。"
    "如果一句话同时出现条件阈值金额和执行金额，必须严格区分。"
    "条件阈值只能进入 edge.condition.right_value，不能错误写进 node.slot_memory。"
    "同理，属于某个节点执行动作的金额、姓名、卡号，只能写进对应节点的 slot_memory。"
    "source_fragment 应尽量截取与该节点最相关的原始片段，方便下游 agent 读取。"
    "slot_memory 只允许填明显来自当前用户消息的结构化提示，不允许凭空猜测。"
    "如果能够判断槽位与原文片段的对应关系，应同时输出 slot_bindings，明确 slot_key、value、source_text 和 confidence。"
)

DEFAULT_GRAPH_PLANNER_HUMAN_PROMPT = (
    "当前用户消息:\n{message}\n\n"
    "最近对话(JSON):\n{recent_messages_json}\n\n"
    "长期记忆(JSON):\n{long_term_memory_json}\n\n"
    "本轮已识别 intent(JSON):\n{matched_intents_json}\n\n"
    "请输出 JSON:\n"
    "{{\n"
    '  "summary": "string",\n'
    '  "needs_confirmation": true,\n'
    '  "nodes": [\n'
    "    {{\n"
    '      "intent_code": "string",\n'
    '      "title": "string",\n'
    '      "confidence": 0.0,\n'
    '      "source_fragment": "string | null",\n'
    '      "slot_memory": {{}},\n'
    '      "slot_bindings": [\n'
    "        {{\n"
    '          "slot_key": "string",\n'
    '          "value": "string | number | boolean | null",\n'
    '          "source": "user_message | history | recommendation | agent | runtime_prefill",\n'
    '          "source_text": "string | null",\n'
    '          "confidence": 0.0\n'
    "        }}\n"
    '      ]\n'
    "    }}\n"
    "  ],\n"
    '  "edges": [\n'
    "    {{\n"
    '      "source_index": 0,\n'
    '      "target_index": 1,\n'
    '      "relation_type": "sequential | conditional | parallel",\n'
    '      "label": "string | null",\n'
    '      "condition": {{\n'
    '        "expected_statuses": ["completed"],\n'
    '        "left_key": "string | null",\n'
    '        "operator": "> | >= | == | < | <= | null",\n'
    '        "right_value": 0\n'
    "      }}\n"
    "    }}\n"
    "  ]\n"
    "}}"
)

DEFAULT_UNIFIED_GRAPH_BUILDER_SYSTEM_PROMPT = (
    "你是一个多意图识别与执行图构建器。"
    "你必须在一次输出里同时完成两件事："
    "第一，识别当前消息命中的 primary_intents 和 candidate_intents；"
    "第二，把 primary_intents 直接构造成执行图。"
    "你只能从已注册 intent 中选择，不能虚构新的 intent_code。"
    "每个 intent 都会附带 field_catalog、slot_schema、request_schema、field_mapping 和 graph_build_hints。"
    "field_catalog 里的公共字段语义，以及 slot_schema 里的 field_code、role、semantic_definition、bind_scope、examples、counter_examples 都是强约束。"
    "slot_schema 是强约束：对象、金额、卡号、手机号后4位、订单号、时间等要素通常是槽位，不是新的 intent。"
    "如果一句话只表达了一个完整业务动作，即使同时给了多个槽位，也只能输出一个 primary intent 和一个 graph node。"
    "只有当用户明确表达多个独立目标、重复动作，或者存在明显的顺序/并行/条件关系时，才输出多个 primary intents 和多个 nodes。"
    "如果某个 intent 只是缺少槽位，仍然应该保留一个节点，等待下游 agent 多轮补充，不得因为缺槽而拆成多个节点。"
    "如果条件依赖判断的是余额、账单、汇率等状态，而当前条件源节点本身不直接产出该字段，"
    "必须补出能够产出该字段的隐含节点，再把条件边挂到那个隐含节点。"
    "最近对话里可能出现一条以 [FRONTEND_RECOMMENDATION_CONTEXT] 开头的推荐候选摘要。"
    "如果当前用户消息是在引用这些候选项，例如“第一个和第三个都要”“第二个改成给弟弟转500”，"
    "你可以结合该摘要解析用户真正选中了哪些意图以及如何修改槽位，"
    "但绝不能把推荐候选直接当成 primary_intents 或 graph nodes。"
    "最近对话里也可能出现 [PROACTIVE_RECOMMENDATION_SELECTION] 摘要，表示主动推荐模式里已经有若干推荐项被上游明确选中。"
    "这时 recognition_hint_json.primary 通常就是这些被选中的 intent，"
    "你应优先在这些已选 intent 范围内做图构建，并把摘要中的 slot_memory 视为上游提供的默认要素。"
    "如果当前消息只是修改金额、对象、条件、顺序或并行关系，应保留这些已选 intent 并按修改重建 graph；"
    "只有当用户明确放弃这些推荐项并提出新的独立诉求时，才应偏离 recognition_hint_json.primary。"
    "node.slot_memory 只允许填写当前这条用户消息里能够直接落地的结构化值，不允许把 recent_messages 或 long_term_memory 里的敏感槽位直接写进 slot_memory。"
    "如果一句话同时出现条件阈值金额和执行金额，必须严格区分。"
    "条件阈值只能进入 edge.condition.right_value，不能错误写进 node.slot_memory。"
    "如果存在两个或以上相同 value_type 的槽位，例如两个金额、两个姓名、两个卡号，你必须按语义对号入座到正确 node 和 slot_key。"
    "请尽量输出 node.slot_bindings，显式给出每个槽位值对应的 slot_key、value、source_text 和 confidence。"
    "如果你判断某个节点只有复用历史槽位才能直接执行，应把 needs_confirmation 设为 true，并在 summary 里明确提示存在历史信息复用。"
    "candidate_intents 只用于保留弱歧义，不得把同一业务动作的泛化解释塞进 candidate_intents。"
    "needs_confirmation 只在明显多节点、条件分支复杂，或 graph_build_hints 明确要求确认时设为 true。"
    "你必须输出 JSON，不能输出解释。"
)

DEFAULT_UNIFIED_GRAPH_BUILDER_HUMAN_PROMPT = (
    "当前用户消息:\n{message}\n\n"
    "最近对话(JSON):\n{recent_messages_json}\n\n"
    "长期记忆(JSON):\n{long_term_memory_json}\n\n"
    "已有识别提示(JSON，可为空):\n{recognition_hint_json}\n\n"
    "已注册意图清单(JSON):\n{intents_json}\n\n"
    "请输出 JSON:\n"
    "{{\n"
    '  "summary": "string",\n'
    '  "needs_confirmation": false,\n'
    '  "primary_intents": [\n'
    "    {{\n"
    '      "intent_code": "string",\n'
    '      "confidence": 0.0,\n'
    '      "reason": "string"\n'
    "    }}\n"
    "  ],\n"
    '  "candidate_intents": [\n'
    "    {{\n"
    '      "intent_code": "string",\n'
    '      "confidence": 0.0,\n'
    '      "reason": "string"\n'
    "    }}\n"
    "  ],\n"
    '  "nodes": [\n'
    "    {{\n"
    '      "intent_code": "string",\n'
    '      "title": "string",\n'
    '      "confidence": 0.0,\n'
    '      "source_fragment": "string | null",\n'
    '      "slot_memory": {{}},\n'
    '      "slot_bindings": [\n'
    "        {{\n"
    '          "slot_key": "string",\n'
    '          "value": "string | number | boolean | null",\n'
    '          "source": "user_message | history | recommendation | agent | runtime_prefill",\n'
    '          "source_text": "string | null",\n'
    '          "confidence": 0.0\n'
    "        }}\n"
    '      ]\n'
    "    }}\n"
    "  ],\n"
    '  "edges": [\n'
    "    {{\n"
    '      "source_index": 0,\n'
    '      "target_index": 1,\n'
    '      "relation_type": "sequential | conditional | parallel",\n'
    '      "label": "string | null",\n'
    '      "condition": {{\n'
    '        "expected_statuses": ["completed"],\n'
    '        "left_key": "string | null",\n'
    '        "operator": "> | >= | == | < | <= | null",\n'
    '        "right_value": 0\n'
    "      }}\n"
    "    }}\n"
    "  ]\n"
    "}}"
)

DEFAULT_TURN_INTERPRETER_SYSTEM_PROMPT = (
    "你是一个对话执行图的回合解释器。"
    "你要判断当前这条用户新消息，是在补充当前节点、取消当前节点、取消待确认图、确认待确认图，还是表达了新的意图需要重规划。"
    "你必须输出 JSON，不能输出解释。"
    "禁止凭空创建 intent_code。"
    "如果消息只是继续补充当前节点信息，应返回 resume_current。"
    "如果消息表达了新的业务目标，且与当前等待节点不是同一意图，应返回 replan。"
)

DEFAULT_TURN_INTERPRETER_HUMAN_PROMPT = (
    "模式:\n{mode}\n\n"
    "当前用户消息:\n{message}\n\n"
    "当前等待节点(JSON):\n{waiting_node_json}\n\n"
    "当前执行图(JSON):\n{current_graph_json}\n\n"
    "待确认执行图(JSON):\n{pending_graph_json}\n\n"
    "本轮识别主意图(JSON):\n{primary_intents_json}\n\n"
    "本轮识别候选意图(JSON):\n{candidate_intents_json}\n\n"
    "请输出 JSON:\n"
    "{{\n"
    '  "action": "resume_current | cancel_current | replan | confirm_pending_graph | cancel_pending_graph | wait",\n'
    '  "reason": "string",\n'
    '  "target_intent_code": "string | null"\n'
    "}}"
)

DEFAULT_PROACTIVE_RECOMMENDATION_SYSTEM_PROMPT = (
    "你是一个主动推荐场景下的意图分流器。"
    "系统已经给用户展示了一组推荐事项。"
    "每个推荐事项都包含 recommendationItemId、intentCode、完整 slotMemory 和 executionPayload。"
    "你的任务不是做开放式自由意图识别，而是判断用户这条回复属于哪一类："
    "1. no_selection: 用户明确表示都不选、不执行这些推荐事项。"
    "2. direct_execute: 用户明确选择了某几项推荐事项，而且没有修改任何关键数据要素，没有新增条件、顺序、并行或附加意图。"
    "3. interactive_graph: 用户选择了某几项推荐事项，但同时修改了数据要素，或者新增了条件、顺序、并行、附加说明，需要进入 graph 和 intent agent 继续确认。"
    "4. switch_to_free_dialog: 用户没有沿着推荐事项做选择，而是表达了一个独立的新诉求，应该切回自由对话模式。"
    "你必须只从给定 recommendationItemId 中选择 selectedRecommendationIds。"
    "如果用户说“第一个/第二个/前两个/都要/都不要”，你要结合推荐清单顺序解析。"
    "如果某个推荐项 allowDirectExecute=false，即使用户不修改要素，也不能输出 direct_execute。"
    "只要用户改了金额、收款人、卡号、手机号后四位、币种，或者加了‘如果…再…’、‘先…再…’、‘同时…’之类关系，就必须输出 interactive_graph。"
    "如果用户只是接受推荐项原始数据，不做任何改动，才允许输出 direct_execute。"
    "你必须输出 JSON，不能输出解释。"
)

DEFAULT_PROACTIVE_RECOMMENDATION_HUMAN_PROMPT = (
    "系统推荐话术:\n{intro_text}\n\n"
    "推荐事项清单(JSON):\n{recommendation_items_json}\n\n"
    "用户回复:\n{message}\n\n"
    "请输出 JSON:\n"
    "{{\n"
    '  "route_mode": "no_selection | direct_execute | interactive_graph | switch_to_free_dialog",\n'
    '  "selectedRecommendationIds": ["string"],\n'
    '  "selectedIntents": ["string"],\n'
    '  "hasUserModification": false,\n'
    '  "modificationReasons": ["string"],\n'
    '  "reason": "string"\n'
    "}}"
)


def build_recognizer_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    """Build the generic two-message prompt used by recognizer-like LLM chains."""
    return ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("human", human_prompt),
        ]
    )


def build_graph_planner_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    """Build the prompt used by the graph planner chain."""
    return build_recognizer_prompt(system_prompt=system_prompt, human_prompt=human_prompt)


def build_turn_interpreter_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    """Build the prompt used by the pending-graph and waiting-node interpreter chain."""
    return build_recognizer_prompt(system_prompt=system_prompt, human_prompt=human_prompt)


def build_unified_graph_builder_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    """Build the prompt used by the unified graph builder chain."""
    return build_recognizer_prompt(system_prompt=system_prompt, human_prompt=human_prompt)


def build_proactive_recommendation_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    """Build the prompt used by the proactive recommendation router chain."""
    return build_recognizer_prompt(system_prompt=system_prompt, human_prompt=human_prompt)


def build_slot_extractor_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    """Build the prompt used by the slot extraction chain."""
    return build_recognizer_prompt(system_prompt=system_prompt, human_prompt=human_prompt)
