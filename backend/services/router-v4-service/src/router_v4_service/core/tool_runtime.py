from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Structured result returned by a Skill-declared tool call."""

    data: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_agent_output(self, *, agent_id: str, skill_id: str) -> dict[str, Any]:
        return {
            "data": list(self.data),
            "agent": agent_id,
            "skill_id": skill_id,
            **dict(self.metadata),
        }


class ToolExecutor(Protocol):
    def execute(self, tool_call: dict[str, Any], *, task_context: dict[str, Any]) -> ToolResult:
        """Execute a tool call declared by the current Skill decision."""


class LocalSkillToolExecutor:
    """Small local tool adapter for the demo runtime.

    The adapter is intentionally generic: the business Skill must name the tool
    and provide the result shape in arguments. Production deployments should
    replace this with HTTP/RPC adapters registered outside Router feature code.
    """

    def execute(self, tool_call: dict[str, Any], *, task_context: dict[str, Any]) -> ToolResult:
        name = str(tool_call.get("name") or task_context.get("scene_id") or "skill.tool")
        raw_arguments = tool_call.get("arguments")
        arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
        result_type = str(arguments.pop("result_type", "") or f"{name}.result")
        audit_id = str(arguments.pop("audit_id", "") or "tool_" + uuid.uuid4().hex[:12])
        status = str(arguments.pop("status", "") or "success")
        data_item = {
            "type": result_type,
            "status": status,
            **arguments,
            "audit_id": audit_id,
        }
        return ToolResult(
            data=[data_item],
            metadata={
                "business_api": {"name": name, "status": status, "audit_id": audit_id},
            },
        )
