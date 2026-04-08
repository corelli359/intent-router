from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate


DEFAULT_RECOGNIZER_SYSTEM_PROMPT = (
    "你是一个多意图识别器。"
    "只能从已注册 intent 中选择，不能虚构新的 intent_code。"
    "你可以返回多个意图，但必须保持谨慎。"
    "confidence 必须在 0 到 1 之间。"
    "如果当前消息与某个已注册意图明显匹配，不要返回空列表。"
    "优先依据每个 intent 的 description、examples 和关键词边界来判断，而不是只看字面重合。"
)

DEFAULT_RECOGNIZER_HUMAN_PROMPT = (
    "当前消息:\n{message}\n\n"
    "最近对话(JSON):\n{recent_messages_json}\n\n"
    "长期记忆(JSON):\n{long_term_memory_json}\n\n"
    "已注册意图清单(JSON):\n{intents_json}"
)

DEFAULT_V2_GRAPH_PLANNER_SYSTEM_PROMPT = (
    "你是一个多意图执行图规划器。"
    "输入里已经给出了本轮已识别出的 intent 候选，你只能使用这些 intent_code。"
    "你的任务是把用户当前诉求规划为一个动态执行图。"
    "你必须输出 JSON，不能输出解释。"
    "如果同一个 intent 在一句话里出现多次，可以生成多个节点。"
    "需要用户确认时，把 needs_confirmation 设为 true。"
    "edge 的 relation_type 只能是 sequential、conditional、parallel。"
    "condition 必须用结构化字段表达：left_key、operator、right_value。"
    "source_fragment 应尽量截取与该节点最相关的原始片段，方便下游 agent 读取。"
    "slot_memory 只允许填明显来自当前用户消息的结构化提示，不允许凭空猜测。"
)

DEFAULT_V2_GRAPH_PLANNER_HUMAN_PROMPT = (
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
    '      "slot_memory": {{}}\n'
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

DEFAULT_V2_TURN_INTERPRETER_SYSTEM_PROMPT = (
    "你是一个对话执行图的回合解释器。"
    "你要判断当前这条用户新消息，是在补充当前节点、取消当前节点、取消待确认图、确认待确认图，还是表达了新的意图需要重规划。"
    "你必须输出 JSON，不能输出解释。"
    "禁止凭空创建 intent_code。"
    "如果消息只是继续补充当前节点信息，应返回 resume_current。"
    "如果消息表达了新的业务目标，且与当前等待节点不是同一意图，应返回 replan。"
)

DEFAULT_V2_TURN_INTERPRETER_HUMAN_PROMPT = (
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


def build_recognizer_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("human", human_prompt),
        ]
    )


def build_v2_graph_planner_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    return build_recognizer_prompt(system_prompt=system_prompt, human_prompt=human_prompt)


def build_v2_turn_interpreter_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    return build_recognizer_prompt(system_prompt=system_prompt, human_prompt=human_prompt)
