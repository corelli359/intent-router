from __future__ import annotations

from datetime import datetime
import os
from typing import Any, Protocol

from pydantic import BaseModel, Field

from router_service.core.shared.domain import CustomerMemory, LongTermMemoryEntry, utc_now
from router_service.core.shared.graph_domain import BusinessMemoryDigest
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
    shared_slot_memory: dict[str, object]
    business_memory_digests: list[object]


class SessionMemorySnapshot(BaseModel):
    """Short-term working set cached by session inside the memory runtime."""

    session_id: str
    cust_id: str
    long_term_memory: list[str] = Field(default_factory=list)
    shared_slot_memory: dict[str, Any] = Field(default_factory=dict)
    business_memory_digests: list[BusinessMemoryDigest] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


class SessionMemoryQuery(BaseModel):
    """Request DTO for reading or warming one session-scoped memory workset."""

    session_id: str
    cust_id: str
    recall_limit: int = 20


class SessionMemoryRememberRequest(BaseModel):
    """Request DTO for mirroring one finalized business into short-term memory."""

    session_id: str
    cust_id: str
    digest: BusinessMemoryDigest
    shared_slot_memory: dict[str, Any] = Field(default_factory=dict)


class SessionMemoryMessageRecord(BaseModel):
    """Serializable message record used when dumping session memory."""

    role: str
    content: str


class SessionMemoryTaskRecord(BaseModel):
    """Serializable task slot-memory record used when dumping session memory."""

    intent_code: str
    slot_memory: dict[str, Any] = Field(default_factory=dict)


class SessionMemoryDumpRequest(BaseModel):
    """Request DTO for promoting one session memory snapshot into long-term memory."""

    session_id: str
    cust_id: str
    messages: list[SessionMemoryMessageRecord] = Field(default_factory=list)
    tasks: list[SessionMemoryTaskRecord] = Field(default_factory=list)
    shared_slot_memory: dict[str, Any] = Field(default_factory=dict)
    business_memory_digests: list[BusinessMemoryDigest] = Field(default_factory=list)
    reason: str = "expired"
    dumped_at: datetime = Field(default_factory=utc_now)


def build_session_memory_dump(
    session: SessionMemoryView,
    *,
    reason: str,
) -> SessionMemoryDumpRequest:
    """Build a serializable dump DTO from one live session view."""
    return SessionMemoryDumpRequest(
        session_id=session.session_id,
        cust_id=session.cust_id,
        messages=[
            SessionMemoryMessageRecord(
                role=str(message.role),
                content=str(message.content),
            )
            for message in getattr(session, "messages", []) or []
            if getattr(message, "role", None) is not None and getattr(message, "content", None) is not None
        ],
        tasks=[
            SessionMemoryTaskRecord(
                intent_code=str(task.intent_code),
                slot_memory=dict(getattr(task, "slot_memory", None) or {}),
            )
            for task in getattr(session, "tasks", []) or []
            if getattr(task, "intent_code", None) is not None
        ],
        shared_slot_memory=dict(getattr(session, "shared_slot_memory", None) or {}),
        business_memory_digests=[
            (
                digest.model_copy(deep=True)
                if isinstance(digest, BusinessMemoryDigest)
                else BusinessMemoryDigest.model_validate(digest)
            )
            for digest in getattr(session, "business_memory_digests", []) or []
        ],
        reason=reason,
    )


def _coerce_session_memory_query(
    request: SessionMemoryQuery | None,
    *,
    session_id: str | None,
    cust_id: str | None,
    recall_limit: int | None,
    default_recall_limit: int,
) -> SessionMemoryQuery:
    """Normalize query inputs for runtime implementations across DTO and legacy call paths."""
    if request is not None:
        return request.model_copy(deep=True)
    if session_id is None or cust_id is None:
        raise TypeError("session_id and cust_id are required when request is not provided")
    resolved_recall_limit = recall_limit if recall_limit is not None else default_recall_limit
    return SessionMemoryQuery(
        session_id=session_id,
        cust_id=cust_id,
        recall_limit=resolved_recall_limit,
    )


def _coerce_session_memory_remember_request(
    request: SessionMemoryRememberRequest | None,
    *,
    session_id: str | None,
    cust_id: str | None,
    digest: BusinessMemoryDigest | None,
    shared_slot_memory: dict[str, Any] | None,
) -> SessionMemoryRememberRequest:
    """Normalize business remember inputs across DTO and legacy call paths."""
    if request is not None:
        return request.model_copy(deep=True)
    if session_id is None or cust_id is None or digest is None:
        raise TypeError("session_id, cust_id, and digest are required when request is not provided")
    return SessionMemoryRememberRequest(
        session_id=session_id,
        cust_id=cust_id,
        digest=digest.model_copy(deep=True),
        shared_slot_memory=dict(shared_slot_memory or {}),
    )


class MemoryRuntime(Protocol):
    """Session-scoped memory runtime backed by long-term and short-term memory."""

    def ensure_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ) -> SessionMemorySnapshot: ...

    def get_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ) -> SessionMemorySnapshot: ...

    def remember_business(
        self,
        request: SessionMemoryRememberRequest | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        digest: BusinessMemoryDigest | None = None,
        shared_slot_memory: dict[str, Any] | None = None,
    ) -> SessionMemorySnapshot: ...

    def expire_session(
        self,
        dump_or_session: SessionMemoryDumpRequest | SessionMemoryView,
        *,
        reason: str = "expired",
    ) -> None: ...


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
        self.promote_dump(build_session_memory_dump(session, reason="session_promote"))

    def promote_dump(self, dump: SessionMemoryDumpRequest) -> None:
        """Promote one serialized session dump into long-term memory."""
        memory = self.get_or_create(dump.cust_id)
        for message in dump.messages[-5:]:
            self._remember_fact(
                memory,
                LongTermMemoryEntry(
                    cust_id=dump.cust_id,
                    memory_type="session_message",
                    content=f"{message.role}: {message.content}",
                    source_session_id=dump.session_id,
                ),
            )
        for task in dump.tasks:
            slot_memory = task.slot_memory
            if not slot_memory:
                continue
            slot_pairs = ", ".join(f"{key}={value}" for key, value in sorted(slot_memory.items()))
            self._remember_fact(
                memory,
                LongTermMemoryEntry(
                    cust_id=dump.cust_id,
                    memory_type="task_slot_memory",
                    content=f"{task.intent_code}: {slot_pairs}",
                    source_session_id=dump.session_id,
                ),
            )
        shared_slot_memory = dump.shared_slot_memory
        if isinstance(shared_slot_memory, dict) and shared_slot_memory:
            slot_pairs = ", ".join(
                f"{key}={value}"
                for key, value in sorted(shared_slot_memory.items())
                if value is not None
            )
            if slot_pairs:
                self._remember_fact(
                    memory,
                    LongTermMemoryEntry(
                        cust_id=dump.cust_id,
                        memory_type="session_shared_slot_memory",
                        content=f"shared_slots: {slot_pairs}",
                        source_session_id=dump.session_id,
                    ),
                )
        for digest in dump.business_memory_digests:
            slot_memory = digest.slot_memory
            intent_codes = digest.intent_codes
            if not isinstance(slot_memory, dict) or not slot_memory:
                continue
            slot_pairs = ", ".join(
                f"{key}={value}"
                for key, value in sorted(slot_memory.items())
                if value is not None
            )
            if not slot_pairs:
                continue
            self._remember_fact(
                memory,
                LongTermMemoryEntry(
                    cust_id=dump.cust_id,
                    memory_type="business_digest_slot_memory",
                    content=f"{'/'.join(intent_codes) or 'business'}: {slot_pairs}",
                    source_session_id=dump.session_id,
                ),
            )


class InProcessMemoryRuntime(LongTermMemoryStore):
    """Local memory runtime backed by long-term memory plus a per-session cache."""

    def __init__(
        self,
        long_term_memory: LongTermMemoryStore | None = None,
        fact_limit: int | None = None,
        default_recall_limit: int = 20,
    ) -> None:
        """Initialize the long-term store and the session snapshot cache."""
        if long_term_memory is None:
            super().__init__(fact_limit=fact_limit)
        else:
            self._customers = long_term_memory._customers
            self._fact_limit = long_term_memory._fact_limit
        self.long_term_memory: LongTermMemoryStore = self
        self._session_memory: dict[str, SessionMemorySnapshot] = {}
        self._session_recall_limits: dict[str, int] = {}
        self._default_recall_limit = (
            default_recall_limit if default_recall_limit > 0 else 20
        )

    def ensure_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ) -> SessionMemorySnapshot:
        """Warm a session snapshot from long-term memory when needed."""
        query = _coerce_session_memory_query(
            request,
            session_id=session_id,
            cust_id=cust_id,
            recall_limit=recall_limit,
            default_recall_limit=self._default_recall_limit,
        )
        resolved_limit = self._resolve_recall_limit(query.recall_limit)
        snapshot = self._session_memory.get(query.session_id)
        cached_limit = self._session_recall_limits.get(query.session_id, 0)
        if snapshot is None or snapshot.cust_id != query.cust_id:
            snapshot = self._store_snapshot(
                SessionMemorySnapshot(
                    session_id=query.session_id,
                    cust_id=query.cust_id,
                    long_term_memory=self.recall(query.cust_id, resolved_limit),
                ),
                recall_limit=resolved_limit,
            )
            return self._copy_snapshot(snapshot)
        if resolved_limit > cached_limit:
            snapshot.long_term_memory = self.recall(query.cust_id, resolved_limit)
            snapshot.updated_at = utc_now()
            self._session_recall_limits[query.session_id] = resolved_limit
        return self._copy_snapshot(snapshot)

    def get_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ) -> SessionMemorySnapshot:
        """Return the current session snapshot, warming it on demand."""
        return self.ensure_session_memory(
            request=request,
            session_id=session_id,
            cust_id=cust_id,
            recall_limit=recall_limit,
        )

    def remember_business(
        self,
        request: SessionMemoryRememberRequest | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        digest: BusinessMemoryDigest | None = None,
        shared_slot_memory: dict[str, Any] | None = None,
    ) -> SessionMemorySnapshot:
        """Update the short-term session snapshot after one business handover."""
        remember_request = _coerce_session_memory_remember_request(
            request,
            session_id=session_id,
            cust_id=cust_id,
            digest=digest,
            shared_slot_memory=shared_slot_memory,
        )
        snapshot = self._session_memory.get(remember_request.session_id)
        if snapshot is None or snapshot.cust_id != remember_request.cust_id:
            snapshot = self._store_snapshot(
                SessionMemorySnapshot(
                    session_id=remember_request.session_id,
                    cust_id=remember_request.cust_id,
                    long_term_memory=self.recall(
                        remember_request.cust_id,
                        self._default_recall_limit,
                    ),
                ),
                recall_limit=self._default_recall_limit,
            )
        snapshot.shared_slot_memory = dict(remember_request.shared_slot_memory)
        snapshot.business_memory_digests.append(remember_request.digest.model_copy(deep=True))
        snapshot.updated_at = utc_now()
        return self._copy_snapshot(snapshot)

    def expire_session(
        self,
        dump_or_session: SessionMemoryDumpRequest | SessionMemoryView,
        *,
        reason: str = "expired",
    ) -> None:
        """Promote one session dump into long-term memory and drop its short-term snapshot."""
        dump = (
            dump_or_session.model_copy(deep=True)
            if isinstance(dump_or_session, SessionMemoryDumpRequest)
            else build_session_memory_dump(dump_or_session, reason=reason)
        )
        self.promote_dump(dump)
        self._session_memory.pop(dump.session_id, None)
        self._session_recall_limits.pop(dump.session_id, None)

    def _store_snapshot(
        self, snapshot: SessionMemorySnapshot, *, recall_limit: int
    ) -> SessionMemorySnapshot:
        """Persist one session snapshot and its effective recall limit."""
        self._session_memory[snapshot.session_id] = snapshot
        self._session_recall_limits[snapshot.session_id] = recall_limit
        return snapshot

    def _resolve_recall_limit(self, recall_limit: int) -> int:
        """Normalize invalid recall limits to the configured session default."""
        return recall_limit if recall_limit > 0 else self._default_recall_limit

    def _copy_snapshot(self, snapshot: SessionMemorySnapshot) -> SessionMemorySnapshot:
        """Protect the cached snapshot from external mutation."""
        return snapshot.model_copy(deep=True)
