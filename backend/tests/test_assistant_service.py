from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ASSISTANT_SRC = BACKEND_ROOT / "services" / "assistant-service" / "src"
if str(ASSISTANT_SRC) not in sys.path:
    sys.path.insert(0, str(ASSISTANT_SRC))

from assistant_service.app import AssistantRunRequest, RouterForwardService, create_app, get_router_forward_service  # noqa: E402


def test_assistant_service_forwards_non_stream_request_to_router() -> None:
    async def run() -> None:
        router_app = FastAPI()
        received: dict[str, object] = {}

        @router_app.post("/api/router/v2/sessions/{session_id}/messages")
        async def router_message(session_id: str, payload: dict[str, object]) -> dict[str, object]:
            received["session_id"] = session_id
            received["payload"] = payload
            return {
                "ok": True,
                "output": {
                    "intent_code": "AG_TRANS",
                    "isHandOver": True,
                    "data": [{"answer": "||200|小明|"}],
                },
            }

        router_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app),
            base_url="http://router.test",
        )
        service = RouterForwardService(
            router_base_url="http://router.test",
            http_client=router_client,
        )
        app = create_app()
        app.dependency_overrides[get_router_forward_service] = lambda: service

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://assistant.test",
        ) as client:
            response = await client.post(
                "/api/assistant/run",
                json=AssistantRunRequest(
                    sessionId="session_assistant_001",
                    txt="给小明转账 200",
                    config_variables=[
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "session_assistant_001"},
                    ],
                ).model_dump(mode="json", by_alias=True),
            )

        await router_client.aclose()

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "output": {
                "intent_code": "AG_TRANS",
                "isHandOver": True,
                "data": [{"answer": "||200|小明|"}],
            },
        }
        assert received == {
            "session_id": "session_assistant_001",
            "payload": {
                "txt": "给小明转账 200",
                "config_variables": [
                    {"name": "custID", "value": "C0001"},
                    {"name": "sessionID", "value": "session_assistant_001"},
                ],
                "executionMode": "execute",
            },
        }

    asyncio.run(run())
