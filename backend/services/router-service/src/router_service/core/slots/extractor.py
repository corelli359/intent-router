from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from pydantic import BaseModel, Field, model_validator

from router_service.core.shared.domain import IntentDefinition
from router_service.core.shared.diagnostics import (
    RouterDiagnostic,
    RouterDiagnosticCode,
    diagnostic,
    merge_diagnostics,
)
from router_service.core.support.json_codec import json_dumps
from router_service.core.support.llm_client import JsonLLMClient, llm_exception_is_retryable
from router_service.core.prompts.prompt_templates import (
    DEFAULT_SLOT_EXTRACTOR_HUMAN_PROMPT,
    DEFAULT_SLOT_EXTRACTOR_SYSTEM_PROMPT,
    build_slot_extractor_prompt,
)
from router_service.core.recognition.recognizer import recognition_intent_payload
from router_service.core.slots.grounding import (
    normalize_structured_slot_memory,
)
from router_service.core.shared.graph_domain import GraphNodeState, SlotBindingSource, SlotBindingState
from router_service.models.intent import IntentSlotDefinition, SlotOverwritePolicy


logger = logging.getLogger(__name__)


class SlotExtractionItemPayload(BaseModel):
    """One extracted slot candidate returned by the LLM."""

    slot_key: str
    value: Any | None = None
    source: SlotBindingSource = SlotBindingSource.USER_MESSAGE
    source_text: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class SlotExtractionPayload(BaseModel):
    """Structured LLM payload for slot extraction results."""

    slots: list[SlotExtractionItemPayload] = Field(default_factory=list)
    ambiguous_slot_keys: list[str] = Field(default_factory=list, alias="ambiguousSlotKeys")
    diagnostics: list[RouterDiagnostic] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> Any:
        """Normalize alternate field names returned by different extraction prompts."""
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "slots" not in normalized:
            for key in ("items", "bindings"):
                candidate = normalized.get(key)
                if isinstance(candidate, list):
                    normalized["slots"] = candidate
                    break
        if "ambiguousSlotKeys" not in normalized:
            for key in ("ambiguous_slots", "ambiguousSlotkeys", "ambiguous"):
                candidate = normalized.get(key)
                if isinstance(candidate, list):
                    normalized["ambiguousSlotKeys"] = candidate
                    break
        return normalized


@dataclass(slots=True)
class SlotExtractionResult:
    """Merged slot extraction result used by downstream slot validation."""

    slot_memory: dict[str, Any]
    slot_bindings: list[SlotBindingState]
    history_slot_keys: list[str]
    ambiguous_slot_keys: list[str]
    diagnostics: list[RouterDiagnostic] | None = None


class SlotExtractor:
    """Extract slot candidates from preserved state plus optional LLM output."""

    def __init__(
        self,
        llm_client: JsonLLMClient | None = None,
        *,
        model: str | None = None,
        system_prompt_template: str = DEFAULT_SLOT_EXTRACTOR_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_SLOT_EXTRACTOR_HUMAN_PROMPT,
    ) -> None:
        """Initialize the extractor and compile the optional LLM prompt template."""
        self.llm_client = llm_client
        self.model = model
        self.prompt = build_slot_extractor_prompt(
            system_prompt=system_prompt_template,
            human_prompt=human_prompt_template,
        )

    async def extract(
        self,
        *,
        intent: IntentDefinition,
        node: GraphNodeState,
        graph_source_message: str,
        current_message: str,
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ) -> SlotExtractionResult:
        """Extract slot candidates from preserved bindings and optional LLM output."""
        slot_schema = intent.slot_schema
        slot_defs_by_key = {slot.slot_key: slot for slot in slot_schema}
        seed_bindings = {binding.slot_key: binding for binding in node.slot_bindings}

        merged_memory: dict[str, Any] = {}
        merged_bindings: dict[str, SlotBindingState] = {}
        history_slot_keys: list[str] = []

        for slot_key, value in normalize_structured_slot_memory(
            slot_memory=node.slot_memory,
            slot_schema=slot_schema,
        ).items():
            slot_def = slot_defs_by_key.get(slot_key)
            if slot_def is None:
                continue
            binding = seed_bindings.get(slot_key)
            normalized_binding, is_history = self._normalize_seed_binding(
                slot_def=slot_def,
                slot_key=slot_key,
                value=value,
                binding=binding,
                from_history=slot_key in node.history_slot_keys,
            )
            if normalized_binding is None:
                continue
            merged_memory[slot_key] = normalized_binding.value
            merged_bindings[slot_key] = normalized_binding
            if is_history:
                history_slot_keys.append(slot_key)

        ambiguous_slot_keys: list[str] = []
        diagnostics: list[RouterDiagnostic] = []
        llm_missing_required = any(
            slot.required and slot.slot_key not in merged_memory
            for slot in slot_schema
        )
        if self.llm_client is not None and (llm_missing_required or not merged_memory):
            llm_result = await self._extract_with_llm(
                intent=intent,
                current_message=current_message or graph_source_message,
                source_fragment=node.source_fragment or graph_source_message,
                existing_slot_memory=merged_memory,
                recent_messages=recent_messages,
            )
            if llm_result is not None:
                ambiguous_slot_keys = list(llm_result.ambiguous_slot_keys)
                diagnostics = merge_diagnostics(diagnostics, llm_result.diagnostics)
                self._merge_items(
                    merged_memory=merged_memory,
                    merged_bindings=merged_bindings,
                    history_slot_keys=history_slot_keys,
                    slot_defs_by_key=slot_defs_by_key,
                    items=llm_result.slots,
                    allow_replace_existing_user_message=True,
                )

        return SlotExtractionResult(
            slot_memory=merged_memory,
            slot_bindings=[merged_bindings[key] for key in merged_memory if key in merged_bindings],
            history_slot_keys=history_slot_keys,
            ambiguous_slot_keys=[
                slot_key
                for slot_key in ambiguous_slot_keys
                if slot_key in slot_defs_by_key and slot_key not in merged_memory
            ],
            diagnostics=diagnostics,
        )

    def _normalize_seed_binding(
        self,
        *,
        slot_def: IntentSlotDefinition,
        slot_key: str,
        value: Any,
        binding: SlotBindingState | None,
        from_history: bool,
    ) -> tuple[SlotBindingState | None, bool]:
        """Validate one existing node binding before reusing it in the current turn."""
        source = binding.source if binding is not None else SlotBindingSource.USER_MESSAGE
        if source == SlotBindingSource.RECOMMENDATION:
            if not slot_def.allow_from_recommendation:
                return None, False
            return (
                SlotBindingState(
                    slot_key=slot_key,
                    value=value,
                    source=source,
                    source_text=binding.source_text if binding is not None else None,
                    confidence=binding.confidence if binding is not None else None,
                    is_modified=binding.is_modified if binding is not None else False,
                ),
                False,
            )

        if source == SlotBindingSource.HISTORY or from_history:
            if not slot_def.allow_from_history:
                return None, False
            return (
                SlotBindingState(
                    slot_key=slot_key,
                    value=value,
                    source=SlotBindingSource.HISTORY,
                    source_text=binding.source_text if binding is not None else None,
                    confidence=binding.confidence if binding is not None else None,
                    is_modified=binding.is_modified if binding is not None else False,
                ),
                True,
            )
        return (
            SlotBindingState(
                slot_key=slot_key,
                value=value,
                source=source,
                source_text=binding.source_text if binding is not None else None,
                confidence=binding.confidence if binding is not None else None,
                is_modified=binding.is_modified if binding is not None else False,
            ),
            False,
        )

    async def _extract_with_llm(
        self,
        *,
        intent: IntentDefinition,
        current_message: str,
        source_fragment: str,
        existing_slot_memory: dict[str, Any],
        recent_messages: list[str] | None = None,
    ) -> SlotExtractionPayload | None:
        """Call the slot extraction LLM and normalize its JSON output."""
        if self.llm_client is None:
            return None
        try:
            raw_response = await self.llm_client.run_json(
                prompt=self.prompt,
                variables={
                    "message": current_message,
                    "source_fragment": source_fragment,
                    "recent_messages_json": json_dumps(recent_messages or []),
                    "intent_json": json_dumps(recognition_intent_payload(intent)),
                    "existing_slot_memory_json": json_dumps(existing_slot_memory),
                },
                model=self.model,
            )
        except Exception as exc:
            if llm_exception_is_retryable(exc):
                logger.debug(
                    "Slot extraction LLM is temporarily unavailable, preserving existing slot state only",
                    exc_info=True,
                )
                return SlotExtractionPayload.model_validate(
                    {
                        "slots": [],
                        "ambiguousSlotKeys": [],
                        "diagnostics": [
                            diagnostic(
                                RouterDiagnosticCode.SLOT_EXTRACTOR_LLM_RETRYABLE_UNAVAILABLE,
                                source="slot_extractor",
                                message="提槽 LLM 暂时不可用，当前仅保留已有槽位",
                                details={"error_type": type(exc).__name__},
                            ).model_dump(mode="json")
                        ],
                    }
                )
            logger.debug("Slot extraction LLM failed, keeping existing slot state only", exc_info=True)
            return SlotExtractionPayload.model_validate(
                {
                    "slots": [],
                    "ambiguousSlotKeys": [],
                    "diagnostics": [
                        diagnostic(
                            RouterDiagnosticCode.SLOT_EXTRACTOR_LLM_FAILED,
                            source="slot_extractor",
                            message="提槽 LLM 失败，当前仅保留已有槽位",
                            details={"error_type": type(exc).__name__},
                        ).model_dump(mode="json")
                    ],
                }
            )
        payload = SlotExtractionPayload.model_validate(raw_response)
        payload.diagnostics = payload.diagnostics or []
        return payload

    def _merge_items(
        self,
        *,
        merged_memory: dict[str, Any],
        merged_bindings: dict[str, SlotBindingState],
        history_slot_keys: list[str],
        slot_defs_by_key: dict[str, IntentSlotDefinition],
        items: list[SlotExtractionItemPayload],
        allow_replace_existing_user_message: bool,
    ) -> None:
        """Merge extracted items into slot memory while enforcing overwrite and source rules."""
        for item in items:
            slot_def = slot_defs_by_key.get(item.slot_key)
            if slot_def is None or item.value is None:
                continue
            value = item.value.strip() if isinstance(item.value, str) else item.value
            if value in {"", None}:
                continue

            source = item.source
            if source == SlotBindingSource.HISTORY:
                if not slot_def.allow_from_history:
                    continue
            elif source == SlotBindingSource.RECOMMENDATION:
                if not slot_def.allow_from_recommendation:
                    continue

            existing = merged_bindings.get(item.slot_key)
            if existing is not None and not self._should_replace(
                slot_def=slot_def,
                existing=existing,
                incoming=item,
                allow_replace_existing_user_message=allow_replace_existing_user_message,
            ):
                continue

            merged_memory[item.slot_key] = value
            merged_bindings[item.slot_key] = SlotBindingState(
                slot_key=item.slot_key,
                value=value,
                source=source,
                source_text=item.source_text,
                confidence=item.confidence,
                is_modified=existing is not None and existing.value != value,
            )
            if source == SlotBindingSource.HISTORY and item.slot_key not in history_slot_keys:
                history_slot_keys.append(item.slot_key)
            if source != SlotBindingSource.HISTORY and item.slot_key in history_slot_keys:
                history_slot_keys.remove(item.slot_key)

    def _should_replace(
        self,
        *,
        slot_def: IntentSlotDefinition,
        existing: SlotBindingState,
        incoming: SlotExtractionItemPayload,
        allow_replace_existing_user_message: bool,
    ) -> bool:
        """Decide whether an incoming extracted value may replace an existing binding."""
        if incoming.value is None:
            return False
        if slot_def.overwrite_policy == SlotOverwritePolicy.ALWAYS_OVERWRITE:
            return True
        if slot_def.overwrite_policy == SlotOverwritePolicy.KEEP_ORIGINAL:
            return False
        if existing.source in {SlotBindingSource.HISTORY, SlotBindingSource.RECOMMENDATION} and incoming.source == SlotBindingSource.USER_MESSAGE:
            return True
        if existing.source == SlotBindingSource.USER_MESSAGE and incoming.source == SlotBindingSource.USER_MESSAGE:
            return allow_replace_existing_user_message and existing.value != incoming.value
        return existing.value != incoming.value
