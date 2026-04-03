from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient


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
        "status": "active",
        "dispatch_priority": 10,
        "request_schema": {"type": "object"},
        "field_mapping": {"amount": "$entities.amount"},
        "resume_policy": "resume_same_task",
    }


def test_intent_crud_and_status_filter_flow() -> None:
    app = create_app()
    repository = InMemoryIntentRepository()
    app.dependency_overrides[get_intent_repository] = lambda: repository
    client = TestClient(app)

    create_response = client.post("/admin/intents", json=_sample_payload())
    assert create_response.status_code == 201
    assert create_response.json()["intent_code"] == "transfer_money"

    list_response = client.get("/admin/intents")
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1

    get_response = client.get("/admin/intents/transfer_money")
    assert get_response.status_code == 200
    assert get_response.json()["name"] == "Transfer Money"

    update_payload = _sample_payload()
    update_payload["status"] = "inactive"
    update_payload["name"] = "Transfer Money Updated"
    update_response = client.put("/admin/intents/transfer_money", json=update_payload)
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "inactive"

    filtered_response = client.get("/admin/intents", params={"status_filter": "active"})
    assert filtered_response.status_code == 200
    assert filtered_response.json()["total"] == 0

    delete_response = client.delete("/admin/intents/transfer_money")
    assert delete_response.status_code == 204

    missing_response = client.get("/admin/intents/transfer_money")
    assert missing_response.status_code == 404


def test_create_duplicate_intent_returns_conflict() -> None:
    app = create_app()
    repository = InMemoryIntentRepository()
    app.dependency_overrides[get_intent_repository] = lambda: repository
    client = TestClient(app)

    response_1 = client.post("/admin/intents", json=_sample_payload("pay_bill"))
    response_2 = client.post("/admin/intents", json=_sample_payload("pay_bill"))

    assert response_1.status_code == 201
    assert response_2.status_code == 409
    assert "already exists" in response_2.json()["detail"].lower()
