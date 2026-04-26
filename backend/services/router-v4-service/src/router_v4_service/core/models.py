from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RouterTurnStatus(StrEnum):
    """Router-level status values returned to the assistant layer."""

    DISPATCHED = "dispatched"
    FORWARDED = "forwarded"
    CLARIFICATION_REQUIRED = "clarification_required"
    FAILED = "failed"


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

    def bind_dispatch(
        self,
        *,
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
    routing_slots: dict[str, Any] = field(default_factory=dict)
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
            "routing_slots": dict(self.routing_slots),
            "action_required": self.action_required,
            "events": [dict(item) for item in self.events],
            "prompt_report": dict(self.prompt_report),
        }
