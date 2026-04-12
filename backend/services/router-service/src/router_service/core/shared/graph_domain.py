from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from router_service.core.shared.domain import ChatMessage, IntentMatch, SESSION_TTL, Task, utc_now


class GraphEdgeType(StrEnum):
    """Relationship type between two graph nodes."""

    SEQUENTIAL = "sequential"
    CONDITIONAL = "conditional"
    PARALLEL = "parallel"


class GraphNodeStatus(StrEnum):
    """Execution status for one graph node."""

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
    """Reason code explaining why a node was skipped instead of executed."""

    CONDITION_NOT_MET = "condition_not_met"
    UPSTREAM_FAILED = "upstream_failed"
    UPSTREAM_CANCELLED = "upstream_cancelled"
    UPSTREAM_SKIPPED = "upstream_skipped"


class GraphStatus(StrEnum):
    """Aggregate execution status for the whole graph."""

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
    """User-visible action exposed on a graph card."""

    code: str
    label: str


class GraphCondition(BaseModel):
    """Runtime-evaluable edge condition produced by the planner/compiler."""

    source_node_id: str
    expected_statuses: list[str] = Field(default_factory=lambda: [GraphNodeStatus.COMPLETED.value])
    left_key: str | None = None
    operator: str | None = None
    right_value: float | int | str | bool | None = None


class GraphEdge(BaseModel):
    """Directed edge connecting two graph nodes, optionally with a condition."""

    edge_id: str = Field(default_factory=lambda: f"edge_{uuid4().hex[:10]}")
    source_node_id: str
    target_node_id: str
    relation_type: GraphEdgeType
    label: str | None = None
    condition: GraphCondition | None = None


class SlotBindingSource(StrEnum):
    """Provenance of one slot value bound onto a graph node."""

    USER_MESSAGE = "user_message"
    HISTORY = "history"
    RECOMMENDATION = "recommendation"
    AGENT = "agent"
    RUNTIME_PREFILL = "runtime_prefill"


class SlotBindingState(BaseModel):
    """Tracks where a slot value came from so later turns can explain or override it."""

    slot_key: str
    value: Any
    source: SlotBindingSource = SlotBindingSource.USER_MESSAGE
    source_text: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    is_modified: bool = False


class GraphNodeState(BaseModel):
    """Execution-time node state for a single leaf intent in the graph."""

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
    slot_bindings: list[SlotBindingState] = Field(default_factory=list)
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
        """Update node status metadata and refresh its timestamp."""
        self.status = status
        self.blocking_reason = blocking_reason
        self.skip_reason_code = skip_reason_code
        self.updated_at = utc_now()


class ExecutionGraphState(BaseModel):
    """Mutable execution graph owned by the router layer for one user turn."""

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
        """Update graph status metadata and refresh its timestamp."""
        if status is not None:
            self.status = status
        self.updated_at = utc_now()

    def node_by_id(self, node_id: str) -> GraphNodeState:
        """Return one graph node by id or raise when it does not exist."""
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(f"node not found: {node_id}")

    def incoming_edges(self, node_id: str) -> list[GraphEdge]:
        """Return all edges pointing into the given node."""
        return [edge for edge in self.edges if edge.target_node_id == node_id]

    def outgoing_edges(self, node_id: str) -> list[GraphEdge]:
        """Return all edges starting from the given node."""
        return [edge for edge in self.edges if edge.source_node_id == node_id]


class GraphSessionState(BaseModel):
    """Router session state spanning messages, tasks, current graph, and pending graph."""

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
        """Refresh session timestamps and extend the expiry deadline."""
        now = utc_now()
        self.updated_at = now
        self.expires_at = now + SESSION_TTL

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return whether the session TTL has elapsed."""
        current = now or utc_now()
        return current >= self.expires_at


class GraphRouterSnapshot(BaseModel):
    """Read model returned to API callers and SSE subscribers."""

    session_id: str
    cust_id: str
    messages: list[ChatMessage]
    candidate_intents: list[IntentMatch]
    current_graph: ExecutionGraphState | None = None
    pending_graph: ExecutionGraphState | None = None
    active_node_id: str | None = None
    expires_at: datetime


class GuidedSelectionIntent(BaseModel):
    """One explicitly selected intent coming from recommendation or UI input."""

    model_config = ConfigDict(populate_by_name=True)

    intent_code: str = Field(alias="intentCode")
    title: str | None = None
    source_fragment: str | None = Field(default=None, alias="sourceFragment")
    slot_memory: dict[str, Any] = Field(default_factory=dict, alias="slotMemory")


class GuidedSelectionPayload(BaseModel):
    """Structured intent selection injected by recommendation or external UI."""

    model_config = ConfigDict(populate_by_name=True)

    selected_intents: list[GuidedSelectionIntent] = Field(default_factory=list, alias="selectedIntents")


class RecommendationIntent(BaseModel):
    """One frontend-provided recommendation candidate for context-only routing."""

    model_config = ConfigDict(populate_by_name=True)

    intent_code: str = Field(alias="intentCode")
    title: str | None = None
    description: str | None = None
    examples: list[str] = Field(default_factory=list)


class RecommendationContextPayload(BaseModel):
    """Non-binding recommendation context shown to the recognizer/planner."""

    model_config = ConfigDict(populate_by_name=True)

    recommendation_id: str | None = Field(default=None, alias="recommendationId")
    intents: list[RecommendationIntent] = Field(default_factory=list, alias="intents")


class ProactiveRecommendationRouteMode(StrEnum):
    """Routing mode selected for a proactive recommendation turn."""

    NO_SELECTION = "no_selection"
    DIRECT_EXECUTE = "direct_execute"
    INTERACTIVE_GRAPH = "interactive_graph"
    SWITCH_TO_FREE_DIALOG = "switch_to_free_dialog"


class ProactiveRecommendationItem(BaseModel):
    """One actionable proactive recommendation item with optional default slots."""

    model_config = ConfigDict(populate_by_name=True)

    recommendation_item_id: str = Field(alias="recommendationItemId")
    intent_code: str = Field(alias="intentCode")
    title: str
    description: str | None = None
    slot_memory: dict[str, Any] = Field(default_factory=dict, alias="slotMemory")
    execution_payload: dict[str, Any] = Field(default_factory=dict, alias="executionPayload")
    allow_direct_execute: bool = Field(default=True, alias="allowDirectExecute")


class ProactiveRecommendationPayload(BaseModel):
    """Full proactive recommendation payload from the upstream recommendation layer."""

    model_config = ConfigDict(populate_by_name=True)

    mode: str = "proactive_recommendation"
    intro_text: str | None = Field(default=None, alias="introText")
    shared_slot_memory: dict[str, Any] = Field(default_factory=dict, alias="sharedSlotMemory")
    items: list[ProactiveRecommendationItem] = Field(default_factory=list, alias="items")


class ProactiveRecommendationRouteDecision(BaseModel):
    """Router decision describing how to handle a proactive recommendation response."""

    route_mode: ProactiveRecommendationRouteMode = ProactiveRecommendationRouteMode.NO_SELECTION
    selected_recommendation_ids: list[str] = Field(default_factory=list, alias="selectedRecommendationIds")
    selected_intents: list[str] = Field(default_factory=list, alias="selectedIntents")
    has_user_modification: bool = Field(default=False, alias="hasUserModification")
    modification_reasons: list[str] = Field(default_factory=list, alias="modificationReasons")
    reason: str = ""
