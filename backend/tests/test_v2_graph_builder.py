from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


from router_service.core.shared.domain import IntentDefinition  # noqa: E402
from router_service.core.shared.diagnostics import RouterDiagnosticCode  # noqa: E402
from router_service.core.shared.graph_domain import GraphStatus, SlotBindingSource  # noqa: E402
from router_service.core.graph.builder import GraphDraftNormalizer, LLMIntentGraphBuilder, UnifiedGraphDraftPayload  # noqa: E402
from router_service.core.recognition.recognizer import RecognitionResult  # noqa: E402


class _StaticLLMClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.variables: dict | None = None

    async def run_json(self, *, prompt, variables, model=None, on_delta=None):
        del prompt, model, on_delta
        self.variables = dict(variables)
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
    client = _StaticLLMClient(
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
    )
    builder = LLMIntentGraphBuilder(
        client,
    )

    async def run() -> None:
        recommend_task = [{"intentCode": "transfer_money", "title": "高风险转账"}]
        result = await builder.build(
            message="给我媳妇儿转1000",
            intents=[_transfer_intent(confirm_policy="always")],
            recent_messages=[],
            long_term_memory=[],
            recommend_task=recommend_task,
        )

        assert client.variables is not None
        assert json.loads(client.variables["recommend_task_json"]) == recommend_task
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


def test_unified_graph_builder_falls_back_to_legacy_chain_when_llm_fails() -> None:
    class _FailingLLMClient:
        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            del prompt, variables, model, on_delta
            raise RuntimeError("llm unavailable")

    class _FallbackRecognizer:
        async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
            del message, intents, recent_messages, long_term_memory, on_delta
            return RecognitionResult(
                primary=[],
                candidates=[],
                diagnostics=[],
            )

    builder = LLMIntentGraphBuilder(
        _FailingLLMClient(),
        fallback_recognizer=_FallbackRecognizer(),
    )

    async def run() -> None:
        result = await builder.build(
            message="帮我查一下余额",
            intents=[_balance_intent()],
            recent_messages=[],
            long_term_memory=[],
        )

        assert result.graph.summary == "未识别到明确事项"
        assert any(item.code == RouterDiagnosticCode.GRAPH_BUILDER_LLM_FAILED_LEGACY_CHAIN for item in result.diagnostics or [])
        assert any(item.details.get("error_type") == "RuntimeError" for item in result.diagnostics or [])

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


def test_graph_draft_normalizer_keeps_slot_bindings_and_condition_thresholds_separate() -> None:
    normalizer = GraphDraftNormalizer()
    result = normalizer.normalize(
        payload=UnifiedGraphDraftPayload.model_validate(
            {
                "summary": "燃气缴费后查余额，若大于20000则转账，若还有余额再换汇",
                "needs_confirmation": True,
                "primary_intents": [
                    {"intent_code": "query_account_balance", "confidence": 0.96, "reason": "balance condition provider"},
                    {"intent_code": "transfer_money", "confidence": 0.95, "reason": "conditional transfer"},
                    {"intent_code": "exchange_forex", "confidence": 0.94, "reason": "post transfer forex"},
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "query_account_balance",
                        "title": "查询账户余额",
                        "confidence": 0.96,
                        "source_fragment": "余额大于20000的话",
                        "slot_memory": {},
                        "slot_bindings": [],
                    },
                    {
                        "intent_code": "transfer_money",
                        "title": "给妈妈转2000",
                        "confidence": 0.95,
                        "source_fragment": "给妈妈转2000",
                        "slot_memory": {"recipient_name": "妈妈", "amount": "2000"},
                        "slot_bindings": [
                            {
                                "slot_key": "recipient_name",
                                "value": "妈妈",
                                "source": "user_message",
                                "source_text": "给妈妈",
                                "confidence": 0.98,
                            },
                            {
                                "slot_key": "amount",
                                "value": "2000",
                                "source": "user_message",
                                "source_text": "转2000",
                                "confidence": 0.99,
                            },
                        ],
                    },
                    {
                        "intent_code": "exchange_forex",
                        "title": "换200美金",
                        "confidence": 0.94,
                        "source_fragment": "我再换200美金",
                        "slot_memory": {"source_currency": "CNY", "target_currency": "USD", "amount": "200"},
                        "slot_bindings": [
                            {
                                "slot_key": "amount",
                                "value": "200",
                                "source": "user_message",
                                "source_text": "换200美金",
                                "confidence": 0.97,
                            }
                        ],
                    },
                ],
                "edges": [
                    {
                        "source_index": 0,
                        "target_index": 1,
                        "relation_type": "conditional",
                        "label": "余额大于20000时转账",
                        "condition": {
                            "left_key": "balance",
                            "operator": ">",
                            "right_value": 20000,
                        },
                    },
                    {
                        "source_index": 1,
                        "target_index": 2,
                        "relation_type": "sequential",
                        "label": "转账后再换汇",
                    },
                ],
            }
        ),
        message="余额大于20000的话，就给妈妈转2000，如果还有余额的话，我再换200美金",
        intents_by_code={
            "query_account_balance": _balance_intent(),
            "transfer_money": _transfer_intent(),
            "exchange_forex": IntentDefinition(
                intent_code="exchange_forex",
                name="换外汇",
                description="执行换汇，需要币种和金额。",
                examples=["换200美金"],
                keywords=["换汇"],
                agent_url="http://agent.example.com/forex",
                dispatch_priority=90,
                primary_threshold=0.75,
                candidate_threshold=0.5,
                slot_schema=[
                    {
                        "slot_key": "source_currency",
                        "label": "卖出币种",
                        "semantic_definition": "用户要卖出的原币种",
                        "value_type": "string",
                        "required": True,
                    },
                    {
                        "slot_key": "target_currency",
                        "label": "买入币种",
                        "semantic_definition": "用户要换入的目标币种",
                        "value_type": "string",
                        "required": True,
                    },
                    {
                        "slot_key": "amount",
                        "label": "换汇金额",
                        "semantic_definition": "本次换汇的执行金额，不是条件阈值金额",
                        "value_type": "currency",
                        "required": True,
                    },
                ],
            ),
        },
    )

    assert result.graph.edges[0].condition is not None
    assert result.graph.edges[0].condition.right_value == 20000
    assert result.graph.nodes[1].slot_memory["amount"] == "2000"
    assert result.graph.nodes[2].slot_memory["amount"] == "200"
    assert result.graph.nodes[1].slot_bindings[1].slot_key == "amount"
    assert result.graph.nodes[1].slot_bindings[1].value == "2000"
    assert result.graph.nodes[1].slot_bindings[1].source == SlotBindingSource.USER_MESSAGE
    assert result.graph.nodes[2].slot_bindings[-1].slot_key == "amount"
    assert result.graph.nodes[2].slot_bindings[-1].value == "200"
