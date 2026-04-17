from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from router_service.core.shared.domain import ChatMessage, IntentMatch, SESSION_TTL, Task, utc_now
from router_service.core.shared.diagnostics import RouterDiagnostic


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
    READY_FOR_DISPATCH = "ready_for_dispatch"
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
    READY_FOR_DISPATCH = "ready_for_dispatch"
    PARTIALLY_COMPLETED = "partially_completed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BusinessObjectStatus(StrEnum):
    """Lifecycle status for one business runtime object attached to a session."""

    ACTIVE = "active"
    PENDING_CONFIRMATION = "pending_confirmation"
    SUSPENDED = "suspended"
    READY_FOR_DISPATCH = "ready_for_dispatch"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
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
    diagnostics: list[RouterDiagnostic] = Field(default_factory=list)
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
    diagnostics: list[RouterDiagnostic] = Field(default_factory=list)
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


class BusinessObjectState(BaseModel):
    """Live business runtime stored under one session and backed by one graph."""

    business_id: str = Field(default_factory=lambda: f"biz_{uuid4().hex[:10]}")
    graph: ExecutionGraphState
    router_only_mode: bool = False
    status: BusinessObjectStatus = BusinessObjectStatus.ACTIVE
    ishandover: bool = False
    suspended_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def sync_from_graph(self) -> None:
        """Derive business status from the underlying graph and router-only policy."""
        graph_status = self.graph.status
        if graph_status == GraphStatus.WAITING_CONFIRMATION:
            self.status = BusinessObjectStatus.PENDING_CONFIRMATION
            self.ishandover = False
        elif graph_status == GraphStatus.READY_FOR_DISPATCH:
            self.status = BusinessObjectStatus.READY_FOR_DISPATCH
            self.ishandover = self.router_only_mode
        elif graph_status == GraphStatus.COMPLETED:
            self.status = BusinessObjectStatus.COMPLETED
            self.ishandover = True
        elif graph_status == GraphStatus.PARTIALLY_COMPLETED:
            self.status = BusinessObjectStatus.PARTIALLY_COMPLETED
            self.ishandover = True
        elif graph_status == GraphStatus.FAILED:
            self.status = BusinessObjectStatus.FAILED
            self.ishandover = True
        elif graph_status == GraphStatus.CANCELLED:
            self.status = BusinessObjectStatus.CANCELLED
            self.ishandover = True
        else:
            self.status = BusinessObjectStatus.ACTIVE
            self.ishandover = False
        self.updated_at = utc_now()

    @property
    def intent_codes(self) -> list[str]:
        """Return the unique intent codes represented by this business object."""
        return list(dict.fromkeys(node.intent_code for node in self.graph.nodes))


class BusinessMemoryDigest(BaseModel):
    """Compact summary persisted after a business object reaches handover."""

    business_id: str
    graph_id: str
    intent_codes: list[str] = Field(default_factory=list)
    status: str
    ishandover: bool
    summary: str = ""
    slot_memory: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    finished_at: datetime = Field(default_factory=utc_now)


class SessionWorkflowState(BaseModel):
    """Minimal orchestration metadata independent from business object internals."""

    focus_business_id: str | None = None
    pending_business_id: str | None = None
    suspended_business_ids: list[str] = Field(default_factory=list)
    completed_business_ids: list[str] = Field(default_factory=list)


class GraphSessionState(BaseModel):
    """Router session state spanning messages, tasks, current graph, and pending graph."""

    session_id: str
    cust_id: str
    messages: list[ChatMessage] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    candidate_intents: list[IntentMatch] = Field(default_factory=list)
    last_diagnostics: list[RouterDiagnostic] = Field(default_factory=list)
    shared_slot_memory: dict[str, Any] = Field(default_factory=dict)
    business_memory_digests: list[BusinessMemoryDigest] = Field(default_factory=list)
    business_objects: list[BusinessObjectState] = Field(default_factory=list)
    workflow: SessionWorkflowState = Field(default_factory=SessionWorkflowState)
    current_graph: ExecutionGraphState | None = None
    pending_graph: ExecutionGraphState | None = None
    active_node_id: str | None = None
    router_only_mode: bool = False
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

    def business_object(self, business_id: str | None) -> BusinessObjectState | None:
        """Return the business object with the given id when present."""
        self._adopt_legacy_aliases()
        if business_id is None:
            return None
        for item in self.business_objects:
            if item.business_id == business_id:
                return item
        return None

    def focus_business(self) -> BusinessObjectState | None:
        """Return the current focus business object."""
        self._adopt_legacy_aliases()
        business_id = self.workflow.focus_business_id
        if business_id is None:
            return None
        for item in self.business_objects:
            if item.business_id == business_id:
                return item
        return None

    def pending_business(self) -> BusinessObjectState | None:
        """Return the current pending-confirmation business object."""
        self._adopt_legacy_aliases()
        business_id = self.workflow.pending_business_id
        if business_id is None:
            return None
        for item in self.business_objects:
            if item.business_id == business_id:
                return item
        return None

    def business_for_graph(self, graph: ExecutionGraphState | None) -> BusinessObjectState | None:
        """Resolve the business object backing the given graph."""
        self._adopt_legacy_aliases()
        if graph is None:
            return None
        for item in self.business_objects:
            if item.graph.graph_id == graph.graph_id:
                return item
        return None

    def attach_business(
        self,
        graph: ExecutionGraphState,
        *,
        router_only_mode: bool,
        pending: bool,
    ) -> BusinessObjectState:
        """Create and focus a new business runtime object backed by the given graph."""
        business = BusinessObjectState(graph=graph, router_only_mode=router_only_mode)
        business.sync_from_graph()
        self.business_objects.append(business)
        if pending:
            self.workflow.pending_business_id = business.business_id
            self.workflow.focus_business_id = None
        else:
            self.workflow.focus_business_id = business.business_id
            self.workflow.pending_business_id = None
        self._sync_focus_aliases()
        return business

    def suspend_focus_business(self, *, reason: str | None = None) -> BusinessObjectState | None:
        """Suspend the focus business so another business can take over the session."""
        business = self.focus_business()
        if business is None:
            return None
        business.status = BusinessObjectStatus.SUSPENDED
        business.ishandover = False
        business.suspended_reason = reason
        business.updated_at = utc_now()
        if business.business_id not in self.workflow.suspended_business_ids:
            self.workflow.suspended_business_ids.append(business.business_id)
        self.workflow.focus_business_id = None
        self._sync_focus_aliases()
        return business

    def suspend_pending_business(self, *, reason: str | None = None) -> BusinessObjectState | None:
        """Suspend the pending business waiting for confirmation."""
        business = self.pending_business()
        if business is None:
            return None
        business.status = BusinessObjectStatus.SUSPENDED
        business.ishandover = False
        business.suspended_reason = reason
        business.updated_at = utc_now()
        if business.business_id not in self.workflow.suspended_business_ids:
            self.workflow.suspended_business_ids.append(business.business_id)
        self.workflow.pending_business_id = None
        self._sync_focus_aliases()
        return business

    def restore_latest_suspended_business(self) -> BusinessObjectState | None:
        """Restore the most recently suspended business as the current session focus."""
        if self.current_graph is not None or self.pending_graph is not None:
            return None
        while self.workflow.suspended_business_ids:
            business_id = self.workflow.suspended_business_ids.pop()
            business = self.business_object(business_id)
            if business is None:
                continue
            business.suspended_reason = None
            business.sync_from_graph()
            if business.graph.status == GraphStatus.WAITING_CONFIRMATION:
                self.workflow.pending_business_id = business.business_id
                self.workflow.focus_business_id = None
            else:
                self.workflow.focus_business_id = business.business_id
                self.workflow.pending_business_id = None
            self._sync_focus_aliases()
            return business
        return None

    def handover_business(self) -> BusinessObjectState | None:
        """Return the business object that is ready to hand over and be compacted."""
        self._adopt_legacy_aliases()
        focus = self.focus_business()
        if focus is not None:
            focus.sync_from_graph()
            if focus.ishandover:
                return focus
        pending = self.pending_business()
        if pending is not None:
            pending.sync_from_graph()
            if pending.ishandover:
                return pending
        return None

    def finalize_business(self, business_id: str) -> BusinessMemoryDigest | None:
        """Compact one completed business into digest + shared slot cache and remove it."""
        business = self.business_object(business_id)
        if business is None:
            return None
        business.sync_from_graph()
        slot_memory = self._collect_business_slot_memory(business)
        self.shared_slot_memory.update(slot_memory)
        digest = BusinessMemoryDigest(
            business_id=business.business_id,
            graph_id=business.graph.graph_id,
            intent_codes=business.intent_codes,
            status=business.status.value,
            ishandover=business.ishandover,
            summary=business.graph.summary,
            slot_memory=slot_memory,
            created_at=business.created_at,
        )
        self.business_memory_digests.append(digest)
        if business.business_id not in self.workflow.completed_business_ids:
            self.workflow.completed_business_ids.append(business.business_id)
        task_ids = {node.task_id for node in business.graph.nodes if node.task_id}
        if task_ids:
            self.tasks = [task for task in self.tasks if task.task_id not in task_ids]
        self.business_objects = [
            item
            for item in self.business_objects
            if item.business_id != business.business_id
        ]
        if self.workflow.focus_business_id == business.business_id:
            self.workflow.focus_business_id = None
        if self.workflow.pending_business_id == business.business_id:
            self.workflow.pending_business_id = None
        self.workflow.suspended_business_ids = [
            item
            for item in self.workflow.suspended_business_ids
            if item != business.business_id
        ]
        self._sync_focus_aliases()
        return digest

    def _collect_business_slot_memory(self, business: BusinessObjectState) -> dict[str, Any]:
        """Collect the slot memory that should survive after business handover."""
        merged: dict[str, Any] = {}
        for node in business.graph.nodes:
            for slot_key, value in node.slot_memory.items():
                if value is None:
                    continue
                merged[slot_key] = value
        return merged

    def _sync_focus_aliases(self) -> None:
        """Keep the compatibility aliases aligned with the workflow focus."""
        focus = next(
            (
                item
                for item in self.business_objects
                if item.business_id == self.workflow.focus_business_id
            ),
            None,
        )
        pending = next(
            (
                item
                for item in self.business_objects
                if item.business_id == self.workflow.pending_business_id
            ),
            None,
        )
        self.current_graph = focus.graph if focus is not None else None
        self.pending_graph = pending.graph if pending is not None else None
        if focus is None:
            self.active_node_id = None
        self.router_only_mode = focus.router_only_mode if focus is not None else False

    def _adopt_legacy_aliases(self) -> None:
        """Backfill business objects from compatibility aliases when old code sets them directly."""
        if self.current_graph is not None and self.workflow.focus_business_id is None:
            existing = next(
                (
                    item
                    for item in self.business_objects
                    if item.graph.graph_id == self.current_graph.graph_id
                ),
                None,
            )
            if existing is None:
                business = BusinessObjectState(
                    graph=self.current_graph,
                    router_only_mode=self.router_only_mode,
                )
                business.sync_from_graph()
                self.business_objects.append(business)
                self.workflow.focus_business_id = business.business_id
        if self.pending_graph is not None and self.workflow.pending_business_id is None:
            existing = next(
                (
                    item
                    for item in self.business_objects
                    if item.graph.graph_id == self.pending_graph.graph_id
                ),
                None,
            )
            if existing is None:
                business = BusinessObjectState(
                    graph=self.pending_graph,
                    router_only_mode=self.router_only_mode,
                )
                business.sync_from_graph()
                self.business_objects.append(business)
                self.workflow.pending_business_id = business.business_id


class GraphRouterSnapshot(BaseModel):
    """Read model returned to API callers and SSE subscribers."""

    session_id: str
    cust_id: str
    messages: list[ChatMessage]
    candidate_intents: list[IntentMatch]
    last_diagnostics: list[RouterDiagnostic] = Field(default_factory=list)
    shared_slot_memory: dict[str, Any] = Field(default_factory=dict)
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
