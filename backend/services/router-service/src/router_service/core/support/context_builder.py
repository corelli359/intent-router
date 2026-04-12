from __future__ import annotations

from typing import Protocol

from router_service.core.shared.domain import ChatMessage, Task


class SessionLike(Protocol):
    session_id: str
    cust_id: str
    messages: list[ChatMessage]


class ContextBuilder:
    """Builds context windows for recognition and task resumption."""

    def build_recent_messages(self, session: SessionLike, limit: int = 15) -> list[str]:
        recent = session.messages[-limit:]
        return [f"{message.role}: {message.content}" for message in recent]

    def build_task_context(
        self,
        session: SessionLike,
        task: Task | None,
        long_term_memory: list[str],
    ) -> dict[str, object]:
        base = {
            "session_id": session.session_id,
            "cust_id": session.cust_id,
            "recent_messages": self.build_recent_messages(session),
            "long_term_memory": long_term_memory,
        }
        if task is None:
            return base
        merged = dict(base)
        merged["slot_memory"] = dict(task.slot_memory)
        merged["task_status"] = task.status
        return merged
