from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RouterTurnStatus(StrEnum):
    """Router-level status values returned to the assistant layer."""

    DISPATCHED = "dispatched"
    FORWARDED = "forwarded"
    PLANNED = "planned"
    NO_ACTION = "no_action"
    TASK_UPDATED = "task_updated"
    CLARIFICATION_REQUIRED = "clarification_required"
    FAILED = "failed"


class TaskStatus(StrEnum):
    """Router-owned task lifecycle states."""

    CREATED = "created"
    PLANNED = "planned"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    HANDOVER_REQUESTED = "handover_requested"
    FALLBACK_DISPATCHED = "fallback_dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    HANDOVER_EXHAUSTED = "handover_exhausted"


class GraphStatus(StrEnum):
    """Router-owned graph lifecycle states."""

    CREATED = "created"
    PLANNED = "planned"
    RUNNING = "running"
    PARTIALLY_COMPLETED = "partially_completed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    """Scene trigger metadata owned by the scene team."""

    examples: tuple[str, ...] = ()
    negative_examples: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutingSlotSpec:
    """A slot that Router is allowed to extract as a dispatch hint."""

    name: str
    source: str
    required_for_dispatch: bool = False
    handoff: bool = True
    extractor: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DispatchContract:
    """Contract used to build an execution-agent task."""

    task_type: str
    handoff_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SceneSpec:
    """Router-side scene routing spec."""

    scene_id: str
    name: str
    version: str
    description: str
    target_agent: str
    triggers: TriggerSpec
    routing_slots: tuple[RoutingSlotSpec, ...]
    dispatch_contract: DispatchContract
    references: tuple[str, ...]
    spec_hash: str


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Execution agent registered for Router dispatch."""

    agent_id: str
    endpoint: str
    accepted_scene_ids: tuple[str, ...]
    task_schema: str
    event_schema: str
    supports_stream: bool = False


@dataclass(slots=True)
class RouterTaskState:
    """Router-owned execution task state.

    Business state remains inside the execution agent. Router stores only
    dispatch, handover, event and structured-output correlation.
    """

    task_id: str
    scene_id: str
    target_agent: str
    agent_task_id: str
    status: TaskStatus
    raw_message: str
    routing_slots: dict[str, Any] = field(default_factory=dict)
    scene_spec_hash: str = ""
    stream_url: str = ""
    resume_token: str = ""
    source: str = "user"
    push_context: dict[str, Any] = field(default_factory=dict)
    original_task_id: str | None = None
    fallback_task_id: str | None = None
    handover_used: bool = False
    agent_output: dict[str, Any] | None = None
    abnormal_agent_output: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "scene_id": self.scene_id,
            "target_agent": self.target_agent,
            "agent_task_id": self.agent_task_id,
            "status": self.status.value,
            "raw_message": self.raw_message,
            "routing_slots": dict(self.routing_slots),
            "scene_spec_hash": self.scene_spec_hash,
            "stream_url": self.stream_url,
            "resume_token": self.resume_token,
            "source": self.source,
            "push_context": dict(self.push_context),
            "original_task_id": self.original_task_id,
            "fallback_task_id": self.fallback_task_id,
            "handover_used": self.handover_used,
            "agent_output": self.agent_output,
            "abnormal_agent_output": self.abnormal_agent_output,
        }


@dataclass(slots=True)
class RouterGraphState:
    """Router-owned multi-intent execution graph state."""

    graph_id: str
    task_ids: list[str]
    status: GraphStatus
    source: str = "user"
    stream_mode: str = "split_by_task"

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "task_ids": list(self.task_ids),
            "status": self.status.value,
            "source": self.source,
            "stream_mode": self.stream_mode,
        }


@dataclass(slots=True)
class RoutingSessionState:
    """Router-owned multi-turn state.

    This is not business execution state. It only tracks scene routing and
    execution-agent dispatch correlation.
    """

    session_id: str
    active_scene_id: str | None = None
    pending_scene_id: str | None = None
    target_agent: str | None = None
    agent_task_id: str | None = None
    dispatch_status: str | None = None
    routing_slots: dict[str, Any] = field(default_factory=dict)
    turn_count: int = 0
    summary: str = ""
    active_graph_id: str | None = None
    active_task_ids: list[str] = field(default_factory=list)
    source: str = "user"
    push_context: dict[str, Any] = field(default_factory=dict)
    raw_messages: list[str] = field(default_factory=list)
    selected_scene_ids: list[str] = field(default_factory=list)
    target_agents: list[str] = field(default_factory=list)
    agent_task_ids: list[str] = field(default_factory=list)
    handover_records: list[dict[str, Any]] = field(default_factory=list)
    agent_outputs: dict[str, Any] = field(default_factory=dict)
    assistant_result_status: str = ""
    tasks: dict[str, RouterTaskState] = field(default_factory=dict)
    graphs: dict[str, RouterGraphState] = field(default_factory=dict)

    def bind_dispatch(
        self,
        *,
        task_id: str,
        scene_id: str,
        target_agent: str,
        agent_task_id: str,
        routing_slots: dict[str, Any],
        summary: str,
    ) -> None:
        self.active_scene_id = scene_id
        self.pending_scene_id = None
        self.target_agent = target_agent
        self.agent_task_id = agent_task_id
        self.dispatch_status = "dispatched"
        self.routing_slots = dict(routing_slots)
        self.summary = summary
        self.active_task_ids = [task_id]
        self.selected_scene_ids = _append_unique(self.selected_scene_ids, scene_id)
        self.target_agents = _append_unique(self.target_agents, target_agent)
        self.agent_task_ids = _append_unique(self.agent_task_ids, agent_task_id)


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Router prompt/context loading budget."""

    max_chars: int = 4000
    recent_turn_limit: int = 6
    retrieved_reference_limit: int = 3


@dataclass(frozen=True, slots=True)
class RouterV4Input:
    """One assistant-to-router turn."""

    session_id: str
    message: str
    user_profile: dict[str, Any] = field(default_factory=dict)
    page_context: dict[str, Any] = field(default_factory=dict)
    agent_registry: dict[str, Any] | list[Any] | None = None
    source: str = "user"
    push_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentDispatchResult:
    """Result returned by the execution-agent dispatch gateway."""

    agent_task_id: str
    status: str
    message: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RouterV4Output:
    """Stable API-facing router v4 output."""

    session_id: str
    status: RouterTurnStatus
    response: str
    scene_id: str | None = None
    target_agent: str | None = None
    agent_task_id: str | None = None
    task_id: str | None = None
    graph_id: str | None = None
    stream_mode: str | None = None
    routing_slots: dict[str, Any] = field(default_factory=dict)
    tasks: tuple[dict[str, Any], ...] = ()
    agent_output: dict[str, Any] | None = None
    action_required: dict[str, Any] | None = None
    events: tuple[dict[str, Any], ...] = ()
    prompt_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status.value,
            "response": self.response,
            "scene_id": self.scene_id,
            "target_agent": self.target_agent,
            "agent_task_id": self.agent_task_id,
            "task_id": self.task_id,
            "graph_id": self.graph_id,
            "stream_mode": self.stream_mode,
            "routing_slots": dict(self.routing_slots),
            "tasks": [dict(item) for item in self.tasks],
            "agent_output": self.agent_output,
            "action_required": self.action_required,
            "events": [dict(item) for item in self.events],
            "prompt_report": dict(self.prompt_report),
        }


def _append_unique(values: list[str], value: str) -> list[str]:
    if value in values:
        return values
    return [*values, value]
