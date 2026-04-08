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


class GraphConfirmPolicy(str, Enum):
    AUTO = "auto"
    ALWAYS = "always"
    MULTI_NODE_ONLY = "multi_node_only"
    NEVER = "never"


class IntentSlotDefinition(BaseModel):
    slot_key: str = Field(min_length=1, max_length=128)
    label: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=2000)
    value_type: SlotValueType = SlotValueType.STRING
    required: bool = False
    allow_from_history: bool = False
    aliases: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    overwrite_policy: SlotOverwritePolicy = SlotOverwritePolicy.OVERWRITE_IF_NEW_NONEMPTY


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
    slot_schema: list[IntentSlotDefinition] = Field(default_factory=list)
    graph_build_hints: IntentGraphBuildHints = Field(default_factory=IntentGraphBuildHints)
    resume_policy: str = Field(default="resume_same_task", min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_agent_url(self) -> "IntentPayload":
        scheme = urlparse(self.agent_url.strip()).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError("agent_url must use http:// or https://")
        return self


class IntentRecord(IntentPayload):
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
