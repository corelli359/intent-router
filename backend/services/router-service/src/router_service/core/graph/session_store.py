from __future__ import annotations

from typing import Any
from uuid import uuid4

from router_service.core.shared.graph_domain import GraphSessionState
from router_service.core.support.memory_store import LongTermMemoryStore


class GraphSessionStore:
    def __init__(self, long_term_memory: LongTermMemoryStore | None = None) -> None:
        self._sessions: dict[str, GraphSessionState] = {}
        self.long_term_memory = long_term_memory or LongTermMemoryStore()

    def create(self, cust_id: str, session_id: str | None = None) -> GraphSessionState:
        resolved_session_id = session_id or f"session_graph_{uuid4().hex[:10]}"
        session = GraphSessionState(session_id=resolved_session_id, cust_id=cust_id)
        self._sessions[resolved_session_id] = session
        return session

    def get(self, session_id: str) -> GraphSessionState:
        return self._sessions[session_id]

    def get_or_create(self, session_id: str | None, cust_id: str) -> GraphSessionState:
        if session_id is None:
            return self.create(cust_id=cust_id)
        if session_id not in self._sessions:
            self._sessions[session_id] = GraphSessionState(session_id=session_id, cust_id=cust_id)
        session = self._sessions[session_id]
        if session.cust_id != cust_id:
            session = GraphSessionState(session_id=session_id, cust_id=cust_id)
            self._sessions[session_id] = session
        if session.is_expired():
            self.long_term_memory.promote_session(self._compat_session_view(session))
            session = GraphSessionState(session_id=session.session_id, cust_id=session.cust_id)
            self._sessions[session_id] = session
        return session

    def _compat_session_view(self, session: GraphSessionState) -> Any:
        class _Compat:
            def __init__(self, source: GraphSessionState) -> None:
                self.session_id = source.session_id
                self.cust_id = source.cust_id
                self.messages = source.messages
                self.tasks = source.tasks

        return _Compat(session)
