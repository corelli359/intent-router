from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from router_service.core.shared.domain import IntentDefinition
from router_service.core.shared.diagnostics import (
    RouterDiagnostic,
    RouterDiagnosticCode,
    diagnostic,
    merge_diagnostics,
)
from router_service.core.support.llm_barrier import llm_barrier_triggered
from router_service.core.support.llm_client import JsonLLMClient, llm_exception_is_retryable
from router_service.core.prompts.prompt_templates import (
    DEFAULT_SLOT_EXTRACTOR_HUMAN_PROMPT,
    DEFAULT_SLOT_EXTRACTOR_SYSTEM_PROMPT,
    build_slot_extractor_prompt,
)
from router_service.core.recognition.recognizer import recognition_intent_payload
from router_service.core.slots.grounding import (
    CURRENCY_ALIASES_BY_CODE,
    combine_distinct_text,
    normalize_structured_slot_memory,
    slot_has_currency_semantics,
    slot_semantic_signature,
    slot_value_grounded_with_currency_fallback,
)
from router_service.core.shared.graph_domain import GraphNodeState, SlotBindingSource, SlotBindingState
from router_service.models.intent import IntentSlotDefinition, SlotOverwritePolicy, SlotValueType


logger = logging.getLogger(__name__)

CARD_NUMBER_RE = re.compile(r"(?<!\d)(\d{6,20})(?!\d)")
PHONE_LAST4_RE = re.compile(r"(?:后4位|后四位|尾号)\D*(\d{4})")
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|人民币)")
GENERIC_NUMBER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")
ACTION_AMOUNT_RE = re.compile(
    r"(?:转账|转给|转|汇款|付款|支付|缴费|交|缴|还款|换汇|换|买入|卖出)[^\d]{0,8}(\d+(?:\.\d+)?)"
)
CHANGE_AMOUNT_RE = re.compile(r"(?:改成|改为|改到|金额改成|金额改为|金额改到)\D*(\d+(?:\.\d+)?)")
NAME_RE = re.compile(
    r"(?:给|向|转给|转账给)([\u4e00-\u9fffA-Za-z]{2,16}?)(?=(?:转账|转|汇款|付款|支付|卡号|银行卡|手机号|尾号|后4位|后四位|金额|[，,。\s]|$))"
)
DATE_RE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日)")


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
    """Extract slot candidates from node context using heuristics plus optional LLM help."""

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
        long_term_memory: list[str] | None = None,
    ) -> SlotExtractionResult:
        """Extract slot candidates from seed bindings, heuristics, and optional LLM output."""
        slot_schema = intent.slot_schema
        slot_defs_by_key = {slot.slot_key: slot for slot in slot_schema}
        history_text = "\n".join(entry for entry in (long_term_memory or []) if entry)
        grounding_text = combine_distinct_text(graph_source_message, node.source_fragment, current_message)
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
                history_text=history_text,
                from_history=slot_key in node.history_slot_keys,
            )
            if normalized_binding is None:
                continue
            merged_memory[slot_key] = normalized_binding.value
            merged_bindings[slot_key] = normalized_binding
            if is_history:
                history_slot_keys.append(slot_key)

        for heuristic_text in (
            node.source_fragment,
            current_message,
            graph_source_message,
        ):
            self._merge_items(
                merged_memory=merged_memory,
                merged_bindings=merged_bindings,
                history_slot_keys=history_slot_keys,
                slot_defs_by_key=slot_defs_by_key,
                items=self._extract_with_heuristics(intent=intent, text=heuristic_text or ""),
                grounding_text=grounding_text,
                history_text=history_text,
                allow_replace_existing_user_message=bool(
                    heuristic_text
                    and heuristic_text == current_message
                    and current_message not in {node.source_fragment, graph_source_message}
                ),
            )

        llm_missing_required = any(
            slot.required and slot.slot_key not in merged_memory
            for slot in slot_schema
        )
        ambiguous_slot_keys: list[str] = []
        diagnostics: list[RouterDiagnostic] = []
        llm_barrier_enabled = bool(getattr(self.llm_client, "barrier_enabled", False))
        if self.llm_client is not None and not llm_barrier_enabled and (llm_missing_required or not merged_memory):
            llm_result = await self._extract_with_llm(
                intent=intent,
                current_message=current_message or graph_source_message,
                source_fragment=node.source_fragment or graph_source_message,
                existing_slot_memory=merged_memory,
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
        elif llm_barrier_enabled and (llm_missing_required or not merged_memory):
            logger.debug(
                "Skipping LLM slot extraction because router perf barrier is enabled "
                "(intent_code=%s, missing_required=%s, merged_memory_empty=%s)",
                intent.intent_code,
                llm_missing_required,
                not merged_memory,
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
        grounding_text: str,
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
            evidence_text = combine_distinct_text(
                binding.source_text if binding is not None else None,
                history_text,
            )
            if not evidence_text or not self._value_is_grounded(
                slot_def=slot_def,
                value=value,
                grounding_text=evidence_text,
            ):
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

        evidence_text = binding.source_text if binding is not None and binding.source_text else grounding_text
        if not self._value_is_grounded(slot_def=slot_def, value=value, grounding_text=evidence_text):
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

    async def _extract_with_llm(
        self,
        *,
        intent: IntentDefinition,
        current_message: str,
        source_fragment: str,
        existing_slot_memory: dict[str, Any],
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
                    "intent_json": json.dumps(
                        recognition_intent_payload(intent),
                        ensure_ascii=False,
                    ),
                    "existing_slot_memory_json": json.dumps(existing_slot_memory, ensure_ascii=False),
                },
                model=self.model,
            )
        except Exception as exc:
            if llm_exception_is_retryable(exc) or llm_barrier_triggered(exc):
                logger.debug(
                    "Slot extraction LLM is temporarily unavailable, preserving heuristic extraction",
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
                                message="提槽 LLM 暂时不可用，已仅保留启发式提取结果",
                                details={"error_type": type(exc).__name__},
                            ).model_dump(mode="json")
                        ],
                    }
                )
            logger.debug("Slot extraction LLM failed, falling back to heuristic extraction", exc_info=True)
            return SlotExtractionPayload.model_validate(
                {
                    "slots": [],
                    "ambiguousSlotKeys": [],
                    "diagnostics": [
                        diagnostic(
                            RouterDiagnosticCode.SLOT_EXTRACTOR_LLM_FAILED_HEURISTIC_ONLY,
                            source="slot_extractor",
                            message="提槽 LLM 失败，已仅保留启发式提取结果",
                            details={"error_type": type(exc).__name__},
                        ).model_dump(mode="json")
                    ],
                }
            )
        payload = SlotExtractionPayload.model_validate(raw_response)
        payload.diagnostics = payload.diagnostics or []
        return payload

    def _extract_with_heuristics(
        self,
        *,
        intent: IntentDefinition,
        text: str,
    ) -> list[SlotExtractionItemPayload]:
        """Run deterministic regex and lexical heuristics over one text fragment."""
        if not text:
            return []
        items: list[SlotExtractionItemPayload] = []
        for slot_def in intent.slot_schema:
            payload = self._extract_slot_value(slot_def=slot_def, text=text)
            if payload is not None:
                items.append(payload)
        return items

    def _extract_slot_value(
        self,
        *,
        slot_def: IntentSlotDefinition,
        text: str,
    ) -> SlotExtractionItemPayload | None:
        """Extract one slot value from text according to the slot's semantic type."""
        value: Any | None = None
        source_text: str | None = None

        if slot_def.value_type == SlotValueType.PERSON_NAME:
            matched = NAME_RE.search(text)
            if matched is not None:
                value = matched.group(1)
                source_text = matched.group(0)
        elif slot_def.value_type == SlotValueType.PHONE_LAST4:
            matched = self._extract_phone_last4_match(slot_def=slot_def, text=text)
            if matched is not None:
                value = matched.group(1)
                source_text = matched.group(0)
        elif slot_def.value_type in {
            SlotValueType.CURRENCY,
            SlotValueType.NUMBER,
            SlotValueType.INTEGER,
        }:
            matched = CHANGE_AMOUNT_RE.search(text)
            if matched is None:
                matched = ACTION_AMOUNT_RE.search(text)
            if matched is None:
                matched = AMOUNT_RE.search(text)
            if matched is None and slot_def.value_type in {SlotValueType.NUMBER, SlotValueType.INTEGER}:
                matched = GENERIC_NUMBER_RE.search(text)
            if matched is not None:
                value = matched.group(1)
                source_text = matched.group(0)
        elif slot_def.value_type in {
            SlotValueType.ACCOUNT_NUMBER,
            SlotValueType.IDENTIFIER,
        }:
            matched = self._extract_account_number_match(slot_def=slot_def, text=text)
            if matched is not None:
                value = matched.group(1)
                source_text = matched.group(0)
        elif slot_def.value_type == SlotValueType.DATE:
            matched = DATE_RE.search(text)
            if matched is not None:
                value = matched.group(1)
                source_text = matched.group(0)
        elif slot_def.value_type == SlotValueType.BOOLEAN:
            lowered = text.lower()
            if any(token in lowered for token in ("是", "需要", "要", "yes", "true")):
                value = True
                source_text = text
            elif any(token in lowered for token in ("否", "不要", "不用", "no", "false")):
                value = False
                source_text = text
        else:
            value, source_text = self._extract_currency_like_slot(slot_def=slot_def, text=text)

        if value is None and slot_def.value_type == SlotValueType.STRING:
            value, source_text = self._extract_string_slot(slot_def=slot_def, text=text)

        if value is None:
            return None
        return SlotExtractionItemPayload(
            slot_key=slot_def.slot_key,
            value=value,
            source=SlotBindingSource.USER_MESSAGE,
            source_text=source_text or text,
            confidence=0.85,
        )

    def _extract_currency_like_slot(
        self,
        *,
        slot_def: IntentSlotDefinition,
        text: str,
    ) -> tuple[Any | None, str | None]:
        """Extract currency-like string slots such as source or target currency."""
        slot_signature = slot_semantic_signature(slot_def)
        upper_text = text.upper()
        if slot_has_currency_semantics(slot_def):
            for currency_code, aliases in CURRENCY_ALIASES_BY_CODE.items():
                for alias in aliases:
                    if alias.upper() in upper_text or alias in text:
                        if "source" in slot_signature or "sell" in slot_signature or "卖出" in slot_signature:
                            if currency_code == "USD" and ("人民币" in text or "CNY" in upper_text):
                                continue
                        if "target" in slot_signature or "buy" in slot_signature or "买入" in slot_signature:
                            if currency_code == "CNY" and ("美元" in text or "USD" in upper_text):
                                continue
                        return currency_code, alias
        return None, None

    def _extract_string_slot(
        self,
        *,
        slot_def: IntentSlotDefinition,
        text: str,
    ) -> tuple[Any | None, str | None]:
        """Extract generic string slots with special handling for currency semantics."""
        slot_signature = slot_semantic_signature(slot_def)
        if any(token in slot_signature for token in ("card", "account", "卡号", "账号", "账户", "银行卡")):
            matched = self._extract_account_number_match(slot_def=slot_def, text=text)
            if matched is not None:
                return matched.group(1), matched.group(0)
        if any(token in slot_signature for token in ("phone", "手机号", "尾号", "后4位", "后四位")):
            matched = self._extract_phone_last4_match(slot_def=slot_def, text=text)
            if matched is not None:
                return matched.group(1), matched.group(0)
        if any(token in slot_signature for token in ("person", "name", "姓名", "收款人", "付款人")):
            matched = NAME_RE.search(text)
            if matched is not None:
                return matched.group(1), matched.group(0)
        if slot_has_currency_semantics(slot_def):
            return self._extract_currency_like_slot(slot_def=slot_def, text=text)
        return None, None

    def _extract_account_number_match(
        self,
        *,
        slot_def: IntentSlotDefinition,
        text: str,
    ) -> re.Match[str] | None:
        """Find an account-like identifier using slot-specific regex patterns."""
        slot_signature = slot_semantic_signature(slot_def)
        patterns: list[str] = []
        if any(token in slot_signature for token in ("gas", "燃气", "户号")):
            patterns.extend([r"(?:燃气户号|户号)\D*(\d{6,20})"])
        if any(token in slot_signature for token in ("recipient", "收款", "对方")):
            patterns.extend([r"(?:收款卡号|收款银行卡号|对方卡号|收款账户|收款账号)\D*(\d{6,20})"])
        else:
            patterns.extend(
                [
                    r"(?:我的卡号|本人卡号|本人的卡号)\D*(\d{6,20})",
                    r"(?:信用卡卡号|银行卡号|卡号)\D*(\d{6,20})",
                ]
            )
            patterns.append(r"(?<!\d)(\d{6,20})(?!\d)")
        for pattern in patterns:
            matched = re.search(pattern, text)
            if matched is not None:
                return matched
        return None

    def _extract_phone_last4_match(
        self,
        *,
        slot_def: IntentSlotDefinition,
        text: str,
    ) -> re.Match[str] | None:
        """Find phone-last4 values using recipient-aware and self-aware regex patterns."""
        slot_signature = slot_semantic_signature(slot_def)
        patterns: list[str] = []
        if any(token in slot_signature for token in ("recipient", "收款", "对方")):
            patterns.extend(
                [
                    r"(?:收款人手机号后4位|收款人手机号后四位|收款手机号后4位|收款手机号后四位)\D*(\d{4})",
                    r"(?:收款人尾号|收款尾号)\D*(\d{4})",
                ]
            )
            if any(token in text for token in ("收款", "对方")):
                patterns.extend(
                    [
                        r"(?:手机号后4位|手机号后四位|后4位|后四位)\D*(\d{4})",
                        r"(?:尾号)\D*(\d{4})",
                    ]
                )
        else:
            patterns.extend(
                [
                    r"(?:我的(?:手机号)?后4位|我的(?:手机号)?后四位|我的尾号)\D*(\d{4})",
                    r"(?:手机号后4位|手机号后四位|尾号)\D*(\d{4})",
                ]
            )
        for pattern in patterns:
            matched = re.search(pattern, text)
            if matched is not None:
                return matched
        if any(token in slot_signature for token in ("recipient", "收款", "对方")):
            return None
        return PHONE_LAST4_RE.search(text)

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
                evidence_text = item.source_text or history_text
            elif source == SlotBindingSource.RECOMMENDATION:
                if not slot_def.allow_from_recommendation:
                    continue
                evidence_text = item.source_text or grounding_text
            else:
                evidence_text = item.source_text or grounding_text

            if source != SlotBindingSource.RECOMMENDATION and not self._value_is_grounded(
                slot_def=slot_def,
                value=value,
                grounding_text=evidence_text,
            ):
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

    def _value_is_grounded(
        self,
        *,
        slot_def: IntentSlotDefinition,
        value: Any,
        grounding_text: str,
    ) -> bool:
        """Apply grounding checks, including currency-specific fallback matching."""
        return slot_value_grounded_with_currency_fallback(
            slot_def=slot_def,
            value=value,
            grounding_text=grounding_text,
        )
