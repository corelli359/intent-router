from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from router_core.domain import IntentDefinition  # noqa: E402
from router_core.v2_domain import GraphStatus  # noqa: E402
from router_core.v2_graph_builder import GraphDraftNormalizer, LLMIntentGraphBuilder, UnifiedGraphDraftPayload  # noqa: E402


class _StaticLLMClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def run_json(self, *, prompt, variables, model=None, on_delta=None):
        return self.payload


def _transfer_intent(*, confirm_policy: str = "auto") -> IntentDefinition:
    return IntentDefinition(
        intent_code="transfer_money",
        name="转账",
        description="执行转账，需要收款人、金额、收款卡号和手机号后4位。",
        examples=["给我弟弟转500"],
        keywords=["转账"],
        agent_url="http://agent.example.com/transfer",
        dispatch_priority=100,
        primary_threshold=0.75,
        candidate_threshold=0.5,
        slot_schema=[
            {
                "slot_key": "recipient_name",
                "label": "收款人",
                "description": "收款人姓名",
                "value_type": "person_name",
                "required": True,
            },
            {
                "slot_key": "amount",
                "label": "转账金额",
                "description": "本次转账金额",
                "value_type": "currency",
                "required": True,
            },
            {
                "slot_key": "recipient_card_number",
                "label": "收款卡号",
                "description": "收款银行卡号",
                "value_type": "account_number",
                "required": False,
                "allow_from_history": False,
            },
        ],
        graph_build_hints={
            "intent_scope_rule": "单次转账动作是一个 intent。",
            "planner_notes": "不要把收款人、金额等槽位拆成额外节点。",
            "confirm_policy": confirm_policy,
        },
    )


def _balance_intent(
    *,
    confirm_policy: str = "auto",
    allow_from_history: bool = True,
) -> IntentDefinition:
    return IntentDefinition(
        intent_code="query_account_balance",
        name="查询账户余额",
        description="查询账户余额，需要卡号和手机号后4位。",
        examples=["帮我查一下余额"],
        keywords=["余额"],
        agent_url="http://agent.example.com/balance",
        dispatch_priority=100,
        primary_threshold=0.75,
        candidate_threshold=0.5,
        slot_schema=[
            {
                "slot_key": "card_number",
                "label": "卡号",
                "description": "银行卡号",
                "value_type": "account_number",
                "required": True,
                "allow_from_history": allow_from_history,
            },
            {
                "slot_key": "phone_last_four",
                "label": "手机号后4位",
                "description": "绑定手机号后4位",
                "value_type": "phone_last4",
                "required": True,
                "allow_from_history": allow_from_history,
            },
        ],
        graph_build_hints={
            "intent_scope_rule": "余额查询是单个 intent。",
            "planner_notes": "卡号和手机号后4位是槽位，不是独立节点。",
            "confirm_policy": confirm_policy,
        },
    )


def test_graph_draft_normalizer_honors_single_node_intent_output() -> None:
    normalizer = GraphDraftNormalizer()
    result = normalizer.normalize(
        payload=UnifiedGraphDraftPayload.model_validate(
            {
                "summary": "识别到 1 个事项",
                "needs_confirmation": False,
                "primary_intents": [
                    {"intent_code": "transfer_money", "confidence": 0.96, "reason": "single transfer action"}
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "transfer_money",
                        "title": "给我弟弟转500",
                        "confidence": 0.96,
                        "source_fragment": "给我弟弟转500",
                        "slot_memory": {"recipient_name": "我弟弟", "amount": "500"},
                    }
                ],
                "edges": [],
            }
        ),
        message="给我弟弟转500",
        intents_by_code={"transfer_money": _transfer_intent()},
    )

    assert [match.intent_code for match in result.recognition.primary] == ["transfer_money"]
    assert len(result.graph.nodes) == 1
    assert result.graph.nodes[0].intent_code == "transfer_money"
    assert result.graph.status == GraphStatus.DRAFT


def test_graph_draft_normalizer_marks_history_slot_reuse_for_confirmation() -> None:
    normalizer = GraphDraftNormalizer()
    result = normalizer.normalize(
        payload=UnifiedGraphDraftPayload.model_validate(
            {
                "summary": "识别到余额查询",
                "needs_confirmation": False,
                "primary_intents": [
                    {"intent_code": "query_account_balance", "confidence": 0.95, "reason": "reuse history"}
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "query_account_balance",
                        "title": "查询账户余额",
                        "confidence": 0.95,
                        "source_fragment": "帮我查一下余额",
                        "slot_memory": {
                            "card_number": "6222021234567890",
                            "phone_last_four": "1234",
                        },
                    }
                ],
                "edges": [],
            }
        ),
        message="帮我查一下余额",
        intents_by_code={"query_account_balance": _balance_intent()},
        recent_messages=["user: 上次查询使用卡号 6222021234567890，尾号 1234"],
    )

    assert result.graph.status == GraphStatus.WAITING_CONFIRMATION
    assert result.graph.nodes[0].slot_memory == {
        "card_number": "6222021234567890",
        "phone_last_four": "1234",
    }
    assert result.graph.nodes[0].history_slot_keys == ["card_number", "phone_last_four"]
    assert "检测到历史信息复用" in result.graph.summary


def test_graph_draft_normalizer_drops_disallowed_history_slots() -> None:
    normalizer = GraphDraftNormalizer()
    result = normalizer.normalize(
        payload=UnifiedGraphDraftPayload.model_validate(
            {
                "summary": "识别到 1 个事项",
                "needs_confirmation": False,
                "primary_intents": [
                    {"intent_code": "transfer_money", "confidence": 0.96, "reason": "single transfer action"}
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "transfer_money",
                        "title": "给我弟弟转500",
                        "confidence": 0.96,
                        "source_fragment": "给我弟弟转500",
                        "slot_memory": {
                            "recipient_name": "我弟弟",
                            "amount": "500",
                            "recipient_card_number": "6222021234567890",
                        },
                    }
                ],
                "edges": [],
            }
        ),
        message="给我弟弟转500",
        intents_by_code={"transfer_money": _transfer_intent()},
    )

    assert result.graph.status == GraphStatus.DRAFT
    assert result.graph.nodes[0].slot_memory == {
        "recipient_name": "我弟弟",
        "amount": "500",
    }
    assert result.graph.nodes[0].history_slot_keys == []


def test_unified_graph_builder_can_force_confirmation_from_intent_hints() -> None:
    builder = LLMIntentGraphBuilder(
        _StaticLLMClient(
            {
                "summary": "识别到 1 个高风险事项",
                "needs_confirmation": False,
                "primary_intents": [
                    {"intent_code": "transfer_money", "confidence": 0.97, "reason": "matched transfer intent"}
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "transfer_money",
                        "title": "给我媳妇儿转1000",
                        "confidence": 0.97,
                        "source_fragment": "给我媳妇儿转1000",
                        "slot_memory": {"recipient_name": "我媳妇儿", "amount": "1000"},
                    }
                ],
                "edges": [],
            }
        ),
    )

    async def run() -> None:
        result = await builder.build(
            message="给我媳妇儿转1000",
            intents=[_transfer_intent(confirm_policy="always")],
            recent_messages=[],
            long_term_memory=[],
        )

        assert len(result.graph.nodes) == 1
        assert result.graph.status == GraphStatus.WAITING_CONFIRMATION
        assert result.graph.actions[0].code == "confirm_graph"

    asyncio.run(run())


def test_unified_graph_builder_propagates_retryable_llm_errors() -> None:
    class _FakeRateLimitError(Exception):
        def __init__(self) -> None:
            super().__init__("rate limited")
            self.status_code = 429

    class _RateLimitedLLMClient:
        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            del prompt, variables, model, on_delta
            raise _FakeRateLimitError()

    builder = LLMIntentGraphBuilder(_RateLimitedLLMClient())

    async def run() -> None:
        with pytest.raises(_FakeRateLimitError):
            await builder.build(
                message="帮我查一下余额",
                intents=[_balance_intent()],
                recent_messages=[],
                long_term_memory=[],
            )

    asyncio.run(run())


def test_graph_draft_normalizer_requires_confirmation_when_reusing_history_slots() -> None:
    normalizer = GraphDraftNormalizer()
    result = normalizer.normalize(
        payload=UnifiedGraphDraftPayload.model_validate(
            {
                "summary": "识别到 1 个事项",
                "needs_confirmation": False,
                "primary_intents": [
                    {"intent_code": "query_account_balance", "confidence": 0.96, "reason": "reuse history"}
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "query_account_balance",
                        "title": "查询账户余额",
                        "confidence": 0.96,
                        "source_fragment": "帮我查一下余额，如果余额够就转账",
                        "slot_memory": {
                            "card_number": "6222021234567890",
                            "phone_last_four": "1234",
                        },
                    }
                ],
                "edges": [],
            }
        ),
        message="帮我查一下余额，如果余额够就转账",
        intents_by_code={"query_account_balance": _balance_intent(allow_from_history=True)},
        recent_messages=["user: 卡号 6222021234567890，手机号后四位 1234"],
        long_term_memory=[],
    )

    assert result.graph.status == GraphStatus.WAITING_CONFIRMATION
    assert result.graph.nodes[0].history_slot_keys == ["card_number", "phone_last_four"]
    assert "检测到历史信息复用" in result.graph.summary


def test_graph_draft_normalizer_drops_unconfirmed_sensitive_slots_when_history_reuse_is_disabled() -> None:
    normalizer = GraphDraftNormalizer()
    result = normalizer.normalize(
        payload=UnifiedGraphDraftPayload.model_validate(
            {
                "summary": "识别到 1 个事项",
                "needs_confirmation": False,
                "primary_intents": [
                    {"intent_code": "query_account_balance", "confidence": 0.96, "reason": "history not allowed"}
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "query_account_balance",
                        "title": "查询账户余额",
                        "confidence": 0.96,
                        "source_fragment": "帮我查一下余额",
                        "slot_memory": {
                            "card_number": "6222021234567890",
                            "phone_last_four": "1234",
                        },
                    }
                ],
                "edges": [],
            }
        ),
        message="帮我查一下余额",
        intents_by_code={"query_account_balance": _balance_intent(allow_from_history=False)},
        recent_messages=["user: 卡号 6222021234567890，手机号后四位 1234"],
        long_term_memory=[],
    )

    assert result.graph.status == GraphStatus.DRAFT
    assert result.graph.nodes[0].slot_memory == {}
    assert result.graph.nodes[0].history_slot_keys == []
