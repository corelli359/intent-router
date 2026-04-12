from __future__ import annotations

import asyncio

from router_service.core.domain import IntentDefinition
from router_service.core.slot_extractor import SlotExtractor
from router_service.core.graph_domain import GraphNodeState, SlotBindingSource


class _RetryableLLMError(Exception):
    status_code = 429


class _RetryableLLMClient:
    async def run_json(self, *, prompt, variables, model=None, on_delta=None):  # pragma: no cover - tiny stub
        raise _RetryableLLMError("rate limited")


def _gas_intent() -> IntentDefinition:
    return IntentDefinition(
        intent_code="pay_gas_bill",
        name="缴纳燃气费",
        description="缴纳燃气费，需要燃气户号和缴费金额。",
        examples=["给燃气户号88001234交88元"],
        keywords=["燃气", "缴费"],
        agent_url="http://agent.example.com/gas",
        slot_schema=[
            {
                "slot_key": "gas_account_number",
                "label": "燃气户号",
                "description": "燃气缴费账户号",
                "value_type": "account_number",
                "required": True,
                "allow_from_history": True,
            },
            {
                "slot_key": "amount",
                "label": "缴费金额",
                "description": "本次缴费金额",
                "value_type": "currency",
                "required": True,
            },
        ],
    )


def test_slot_extractor_returns_structured_slots_from_heuristics() -> None:
    async def run() -> None:
        extractor = SlotExtractor()
        result = await extractor.extract(
            intent=_gas_intent(),
            node=GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳燃气费",
                confidence=0.95,
                source_fragment="给燃气户号88001234交88元",
            ),
            graph_source_message="给燃气户号88001234交88元",
            current_message="给燃气户号88001234交88元",
            long_term_memory=[],
        )

        assert result.slot_memory == {"gas_account_number": "88001234", "amount": "88"}
        assert result.ambiguous_slot_keys == []
        binding_by_key = {binding.slot_key: binding for binding in result.slot_bindings}
        assert binding_by_key["gas_account_number"].source == SlotBindingSource.USER_MESSAGE
        assert binding_by_key["gas_account_number"].source_text
        assert binding_by_key["amount"].value == "88"

    asyncio.run(run())


def test_slot_extractor_preserves_history_bound_seed_slots() -> None:
    async def run() -> None:
        extractor = SlotExtractor()
        result = await extractor.extract(
            intent=_gas_intent(),
            node=GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳燃气费",
                confidence=0.95,
                source_fragment="帮我缴燃气费",
                slot_memory={"gas_account_number": "88001234"},
                history_slot_keys=["gas_account_number"],
            ),
            graph_source_message="帮我缴燃气费",
            current_message="帮我缴燃气费",
            long_term_memory=["gas_account_number=88001234"],
        )

        assert result.slot_memory == {"gas_account_number": "88001234"}
        assert result.history_slot_keys == ["gas_account_number"]

    asyncio.run(run())


def test_slot_extractor_keeps_heuristic_slots_when_llm_is_rate_limited() -> None:
    async def run() -> None:
        extractor = SlotExtractor(llm_client=_RetryableLLMClient())
        result = await extractor.extract(
            intent=_gas_intent(),
            node=GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳燃气费",
                confidence=0.95,
                source_fragment="帮我交燃气费",
            ),
            graph_source_message="帮我交燃气费",
            current_message="燃气户号 88001234",
            long_term_memory=[],
        )

        assert result.slot_memory == {"gas_account_number": "88001234"}
        assert result.ambiguous_slot_keys == []

    asyncio.run(run())
