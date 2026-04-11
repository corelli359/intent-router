from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import DateTime, String, Text, create_engine, inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from intent_registry_contracts.models import IntentFieldDefinition, IntentFieldRecord, SlotValueType
from admin_service.storage.field_repository import (
    IntentFieldAlreadyExistsError,
    IntentFieldNotFoundError,
    IntentFieldRepository,
)
from admin_service.storage.sql_intent_repository import normalize_database_url


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class IntentFieldRow(Base):
    __tablename__ = "intent_field_catalog"

    field_code: Mapped[str] = mapped_column(String(128), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    semantic_definition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    value_type: Mapped[str] = mapped_column(String(64), nullable=False, default=SlotValueType.STRING.value)
    aliases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    examples_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    counter_examples_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    format_hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    normalization_hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    validation_hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DatabaseIntentFieldRepository(IntentFieldRepository):
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

    def list_fields(self) -> list[IntentFieldRecord]:
        with self._session() as session:
            rows = session.scalars(select(IntentFieldRow).order_by(IntentFieldRow.created_at.asc(), IntentFieldRow.field_code.asc())).all()
            return [self._to_record(row) for row in rows]

    def get_field(self, field_code: str) -> IntentFieldRecord:
        with self._session() as session:
            row = session.get(IntentFieldRow, field_code)
            if row is None:
                raise IntentFieldNotFoundError(f"Field not found: {field_code}")
            return self._to_record(row)

    def create_field(self, payload: IntentFieldDefinition) -> IntentFieldRecord:
        with self._session() as session:
            existing = session.get(IntentFieldRow, payload.field_code)
            if existing is not None:
                raise IntentFieldAlreadyExistsError(f"Field already exists: {payload.field_code}")
            now = utcnow()
            row = IntentFieldRow(
                field_code=payload.field_code,
                label=payload.label,
                semantic_definition=payload.semantic_definition,
                value_type=payload.value_type.value,
                aliases_json=self._dump_json(payload.aliases),
                examples_json=self._dump_json(payload.examples),
                counter_examples_json=self._dump_json(payload.counter_examples),
                format_hint=payload.format_hint,
                normalization_hint=payload.normalization_hint,
                validation_hint=payload.validation_hint,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return self._to_record(row)

    def update_field(self, field_code: str, payload: IntentFieldDefinition) -> IntentFieldRecord:
        with self._session() as session:
            row = session.get(IntentFieldRow, field_code)
            if row is None:
                raise IntentFieldNotFoundError(f"Field not found: {field_code}")
            if payload.field_code != field_code:
                replacement = session.get(IntentFieldRow, payload.field_code)
                if replacement is not None:
                    raise IntentFieldAlreadyExistsError(f"Field already exists: {payload.field_code}")
            row.field_code = payload.field_code
            row.label = payload.label
            row.semantic_definition = payload.semantic_definition
            row.value_type = payload.value_type.value
            row.aliases_json = self._dump_json(payload.aliases)
            row.examples_json = self._dump_json(payload.examples)
            row.counter_examples_json = self._dump_json(payload.counter_examples)
            row.format_hint = payload.format_hint
            row.normalization_hint = payload.normalization_hint
            row.validation_hint = payload.validation_hint
            row.updated_at = utcnow()
            session.commit()
            return self._to_record(row)

    def delete_field(self, field_code: str) -> None:
        with self._session() as session:
            row = session.get(IntentFieldRow, field_code)
            if row is None:
                raise IntentFieldNotFoundError(f"Field not found: {field_code}")
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

    def _ensure_compatible_schema(self) -> None:
        inspector = inspect(self._engine)
        if "intent_field_catalog" not in inspector.get_table_names():
            return

    def _to_record(self, row: IntentFieldRow) -> IntentFieldRecord:
        return IntentFieldRecord(
            field_code=row.field_code,
            label=row.label,
            semantic_definition=row.semantic_definition,
            value_type=SlotValueType(row.value_type),
            aliases=self._load_json_list(row.aliases_json),
            examples=self._load_json_list(row.examples_json),
            counter_examples=self._load_json_list(row.counter_examples_json),
            format_hint=row.format_hint,
            normalization_hint=row.normalization_hint,
            validation_hint=row.validation_hint,
            created_at=self._ensure_aware(row.created_at),
            updated_at=self._ensure_aware(row.updated_at),
        )

    def _dump_json(self, value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _load_json_list(self, raw_value: str) -> list[str]:
        loaded = json.loads(raw_value or "[]")
        if isinstance(loaded, list):
            return [str(item) for item in loaded]
        return []

    def _ensure_aware(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
