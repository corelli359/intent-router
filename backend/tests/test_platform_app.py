from __future__ import annotations

from fastapi.testclient import TestClient

from app import create_app


def test_platform_root_app_mounts_admin_and_router() -> None:
    client = TestClient(create_app())

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["service"] == "intent-router-platform"

    admin_health = client.get("/api/admin/health")
    assert admin_health.status_code == 200

    session = client.post("/api/router/sessions")
    assert session.status_code == 201

