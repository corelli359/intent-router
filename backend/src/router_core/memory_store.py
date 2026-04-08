from __future__ import annotations

from typing import Protocol

from router_core.domain import CustomerMemory, LongTermMemoryEntry


class SessionMemoryView(Protocol):
    session_id: str
    cust_id: str
    messages: list[object]
    tasks: list[object]


class LongTermMemoryStore:
    def __init__(self) -> None:
        self._customers: dict[str, CustomerMemory] = {}

    def get_or_create(self, cust_id: str) -> CustomerMemory:
        if cust_id not in self._customers:
            self._customers[cust_id] = CustomerMemory(cust_id=cust_id)
        return self._customers[cust_id]

    def recall(self, cust_id: str, limit: int = 10) -> list[str]:
        memory = self.get_or_create(cust_id)
        return [entry.content for entry in memory.facts[-limit:]]

    def promote_session(self, session: SessionMemoryView) -> None:
        memory = self.get_or_create(session.cust_id)
        for message in session.messages[-5:]:
            memory.remember(
                LongTermMemoryEntry(
                    cust_id=session.cust_id,
                    memory_type="session_message",
                    content=f"{message.role}: {message.content}",
                    source_session_id=session.session_id,
                )
            )
        for task in session.tasks:
            slot_memory = getattr(task, "slot_memory", None)
            if not slot_memory:
                continue
            slot_pairs = ", ".join(f"{key}={value}" for key, value in sorted(slot_memory.items()))
            memory.remember(
                LongTermMemoryEntry(
                    cust_id=session.cust_id,
                    memory_type="task_slot_memory",
                    content=f"{task.intent_code}: {slot_pairs}",
                    source_session_id=session.session_id,
                )
            )
