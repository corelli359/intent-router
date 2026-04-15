from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from router_service.api.app import create_router_app
from router_service.api.dependencies import get_orchestrator, get_orchestrator_v2
from router_service.core.graph.orchestrator import MessageAnalysisResult
from router_service.core.recognition.recognizer import RecognitionResult
from router_service.core.shared.diagnostics import RouterDiagnostic, RouterDiagnosticCode
from router_service.core.shared.graph_domain import GraphRouterSnapshot


class _StubOrchestrator:
    """Minimal orchestrator stub used to exercise API envelopes."""

    def __init__(self) -> None:
        self.analysis = MessageAnalysisResult(
            session_id="session_demo",
            cust_id="cust_demo",
            content="给小红转200",
            recognition=RecognitionResult(primary=[], candidates=[], diagnostics=[]),
            graph=None,
            no_match=True,
            diagnostics=[
                RouterDiagnostic(
                    code=RouterDiagnosticCode.ROUTER_NO_MATCH,
                    source="compiler",
                    message="当前消息未识别到可执行意图",
                    details={"content": "给小红转200"},
                )
            ],
        )
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

    async def analyze_user_message(self, *args, **kwargs):
        return self.analysis

    async def handle_user_message(self, *args, **kwargs):
        return self.snapshot_payload


def _app_with_stub_orchestrator() -> tuple[object, _StubOrchestrator]:
    orchestrator = _StubOrchestrator()
    app = create_router_app()
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_orchestrator_v2] = lambda: orchestrator
    return app, orchestrator


def test_router_api_returns_structured_session_not_found_error() -> None:
    async def run() -> None:
        app, _ = _app_with_stub_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/router/sessions/missing-session")

        assert response.status_code == 404
        payload = response.json()
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ROUTER_SESSION_NOT_FOUND"
        assert payload["error"]["details"]["session_id"] == "missing-session"

    asyncio.run(run())


def test_router_api_returns_structured_validation_error() -> None:
    async def run() -> None:
        app, _ = _app_with_stub_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post("/api/router/sessions/session_demo/messages", json={})

        assert response.status_code == 422
        payload = response.json()
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ROUTER_REQUEST_VALIDATION_FAILED"

    asyncio.run(run())


def test_router_analyze_response_includes_diagnostics() -> None:
    async def run() -> None:
        app, _ = _app_with_stub_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/router/sessions/session_demo/messages/analyze",
                json={"content": "给小红转200"},
            )

        assert response.status_code == 200
        payload = response.json()["analysis"]
        assert payload["diagnostics"][0]["code"] == "ROUTER_NO_MATCH"

    asyncio.run(run())


def test_router_execute_snapshot_includes_last_diagnostics() -> None:
    async def run() -> None:
        app, _ = _app_with_stub_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/router/sessions/session_demo/messages",
                json={"content": "帮我转账"},
            )

        assert response.status_code == 200
        payload = response.json()["snapshot"]
        assert payload["last_diagnostics"][0]["code"] == "SLOT_REQUIRED_MISSING"

    asyncio.run(run())
