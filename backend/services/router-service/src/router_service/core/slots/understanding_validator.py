from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from router_service.core.shared.domain import IntentDefinition
from router_service.core.shared.diagnostics import RouterDiagnostic, merge_diagnostics
from router_service.core.support.trace_logging import router_stage
from router_service.core.slots.extractor import SlotExtractor
from router_service.core.slots.validator import SlotValidationResult, SlotValidator
from router_service.core.shared.graph_domain import GraphNodeState, SlotBindingState


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UnderstandingValidationResult:
    """Router-facing validation result that combines extraction and validation output."""

    slot_memory: dict[str, Any]
    slot_bindings: list[SlotBindingState]
    history_slot_keys: list[str]
    missing_required_slots: list[str]
    ambiguous_slot_keys: list[str]
    invalid_slot_keys: list[str]
    needs_confirmation: bool
    can_dispatch: bool
    prompt_message: str | None
    diagnostics: list[RouterDiagnostic] | None = None


class UnderstandingValidator:
    """Coordinate slot extraction and slot validation for one graph node."""

    def __init__(
        self,
        slot_extractor: SlotExtractor | None = None,
        slot_validator: SlotValidator | None = None,
    ) -> None:
        """Initialize the validator with pluggable extractor and validator components."""
        self.slot_extractor = slot_extractor or SlotExtractor()
        self.slot_validator = slot_validator or SlotValidator()

    async def validate_node(
        self,
        *,
        intent: IntentDefinition,
        node: GraphNodeState,
        graph_source_message: str,
        current_message: str,
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ) -> UnderstandingValidationResult:
        """Extract then validate slots for one node before agent dispatch."""
        with router_stage(
            logger,
            "understanding_validator.validate_node",
            intent_code=intent.intent_code,
            node_id=node.node_id,
        ):
            with router_stage(
                logger,
                "understanding_validator.extract_slots",
                intent_code=intent.intent_code,
                node_id=node.node_id,
            ):
                extraction = await self.slot_extractor.extract(
                    intent=intent,
                    node=node,
                    graph_source_message=graph_source_message,
                    current_message=current_message,
                    recent_messages=recent_messages,
                    long_term_memory=long_term_memory,
                )
            with router_stage(
                logger,
                "understanding_validator.validate_slots",
                intent_code=intent.intent_code,
                node_id=node.node_id,
            ):
                validation = self.slot_validator.validate(
                    intent=intent,
                    slot_memory=extraction.slot_memory,
                    slot_bindings=extraction.slot_bindings,
                    history_slot_keys=extraction.history_slot_keys,
                    ambiguous_slot_keys=extraction.ambiguous_slot_keys,
                    graph_source_message=graph_source_message,
                    node_source_fragment=node.source_fragment,
                    current_message=current_message,
                    recent_messages=recent_messages,
                    long_term_memory=long_term_memory,
                )
            return self._compose(validation, extraction_diagnostics=extraction.diagnostics or [])

    def _compose(
        self,
        validation: SlotValidationResult,
        *,
        extraction_diagnostics: list[RouterDiagnostic],
    ) -> UnderstandingValidationResult:
        """Convert the low-level slot validation result into the router-facing result model."""
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
            diagnostics=merge_diagnostics(
                extraction_diagnostics,
                validation.diagnostics,
            ),
        )
