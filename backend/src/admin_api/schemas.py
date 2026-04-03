from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from models.intent import IntentRecord, IntentStatus


class IntentCreateRequest(BaseModel):
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


class IntentUpdateRequest(IntentCreateRequest):
    pass


class IntentResponse(BaseModel):
    intent_code: str
    name: str
    description: str
    examples: list[str]
    agent_url: str
    status: IntentStatus
    dispatch_priority: int
    request_schema: dict[str, Any]
    field_mapping: dict[str, str]
    resume_policy: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: IntentRecord) -> "IntentResponse":
        return cls(**record.model_dump())


class IntentListResponse(BaseModel):
    items: list[IntentResponse]
    total: int

