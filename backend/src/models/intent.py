from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntentStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    GRAYSCALE = "grayscale"


class IntentPayload(BaseModel):
    intent_code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=4000)
    examples: list[str] = Field(default_factory=list)
    agent_url: str = Field(min_length=1, max_length=2048)
    status: IntentStatus = IntentStatus.ACTIVE
    dispatch_priority: int = Field(default=100, ge=0, le=10_000)
    request_schema: dict[str, Any] = Field(default_factory=dict)
    field_mapping: dict[str, str] = Field(default_factory=dict)
    resume_policy: str = Field(default="resume_same_task", min_length=1, max_length=128)


class IntentRecord(IntentPayload):
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

