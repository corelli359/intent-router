from __future__ import annotations

from typing import Protocol

from router_service.core.shared.domain import ChatMessage, Task


class SessionLike(Protocol):
    """Minimal session protocol required to build recognition and task context."""

    session_id: str
    cust_id: str
    messages: list[ChatMessage]
    shared_slot_memory: dict[str, object]
    business_memory_digests: list[object]


class ContextBuilder:
    """Builds context windows for recognition and task resumption."""

    def build_recent_messages(self, session: SessionLike, limit: int = 15) -> list[str]:
        """Return the most recent chat messages in `role: content` format."""
        recent = session.messages[-limit:]
        return [f"{message.role}: {message.content}" for message in recent]

    def build_task_context(
        self,
        session: SessionLike,
        task: Task | None,
        long_term_memory: list[str],
    ) -> dict[str, object]:
        """Assemble the base context payload used by recognizers and agents."""
        base = {
            "session_id": session.session_id,
            "cust_id": session.cust_id,
            "recent_messages": self.build_recent_messages(session),
            "long_term_memory": long_term_memory,
            "shared_slot_memory": dict(getattr(session, "shared_slot_memory", {}) or {}),
            "config_variables": (
                session.upstream_config_variables()
                if hasattr(session, "upstream_config_variables")
                else {}
            ),
            "request_slots_data": (
                session.upstream_slots_data()
                if hasattr(session, "upstream_slots_data")
                else {}
            ),
            "business_memory_digests": [
                digest.model_dump(mode="json") if hasattr(digest, "model_dump") else dict(digest)
                for digest in getattr(session, "business_memory_digests", []) or []
            ],
        }
        if task is None:
            return base
        merged = dict(base)
        merged["slot_memory"] = dict(task.slot_memory)
        merged["task_status"] = task.status
        return merged
