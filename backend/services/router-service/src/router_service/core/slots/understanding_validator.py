from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from router_service.core.domain import IntentDefinition
from router_service.core.slot_extractor import SlotExtractor
from router_service.core.slot_validator import SlotValidationResult, SlotValidator
from router_service.core.graph_domain import GraphNodeState, SlotBindingState


@dataclass(slots=True)
class UnderstandingValidationResult:
    slot_memory: dict[str, Any]
    slot_bindings: list[SlotBindingState]
    history_slot_keys: list[str]
    missing_required_slots: list[str]
    ambiguous_slot_keys: list[str]
    invalid_slot_keys: list[str]
    needs_confirmation: bool
    can_dispatch: bool
    prompt_message: str | None


class UnderstandingValidator:
    def __init__(
        self,
        slot_extractor: SlotExtractor | None = None,
        slot_validator: SlotValidator | None = None,
    ) -> None:
        self.slot_extractor = slot_extractor or SlotExtractor()
        self.slot_validator = slot_validator or SlotValidator()

    async def validate_node(
        self,
        *,
        intent: IntentDefinition,
        node: GraphNodeState,
        graph_source_message: str,
        current_message: str,
        long_term_memory: list[str] | None = None,
    ) -> UnderstandingValidationResult:
        extraction = await self.slot_extractor.extract(
            intent=intent,
            node=node,
            graph_source_message=graph_source_message,
            current_message=current_message,
            long_term_memory=long_term_memory,
        )
        validation = self.slot_validator.validate(
            intent=intent,
            slot_memory=extraction.slot_memory,
            slot_bindings=extraction.slot_bindings,
            history_slot_keys=extraction.history_slot_keys,
            ambiguous_slot_keys=extraction.ambiguous_slot_keys,
            graph_source_message=graph_source_message,
            node_source_fragment=node.source_fragment,
            current_message=current_message,
            long_term_memory=long_term_memory,
        )
        return self._compose(validation)

    def _compose(self, validation: SlotValidationResult) -> UnderstandingValidationResult:
        return UnderstandingValidationResult(
            slot_memory=validation.slot_memory,
            slot_bindings=validation.slot_bindings,
            history_slot_keys=validation.history_slot_keys,
            missing_required_slots=validation.missing_required_slots,
            ambiguous_slot_keys=validation.ambiguous_slot_keys,
            invalid_slot_keys=validation.invalid_slot_keys,
            needs_confirmation=validation.needs_confirmation,
            can_dispatch=validation.can_dispatch,
            prompt_message=validation.prompt_message,
        )
