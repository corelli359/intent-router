from __future__ import annotations

import asyncio

import httpx

from admin_service.api.app import create_app
from admin_service.api.dependencies import get_field_repository, get_intent_repository
from admin_service.storage.in_memory_field_repository import InMemoryIntentFieldRepository
from admin_service.storage.in_memory_intent_repository import InMemoryIntentRepository


def _sample_field(field_code: str = "person_name") -> dict:
    return {
        "field_code": field_code,
        "label": "姓名" if field_code == "person_name" else field_code,
        "semantic_definition": "用于标识自然人的姓名字段",
        "value_type": "person_name",
        "examples": ["张三", "李四"],
    }


def _sample_intent() -> dict:
    return {
        "intent_code": "transfer_money",
        "name": "Transfer Money",
        "description": "Handle transfer requests",
        "examples": ["transfer 100 to Alex"],
        "agent_url": "https://agent.example.com/transfer",
        "status": "inactive",
        "dispatch_priority": 10,
        "request_schema": {"type": "object"},
        "field_mapping": {"amount": "$entities.amount"},
        "slot_schema": [
            {
                "slot_key": "recipient_name",
                "field_code": "person_name",
                "role": "recipient_name",
                "label": "收款人",
                "description": "收款人姓名",
                "semantic_definition": "本次转账的收款人姓名",
                "value_type": "person_name",
                "required": True,
            }
        ],
        "resume_policy": "resume_same_task",
    }


def test_field_crud_and_delete_in_use_guard() -> None:
    async def run() -> None:
        app = create_app()
        field_repository = InMemoryIntentFieldRepository()
        intent_repository = InMemoryIntentRepository()
        app.dependency_overrides[get_field_repository] = lambda: field_repository
        app.dependency_overrides[get_intent_repository] = lambda: intent_repository

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            create_response = await client.post("/api/admin/fields", json=_sample_field())
            assert create_response.status_code == 201
            assert create_response.json()["field_code"] == "person_name"

            list_response = await client.get("/api/admin/fields")
            assert list_response.status_code == 200
            assert list_response.json()["total"] == 1

            get_response = await client.get("/api/admin/fields/person_name")
            assert get_response.status_code == 200
            assert get_response.json()["semantic_definition"] == "用于标识自然人的姓名字段"

            update_payload = _sample_field()
            update_payload["label"] = "自然人姓名"
            update_response = await client.put("/api/admin/fields/person_name", json=update_payload)
            assert update_response.status_code == 200
            assert update_response.json()["label"] == "自然人姓名"

            intent_response = await client.post("/api/admin/intents", json=_sample_intent())
            assert intent_response.status_code == 201
            assert intent_response.json()["field_catalog"][0]["field_code"] == "person_name"

            in_use_delete = await client.delete("/api/admin/fields/person_name")
            assert in_use_delete.status_code == 409
            assert "referenced" in in_use_delete.json()["detail"].lower()

            delete_intent = await client.delete("/api/admin/intents/transfer_money")
            assert delete_intent.status_code == 204

            delete_field = await client.delete("/api/admin/fields/person_name")
            assert delete_field.status_code == 204

    asyncio.run(run())
