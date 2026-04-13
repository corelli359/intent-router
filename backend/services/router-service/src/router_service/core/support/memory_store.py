from __future__ import annotations

import os
from typing import Protocol

from router_service.core.shared.domain import CustomerMemory, LongTermMemoryEntry
from router_service.settings import (
    ROUTER_LONG_TERM_MEMORY_FACT_LIMIT_ENV,
    parse_long_term_memory_fact_limit,
)


class SessionMemoryView(Protocol):
    """Minimal session view required to promote facts into long-term memory."""

    session_id: str
    cust_id: str
    messages: list[object]
    tasks: list[object]


class LongTermMemoryStore:
    """In-memory long-term memory store keyed by customer id."""

    def __init__(self, fact_limit: int | None = None) -> None:
        """Initialize the empty customer memory map and optional capacity limit."""
        self._customers: dict[str, CustomerMemory] = {}
        if fact_limit is not None:
            self._fact_limit = fact_limit if fact_limit > 0 else None
        else:
            self._fact_limit = parse_long_term_memory_fact_limit(
                os.getenv(ROUTER_LONG_TERM_MEMORY_FACT_LIMIT_ENV)
            )

    def get_or_create(self, cust_id: str) -> CustomerMemory:
        """Return the customer memory bucket, creating it on first access."""
        if cust_id not in self._customers:
            self._customers[cust_id] = CustomerMemory(cust_id=cust_id)
        return self._customers[cust_id]

    def recall(self, cust_id: str, limit: int = 10) -> list[str]:
        """Recall the most recent long-term memory facts for a customer."""
        memory = self.get_or_create(cust_id)
        return [entry.content for entry in memory.facts[-limit:]]

    def _remember_fact(self, memory: CustomerMemory, entry: LongTermMemoryEntry) -> None:
        """Store a fact while enforcing the configured capacity."""
        memory.remember(entry)
        self._enforce_capacity(memory)

    def _enforce_capacity(self, memory: CustomerMemory) -> None:
        """Trim the oldest facts when a customer exceeds the per-customer limit."""
        limit = self._fact_limit
        if limit is None:
            return
        excess = len(memory.facts) - limit
        if excess <= 0:
            return
        del memory.facts[:excess]

    def promote_session(self, session: SessionMemoryView) -> None:
        """Promote recent messages and task slot memories from one session into long-term memory."""
        memory = self.get_or_create(session.cust_id)
        for message in session.messages[-5:]:
            self._remember_fact(
                memory,
                LongTermMemoryEntry(
                    cust_id=session.cust_id,
                    memory_type="session_message",
                    content=f"{message.role}: {message.content}",
                    source_session_id=session.session_id,
                ),
            )
        for task in session.tasks:
            slot_memory = getattr(task, "slot_memory", None)
            if not slot_memory:
                continue
            slot_pairs = ", ".join(f"{key}={value}" for key, value in sorted(slot_memory.items()))
            self._remember_fact(
                memory,
                LongTermMemoryEntry(
                    cust_id=session.cust_id,
                    memory_type="task_slot_memory",
                    content=f"{task.intent_code}: {slot_pairs}",
                    source_session_id=session.session_id,
                ),
            )
