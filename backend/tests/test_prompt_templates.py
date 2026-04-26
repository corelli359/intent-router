from __future__ import annotations

import sys
from pathlib import Path


from router_service.core.prompts.prompt_templates import (  # noqa: E402
    DEFAULT_DOMAIN_ROUTER_HUMAN_PROMPT,
    DEFAULT_DOMAIN_ROUTER_SYSTEM_PROMPT,
    DEFAULT_LEAF_ROUTER_HUMAN_PROMPT,
    DEFAULT_LEAF_ROUTER_SYSTEM_PROMPT,
    DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
    DEFAULT_GRAPH_PLANNER_HUMAN_PROMPT,
    DEFAULT_GRAPH_PLANNER_SYSTEM_PROMPT,
    DEFAULT_SLOT_EXTRACTOR_HUMAN_PROMPT,
    DEFAULT_SLOT_EXTRACTOR_SYSTEM_PROMPT,
    DEFAULT_TURN_INTERPRETER_HUMAN_PROMPT,
    DEFAULT_TURN_INTERPRETER_SYSTEM_PROMPT,
    DEFAULT_UNIFIED_GRAPH_BUILDER_HUMAN_PROMPT,
    DEFAULT_UNIFIED_GRAPH_BUILDER_SYSTEM_PROMPT,
    build_recognizer_prompt,
    build_graph_planner_prompt,
    build_slot_extractor_prompt,
    build_turn_interpreter_prompt,
    build_unified_graph_builder_prompt,
)


def test_recognizer_prompt_explicitly_prevents_single_action_over_split() -> None:
    prompt = build_recognizer_prompt(
        system_prompt=DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
        human_prompt=DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    )

    messages = prompt.format_messages(
        message="我要给我弟弟转500",
        recent_messages_json="[]",
        long_term_memory_json="[]",
        intents_json="[]",
    )

    assert len(messages) == 2
    assert "只返回一个 intent" in messages[0].content
    assert "routing_examples" in messages[0].content
    assert "field_catalog" in messages[0].content
    assert "我要给我弟弟转500" in messages[1].content
    assert "已注册意图清单" in messages[1].content


def test_domain_router_prompt_accepts_expected_variables() -> None:
    prompt = build_recognizer_prompt(
        system_prompt=DEFAULT_DOMAIN_ROUTER_SYSTEM_PROMPT,
        human_prompt=DEFAULT_DOMAIN_ROUTER_HUMAN_PROMPT,
    )

    messages = prompt.format_messages(
        message="我要交电费",
        recent_messages_json="[]",
        long_term_memory_json="[]",
        intents_json='[{"intent_code":"payment"}]',
    )

    assert len(messages) == 2
    assert "大类识别器" in messages[0].content
    assert "routing_examples" in messages[0].content
    assert "可选 domain 列表" in messages[1].content


def test_leaf_router_prompt_accepts_expected_variables() -> None:
    prompt = build_recognizer_prompt(
        system_prompt=DEFAULT_LEAF_ROUTER_SYSTEM_PROMPT,
        human_prompt=DEFAULT_LEAF_ROUTER_HUMAN_PROMPT,
    )

    messages = prompt.format_messages(
        message="我要交电费",
        recent_messages_json="[]",
        long_term_memory_json="[]",
        intents_json='[{"intent_code":"pay_electricity"}]',
    )

    assert len(messages) == 2
    assert "leaf intent 识别器" in messages[0].content
    assert "当前 domain 里的 leaf intents" in messages[1].content


def test_v2_graph_planner_prompt_accepts_expected_variables() -> None:
    prompt = build_graph_planner_prompt(
        system_prompt=DEFAULT_GRAPH_PLANNER_SYSTEM_PROMPT,
        human_prompt=DEFAULT_GRAPH_PLANNER_HUMAN_PROMPT,
    )

    messages = prompt.format_messages(
        message="帮我查一下余额，如果超过5000，就跟我媳妇儿转1000",
        recent_messages_json="[]",
        long_term_memory_json="[]",
        matched_intents_json="[]",
    )

    assert len(messages) == 2
    assert "needs_confirmation=false" in messages[0].content
    assert "field_catalog、slot_schema 和 graph_build_hints" in messages[0].content
    assert "field_code、role、semantic_definition" in messages[0].content
    assert "条件阈值只能进入 edge.condition.right_value" in messages[0].content
    assert "summary" in messages[1].content
    assert '"slot_memory": {}' in messages[1].content
    assert '"slot_bindings"' in messages[1].content


def test_v2_turn_interpreter_prompt_accepts_expected_variables() -> None:
    prompt = build_turn_interpreter_prompt(
        system_prompt=DEFAULT_TURN_INTERPRETER_SYSTEM_PROMPT,
        human_prompt=DEFAULT_TURN_INTERPRETER_HUMAN_PROMPT,
    )

    messages = prompt.format_messages(
        mode="waiting_node",
        message="算了，先不要转了",
        waiting_node_json="{}",
        current_graph_json="{}",
        pending_graph_json="null",
        primary_intents_json="[]",
        candidate_intents_json="[]",
    )

    assert len(messages) == 2
    assert "resume_current" in messages[1].content
    assert '"target_intent_code": "string | null"' in messages[1].content


def test_v2_slot_extractor_prompt_accepts_recent_messages_and_existing_slots() -> None:
    prompt = build_slot_extractor_prompt(
        system_prompt=DEFAULT_SLOT_EXTRACTOR_SYSTEM_PROMPT,
        human_prompt=DEFAULT_SLOT_EXTRACTOR_HUMAN_PROMPT,
    )

    messages = prompt.format_messages(
        message="200",
        source_fragment="我要转账",
        recent_messages_json='["user: 我要转账","assistant: 请提供金额"]',
        intent_json='{"intent_code":"AG_TRANS"}',
        existing_slot_memory_json='{"payee_name":"小红"}',
    )

    assert len(messages) == 2
    assert "recent_messages 只用于帮助你理解当前轮和既有槽位之间的上下文连续性" in messages[0].content
    assert "我要转壹贰叁肆给姐姐" in messages[0].content
    assert "最近对话(JSON)" in messages[1].content
    assert '"payee_name":"小红"' in messages[1].content


def test_v2_unified_graph_builder_prompt_accepts_expected_variables() -> None:
    prompt = build_unified_graph_builder_prompt(
        system_prompt=DEFAULT_UNIFIED_GRAPH_BUILDER_SYSTEM_PROMPT,
        human_prompt=DEFAULT_UNIFIED_GRAPH_BUILDER_HUMAN_PROMPT,
    )

    messages = prompt.format_messages(
        message="帮我查一下余额，如果超过5000，就给我媳妇儿转1000",
        recent_messages_json="[]",
        long_term_memory_json='["历史上收款人常见为我媳妇儿"]',
        recognition_hint_json="null",
        intents_json="[]",
    )

    assert len(messages) == 2
    assert "slot_schema 是强约束" in messages[0].content
    assert "我要转壹贰叁肆给姐姐" in messages[0].content
    assert "field_catalog" in messages[0].content
    assert "请尽量输出 node.slot_bindings" in messages[0].content
    assert "条件阈值只能进入 edge.condition.right_value" in messages[0].content
    assert '"primary_intents"' in messages[1].content
    assert '"candidate_intents"' in messages[1].content
    assert '"edges"' in messages[1].content
    assert '"slot_bindings"' in messages[1].content


def test_v2_graph_planner_prompt_requires_tail_payee_and_amount_on_same_node() -> None:
    prompt = build_graph_planner_prompt(
        system_prompt=DEFAULT_GRAPH_PLANNER_SYSTEM_PROMPT,
        human_prompt=DEFAULT_GRAPH_PLANNER_HUMAN_PROMPT,
    )

    messages = prompt.format_messages(
        message="我要转壹贰叁肆给姐姐",
        recent_messages_json="[]",
        long_term_memory_json="[]",
        matched_intents_json="[]",
    )

    assert len(messages) == 2
    assert "我要转壹贰叁肆给姐姐" in messages[0].content
    assert "amount 与 payee_name 必须都落在同一个节点里" in messages[0].content
