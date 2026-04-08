from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from admin_api.app import create_app  # noqa: E402
from admin_api.dependencies import get_intent_repository  # noqa: E402
from persistence.in_memory_intent_repository import InMemoryIntentRepository  # noqa: E402


def _sample_payload(intent_code: str = "transfer_money") -> dict:
    return {
        "intent_code": intent_code,
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
                "label": "收款人",
                "description": "收款人姓名",
                "value_type": "person_name",
                "required": True,
                "allow_from_history": False,
                "aliases": ["收款人", "对方姓名"],
                "examples": ["张三", "我弟弟"],
                "overwrite_policy": "overwrite_if_new_nonempty",
            },
            {
                "slot_key": "amount",
                "label": "转账金额",
                "description": "本次转账金额",
                "value_type": "currency",
                "required": True,
                "allow_from_history": False,
                "aliases": ["金额", "转账金额"],
                "examples": ["500", "1000"],
                "overwrite_policy": "overwrite_if_new_nonempty",
            },
        ],
        "graph_build_hints": {
            "intent_scope_rule": "单次转账动作即使包含收款人、金额、卡号等要素，也只算一个 intent。",
            "planner_notes": "只有明确表达两个独立转账动作时，才允许生成多个 transfer_money 节点。",
            "single_node_examples": ["我要给我弟弟转500"],
            "multi_node_examples": ["先给我媳妇儿转500，再给我弟弟转300"],
            "confirm_policy": "auto",
            "max_nodes_per_message": 4,
        },
        "resume_policy": "resume_same_task",
    }


def test_intent_crud_and_status_filter_flow() -> None:
    async def run() -> None:
        app = create_app()
        repository = InMemoryIntentRepository()
        app.dependency_overrides[get_intent_repository] = lambda: repository

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            create_response = await client.post("/api/admin/intents", json=_sample_payload())
            assert create_response.status_code == 201
            assert create_response.json()["intent_code"] == "transfer_money"
            assert create_response.json()["status"] == "inactive"
            assert create_response.json()["slot_schema"][0]["slot_key"] == "recipient_name"
            assert create_response.json()["graph_build_hints"]["confirm_policy"] == "auto"

            list_response = await client.get("/api/admin/intents")
            assert list_response.status_code == 200
            assert list_response.json()["total"] == 1

            get_response = await client.get("/api/admin/intents/transfer_money")
            assert get_response.status_code == 200
            assert get_response.json()["name"] == "Transfer Money"

            update_payload = _sample_payload()
            update_payload["status"] = "inactive"
            update_payload["name"] = "Transfer Money Updated"
            update_response = await client.put("/api/admin/intents/transfer_money", json=update_payload)
            assert update_response.status_code == 200
            assert update_response.json()["status"] == "inactive"

            filtered_response = await client.get("/api/admin/intents", params={"status_filter": "active"})
            assert filtered_response.status_code == 200
            assert filtered_response.json()["total"] == 0

            delete_response = await client.delete("/api/admin/intents/transfer_money")
            assert delete_response.status_code == 204

            missing_response = await client.get("/api/admin/intents/transfer_money")
            assert missing_response.status_code == 404

    asyncio.run(run())


def test_activate_and_deactivate_endpoints_change_effective_status() -> None:
    async def run() -> None:
        app = create_app()
        repository = InMemoryIntentRepository()
        app.dependency_overrides[get_intent_repository] = lambda: repository

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            create_response = await client.post("/api/admin/intents", json=_sample_payload("query_order_status"))
            assert create_response.status_code == 201
            assert create_response.json()["status"] == "inactive"

            activate_response = await client.post("/api/admin/intents/query_order_status/activate")
            assert activate_response.status_code == 200
            assert activate_response.json()["status"] == "active"

            filtered_response = await client.get("/api/admin/intents", params={"status_filter": "active"})
            assert filtered_response.status_code == 200
            assert filtered_response.json()["total"] == 1

            deactivate_response = await client.post("/api/admin/intents/query_order_status/deactivate")
            assert deactivate_response.status_code == 200
            assert deactivate_response.json()["status"] == "inactive"

    asyncio.run(run())


def test_create_duplicate_intent_returns_conflict() -> None:
    async def run() -> None:
        app = create_app()
        repository = InMemoryIntentRepository()
        app.dependency_overrides[get_intent_repository] = lambda: repository

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response_1 = await client.post("/api/admin/intents", json=_sample_payload("pay_bill"))
            response_2 = await client.post("/api/admin/intents", json=_sample_payload("pay_bill"))

        assert response_1.status_code == 201
        assert response_2.status_code == 409
        assert "already exists" in response_2.json()["detail"].lower()

    asyncio.run(run())
