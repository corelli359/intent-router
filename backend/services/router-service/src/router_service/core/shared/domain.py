from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from router_service.models.intent import IntentFieldDefinition, IntentGraphBuildHints, IntentSlotDefinition


SESSION_TTL = timedelta(minutes=30)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChatMessage(BaseModel):
    role: str
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class IntentDefinition(BaseModel):
    intent_code: str
    name: str
    description: str
    domain_code: str = ""
    domain_name: str = ""
    domain_description: str = ""
    examples: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    agent_url: str
    status: str = "active"
    is_fallback: bool = False
    dispatch_priority: int = 0
    primary_threshold: float = 0.75
    candidate_threshold: float = 0.5
    request_schema: dict[str, Any] = Field(default_factory=dict)
    field_mapping: dict[str, str] = Field(default_factory=dict)
    field_catalog: list[IntentFieldDefinition] = Field(default_factory=list)
    slot_schema: list[IntentSlotDefinition] = Field(default_factory=list)
    graph_build_hints: IntentGraphBuildHints = Field(default_factory=IntentGraphBuildHints)
    resume_policy: str = "resume_same_task"
    is_leaf_intent: bool = True
    parent_intent_code: str = ""
    routing_examples: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IntentDomain:
    domain_code: str
    domain_name: str
    domain_description: str
    routing_examples: tuple[str, ...]
    leaf_intents: tuple[IntentDefinition, ...]
    dispatch_priority: int

    @property
    def is_single_leaf(self) -> bool:
        return len(self.leaf_intents) == 1


class IntentMatch(BaseModel):
    intent_code: str
    confidence: float
    reason: str


class TaskEvent(BaseModel):
    event: str
    task_id: str
    session_id: str
    intent_code: str
    status: TaskStatus
    message: str | None = None
    ishandover: bool | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Task(BaseModel):
    task_id: str = Field(default_factory=lambda: f"task_{uuid4().hex[:10]}")
    session_id: str
    intent_code: str
    agent_url: str
    intent_name: str = ""
    intent_description: str = ""
    intent_examples: list[str] = Field(default_factory=list)
    request_schema: dict[str, Any] = Field(default_factory=dict)
    field_mapping: dict[str, str] = Field(default_factory=dict)
    confidence: float
    status: TaskStatus = TaskStatus.CREATED
    input_context: dict[str, Any] = Field(default_factory=dict)
    slot_memory: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def touch(self, status: TaskStatus) -> None:
        self.status = status
        self.updated_at = utc_now()


class LongTermMemoryEntry(BaseModel):
    cust_id: str
    memory_type: str
    content: str
    source_session_id: str
    created_at: datetime = Field(default_factory=utc_now)


class CustomerMemory(BaseModel):
    cust_id: str
    facts: list[LongTermMemoryEntry] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    def remember(self, entry: LongTermMemoryEntry) -> None:
        self.facts.append(entry)
        self.updated_at = utc_now()


class AgentStreamChunk(BaseModel):
    task_id: str
    event: str
    content: str
    ishandover: bool
    status: TaskStatus
    payload: dict[str, Any] = Field(default_factory=dict)
