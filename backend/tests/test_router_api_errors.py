from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from router_service.api.app import create_router_app
from router_service.api.dependencies import get_orchestrator
from router_service.core.shared.diagnostics import RouterDiagnostic, RouterDiagnosticCode
from router_service.core.shared.graph_domain import GraphRouterSnapshot
from router_service.core.support.agent_barrier import AgentBarrierTriggeredError


class _StubOrchestrator:
    """Minimal orchestrator stub used to exercise API envelopes."""

    def __init__(self) -> None:
        self.snapshot_payload = GraphRouterSnapshot(
            session_id="session_demo",
            cust_id="cust_demo",
            messages=[],
            candidate_intents=[],
            last_diagnostics=[
                RouterDiagnostic(
                    code=RouterDiagnosticCode.SLOT_REQUIRED_MISSING,
                    source="slot_validator",
                    message="当前节点仍缺少必填槽位",
                    details={"missing_required_slots": ["amount"]},
                )
            ],
            current_graph=None,
            pending_graph=None,
            active_node_id=None,
            expires_at=datetime.now(timezone.utc),
        )

    def snapshot(self, session_id: str):
        if session_id == "missing-session":
            raise KeyError("missing")
        return self.snapshot_payload

    async def handle_user_message_serialized(self, *, serializer, **kwargs):
        del kwargs
        return serializer(self.snapshot_payload)

    async def handle_user_message(self, *args, **kwargs):
        return self.snapshot_payload

    async def handle_task_completion_serialized(self, *, session_id, task_id, completion_signal, serializer, **kwargs):
        del serializer, kwargs
        if session_id == "missing-session":
            raise KeyError(session_id)
        if task_id == "missing-task":
            raise KeyError(task_id)
        return {
            "ok": True,
            "current_task": task_id,
            "task_list": [{"name": task_id, "status": "completed"}],
            "completion_state": 2,
            "completion_reason": "assistant_final_done",
            "intent_code": "AG_TRANS",
            "status": "completed",
            "message": "执行图已完成",
            "slot_memory": {},
            "output": {},
        }


def _app_with_stub_orchestrator() -> tuple[object, _StubOrchestrator]:
    orchestrator = _StubOrchestrator()
    app = create_router_app()
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    return app, orchestrator


def test_legacy_router_session_endpoints_are_removed() -> None:
    app, _ = _app_with_stub_orchestrator()
    assert all("/sessions" not in getattr(route, "path", "") for route in app.routes)


def test_router_v1_message_returns_structured_validation_error() -> None:
    async def run() -> None:
        app, _ = _app_with_stub_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post("/api/v1/message", json={})

        assert response.status_code == 422
        payload = response.json()
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ROUTER_REQUEST_VALIDATION_FAILED"

    asyncio.run(run())

def test_router_v1_message_returns_output_envelope_without_snapshot() -> None:
    async def run() -> None:
        app, _ = _app_with_stub_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "session_demo",
                    "txt": "帮我转账",
                    "stream": False,
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert "snapshot" not in payload
        assert payload["status"] == "idle"
        assert payload["output"] == {}

    asyncio.run(run())


def test_router_v1_message_non_stream_returns_output_without_snapshot() -> None:
    async def run() -> None:
        app, _ = _app_with_stub_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "session_demo",
                    "txt": "帮我转账",
                    "stream": False,
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert "snapshot" not in payload
        assert payload["status"] == "idle"
        assert payload["output"] == {}

    asyncio.run(run())


def test_router_v1_task_completion_returns_output_without_snapshot() -> None:
    async def run() -> None:
        app, _ = _app_with_stub_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/task/completion",
                json={
                    "sessionId": "session_demo",
                    "taskId": "task_demo",
                    "completionSignal": 2,
                    "stream": False,
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert "snapshot" not in payload
        assert payload["current_task"] == "task_demo"
        assert payload["completion_state"] == 2
        assert payload["output"] == {}

    asyncio.run(run())


def test_router_v1_task_completion_returns_structured_session_not_found_error() -> None:
    async def run() -> None:
        app, _ = _app_with_stub_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/task/completion",
                json={
                    "sessionId": "missing-session",
                    "taskId": "task_demo",
                    "completionSignal": 2,
                    "stream": False,
                },
            )

        assert response.status_code == 404
        payload = response.json()
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ROUTER_SESSION_NOT_FOUND"

    asyncio.run(run())


def test_router_api_returns_structured_agent_barrier_error() -> None:
    class _AgentBarrierOrchestrator(_StubOrchestrator):
        async def handle_user_message_serialized(self, **kwargs):
            del kwargs
            raise AgentBarrierTriggeredError("ROUTER_AGENT_BARRIER_ENABLED=true blocked a real agent call")

        async def handle_user_message(self, *args, **kwargs):
            raise AgentBarrierTriggeredError("ROUTER_AGENT_BARRIER_ENABLED=true blocked a real agent call")

    async def run() -> None:
        app = create_router_app()
        app.dependency_overrides[get_orchestrator] = lambda: _AgentBarrierOrchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "session_demo",
                    "txt": "帮我转账",
                    "stream": False,
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is False
        assert payload["status"] == "failed"
        assert payload["errorCode"] == "ROUTER_AGENT_BARRIER_TRIGGERED"
        assert payload["message"] == "ROUTER_AGENT_BARRIER_ENABLED=true blocked a real agent call"

    asyncio.run(run())
