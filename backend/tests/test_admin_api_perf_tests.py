from __future__ import annotations

import asyncio

import httpx

from admin_service.api.app import create_admin_app
from admin_service.api.dependencies import get_perf_test_service
from admin_service.perf.case_catalog import PerfTestCaseCatalog
from admin_service.perf.models import (
    PerfTestCaseDefinition,
    PerfTestCreateRunRequest,
    PerfTestExpectation,
    PerfTestRunStatus,
    PerfTestStepPlan,
    PerfTestStepStatus,
)
from admin_service.perf.registry import PerfTestRunRegistry
from admin_service.perf.service import PerfTestService
from admin_service.settings import Settings


class _MockRouterTarget:
    def __init__(
        self,
        *,
        response_primary_intent_code: str = "AG_TRANS",
        response_slot_memory: dict[str, str] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self.response_primary_intent_code = response_primary_intent_code
        self.response_slot_memory = response_slot_memory or {
            "payee_name": "小明",
            "amount": "500",
        }
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, str]] = []
        self._session_counter = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.host, request.url.path))
        if request.url.path == "/api/router/v2/sessions":
            self._session_counter += 1
            return httpx.Response(
                201,
                json={
                    "session_id": f"session-{self._session_counter}",
                    "cust_id": "perf_test_cust",
                },
            )
        if request.url.path.startswith("/api/router/v2/sessions/") and request.url.path.endswith("/messages"):
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)
            return httpx.Response(
                200,
                json={
                    "analysis": {
                        "session_id": "session-analysis",
                        "cust_id": "perf_test_cust",
                        "content": "给小明转500元",
                        "no_match": False,
                        "recognition": {
                            "primary": [
                                {
                                    "intent_code": self.response_primary_intent_code,
                                    "confidence": 0.92,
                                    "reason": "heuristic matched catalog text '转5000元给朋友'",
                                }
                            ],
                            "candidates": [],
                        },
                        "slot_nodes": [
                            {
                                "node_id": "node_transfer",
                                "intent_code": self.response_primary_intent_code,
                                "title": "立即发起一笔转账交易",
                                "confidence": 0.92,
                                "position": 0,
                                "status": "ready_for_dispatch",
                                "slot_memory": dict(self.response_slot_memory),
                                "slot_bindings": [],
                                "history_slot_keys": [],
                                "diagnostics": [],
                                "output_payload": {},
                                "created_at": "2026-04-17T00:00:00Z",
                                "updated_at": "2026-04-17T00:00:00Z",
                            }
                        ],
                        "conditional_edges": [],
                        "diagnostics": [],
                    }
                },
            )
        return httpx.Response(404, json={"detail": "unexpected path"})


def _perf_case(
    *,
    case_id: str = "transfer-intent-slot-analysis",
    required_primary_intent_code: str = "AG_TRANS",
    required_slot_values: dict[str, str] | None = None,
) -> PerfTestCaseDefinition:
    return PerfTestCaseDefinition(
        case_id=case_id,
        name="Transfer Intent And Slot Analysis",
        description="Analyze-only transfer perf case",
        category="analyze_only",
        tags=["transfer", "analyze_only"],
        target_route="/api/router/v2/sessions/{session_id}/messages",
        notes=["test fixture"],
        session_request={"cust_id": "perf_test_case"},
        message_request={
            "content": "给小明转500元",
            "executionMode": "analyze_only",
        },
        default_steps=[PerfTestStepPlan(concurrency=2, duration_sec=0.05, warmup_sec=0.0, timeout_ms=1000)],
        expectations=PerfTestExpectation(
            required_primary_intent_code=required_primary_intent_code,
            required_slot_keys=["payee_name", "amount"],
            required_slot_values=required_slot_values or {
                "payee_name": "小明",
                "amount": "500",
            },
        ),
    )


def _build_perf_service(
    target: _MockRouterTarget,
    *,
    cases: list[PerfTestCaseDefinition] | None = None,
) -> PerfTestService:
    settings = Settings(
        perf_test_target_base_url="http://router-api-test.intent.svc.cluster.local:8000",
        perf_test_session_create_path="/api/router/v2/sessions",
        perf_test_message_path_template="/api/router/v2/sessions/{session_id}/messages",
        perf_test_request_timeout_seconds=2.0,
    )

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(target),
            base_url=settings.perf_test_target_base_url,
            timeout=settings.perf_test_request_timeout_seconds,
        )

    return PerfTestService(
        settings=settings,
        case_catalog=PerfTestCaseCatalog(cases or [_perf_case()]),
        registry=PerfTestRunRegistry(),
        client_factory=client_factory,
    )


async def _poll_run_until_terminal(client: httpx.AsyncClient, run_id: str) -> dict:
    for _ in range(100):
        response = await client.get(f"/api/admin/perf-tests/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {PerfTestRunStatus.COMPLETED, PerfTestRunStatus.FAILED}:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"run did not finish in time: {run_id}")


def test_perf_case_catalog_loads_default_transfer_analysis_case() -> None:
    catalog = PerfTestCaseCatalog.from_default_resource()

    cases = catalog.list_cases()

    assert [case.case_id for case in cases] == ["transfer-intent-slot-analysis"]
    assert cases[0].message_request["executionMode"] == "analyze_only"
    assert cases[0].message_request["content"] == "给小明转500元"
    assert cases[0].expectations.required_primary_intent_code == "AG_TRANS"
    assert cases[0].expectations.required_slot_values == {
        "payee_name": "小明",
        "amount": "500",
    }


def test_perf_service_executes_duration_ladder_and_uses_internal_target_url() -> None:
    async def run() -> None:
        target = _MockRouterTarget(delay_seconds=0.01)
        service = _build_perf_service(target)

        created = await service.create_run(
            PerfTestCreateRunRequest(
                case_id="transfer-intent-slot-analysis",
                ladder_steps=[PerfTestStepPlan(concurrency=2, duration_sec=0.05, timeout_ms=1000)],
            )
        )
        await asyncio.sleep(0.005)
        in_progress = service.get_run(created.run_id)
        assert in_progress.status in {PerfTestRunStatus.VALIDATING, PerfTestRunStatus.RUNNING}

        completed = await service.wait_for_run(created.run_id, timeout_seconds=5)

        assert completed.status == PerfTestRunStatus.COMPLETED
        assert completed.target_base_url == "http://router-api-test.intent.svc.cluster.local:8000"
        assert completed.progress.total_stages == 1
        assert completed.aggregate_metrics.total_requests >= 1
        assert completed.aggregate_metrics.success_count >= 1
        assert completed.aggregate_metrics.failure_count == 0
        assert completed.step_results[0].status == PerfTestStepStatus.COMPLETED
        assert completed.error_samples == []

        assert any(path == "/api/router/v2/sessions" for _, path in target.calls)
        assert any(path.endswith("/messages") for _, path in target.calls)
        assert {host for host, _ in target.calls} == {"router-api-test.intent.svc.cluster.local"}

    asyncio.run(run())


def test_perf_service_records_failed_validation_samples() -> None:
    async def run() -> None:
        target = _MockRouterTarget(response_primary_intent_code="AG_MENU_21")
        service = _build_perf_service(
            target,
            cases=[
                _perf_case(
                    required_primary_intent_code="AG_TRANS",
                    required_slot_values={"payee_name": "小明", "amount": "500"},
                )
            ],
        )

        created = await service.create_run(
            PerfTestCreateRunRequest(
                case_id="transfer-intent-slot-analysis",
                ladder_steps=[PerfTestStepPlan(concurrency=1, duration_sec=0.02, timeout_ms=1000)],
            )
        )
        failed = await service.wait_for_run(created.run_id, timeout_seconds=5)

        assert failed.status == PerfTestRunStatus.FAILED
        assert failed.aggregate_metrics.total_requests >= 1
        assert failed.aggregate_metrics.success_count == 0
        assert failed.aggregate_metrics.failure_count >= 1
        assert failed.step_results[0].status == PerfTestStepStatus.FAILED
        assert failed.step_results[0].failure_samples[0].error_type == "response_validation_error"
        assert "expected primary intent AG_TRANS" in failed.step_results[0].failure_samples[0].message
        assert failed.aggregate_metrics.status_code_breakdown.get("200", 0) >= 1

    asyncio.run(run())


def test_perf_api_exposes_cases_run_creation_polling_and_run_list() -> None:
    async def run() -> None:
        target = _MockRouterTarget(delay_seconds=0.005)
        service = _build_perf_service(target)
        app = create_admin_app()
        app.dependency_overrides[get_perf_test_service] = lambda: service

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            cases_response = await client.get("/api/admin/perf-tests/cases")
            assert cases_response.status_code == 200
            assert cases_response.json()["total"] == 1
            assert cases_response.json()["items"][0]["case_id"] == "transfer-intent-slot-analysis"
            assert "default_steps" in cases_response.json()["items"][0]

            create_response = await client.post(
                "/api/admin/perf-tests/runs",
                json={
                    "case_id": "transfer-intent-slot-analysis",
                    "ladder_steps": [
                        {
                            "name": "阶段 1",
                            "concurrency": 2,
                            "duration_sec": 0.05,
                            "warmup_sec": 0,
                            "timeout_ms": 1000,
                        }
                    ],
                    "max_failed_samples": 5,
                },
            )
            assert create_response.status_code == 201
            created_payload = create_response.json()
            run_id = created_payload["run_id"]
            assert run_id.startswith("perf_")
            assert created_payload["target_base_url"] == "http://router-api-test.intent.svc.cluster.local:8000"
            assert created_payload["status"] in {"queued", "validating", "running"}
            assert len(created_payload["ladder_steps"]) == 1

            detail_payload = await _poll_run_until_terminal(client, run_id)
            assert detail_payload["status"] == "completed"
            assert detail_payload["aggregate_metrics"]["success_count"] >= 1
            assert detail_payload["aggregate_metrics"]["failure_count"] == 0
            assert "progress" in detail_payload

            runs_response = await client.get("/api/admin/perf-tests/runs")
            assert runs_response.status_code == 200
            assert runs_response.json()["total"] == 1
            assert runs_response.json()["items"][0]["run_id"] == run_id
            assert runs_response.json()["items"][0]["total_stages"] == 1

    asyncio.run(run())
