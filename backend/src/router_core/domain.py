from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


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


class SessionPlanStatus(StrEnum):
    DRAFT = "draft"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNNING = "running"
    PARTIALLY_COMPLETED = "partially_completed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SessionPlanItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ChatMessage(BaseModel):
    role: str
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class IntentDefinition(BaseModel):
    intent_code: str
    name: str
    description: str
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
    resume_policy: str = "resume_same_task"


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


class SessionPlanItem(BaseModel):
    intent_code: str
    title: str
    confidence: float
    task_id: str | None = None
    status: SessionPlanItemStatus = SessionPlanItemStatus.PENDING


class SessionPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: f"plan_{uuid4().hex[:10]}")
    source_message: str
    confirm_token: str = Field(default_factory=lambda: uuid4().hex)
    status: SessionPlanStatus = SessionPlanStatus.WAITING_CONFIRMATION
    items: list[SessionPlanItem] = Field(default_factory=list)
    source: str = "router"
    card_type: str = "plan_confirm"
    title: str = "请确认执行计划"
    summary: str = ""
    version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def touch(self, status: SessionPlanStatus | None = None) -> None:
        if status is not None:
            self.status = status
        self.updated_at = utc_now()


class SessionState(BaseModel):
    session_id: str
    cust_id: str
    messages: list[ChatMessage] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    candidate_intents: list[IntentMatch] = Field(default_factory=list)
    pending_plan: SessionPlan | None = None
    active_task_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + SESSION_TTL)

    def touch(self) -> None:
        now = utc_now()
        self.updated_at = now
        self.expires_at = now + SESSION_TTL

    def is_expired(self, now: datetime | None = None) -> bool:
        current = now or utc_now()
        return current >= self.expires_at


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


class RouterSnapshot(BaseModel):
    session_id: str
    cust_id: str
    messages: list[ChatMessage]
    tasks: list[Task]
    candidate_intents: list[IntentMatch]
    pending_plan: SessionPlan | None = None
    active_task_id: str | None
    expires_at: datetime


class AgentStreamChunk(BaseModel):
    task_id: str
    event: str
    content: str
    ishandover: bool
    status: TaskStatus
    payload: dict[str, Any] = Field(default_factory=dict)
