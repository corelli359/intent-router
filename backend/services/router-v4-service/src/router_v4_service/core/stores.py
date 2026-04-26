from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from router_v4_service.core.models import RoutingSessionState


class RoutingSessionStore:
    """Store for router-owned multi-turn state."""

    def get_or_create(self, session_id: str) -> RoutingSessionState:
        raise NotImplementedError

    def save(self, state: RoutingSessionState) -> None:
        raise NotImplementedError


class InMemoryRoutingSessionStore(RoutingSessionStore):
    """In-memory session store for the first standalone service version."""

    def __init__(self) -> None:
        self._items: dict[str, RoutingSessionState] = {}

    def get_or_create(self, session_id: str) -> RoutingSessionState:
        if session_id not in self._items:
            self._items[session_id] = RoutingSessionState(session_id=session_id)
        return self._items[session_id]

    def save(self, state: RoutingSessionState) -> None:
        self._items[state.session_id] = state


class FileRoutingSessionStore(RoutingSessionStore):
    """File-backed session store for local persistence and service restarts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def get_or_create(self, session_id: str) -> RoutingSessionState:
        path = self._path_for_session(session_id)
        if not path.exists():
            return RoutingSessionState(session_id=session_id)
        return _state_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, state: RoutingSessionState) -> None:
        path = self._path_for_session(state.session_id)
        path.write_text(
            json.dumps(_state_to_dict(state), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _path_for_session(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_stable_key(session_id)}.json"


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    """One router transcript record."""

    session_id: str
    turn_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "created_at": self.created_at.isoformat(),
        }


class TranscriptStore:
    """Append-only router transcript store."""

    def append(self, record: TranscriptRecord) -> None:
        raise NotImplementedError

    def list_for_session(self, session_id: str) -> list[TranscriptRecord]:
        raise NotImplementedError


class InMemoryTranscriptStore(TranscriptStore):
    """In-memory transcript store for the first standalone service version."""

    def __init__(self) -> None:
        self._items: list[TranscriptRecord] = []

    def append(self, record: TranscriptRecord) -> None:
        self._items.append(record)

    def list_for_session(self, session_id: str) -> list[TranscriptRecord]:
        return [item for item in self._items if item.session_id == session_id]


class FileTranscriptStore(TranscriptStore):
    """Append-only JSONL transcript store keyed by router session."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.transcripts_dir = self.root / "transcripts"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)

    def append(self, record: TranscriptRecord) -> None:
        path = self._path_for_session(record.session_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    def list_for_session(self, session_id: str) -> list[TranscriptRecord]:
        path = self._path_for_session(session_id)
        if not path.exists():
            return []
        records: list[TranscriptRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            records.append(_record_from_dict(json.loads(line)))
        return records

    def _path_for_session(self, session_id: str) -> Path:
        return self.transcripts_dir / f"{_stable_key(session_id)}.jsonl"


def _state_to_dict(state: RoutingSessionState) -> dict[str, Any]:
    return {
        "session_id": state.session_id,
        "active_scene_id": state.active_scene_id,
        "pending_scene_id": state.pending_scene_id,
        "target_agent": state.target_agent,
        "agent_task_id": state.agent_task_id,
        "dispatch_status": state.dispatch_status,
        "routing_slots": dict(state.routing_slots),
        "turn_count": state.turn_count,
        "summary": state.summary,
    }


def _state_from_dict(payload: dict[str, Any]) -> RoutingSessionState:
    return RoutingSessionState(
        session_id=str(payload.get("session_id") or ""),
        active_scene_id=_optional_str(payload.get("active_scene_id")),
        pending_scene_id=_optional_str(payload.get("pending_scene_id")),
        target_agent=_optional_str(payload.get("target_agent")),
        agent_task_id=_optional_str(payload.get("agent_task_id")),
        dispatch_status=_optional_str(payload.get("dispatch_status")),
        routing_slots=dict(payload.get("routing_slots") or {}),
        turn_count=int(payload.get("turn_count") or 0),
        summary=str(payload.get("summary") or ""),
    )


def _record_from_dict(payload: dict[str, Any]) -> TranscriptRecord:
    raw_created_at = str(payload.get("created_at") or "")
    try:
        created_at = datetime.fromisoformat(raw_created_at)
    except ValueError:
        created_at = datetime.now(UTC)
    return TranscriptRecord(
        session_id=str(payload.get("session_id") or ""),
        turn_id=str(payload.get("turn_id") or ""),
        event_type=str(payload.get("event_type") or ""),
        payload=dict(payload.get("payload") or {}),
        created_at=created_at,
    )


def _optional_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _stable_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
