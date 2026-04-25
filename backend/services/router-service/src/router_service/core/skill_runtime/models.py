from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    """One markdown spec document with optional frontmatter metadata."""

    path: str
    metadata: dict[str, Any]
    body: str


@dataclass(frozen=True, slots=True)
class SkillIndexEntry:
    """Small skill card safe to place in the initial controller context."""

    skill_id: str
    name: str
    version: str
    status: str
    description: str
    keywords: tuple[str, ...]
    risk_level: str
    path: str


@dataclass(frozen=True, slots=True)
class SlotDefinition:
    """Structured slot definition loaded from a skill markdown document."""

    name: str
    required: bool
    prompt: str
    description: str = ""
    extractor: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StepDefinition:
    """Runtime step definition loaded from a skill markdown document."""

    step_id: int
    kind: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """Full skill specification loaded only after a skill has been selected."""

    index: SkillIndexEntry
    slots: tuple[SlotDefinition, ...]
    steps: tuple[StepDefinition, ...]
    references: tuple[str, ...]
    allowed_capabilities: tuple[str, ...]
    exception_messages: dict[str, str]
    raw_body: str

    @property
    def skill_id(self) -> str:
        return self.index.skill_id


@dataclass(slots=True)
class SkillSessionState:
    """Per-session state owned by code, not by the model transcript."""

    session_id: str
    current_skill_id: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    awaiting_slot: str | None = None
    pending_confirmation: dict[str, Any] | None = None
    turn_count: int = 0
    summary: str = ""

    def reset_current_skill(self) -> None:
        self.current_skill_id = None
        self.slots.clear()
        self.awaiting_slot = None
        self.pending_confirmation = None


@dataclass(frozen=True, slots=True)
class SkillRuntimeInput:
    """One v4 controller turn."""

    session_id: str
    message: str
    user_profile: dict[str, Any] = field(default_factory=dict)
    page_context: dict[str, Any] = field(default_factory=dict)
    business_apis: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolCallLog:
    """Audit record for a controller-owned tool call."""

    tool: str
    args: dict[str, Any]
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool": self.tool, "args": dict(self.args)}
        if self.result is not None:
            payload["result"] = dict(self.result)
        return payload


@dataclass(frozen=True, slots=True)
class SkillRuntimeOutput:
    """Stable API-facing v4 response."""

    session_id: str
    response: str
    status: str
    action_required: dict[str, Any] | None = None
    skill: str | None = None
    skill_step: int | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    missing_slots: tuple[str, ...] = ()
    tool_calls_log: tuple[ToolCallLog, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "response": self.response,
            "status": self.status,
            "action_required": self.action_required,
            "skill": self.skill,
            "skill_step": self.skill_step,
            "slots": dict(self.slots),
            "missing_slots": list(self.missing_slots),
            "tool_calls_log": [item.to_dict() for item in self.tool_calls_log],
        }
