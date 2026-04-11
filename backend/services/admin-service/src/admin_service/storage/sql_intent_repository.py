from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from intent_registry_contracts.models import (
    IntentFieldDefinition,
    IntentGraphBuildHints,
    IntentPayload,
    IntentRecord,
    IntentSlotDefinition,
    IntentStatus,
)
from admin_service.storage.intent_repository import (
    IntentAlreadyExistsError,
    IntentNotFoundError,
    IntentRepository,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_database_url(database_url: str) -> str:
    normalized = database_url.strip()
    if normalized.startswith("mysql://"):
        return normalized.replace("mysql://", "mysql+pymysql://", 1)
    return normalized


class Base(DeclarativeBase):
    pass


class IntentRow(Base):
    __tablename__ = "intent_registry"

    intent_code: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    examples_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    agent_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dispatch_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    request_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    field_mapping_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    field_catalog_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    slot_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    graph_build_hints_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    resume_policy: Mapped[str] = mapped_column(String(128), nullable=False, default="resume_same_task")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DatabaseIntentRepository(IntentRepository):
    def __init__(self, database_url: str) -> None:
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
        with self._session() as session:
            statement = select(IntentRow).order_by(IntentRow.created_at.asc(), IntentRow.intent_code.asc())
            if status is not None:
                statement = statement.where(IntentRow.status == status.value)
            rows = session.scalars(statement).all()
            return [self._to_record(row) for row in rows]

    def get_intent(self, intent_code: str) -> IntentRecord:
        with self._session() as session:
            row = session.get(IntentRow, intent_code)
            if row is None:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")
            return self._to_record(row)

    def create_intent(self, payload: IntentPayload) -> IntentRecord:
        with self._session() as session:
            existing = session.get(IntentRow, payload.intent_code)
            if existing is not None:
                raise IntentAlreadyExistsError(f"Intent already exists: {payload.intent_code}")

            now = utcnow()
            row = IntentRow(
                intent_code=payload.intent_code,
                name=payload.name,
                description=payload.description,
                examples_json=self._dump_json(payload.examples),
                agent_url=payload.agent_url,
                status=payload.status.value,
                is_fallback=payload.is_fallback,
                dispatch_priority=payload.dispatch_priority,
                request_schema_json=self._dump_json(payload.request_schema),
                field_mapping_json=self._dump_json(payload.field_mapping),
                field_catalog_json=self._dump_json([field.model_dump(mode="json") for field in payload.field_catalog]),
                slot_schema_json=self._dump_json([slot.model_dump(mode="json") for slot in payload.slot_schema]),
                graph_build_hints_json=self._dump_json(payload.graph_build_hints.model_dump(mode="json")),
                resume_policy=payload.resume_policy,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return self._to_record(row)

    def update_intent(self, intent_code: str, payload: IntentPayload) -> IntentRecord:
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
            row.examples_json = self._dump_json(payload.examples)
            row.agent_url = payload.agent_url
            row.status = payload.status.value
            row.is_fallback = payload.is_fallback
            row.dispatch_priority = payload.dispatch_priority
            row.request_schema_json = self._dump_json(payload.request_schema)
            row.field_mapping_json = self._dump_json(payload.field_mapping)
            row.field_catalog_json = self._dump_json([field.model_dump(mode="json") for field in payload.field_catalog])
            row.slot_schema_json = self._dump_json([slot.model_dump(mode="json") for slot in payload.slot_schema])
            row.graph_build_hints_json = self._dump_json(payload.graph_build_hints.model_dump(mode="json"))
            row.resume_policy = payload.resume_policy
            row.updated_at = utcnow()

            session.commit()
            return self._to_record(row)

    def delete_intent(self, intent_code: str) -> None:
        with self._session() as session:
            row = session.get(IntentRow, intent_code)
            if row is None:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")
            session.delete(row)
            session.commit()

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _to_record(self, row: IntentRow) -> IntentRecord:
        return IntentRecord(
            intent_code=row.intent_code,
            name=row.name,
            description=row.description,
            examples=self._load_json_list(row.examples_json),
            agent_url=row.agent_url,
            status=IntentStatus(row.status),
            is_fallback=row.is_fallback,
            dispatch_priority=row.dispatch_priority,
            request_schema=self._load_json_object(row.request_schema_json),
            field_mapping=self._load_json_str_dict(row.field_mapping_json),
            field_catalog=self._load_field_catalog(getattr(row, "field_catalog_json", "[]")),
            slot_schema=self._load_slot_schema(getattr(row, "slot_schema_json", "[]")),
            graph_build_hints=self._load_graph_build_hints(getattr(row, "graph_build_hints_json", "{}")),
            resume_policy=row.resume_policy,
            created_at=self._ensure_aware(row.created_at),
            updated_at=self._ensure_aware(row.updated_at),
        )

    def _ensure_compatible_schema(self) -> None:
        inspector = inspect(self._engine)
        if "intent_registry" not in inspector.get_table_names():
            return
        existing_columns = {column["name"] for column in inspector.get_columns("intent_registry")}
        additions = {
            "field_catalog_json": "[]",
            "slot_schema_json": "[]",
            "graph_build_hints_json": "{}",
        }
        missing = {name: default for name, default in additions.items() if name not in existing_columns}
        if not missing:
            return

        with self._engine.begin() as connection:
            for column_name, default_value in missing.items():
                connection.execute(
                    text(
                        f"ALTER TABLE intent_registry "
                        f"ADD COLUMN {column_name} TEXT NOT NULL DEFAULT '{default_value}'"
                    )
                )

    def _dump_json(self, value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _load_json_object(self, raw_value: str) -> dict[str, object]:
        loaded = json.loads(raw_value or "{}")
        if isinstance(loaded, dict):
            return loaded
        return {}

    def _load_json_list(self, raw_value: str) -> list[str]:
        loaded = json.loads(raw_value or "[]")
        if isinstance(loaded, list):
            return [str(item) for item in loaded]
        return []

    def _load_json_str_dict(self, raw_value: str) -> dict[str, str]:
        loaded = self._load_json_object(raw_value)
        return {str(key): str(value) for key, value in loaded.items()}

    def _load_field_catalog(self, raw_value: str) -> list[IntentFieldDefinition]:
        loaded = json.loads(raw_value or "[]")
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
        loaded = json.loads(raw_value or "[]")
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
        loaded = self._load_json_object(raw_value)
        try:
            return IntentGraphBuildHints.model_validate(loaded)
        except Exception:
            return IntentGraphBuildHints()

    def _ensure_aware(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
