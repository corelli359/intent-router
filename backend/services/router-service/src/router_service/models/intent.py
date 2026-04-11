from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntentStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    GRAYSCALE = "grayscale"


class SlotValueType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    CURRENCY = "currency"
    PERSON_NAME = "person_name"
    ACCOUNT_NUMBER = "account_number"
    PHONE_LAST4 = "phone_last4"
    IDENTIFIER = "identifier"


class SlotOverwritePolicy(str, Enum):
    OVERWRITE_IF_NEW_NONEMPTY = "overwrite_if_new_nonempty"
    KEEP_ORIGINAL = "keep_original"
    ALWAYS_OVERWRITE = "always_overwrite"


class SlotBindingScope(str, Enum):
    NODE_INPUT = "node_input"
    CONDITION_OPERAND = "condition_operand"
    SHARED_PREFILL = "shared_prefill"


class SlotConfirmationPolicy(str, Enum):
    NEVER = "never"
    WHEN_AMBIGUOUS = "when_ambiguous"
    ALWAYS = "always"


class GraphConfirmPolicy(str, Enum):
    AUTO = "auto"
    ALWAYS = "always"
    MULTI_NODE_ONLY = "multi_node_only"
    NEVER = "never"


class IntentFieldDefinition(BaseModel):
    field_code: str = Field(min_length=1, max_length=128)
    label: str = Field(default="", max_length=128)
    semantic_definition: str = Field(default="", max_length=2000)
    value_type: SlotValueType = SlotValueType.STRING
    aliases: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    counter_examples: list[str] = Field(default_factory=list)
    format_hint: str = Field(default="", max_length=1000)
    normalization_hint: str = Field(default="", max_length=1000)
    validation_hint: str = Field(default="", max_length=1000)


class IntentSlotDefinition(BaseModel):
    slot_key: str = Field(min_length=1, max_length=128)
    field_code: str = Field(default="", max_length=128)
    role: str = Field(default="", max_length=128)
    label: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=2000)
    semantic_definition: str = Field(default="", max_length=2000)
    value_type: SlotValueType = SlotValueType.STRING
    required: bool = False
    allow_from_history: bool = False
    allow_from_recommendation: bool = True
    allow_from_context: bool = False
    aliases: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    counter_examples: list[str] = Field(default_factory=list)
    bind_scope: SlotBindingScope = SlotBindingScope.NODE_INPUT
    confirmation_policy: SlotConfirmationPolicy = SlotConfirmationPolicy.WHEN_AMBIGUOUS
    overwrite_policy: SlotOverwritePolicy = SlotOverwritePolicy.OVERWRITE_IF_NEW_NONEMPTY
    prompt_hint: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_slot_definition(self) -> "IntentSlotDefinition":
        if self.required and not (self.semantic_definition or self.field_code or self.description):
            raise ValueError("required slot must define semantic_definition, field_code, or description")
        return self


class IntentGraphBuildHints(BaseModel):
    intent_scope_rule: str = Field(default="", max_length=2000)
    planner_notes: str = Field(default="", max_length=2000)
    single_node_examples: list[str] = Field(default_factory=list)
    multi_node_examples: list[str] = Field(default_factory=list)
    provides_context_keys: list[str] = Field(default_factory=list)
    confirm_policy: GraphConfirmPolicy = GraphConfirmPolicy.AUTO
    max_nodes_per_message: int = Field(default=4, ge=1, le=32)


class IntentPayload(BaseModel):
    intent_code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=4000)
    examples: list[str] = Field(default_factory=list)
    agent_url: str = Field(min_length=1, max_length=2048)
    status: IntentStatus = IntentStatus.INACTIVE
    is_fallback: bool = False
    dispatch_priority: int = Field(default=100, ge=0, le=10_000)
    request_schema: dict[str, Any] = Field(default_factory=dict)
    field_mapping: dict[str, str] = Field(default_factory=dict)
    field_catalog: list[IntentFieldDefinition] = Field(default_factory=list)
    slot_schema: list[IntentSlotDefinition] = Field(default_factory=list)
    graph_build_hints: IntentGraphBuildHints = Field(default_factory=IntentGraphBuildHints)
    resume_policy: str = Field(default="resume_same_task", min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_payload(self) -> "IntentPayload":
        scheme = urlparse(self.agent_url.strip()).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError("agent_url must use http:// or https://")
        field_codes = {item.field_code for item in self.field_catalog}
        seen_slot_keys: set[str] = set()
        for slot in self.slot_schema:
            if slot.slot_key in seen_slot_keys:
                raise ValueError(f"duplicate slot_key in slot_schema: {slot.slot_key}")
            seen_slot_keys.add(slot.slot_key)
            if slot.field_code and field_codes and slot.field_code not in field_codes:
                raise ValueError(f"slot field_code not found in field_catalog: {slot.field_code}")
        return self


class IntentRecord(IntentPayload):
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
