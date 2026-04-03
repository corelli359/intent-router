from __future__ import annotations

import asyncio

import httpx

from app import create_app


def test_platform_root_app_mounts_admin_and_router() -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()),
            base_url="http://testserver",
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["service"] == "intent-router-platform"

            admin_health = await client.get("/api/admin/health")
            assert admin_health.status_code == 200

            session = await client.post("/api/router/sessions")
            assert session.status_code == 201

    asyncio.run(run())
