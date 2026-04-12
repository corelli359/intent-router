from __future__ import annotations

import asyncio

from router_service.core.domain import IntentDefinition
from router_service.core.understanding_validator import UnderstandingValidator
from router_service.core.v2_domain import GraphNodeState


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


def test_understanding_validator_requires_all_slots_before_dispatch() -> None:
    async def run() -> None:
        validator = UnderstandingValidator()
        result = await validator.validate_node(
            intent=_gas_intent(),
            node=GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳燃气费",
                confidence=0.95,
                source_fragment="给燃气户号88001234交一下",
            ),
            graph_source_message="给燃气户号88001234交一下",
            current_message="给燃气户号88001234交一下",
            long_term_memory=[],
        )

        assert result.missing_required_slots == ["amount"]
        assert result.can_dispatch is False
        assert not result.needs_confirmation

    asyncio.run(run())


def test_understanding_validator_allows_dispatch_with_complete_slots() -> None:
    async def run() -> None:
        validator = UnderstandingValidator()
        result = await validator.validate_node(
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

        assert result.missing_required_slots == []
        assert result.can_dispatch is True
        assert result.slot_memory == {"gas_account_number": "88001234", "amount": "88"}

    asyncio.run(run())
