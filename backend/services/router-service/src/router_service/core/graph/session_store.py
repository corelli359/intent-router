from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import heapq
import threading
from typing import Protocol
from uuid import uuid4

from router_service.core.shared.domain import utc_now
from router_service.core.shared.graph_domain import BusinessMemoryDigest, GraphSessionState
from router_service.core.support.memory_store import (
    LongTermMemoryStore,
    SessionMemoryDumpRequest,
    SessionMemoryQuery,
    SessionMemoryRememberRequest,
    SessionMemorySnapshot,
    build_session_memory_dump,
)


DEFAULT_MEMORY_RECALL_LIMIT = 20


@dataclass(slots=True)
class _LegacySessionMemorySnapshot:
    """Fallback snapshot used until a full memory runtime is available."""

    session_id: str
    cust_id: str
    long_term_memory: list[str]
    shared_slot_memory: dict[str, object]
    business_memory_digests: list[object]
    updated_at: datetime


class _MemoryRuntime(Protocol):
    """Minimal memory runtime contract used by the session store."""

    def get_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ) -> object: ...

    def ensure_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ) -> object: ...

    def remember_business(
        self,
        request: SessionMemoryRememberRequest | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        digest: BusinessMemoryDigest | None = None,
        shared_slot_memory: dict[str, object] | None = None,
    ) -> object: ...

    def expire_session(
        self,
        dump_or_session: SessionMemoryDumpRequest | GraphSessionState,
        *,
        reason: str = "expired",
    ) -> None: ...


class _LegacyLongTermMemoryRuntime:
    """Compatibility adapter for branches where only LongTermMemoryStore exists."""

    def __init__(
        self,
        long_term_memory: LongTermMemoryStore | None = None,
        *,
        default_recall_limit: int = DEFAULT_MEMORY_RECALL_LIMIT,
    ) -> None:
        self.long_term_memory = long_term_memory or LongTermMemoryStore()
        self._snapshots: dict[str, _LegacySessionMemorySnapshot] = {}
        self._default_recall_limit = max(default_recall_limit, 0)

    def ensure_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ) -> _LegacySessionMemorySnapshot:
        if request is not None:
            session_id = request.session_id
            cust_id = request.cust_id
            recall_limit = request.recall_limit
        if session_id is None or cust_id is None or recall_limit is None:
            raise TypeError("session_id, cust_id, and recall_limit are required")
        snapshot = self._snapshots.get(session_id)
        if snapshot is None or snapshot.cust_id != cust_id:
            snapshot = _LegacySessionMemorySnapshot(
                session_id=session_id,
                cust_id=cust_id,
                long_term_memory=self.long_term_memory.recall(cust_id, limit=recall_limit),
                shared_slot_memory={},
                business_memory_digests=[],
                updated_at=utc_now(),
            )
            self._snapshots[session_id] = snapshot
            return snapshot
        snapshot.updated_at = utc_now()
        return snapshot

    def get_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ) -> _LegacySessionMemorySnapshot:
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
        shared_slot_memory: dict[str, object] | None = None,
    ) -> _LegacySessionMemorySnapshot:
        if request is not None:
            session_id = request.session_id
            cust_id = request.cust_id
            digest = request.digest
            shared_slot_memory = request.shared_slot_memory
        if session_id is None or cust_id is None or digest is None:
            raise TypeError("session_id, cust_id, and digest are required")
        snapshot = self.ensure_session_memory(
            session_id=session_id,
            cust_id=cust_id,
            recall_limit=self._default_recall_limit,
        )
        snapshot.shared_slot_memory = dict(shared_slot_memory or {})
        snapshot.business_memory_digests = [*snapshot.business_memory_digests, digest]
        snapshot.updated_at = utc_now()
        return snapshot

    def expire_session(
        self,
        dump_or_session: SessionMemoryDumpRequest | GraphSessionState,
        *,
        reason: str = "expired",
    ) -> None:
        dump = (
            dump_or_session
            if isinstance(dump_or_session, SessionMemoryDumpRequest)
            else build_session_memory_dump(dump_or_session, reason=reason)
        )
        self.long_term_memory.promote_dump(dump)
        self._snapshots.pop(dump.session_id, None)


class GraphSessionStore:
    """In-memory session store responsible for graph session lifecycle and expiry handling."""

    def __init__(
        self,
        long_term_memory: LongTermMemoryStore | None = None,
        *,
        memory_runtime: _MemoryRuntime | None = None,
        memory_recall_limit: int = DEFAULT_MEMORY_RECALL_LIMIT,
    ) -> None:
        """Initialize the session store and its backing memory runtime."""
        self._sessions: dict[str, GraphSessionState] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._expiry_heap: list[tuple[datetime, str]] = []
        self._state_lock = threading.RLock()
        self._memory_recall_limit = max(memory_recall_limit, 0)
        self.memory_runtime = memory_runtime or _LegacyLongTermMemoryRuntime(
            long_term_memory=long_term_memory,
            default_recall_limit=self._memory_recall_limit,
        )
        self.long_term_memory = long_term_memory
        if self.long_term_memory is None and isinstance(self.memory_runtime, LongTermMemoryStore):
            self.long_term_memory = self.memory_runtime
        if self.long_term_memory is None:
            self.long_term_memory = getattr(self.memory_runtime, "long_term_memory", None)
        if self.long_term_memory is None:
            self.long_term_memory = getattr(self.memory_runtime, "_long_term_memory", None)
        if self.long_term_memory is None:
            raise TypeError("GraphSessionStore requires a long_term_memory backend or a runtime exposing one")

    def create(self, cust_id: str, session_id: str | None = None) -> GraphSessionState:
        """Create and store a fresh graph session."""
        resolved_session_id = session_id or f"session_graph_{uuid4().hex[:10]}"
        session = GraphSessionState(session_id=resolved_session_id, cust_id=cust_id)
        with self._state_lock:
            return self._store_session(session)

    def get(self, session_id: str) -> GraphSessionState:
        """Return an existing graph session by id."""
        with self._state_lock:
            return self._sessions[session_id]

    def ensure_session_memory(self, session: GraphSessionState) -> object:
        """Warm and return the session-scoped memory snapshot for the active session."""
        if session.memory_warmed:
            return self._build_live_session_memory_snapshot(session)
        snapshot = self.memory_runtime.ensure_session_memory(request=self._build_session_memory_query(session))
        session.warm_session_memory(self._snapshot_long_term_memory(snapshot))
        return self._build_live_session_memory_snapshot(session)

    def get_session_memory(self, session: GraphSessionState) -> object:
        """Return the current session-scoped memory snapshot for the active session."""
        if not session.memory_warmed:
            return self.ensure_session_memory(session)
        return self._build_live_session_memory_snapshot(session)

    def remember_business_handover(self, session: GraphSessionState, digest: BusinessMemoryDigest) -> object:
        """Return the current live session memory view after one business handover."""
        del digest
        return self._build_live_session_memory_snapshot(session)

    def expire_session(self, session: GraphSessionState, *, reason: str = "expired") -> None:
        """Dump one live session into memory runtime and clear its short-term workset."""
        self.memory_runtime.expire_session(self._build_session_memory_dump(session, reason=reason))

    @asynccontextmanager
    async def session_lock(self, session_id: str) -> AsyncIterator[None]:
        """Serialize concurrent mutations for one session id."""
        with self._state_lock:
            lock = self._locks[session_id]
        async with lock:
            yield

    def get_or_create(self, session_id: str | None, cust_id: str) -> GraphSessionState:
        """Resolve a session id, recreating expired or customer-mismatched sessions when needed."""
        if session_id is None:
            return self.create(cust_id=cust_id)
        with self._state_lock:
            session = self._sessions.get(session_id)
            if session is None:
                return self._store_session(GraphSessionState(session_id=session_id, cust_id=cust_id))
            if session.cust_id != cust_id:
                self.expire_session(session, reason="cust_mismatch")
                return self._store_session(GraphSessionState(session_id=session_id, cust_id=cust_id))
            if session.is_expired():
                self.expire_session(session, reason="expired")
                return self._store_session(GraphSessionState(session_id=session.session_id, cust_id=session.cust_id))
            return session

    def note_session_expiry(self, session: GraphSessionState) -> None:
        """Track an externally updated session expiry without changing session contents."""
        with self._state_lock:
            if self._sessions.get(session.session_id) is session:
                self._push_expiry(session)

    def _store_session(self, session: GraphSessionState) -> GraphSessionState:
        """Persist one live session and index its current expiry deadline."""
        self._sessions[session.session_id] = session
        self._push_expiry(session)
        return session

    def _push_expiry(self, session: GraphSessionState) -> None:
        """Record the session's current expiry in the lazy invalidation heap."""
        heapq.heappush(self._expiry_heap, (session.expires_at, session.session_id))

    def purge_expired(self, now: datetime | None = None) -> list[str]:
        """Expire and remove any sessions whose TTL has elapsed."""
        current_time = now or utc_now()
        expired_sessions: list[str] = []
        with self._state_lock:
            while self._expiry_heap:
                tracked_expiry, session_id = self._expiry_heap[0]
                if tracked_expiry > current_time:
                    break
                heapq.heappop(self._expiry_heap)
                session = self._sessions.get(session_id)
                if session is None:
                    continue
                if session.expires_at > tracked_expiry:
                    self._push_expiry(session)
                    continue
                if not session.is_expired(now=current_time):
                    self._push_expiry(session)
                    continue
                self.expire_session(session, reason="expired")
                expired_sessions.append(session_id)
                del self._sessions[session_id]
                self._locks.pop(session_id, None)
        return expired_sessions

    def _build_session_memory_query(self, session: GraphSessionState) -> SessionMemoryQuery:
        """Create the standard session-memory query DTO for one live session."""
        return SessionMemoryQuery(
            session_id=session.session_id,
            cust_id=session.cust_id,
            recall_limit=self._memory_recall_limit,
        )

    def _build_session_memory_dump(
        self,
        session: GraphSessionState,
        *,
        reason: str,
    ) -> SessionMemoryDumpRequest:
        """Create the session dump DTO used by runtime expire/dump calls."""
        return build_session_memory_dump(session, reason=reason)

    def _build_live_session_memory_snapshot(self, session: GraphSessionState) -> SessionMemorySnapshot:
        """Build one hot-session memory view from the in-process session state."""
        return SessionMemorySnapshot(
            session_id=session.session_id,
            cust_id=session.cust_id,
            long_term_memory=session.session_long_term_memory(),
            shared_slot_memory=dict(session.shared_slot_memory),
            business_memory_digests=[
                digest.model_copy(deep=True)
                for digest in session.business_memory_digests
            ],
            updated_at=session.updated_at,
        )

    def _snapshot_long_term_memory(self, snapshot: object | None) -> list[str]:
        """Extract recalled long-term memory facts from one runtime warmup snapshot."""
        if snapshot is None:
            return []
        if isinstance(snapshot, dict):
            return list(snapshot.get("long_term_memory") or [])
        return list(getattr(snapshot, "long_term_memory", None) or [])
