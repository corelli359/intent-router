from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from router_service.models.intent import (
    IntentFieldDefinition,
    IntentGraphBuildHints,
    IntentPayload,
    IntentRecord,
    IntentSlotDefinition,
    IntentStatus,
)
from router_service.catalog.intent_repository import (
    IntentAlreadyExistsError,
    IntentNotFoundError,
    IntentRepository,
)
from router_service.core.support.json_codec import json_dumps, json_loads


def utcnow() -> datetime:
    """Return the current UTC timestamp for persisted rows."""
    return datetime.now(timezone.utc)


def normalize_database_url(database_url: str) -> str:
    """Normalize SQLAlchemy URLs, including automatic MySQL driver selection."""
    normalized = database_url.strip()
    if normalized.startswith("mysql://"):
        return normalized.replace("mysql://", "mysql+pymysql://", 1)
    return normalized


class Base(DeclarativeBase):
    """Declarative SQLAlchemy base for catalog tables."""

    pass


class IntentRow(Base):
    """ORM row mapping for the intent registry table."""

    __tablename__ = "intent_registry"

    intent_code: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    domain_code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    domain_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    domain_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    examples_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    agent_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_leaf_intent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    dispatch_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    request_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    field_mapping_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    field_catalog_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    slot_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    graph_build_hints_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    parent_intent_code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    routing_examples_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    resume_policy: Mapped[str] = mapped_column(String(128), nullable=False, default="resume_same_task")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DatabaseIntentRepository(IntentRepository):
    """SQLAlchemy-backed repository for persisted intent definitions."""

    def __init__(self, database_url: str) -> None:
        """Create the SQLAlchemy engine, session factory, and initial schema."""
        self.database_url = normalize_database_url(database_url)
        connect_args = {}
        if self.database_url.startswith("sqlite:///"):
            database_path = self.database_url.removeprefix("sqlite:///")
            if database_path and database_path != ":memory:":
                Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            connect_args["check_same_thread"] = False
        self._engine = create_engine(self.database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False, class_=Session)
        Base.metadata.create_all(self._engine)
        self._ensure_compatible_schema()

    def list_intents(self, status: IntentStatus | None = None) -> list[IntentRecord]:
        """List persisted intents, optionally filtered by status."""
        with self._session() as session:
            statement = select(IntentRow).order_by(IntentRow.created_at.asc(), IntentRow.intent_code.asc())
            if status is not None:
                statement = statement.where(IntentRow.status == status.value)
            rows = session.scalars(statement).all()
            return [self._to_record(row) for row in rows]

    def get_intent(self, intent_code: str) -> IntentRecord:
        """Fetch one persisted intent record by code."""
        with self._session() as session:
            row = session.get(IntentRow, intent_code)
            if row is None:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")
            return self._to_record(row)

    def create_intent(self, payload: IntentPayload) -> IntentRecord:
        """Insert a new intent into the database."""
        with self._session() as session:
            existing = session.get(IntentRow, payload.intent_code)
            if existing is not None:
                raise IntentAlreadyExistsError(f"Intent already exists: {payload.intent_code}")

            now = utcnow()
            row = IntentRow(
                intent_code=payload.intent_code,
                name=payload.name,
                description=payload.description,
                domain_code=payload.domain_code,
                domain_name=payload.domain_name,
                domain_description=payload.domain_description,
                examples_json=self._dump_json(payload.examples),
                agent_url=payload.agent_url,
                status=payload.status.value,
                is_fallback=payload.is_fallback,
                is_leaf_intent=payload.is_leaf_intent,
                dispatch_priority=payload.dispatch_priority,
                request_schema_json=self._dump_json(payload.request_schema),
                field_mapping_json=self._dump_json(payload.field_mapping),
                field_catalog_json=self._dump_json([field.model_dump(mode="json") for field in payload.field_catalog]),
                slot_schema_json=self._dump_json([slot.model_dump(mode="json") for slot in payload.slot_schema]),
                graph_build_hints_json=self._dump_json(payload.graph_build_hints.model_dump(mode="json")),
                parent_intent_code=payload.parent_intent_code,
                routing_examples_json=self._dump_json(payload.routing_examples),
                resume_policy=payload.resume_policy,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return self._to_record(row)

    def update_intent(self, intent_code: str, payload: IntentPayload) -> IntentRecord:
        """Update an existing database row from the provided payload."""
        with self._session() as session:
            row = session.get(IntentRow, intent_code)
            if row is None:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")

            if payload.intent_code != intent_code:
                replacement = session.get(IntentRow, payload.intent_code)
                if replacement is not None:
                    raise IntentAlreadyExistsError(f"Intent already exists: {payload.intent_code}")

            row.intent_code = payload.intent_code
            row.name = payload.name
            row.description = payload.description
            row.domain_code = payload.domain_code
            row.domain_name = payload.domain_name
            row.domain_description = payload.domain_description
            row.examples_json = self._dump_json(payload.examples)
            row.agent_url = payload.agent_url
            row.status = payload.status.value
            row.is_fallback = payload.is_fallback
            row.is_leaf_intent = payload.is_leaf_intent
            row.dispatch_priority = payload.dispatch_priority
            row.request_schema_json = self._dump_json(payload.request_schema)
            row.field_mapping_json = self._dump_json(payload.field_mapping)
            row.field_catalog_json = self._dump_json([field.model_dump(mode="json") for field in payload.field_catalog])
            row.slot_schema_json = self._dump_json([slot.model_dump(mode="json") for slot in payload.slot_schema])
            row.graph_build_hints_json = self._dump_json(payload.graph_build_hints.model_dump(mode="json"))
            row.parent_intent_code = payload.parent_intent_code
            row.routing_examples_json = self._dump_json(payload.routing_examples)
            row.resume_policy = payload.resume_policy
            row.updated_at = utcnow()

            session.commit()
            return self._to_record(row)

    def delete_intent(self, intent_code: str) -> None:
        """Delete one intent row from the database."""
        with self._session() as session:
            row = session.get(IntentRow, intent_code)
            if row is None:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")
            session.delete(row)
            session.commit()

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Yield a SQLAlchemy session with rollback-on-error semantics."""
        session = self._session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _to_record(self, row: IntentRow) -> IntentRecord:
        """Convert one ORM row into the public Pydantic record model."""
        return IntentRecord(
            intent_code=row.intent_code,
            name=row.name,
            description=row.description,
            domain_code=getattr(row, "domain_code", ""),
            domain_name=getattr(row, "domain_name", ""),
            domain_description=getattr(row, "domain_description", ""),
            examples=self._load_json_list(row.examples_json),
            agent_url=row.agent_url,
            status=IntentStatus(row.status),
            is_fallback=row.is_fallback,
            is_leaf_intent=self._coerce_bool(getattr(row, "is_leaf_intent", True)),
            dispatch_priority=row.dispatch_priority,
            request_schema=self._load_json_object(row.request_schema_json),
            field_mapping=self._load_json_str_dict(row.field_mapping_json),
            field_catalog=self._load_field_catalog(getattr(row, "field_catalog_json", "[]")),
            slot_schema=self._load_slot_schema(getattr(row, "slot_schema_json", "[]")),
            graph_build_hints=self._load_graph_build_hints(getattr(row, "graph_build_hints_json", "{}")),
            parent_intent_code=getattr(row, "parent_intent_code", ""),
            routing_examples=self._load_json_list(getattr(row, "routing_examples_json", "[]")),
            resume_policy=row.resume_policy,
            created_at=self._ensure_aware(row.created_at),
            updated_at=self._ensure_aware(row.updated_at),
        )

    def _ensure_compatible_schema(self) -> None:
        """Backfill columns required by the current router schema when upgrading."""
        inspector = inspect(self._engine)
        if "intent_registry" not in inspector.get_table_names():
            return
        existing_columns = {column["name"] for column in inspector.get_columns("intent_registry")}
        additions = {
            "field_catalog_json": ("TEXT", "'[]'"),
            "slot_schema_json": ("TEXT", "'[]'"),
            "graph_build_hints_json": ("TEXT", "'{}'"),
            "domain_code": ("TEXT", "''"),
            "domain_name": ("TEXT", "''"),
            "domain_description": ("TEXT", "''"),
            "is_leaf_intent": ("BOOLEAN", "1"),
            "parent_intent_code": ("TEXT", "''"),
            "routing_examples_json": ("TEXT", "'[]'"),
        }
        missing = {name: spec for name, spec in additions.items() if name not in existing_columns}
        if not missing:
            return

        with self._engine.begin() as connection:
            for column_name, (column_type, default_value) in missing.items():
                connection.execute(
                    text(
                        f"ALTER TABLE intent_registry "
                        f"ADD COLUMN {column_name} {column_type} NOT NULL DEFAULT {default_value}"
                    )
                )

    def _dump_json(self, value: object) -> str:
        """Serialize a Python value into stable UTF-8 JSON text."""
        return json_dumps(value, sort_keys=True)

    def _load_json_object(self, raw_value: str) -> dict[str, object]:
        """Load a JSON object column defensively, falling back to an empty dict."""
        loaded = json_loads(raw_value or "{}")
        if isinstance(loaded, dict):
            return loaded
        return {}

    def _load_json_list(self, raw_value: str) -> list[str]:
        """Load a JSON array column as a list of strings."""
        loaded = json_loads(raw_value or "[]")
        if isinstance(loaded, list):
            return [str(item) for item in loaded]
        return []

    def _load_json_str_dict(self, raw_value: str) -> dict[str, str]:
        """Load a JSON object column as a string-to-string map."""
        loaded = self._load_json_object(raw_value)
        return {str(key): str(value) for key, value in loaded.items()}

    def _load_field_catalog(self, raw_value: str) -> list[IntentFieldDefinition]:
        """Parse field catalog JSON into validated field definitions."""
        loaded = json_loads(raw_value or "[]")
        if not isinstance(loaded, list):
            return []
        fields: list[IntentFieldDefinition] = []
        for item in loaded:
            try:
                fields.append(IntentFieldDefinition.model_validate(item))
            except Exception:
                continue
        return fields

    def _load_slot_schema(self, raw_value: str) -> list[IntentSlotDefinition]:
        """Parse slot schema JSON into validated slot definitions."""
        loaded = json_loads(raw_value or "[]")
        if not isinstance(loaded, list):
            return []
        slots: list[IntentSlotDefinition] = []
        for item in loaded:
            try:
                slots.append(IntentSlotDefinition.model_validate(item))
            except Exception:
                continue
        return slots

    def _load_graph_build_hints(self, raw_value: str) -> IntentGraphBuildHints:
        """Parse graph build hints JSON with a safe empty fallback."""
        loaded = self._load_json_object(raw_value)
        try:
            return IntentGraphBuildHints.model_validate(loaded)
        except Exception:
            return IntentGraphBuildHints()

    def _ensure_aware(self, value: datetime) -> datetime:
        """Normalize naive timestamps to UTC-aware timestamps."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _coerce_bool(self, value: object) -> bool:
        """Coerce legacy database truthy values into a strict boolean."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}
