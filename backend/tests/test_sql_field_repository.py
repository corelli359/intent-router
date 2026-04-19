from __future__ import annotations

from pathlib import Path

from admin_service.models.intent import IntentFieldDefinition, SlotValueType
from admin_service.storage.sql_field_repository import DatabaseIntentFieldRepository


def _field(field_code: str = "person_name") -> IntentFieldDefinition:
    return IntentFieldDefinition(
        field_code=field_code,
        label="姓名",
        semantic_definition="用于标识自然人的姓名字段",
        value_type=SlotValueType.PERSON_NAME,
        aliases=["姓名"],
        examples=["张三"],
    )


def test_database_field_repository_persists_records_across_instances(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'intent-fields.db'}"
    repository = DatabaseIntentFieldRepository(database_url)
    repository.create_field(_field())

    reloaded = DatabaseIntentFieldRepository(database_url)
    fields = reloaded.list_fields()
    record = reloaded.get_field("person_name")

    assert [field.field_code for field in fields] == ["person_name"]
    assert record.value_type == SlotValueType.PERSON_NAME
    assert record.aliases == ["姓名"]
