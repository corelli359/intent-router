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
    """Return the current UTC timestamp used across runtime domain models."""
    return datetime.now(timezone.utc)


class TaskStatus(StrEnum):
    """Lifecycle status shared by router tasks, nodes, and SSE events."""

    CREATED = "created"
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    READY_FOR_DISPATCH = "ready_for_dispatch"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChatMessage(BaseModel):
    """One chat transcript item stored in a router session."""

    role: str
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class IntentDefinition(BaseModel):
    """Runtime intent definition consumed by recognition, planning, and dispatch."""

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
    """Domain-level view of related leaf intents used in hierarchical routing."""

    domain_code: str
    domain_name: str
    domain_description: str
    routing_examples: tuple[str, ...]
    leaf_intents: tuple[IntentDefinition, ...]
    dispatch_priority: int

    @property
    def is_single_leaf(self) -> bool:
        """Report whether the domain contains exactly one active leaf intent."""
        return len(self.leaf_intents) == 1


class IntentMatch(BaseModel):
    """Recognition match containing an intent code, confidence, and reason."""

    intent_code: str
    confidence: float
    reason: str


class TaskEvent(BaseModel):
    """Event payload published over SSE for graph, node, and session state changes."""

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
    """Router-side task passed to the downstream intent agent."""

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
        """Update the task status and refresh its timestamp."""
        self.status = status
        self.updated_at = utc_now()


class LongTermMemoryEntry(BaseModel):
    """One long-term memory fact promoted out of a session."""

    cust_id: str
    memory_type: str
    content: str
    source_session_id: str
    created_at: datetime = Field(default_factory=utc_now)


class CustomerMemory(BaseModel):
    """Collection of long-term memory facts for one customer."""

    cust_id: str
    facts: list[LongTermMemoryEntry] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    def remember(self, entry: LongTermMemoryEntry) -> None:
        """Append one memory fact and refresh the update timestamp."""
        self.facts.append(entry)
        self.updated_at = utc_now()


class AgentStreamChunk(BaseModel):
    """Normalized streaming chunk returned by an intent agent."""

    task_id: str
    event: str
    content: str
    ishandover: bool
    status: TaskStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
