from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from router_core.domain import ChatMessage, IntentMatch, SESSION_TTL, Task, utc_now


class GraphEdgeType(StrEnum):
    SEQUENTIAL = "sequential"
    CONDITIONAL = "conditional"
    PARALLEL = "parallel"


class GraphNodeStatus(StrEnum):
    DRAFT = "draft"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class GraphNodeSkipReason(StrEnum):
    CONDITION_NOT_MET = "condition_not_met"
    UPSTREAM_FAILED = "upstream_failed"
    UPSTREAM_CANCELLED = "upstream_cancelled"
    UPSTREAM_SKIPPED = "upstream_skipped"


class GraphStatus(StrEnum):
    DRAFT = "draft"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNNING = "running"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_CONFIRMATION_NODE = "waiting_confirmation_node"
    PARTIALLY_COMPLETED = "partially_completed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GraphAction(BaseModel):
    code: str
    label: str


class GraphCondition(BaseModel):
    source_node_id: str
    expected_statuses: list[str] = Field(default_factory=lambda: [GraphNodeStatus.COMPLETED.value])
    left_key: str | None = None
    operator: str | None = None
    right_value: float | int | str | bool | None = None


class GraphEdge(BaseModel):
    edge_id: str = Field(default_factory=lambda: f"edge_{uuid4().hex[:10]}")
    source_node_id: str
    target_node_id: str
    relation_type: GraphEdgeType
    label: str | None = None
    condition: GraphCondition | None = None


class GraphNodeState(BaseModel):
    node_id: str = Field(default_factory=lambda: f"node_{uuid4().hex[:10]}")
    intent_code: str
    title: str
    confidence: float
    position: int = 0
    source_fragment: str | None = None
    status: GraphNodeStatus = GraphNodeStatus.DRAFT
    task_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    blocking_reason: str | None = None
    skip_reason_code: str | None = None
    relation_reason: str | None = None
    slot_memory: dict[str, Any] = Field(default_factory=dict)
    history_slot_keys: list[str] = Field(default_factory=list)
    output_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def touch(
        self,
        status: GraphNodeStatus,
        *,
        blocking_reason: str | None = None,
        skip_reason_code: str | None = None,
    ) -> None:
        self.status = status
        self.blocking_reason = blocking_reason
        self.skip_reason_code = skip_reason_code
        self.updated_at = utc_now()


class ExecutionGraphState(BaseModel):
    graph_id: str = Field(default_factory=lambda: f"graph_{uuid4().hex[:10]}")
    source_message: str
    summary: str = ""
    version: int = 1
    status: GraphStatus = GraphStatus.DRAFT
    confirm_token: str = Field(default_factory=lambda: uuid4().hex)
    nodes: list[GraphNodeState] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    actions: list[GraphAction] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def touch(self, status: GraphStatus | None = None) -> None:
        if status is not None:
            self.status = status
        self.updated_at = utc_now()

    def node_by_id(self, node_id: str) -> GraphNodeState:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(f"node not found: {node_id}")

    def incoming_edges(self, node_id: str) -> list[GraphEdge]:
        return [edge for edge in self.edges if edge.target_node_id == node_id]

    def outgoing_edges(self, node_id: str) -> list[GraphEdge]:
        return [edge for edge in self.edges if edge.source_node_id == node_id]


class GraphSessionState(BaseModel):
    session_id: str
    cust_id: str
    messages: list[ChatMessage] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    candidate_intents: list[IntentMatch] = Field(default_factory=list)
    current_graph: ExecutionGraphState | None = None
    pending_graph: ExecutionGraphState | None = None
    active_node_id: str | None = None
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


class GraphRouterSnapshot(BaseModel):
    session_id: str
    cust_id: str
    messages: list[ChatMessage]
    candidate_intents: list[IntentMatch]
    current_graph: ExecutionGraphState | None = None
    pending_graph: ExecutionGraphState | None = None
    active_node_id: str | None = None
    expires_at: datetime


class GuidedSelectionIntent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    intent_code: str = Field(alias="intentCode")
    title: str | None = None
    source_fragment: str | None = Field(default=None, alias="sourceFragment")
    slot_memory: dict[str, Any] = Field(default_factory=dict, alias="slotMemory")


class GuidedSelectionPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    selected_intents: list[GuidedSelectionIntent] = Field(default_factory=list, alias="selectedIntents")


class RecommendationIntent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    intent_code: str = Field(alias="intentCode")
    title: str | None = None
    description: str | None = None
    examples: list[str] = Field(default_factory=list)


class RecommendationContextPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recommendation_id: str | None = Field(default=None, alias="recommendationId")
    intents: list[RecommendationIntent] = Field(default_factory=list, alias="intents")
