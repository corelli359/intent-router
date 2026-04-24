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
    combine_distinct_text,
    normalize_structured_slot_memory,
)
from router_service.core.shared.graph_domain import GraphNodeState, SlotBindingSource, SlotBindingState
from router_service.models.intent import IntentSlotDefinition, SlotOverwritePolicy, SlotValueType


logger = logging.getLogger(__name__)


class SlotExtractionItemPayload(BaseModel):
    """One extracted slot candidate returned by heuristics or the LLM."""

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
        history_text = "\n".join(entry for entry in (long_term_memory or []) if entry)
        grounding_text = combine_distinct_text(graph_source_message, node.source_fragment, current_message)
        turn_history_text = combine_distinct_text(*(recent_messages or []), grounding_text)
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
                grounding_text=grounding_text,
                turn_history_text=turn_history_text,
                history_text=history_text,
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
        local_result = self._extract_with_local_heuristics(
            intent=intent,
            current_message=current_message or graph_source_message,
            source_fragment=node.source_fragment or graph_source_message,
            existing_slot_memory=merged_memory,
            long_term_memory=long_term_memory,
        )
        if local_result is not None:
            self._merge_items(
                merged_memory=merged_memory,
                merged_bindings=merged_bindings,
                history_slot_keys=history_slot_keys,
                slot_defs_by_key=slot_defs_by_key,
                items=local_result.slots,
                grounding_text=grounding_text,
                history_text=history_text,
                allow_replace_existing_user_message=True,
            )
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
                    grounding_text=grounding_text,
                    history_text=history_text,
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

    def _extract_with_local_heuristics(
        self,
        *,
        intent: IntentDefinition,
        current_message: str,
        source_fragment: str,
        existing_slot_memory: dict[str, Any],
        long_term_memory: list[str] | None = None,
    ) -> SlotExtractionPayload | None:
        """Extract unambiguous typed values without guessing business semantics in code."""
        text = combine_distinct_text(source_fragment, current_message)
        if not text:
            return None
        digit_spans = self._digit_spans(text)
        claimed_numeric_values = self._claimed_numeric_values(long_term_memory or [])
        missing_slot_defs = [
            slot_def
            for slot_def in intent.slot_schema
            if slot_def.slot_key not in existing_slot_memory
        ]
        items: list[SlotExtractionItemPayload] = []
        account_candidate = self._pick_account_number_candidate(
            text=text,
            digit_spans=digit_spans,
            slot_defs=missing_slot_defs,
            claimed_numeric_values=claimed_numeric_values,
        )
        self._append_local_typed_item(
            items=items,
            slot_defs=missing_slot_defs,
            value_type=SlotValueType.ACCOUNT_NUMBER,
            value=account_candidate[0] if account_candidate is not None else None,
            source_text=current_message,
        )
        self._append_local_typed_item(
            items=items,
            slot_defs=missing_slot_defs,
            value_type=SlotValueType.PHONE_LAST4,
            value=self._pick_phone_last_four(
                text=text,
                digit_spans=digit_spans,
                slot_defs=missing_slot_defs,
                preferred_group_span=account_candidate,
            ),
            source_text=current_message,
        )
        self._append_local_typed_item(
            items=items,
            slot_defs=missing_slot_defs,
            value_type=SlotValueType.CURRENCY,
            value=self._pick_amount(text=text, digit_spans=digit_spans, slot_defs=missing_slot_defs),
            source_text=current_message,
        )
        self._append_local_typed_item(
            items=items,
            slot_defs=missing_slot_defs,
            value_type=SlotValueType.INTEGER,
            value=self._pick_integer(text=text, digit_spans=digit_spans, slot_defs=missing_slot_defs),
            source_text=current_message,
        )
        self._append_local_typed_item(
            items=items,
            slot_defs=missing_slot_defs,
            value_type=SlotValueType.NUMBER,
            value=self._pick_number(text=text, digit_spans=digit_spans, slot_defs=missing_slot_defs),
            source_text=current_message,
        )
        if not items:
            return None
        return SlotExtractionPayload(slots=items, ambiguousSlotKeys=[])

    def _append_local_typed_item(
        self,
        *,
        items: list[SlotExtractionItemPayload],
        slot_defs: list[IntentSlotDefinition],
        value_type: SlotValueType,
        value: Any | None,
        source_text: str,
    ) -> None:
        """Append one local typed binding only when the slot family is unambiguous."""
        if value is None:
            return
        matching_slot_defs = [slot_def for slot_def in slot_defs if slot_def.value_type == value_type]
        if len(matching_slot_defs) != 1:
            return
        items.append(
            SlotExtractionItemPayload(
                slot_key=matching_slot_defs[0].slot_key,
                value=value,
                source=SlotBindingSource.USER_MESSAGE,
                source_text=source_text,
                confidence=0.93,
            )
        )

    def _digit_spans(self, text: str) -> list[tuple[str, int, int]]:
        """Return contiguous digit spans from one text fragment."""
        spans: list[tuple[str, int, int]] = []
        start: int | None = None
        for index, character in enumerate(text):
            if character.isdigit():
                if start is None:
                    start = index
                continue
            if start is not None:
                spans.append((text[start:index], start, index))
                start = None
        if start is not None:
            spans.append((text[start:], start, len(text)))
        return spans

    def _pick_account_number_candidate(
        self,
        *,
        text: str,
        digit_spans: list[tuple[str, int, int]],
        slot_defs: list[IntentSlotDefinition],
        claimed_numeric_values: set[str],
    ) -> tuple[str, int, int] | None:
        """Choose one account-like span via slot anchors or unambiguous raw fallback."""
        matching_slot_defs = [slot_def for slot_def in slot_defs if slot_def.value_type == SlotValueType.ACCOUNT_NUMBER]
        if len(matching_slot_defs) != 1:
            return None
        candidates = [
            (digits, start, end)
            for digits, start, end in digit_spans
            if 6 <= len(digits) <= 20
        ]
        if len(candidates) > 1:
            unclaimed_candidates = [
                candidate
                for candidate in candidates
                if candidate[0] not in claimed_numeric_values
            ]
            if len(unclaimed_candidates) == 1:
                candidates = unclaimed_candidates
        return self._pick_span_candidate(
            text=text,
            span_candidates=candidates,
            slot_def=matching_slot_defs[0],
            generic_markers=("卡号", "账号", "账户", "银行卡", "银行卡号", "户号"),
            allow_raw_fallback=True,
            raw_fallback_requires_exact_text=False,
        )

    def _pick_phone_last_four(
        self,
        *,
        text: str,
        digit_spans: list[tuple[str, int, int]],
        slot_defs: list[IntentSlotDefinition],
        preferred_group_span: tuple[str, int, int] | None = None,
    ) -> str | None:
        """Choose one phone-last-four span via slot anchors or safe raw fallback."""
        matching_slot_defs = [slot_def for slot_def in slot_defs if slot_def.value_type == SlotValueType.PHONE_LAST4]
        if len(matching_slot_defs) != 1:
            return None
        candidates = [
            (digits, start, end)
            for digits, start, end in digit_spans
            if len(digits) == 4
        ]
        has_other_numeric_slots = any(
            slot_def.value_type in {SlotValueType.CURRENCY, SlotValueType.NUMBER, SlotValueType.INTEGER}
            for slot_def in slot_defs
            if slot_def.value_type != SlotValueType.PHONE_LAST4
        )
        candidate = self._pick_span_candidate(
            text=text,
            span_candidates=candidates,
            slot_def=matching_slot_defs[0],
            generic_markers=("尾号", "后4位", "后四位", "手机号", "手机", "手机号后4位", "手机号后四位"),
            allow_raw_fallback=not has_other_numeric_slots,
            raw_fallback_requires_exact_text=True,
        )
        if candidate is not None:
            return candidate[0]
        if preferred_group_span is None or len(candidates) <= 1:
            return None
        same_group_candidates = [
            candidate
            for candidate in candidates
            if self._spans_share_phrase_group(
                text=text,
                left_span=preferred_group_span,
                right_span=candidate,
            )
        ]
        if len(same_group_candidates) != 1:
            return None
        return same_group_candidates[0][0]

    def _pick_amount(
        self,
        *,
        text: str,
        digit_spans: list[tuple[str, int, int]],
        slot_defs: list[IntentSlotDefinition],
    ) -> str | None:
        """Choose one amount-like span via syntax cues or raw numeric fallback."""
        matching_slot_defs = [slot_def for slot_def in slot_defs if slot_def.value_type == SlotValueType.CURRENCY]
        if len(matching_slot_defs) != 1:
            return None
        unit_candidates = [
            (digits, start, end)
            for digits, start, end in digit_spans
            if self._span_has_amount_context(
                text=text,
                start=start,
                end=end,
            )
        ]
        if len(unit_candidates) == 1:
            return unit_candidates[0][0]
        if len(unit_candidates) > 1:
            return None
        stripped = self._strip_inline_whitespace(text)
        if len(digit_spans) != 1 or not stripped.isdigit():
            return None
        digits, _start, _end = digit_spans[0]
        if digits != stripped or len(digits) > 6:
            return None
        has_phone_slot = any(slot_def.value_type == SlotValueType.PHONE_LAST4 for slot_def in slot_defs)
        if has_phone_slot and len(digits) == 4:
            return None
        return digits

    def _pick_integer(
        self,
        *,
        text: str,
        digit_spans: list[tuple[str, int, int]],
        slot_defs: list[IntentSlotDefinition],
    ) -> str | None:
        """Choose one integer value only when the user turn is a raw integer token."""
        matching_slot_defs = [slot_def for slot_def in slot_defs if slot_def.value_type == SlotValueType.INTEGER]
        if len(matching_slot_defs) != 1:
            return None
        stripped = self._strip_inline_whitespace(text)
        if len(digit_spans) != 1 or not stripped.isdigit():
            return None
        digits, _start, _end = digit_spans[0]
        return digits if digits == stripped else None

    def _pick_number(
        self,
        *,
        text: str,
        digit_spans: list[tuple[str, int, int]],
        slot_defs: list[IntentSlotDefinition],
    ) -> str | None:
        """Choose one numeric token only when the user turn is a raw integer token."""
        matching_slot_defs = [slot_def for slot_def in slot_defs if slot_def.value_type == SlotValueType.NUMBER]
        if len(matching_slot_defs) != 1:
            return None
        stripped = self._strip_inline_whitespace(text)
        if len(digit_spans) != 1 or not stripped.isdigit():
            return None
        digits, _start, _end = digit_spans[0]
        return digits if digits == stripped else None

    def _pick_span_candidate(
        self,
        *,
        text: str,
        span_candidates: list[tuple[str, int, int]],
        slot_def: IntentSlotDefinition,
        generic_markers: tuple[str, ...],
        allow_raw_fallback: bool,
        raw_fallback_requires_exact_text: bool,
    ) -> tuple[str, int, int] | None:
        """Choose one span by preferring slot-defined anchors over generic type markers."""
        if not span_candidates:
            return None
        anchored = self._anchored_spans(
            text=text,
            span_candidates=span_candidates,
            markers=tuple(self._slot_anchor_phrases(slot_def)),
        )
        if len(anchored) == 1:
            return anchored[0]
        if len(anchored) > 1:
            return None
        slot_anchors = tuple(self._slot_anchor_phrases(slot_def))
        explicit_anchor_required = self._slot_requires_explicit_anchor(
            slot_anchors=slot_anchors,
            generic_markers=generic_markers,
        )
        generic = self._anchored_spans(
            text=text,
            span_candidates=span_candidates,
            markers=generic_markers,
        )
        if explicit_anchor_required:
            generic = [
                candidate
                for candidate in generic
                if self._span_has_slot_role_hint(
                    text=text,
                    start=candidate[1],
                    end=candidate[2],
                    slot_def=slot_def,
                    generic_markers=generic_markers,
                )
            ]
        if len(generic) == 1:
            return generic[0]
        if len(generic) > 1:
            return None
        if explicit_anchor_required:
            return None
        stripped = self._strip_inline_whitespace(text)
        if (
            allow_raw_fallback
            and len(span_candidates) == 1
            and (
                not raw_fallback_requires_exact_text
                or stripped == span_candidates[0][0]
            )
        ):
            return span_candidates[0]
        return None

    def _slot_requires_explicit_anchor(
        self,
        *,
        slot_anchors: tuple[str, ...],
        generic_markers: tuple[str, ...],
    ) -> bool:
        """Return whether the slot schema names an explicit role that should not degrade to generic matching."""
        return bool(slot_anchors) and all(anchor not in generic_markers for anchor in slot_anchors)

    def _span_has_slot_role_hint(
        self,
        *,
        text: str,
        start: int,
        end: int,
        slot_def: IntentSlotDefinition,
        generic_markers: tuple[str, ...],
    ) -> bool:
        """Return whether the candidate clause contains a schema-derived role hint for this slot."""
        role_hints = self._slot_role_hints(slot_def=slot_def, generic_markers=generic_markers)
        if not role_hints:
            return False
        clause = self._candidate_clause(text=text, start=start, end=end)
        return any(role_hint in clause for role_hint in role_hints)

    def _slot_role_hints(
        self,
        *,
        slot_def: IntentSlotDefinition,
        generic_markers: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Derive stable role hints from structured slot anchors by stripping generic type words."""
        role_hints: list[str] = []
        for anchor in self._slot_anchor_phrases(slot_def):
            role_hint = anchor
            for marker in generic_markers:
                role_hint = role_hint.replace(marker, "")
            role_hint = role_hint.strip(" :：-_()（）[]【】0123456789")
            role_hint = role_hint.replace("后四位", "").replace("后4位", "").strip()
            if role_hint.endswith("人"):
                compact = role_hint[:-1].strip()
                if compact and compact not in role_hints:
                    role_hints.append(compact)
            if role_hint and role_hint not in role_hints:
                role_hints.append(role_hint)
        role_hints.sort(key=len, reverse=True)
        return tuple(role_hints)

    def _candidate_clause(
        self,
        *,
        text: str,
        start: int,
        end: int,
    ) -> str:
        """Return the local phrase group around a candidate span."""
        separators = "；;。！？!?\n"
        left = start
        right = end
        while left > 0 and text[left - 1] not in separators:
            left -= 1
        while right < len(text) and text[right] not in separators:
            right += 1
        return text[left:right]

    def _spans_share_phrase_group(
        self,
        *,
        text: str,
        left_span: tuple[str, int, int],
        right_span: tuple[str, int, int],
    ) -> bool:
        """Return whether two spans belong to the same semicolon-delimited phrase group."""
        left_group = self._candidate_clause(text=text, start=left_span[1], end=left_span[2])
        right_group = self._candidate_clause(text=text, start=right_span[1], end=right_span[2])
        return bool(left_group) and left_group == right_group

    def _claimed_numeric_values(self, memory_entries: list[str]) -> set[str]:
        """Collect numeric values that were already claimed elsewhere in the session context."""
        claimed: set[str] = set()
        for entry in memory_entries:
            if "=" not in entry:
                continue
            _slot_key, raw_value = entry.split("=", 1)
            digits = "".join(character for character in raw_value if character.isdigit())
            if digits:
                claimed.add(digits)
        return claimed

    def _span_has_amount_context(
        self,
        *,
        text: str,
        start: int,
        end: int,
    ) -> bool:
        """Return whether one digit span is immediately shaped like an amount token."""
        before = text[max(0, start - 6):start]
        after = text[end:end + 4].lstrip()
        return (
            after.startswith(("元", "块", "人民币"))
            or "金额" in before
            or before.endswith(("改成", "改为", "改到"))
        )

    def _anchored_spans(
        self,
        *,
        text: str,
        span_candidates: list[tuple[str, int, int]],
        markers: tuple[str, ...],
    ) -> list[tuple[str, int, int]]:
        """Return span candidates whose nearby text contains one explicit anchor phrase."""
        if not markers:
            return []
        return [
            candidate
            for candidate in span_candidates
            if self._span_has_marker(
                text=text,
                start=candidate[1],
                end=candidate[2],
                markers=markers,
            )
        ]

    def _span_has_marker(
        self,
        *,
        text: str,
        start: int,
        end: int,
        markers: tuple[str, ...],
    ) -> bool:
        """Return whether one marker appears near the candidate span."""
        before = text[max(0, start - 12):start]
        after = text[end:end + 12]
        return any(marker and (marker in before or marker in after) for marker in markers)

    def _slot_anchor_phrases(self, slot_def: IntentSlotDefinition) -> list[str]:
        """Return explicit slot-owned anchor phrases derived from structured schema metadata."""
        anchors: list[str] = []
        for raw_value in [slot_def.label, *slot_def.aliases]:
            cleaned = (raw_value or "").strip()
            if not cleaned or cleaned in anchors:
                continue
            anchors.append(cleaned)
        anchors.sort(key=len, reverse=True)
        return anchors

    def _strip_inline_whitespace(self, text: str) -> str:
        """Remove inline whitespace so raw-token fallbacks stay deterministic."""
        return "".join(character for character in text if not character.isspace())

    def _normalize_seed_binding(
        self,
        *,
        slot_def: IntentSlotDefinition,
        slot_key: str,
        value: Any,
        binding: SlotBindingState | None,
        grounding_text: str,
        turn_history_text: str,
        history_text: str,
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
        grounding_text: str,
        history_text: str,
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
