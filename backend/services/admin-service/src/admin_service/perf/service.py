from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx

from admin_service.perf.case_catalog import PerfTestCaseCatalog
from admin_service.perf.models import (
    PerfFailureSample,
    PerfMetrics,
    PerfRunProgress,
    PerfStageResult,
    PerfTestCaseDefinition,
    PerfTestCaseRuntimeOverride,
    PerfTestCreateRunRequest,
    PerfTestRunDetail,
    PerfTestRunStatus,
    PerfTestRunSummary,
    PerfTestStepPlan,
    PerfTestStepStatus,
)
from admin_service.perf.registry import PerfTestRunRegistry
from admin_service.settings import Settings


ClientFactory = Callable[[], httpx.AsyncClient]


@dataclass(slots=True)
class _RequestExecutionResult:
    request_number: int
    success: bool
    latency_ms: float
    finished_counter: float
    status_code: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    response_excerpt: str | None = None
    timeout: bool = False


class PerfTestService:
    def __init__(
        self,
        *,
        settings: Settings,
        case_catalog: PerfTestCaseCatalog,
        registry: PerfTestRunRegistry,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._case_catalog = case_catalog
        self._registry = registry
        self._client_factory = client_factory or self._default_client_factory

    def list_cases(self) -> list[PerfTestCaseDefinition]:
        return self._case_catalog.list_cases()

    def list_runs(self) -> list[PerfTestRunSummary]:
        return [PerfTestRunSummary.from_run(run) for run in self._registry.list_runs()]

    def get_run(self, run_id: str) -> PerfTestRunDetail:
        return self._registry.get_run(run_id)

    async def create_run(self, request: PerfTestCreateRunRequest) -> PerfTestRunDetail:
        case = self._resolve_case(request.case_id, request.case_override)
        plan = [step.model_copy(deep=True) for step in (request.ladder_steps or case.default_steps)]
        if not plan:
            raise RuntimeError(f"perf test case has no default steps: {case.case_id}")

        created_at = self._utcnow()
        run = PerfTestRunDetail(
            run_id=self._build_run_id(),
            case_id=case.case_id,
            case_name=case.name,
            status=PerfTestRunStatus.QUEUED,
            target_base_url=self._settings.perf_test_target_base_url,
            created_at=created_at,
            updated_at=created_at,
            progress=PerfRunProgress(total_stages=len(plan)),
            session_request=dict(case.session_request),
            message_request=dict(case.message_request),
            expectations=case.expectations.model_copy(deep=True),
            ladder_steps=plan,
            step_results=[
                PerfStageResult(
                    stage_id=step.step_id or f"stage-{index + 1}",
                    step_index=index,
                    name=step.name or f"阶段 {index + 1}",
                    concurrency=step.concurrency,
                    duration_sec=step.duration_sec,
                    warmup_sec=step.warmup_sec,
                    request_limit=step.request_limit,
                    cooldown_sec=step.cooldown_sec,
                    timeout_ms=step.timeout_ms,
                )
                for index, step in enumerate(plan)
            ],
        )
        self._registry.create_run(run)

        task = asyncio.create_task(
            self._execute_run(
                run_id=run.run_id,
                case=case,
                plan=plan,
                max_failed_samples=request.max_failed_samples,
            )
        )
        self._registry.set_task(run.run_id, task)
        return self._registry.get_run(run.run_id)

    async def cancel_run(self, run_id: str) -> PerfTestRunDetail:
        run = self._registry.get_run(run_id)
        if run.status in {
            PerfTestRunStatus.COMPLETED,
            PerfTestRunStatus.FAILED,
            PerfTestRunStatus.CANCELLED,
        }:
            return run

        task = self._registry.get_task(run_id)
        if task is None:
            self._registry.update_run(
                run_id,
                lambda current: self._finalize_run_cancelled(current, "task_missing"),
            )
            return self._registry.get_run(run_id)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        return self._registry.get_run(run_id)

    async def wait_for_run(self, run_id: str, timeout_seconds: float = 10.0) -> PerfTestRunDetail:
        task = self._registry.get_task(run_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        return self._registry.get_run(run_id)

    def _default_client_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.perf_test_target_base_url,
            timeout=self._settings.perf_test_request_timeout_seconds,
        )

    async def _execute_run(
        self,
        *,
        run_id: str,
        case: PerfTestCaseDefinition,
        plan: list[PerfTestStepPlan],
        max_failed_samples: int,
    ) -> None:
        self._registry.update_run(run_id, self._mark_run_validating)
        try:
            async with self._client_factory() as client:
                self._registry.update_run(run_id, self._mark_run_running)
                for step_index, step in enumerate(plan):
                    await self._execute_step(
                        client=client,
                        run_id=run_id,
                        case=case,
                        step_index=step_index,
                        step=step,
                        max_failed_samples=max_failed_samples,
                    )
            self._registry.update_run(run_id, self._finalize_run_success)
        except asyncio.CancelledError:
            self._registry.update_run(
                run_id,
                lambda run: self._finalize_run_cancelled(run, "cancelled_by_admin"),
            )
            raise
        except Exception as exc:
            self._registry.update_run(
                run_id,
                lambda run: self._finalize_run_failure(run, str(exc)),
            )
        finally:
            self._registry.clear_task(run_id)

    async def _execute_step(
        self,
        *,
        client: httpx.AsyncClient,
        run_id: str,
        case: PerfTestCaseDefinition,
        step_index: int,
        step: PerfTestStepPlan,
        max_failed_samples: int,
    ) -> None:
        step_started_at = self._utcnow()
        step_started_counter = perf_counter()
        warmup_until = step_started_counter + step.warmup_sec
        deadline = step_started_counter + step.duration_sec
        issued_requests = 0
        issue_lock = asyncio.Lock()
        measured_latencies_ms: list[float] = []

        self._registry.update_run(
            run_id,
            lambda run: self._mark_step_started(run, step_index, step_started_at),
        )

        async def next_request_number() -> int | None:
            nonlocal issued_requests
            async with issue_lock:
                if perf_counter() >= deadline:
                    return None
                if step.request_limit is not None and issued_requests >= step.request_limit:
                    return None
                issued_requests += 1
                return issued_requests

        async def worker() -> None:
            while True:
                request_number = await next_request_number()
                if request_number is None:
                    return
                result = await self._exercise_case(
                    client=client,
                    case=case,
                    request_number=request_number,
                    timeout_ms=step.timeout_ms,
                )
                measured = result.finished_counter >= warmup_until
                failure = None
                if measured and not result.success:
                    failure = PerfFailureSample(
                        sample_id=f"{run_id}-{step_index + 1}-{request_number}",
                        stage_name=step.name,
                        step_index=step_index,
                        status_code=result.status_code,
                        error_type=result.error_type or "request_failed",
                        message=result.error_message or "request failed",
                        latency_ms=result.latency_ms,
                        request_summary=case.name,
                        occurred_at=self._utcnow(),
                    )
                self._registry.update_run(
                    run_id,
                    lambda run: self._record_request_result(
                        run=run,
                        step_index=step_index,
                        result=result,
                        failure=failure,
                        measured=measured,
                        max_failed_samples=max_failed_samples,
                        measured_latencies_ms=measured_latencies_ms,
                    ),
                )

        workers = [
            asyncio.create_task(worker())
            for _ in range(step.concurrency)
        ]
        await asyncio.gather(*workers)

        wall_time_ms = (perf_counter() - step_started_counter) * 1000
        self._registry.update_run(
            run_id,
            lambda run: self._mark_step_finished(
                run=run,
                step_index=step_index,
                finished_at=self._utcnow(),
                measured_latencies_ms=measured_latencies_ms,
                wall_time_ms=wall_time_ms,
            ),
        )
        if step.cooldown_sec > 0:
            await asyncio.sleep(step.cooldown_sec)

    async def _exercise_case(
        self,
        *,
        client: httpx.AsyncClient,
        case: PerfTestCaseDefinition,
        request_number: int,
        timeout_ms: int | None,
    ) -> _RequestExecutionResult:
        started = perf_counter()
        request_timeout = (timeout_ms / 1000) if timeout_ms is not None else None
        try:
            session_response = await client.post(
                self._settings.perf_test_session_create_path,
                json=case.session_request,
                timeout=request_timeout,
            )
            session_payload = self._json_or_none(session_response)
            session_id = session_payload.get("session_id") if isinstance(session_payload, dict) else None
            if session_response.status_code != case.expectations.session_status_code:
                return _RequestExecutionResult(
                    request_number=request_number,
                    success=False,
                    latency_ms=(perf_counter() - started) * 1000,
                    finished_counter=perf_counter(),
                    status_code=session_response.status_code,
                    error_type="session_status_mismatch",
                    error_message=(
                        f"expected session status {case.expectations.session_status_code}, "
                        f"got {session_response.status_code}"
                    ),
                    response_excerpt=self._response_excerpt(session_response),
                )
            if not session_id:
                return _RequestExecutionResult(
                    request_number=request_number,
                    success=False,
                    latency_ms=(perf_counter() - started) * 1000,
                    finished_counter=perf_counter(),
                    status_code=session_response.status_code,
                    error_type="session_id_missing",
                    error_message="session create response did not include session_id",
                    response_excerpt=self._response_excerpt(session_response),
                )

            message_response = await client.post(
                self._settings.perf_test_message_path_template.format(session_id=session_id),
                json=case.message_request,
                timeout=request_timeout,
            )
            latency_ms = (perf_counter() - started) * 1000
            if message_response.status_code != case.expectations.message_status_code:
                return _RequestExecutionResult(
                    request_number=request_number,
                    success=False,
                    latency_ms=latency_ms,
                    finished_counter=perf_counter(),
                    status_code=message_response.status_code,
                    error_type="message_status_mismatch",
                    error_message=(
                        f"expected message status {case.expectations.message_status_code}, "
                        f"got {message_response.status_code}"
                    ),
                    response_excerpt=self._response_excerpt(message_response),
                )

            validation_error = self._validate_message_response(case=case, response=message_response)
            if validation_error:
                return _RequestExecutionResult(
                    request_number=request_number,
                    success=False,
                    latency_ms=latency_ms,
                    finished_counter=perf_counter(),
                    status_code=message_response.status_code,
                    error_type="response_validation_error",
                    error_message=validation_error,
                    response_excerpt=self._response_excerpt(message_response),
                )

            return _RequestExecutionResult(
                request_number=request_number,
                success=True,
                latency_ms=latency_ms,
                finished_counter=perf_counter(),
                status_code=message_response.status_code,
            )
        except httpx.TimeoutException as exc:
            return _RequestExecutionResult(
                request_number=request_number,
                success=False,
                latency_ms=(perf_counter() - started) * 1000,
                finished_counter=perf_counter(),
                error_type="timeout",
                error_message=str(exc) or "target request timed out",
                timeout=True,
            )
        except httpx.HTTPError as exc:
            return _RequestExecutionResult(
                request_number=request_number,
                success=False,
                latency_ms=(perf_counter() - started) * 1000,
                finished_counter=perf_counter(),
                error_type="transport_error",
                error_message=str(exc) or "target request failed",
            )
        except Exception as exc:
            return _RequestExecutionResult(
                request_number=request_number,
                success=False,
                latency_ms=(perf_counter() - started) * 1000,
                finished_counter=perf_counter(),
                error_type="unexpected_error",
                error_message=str(exc) or "unexpected execution error",
            )

    def _mark_run_validating(self, run: PerfTestRunDetail) -> None:
        run.status = PerfTestRunStatus.VALIDATING
        run.updated_at = self._utcnow()

    def _mark_run_running(self, run: PerfTestRunDetail) -> None:
        now = self._utcnow()
        run.status = PerfTestRunStatus.RUNNING
        run.started_at = run.started_at or now
        run.updated_at = now
        run.progress.last_heartbeat_at = now
        run.progress.elapsed_sec = self._calculate_elapsed_from_created(run, now)

    def _mark_step_started(
        self,
        run: PerfTestRunDetail,
        step_index: int,
        started_at: datetime,
    ) -> None:
        stage = run.step_results[step_index]
        stage.status = PerfTestStepStatus.RUNNING
        stage.started_at = started_at
        stage.finished_at = None
        run.status = PerfTestRunStatus.RUNNING
        run.started_at = run.started_at or started_at
        run.updated_at = self._utcnow()
        run.progress.current_stage_index = step_index
        run.progress.current_stage_name = stage.name
        run.progress.last_heartbeat_at = run.updated_at
        run.progress.total_stages = len(run.step_results)

    def _record_request_result(
        self,
        *,
        run: PerfTestRunDetail,
        step_index: int,
        result: _RequestExecutionResult,
        failure: PerfFailureSample | None,
        measured: bool,
        max_failed_samples: int,
        measured_latencies_ms: list[float],
    ) -> None:
        if not measured:
            run.updated_at = self._utcnow()
            run.progress.last_heartbeat_at = run.updated_at
            return

        stage = run.step_results[step_index]
        stage.metrics.total_requests += 1
        stage.latency_samples_ms.append(result.latency_ms)
        measured_latencies_ms.append(result.latency_ms)
        if result.success:
            stage.metrics.success_count += 1
        else:
            stage.metrics.failure_count += 1
            if result.timeout:
                stage.metrics.timeout_count += 1
            error_key = result.error_type or "request_failed"
            stage.metrics.error_type_breakdown[error_key] = (
                stage.metrics.error_type_breakdown.get(error_key, 0) + 1
            )
            if failure is not None and len(run.error_samples) < max_failed_samples:
                run.error_samples.append(failure)
                stage.failure_samples.append(failure)

        if result.status_code is not None:
            status_key = str(result.status_code)
            stage.metrics.status_code_breakdown[status_key] = (
                stage.metrics.status_code_breakdown.get(status_key, 0) + 1
            )

        run.aggregate_metrics = self._aggregate_metrics(run.step_results)
        run.updated_at = self._utcnow()
        run.progress.last_heartbeat_at = run.updated_at
        run.progress.elapsed_sec = self._calculate_elapsed_from_created(run, run.updated_at)

    def _mark_step_finished(
        self,
        *,
        run: PerfTestRunDetail,
        step_index: int,
        finished_at: datetime,
        measured_latencies_ms: list[float],
        wall_time_ms: float,
    ) -> None:
        stage = run.step_results[step_index]
        stage.finished_at = finished_at
        stage.status = (
            PerfTestStepStatus.FAILED
            if stage.metrics.failure_count > 0
            else PerfTestStepStatus.COMPLETED
        )
        stage.metrics = self._finalize_metrics(
            current=stage.metrics,
            latencies_ms=measured_latencies_ms,
            wall_time_ms=wall_time_ms,
        )
        run.aggregate_metrics = self._aggregate_metrics(run.step_results)
        run.progress.completed_stages = len(
            [item for item in run.step_results if item.status == PerfTestStepStatus.COMPLETED]
        )
        run.updated_at = self._utcnow()
        run.progress.last_heartbeat_at = run.updated_at
        run.progress.elapsed_sec = self._calculate_elapsed_from_created(run, run.updated_at)

    def _finalize_run_success(self, run: PerfTestRunDetail) -> None:
        run.finished_at = self._utcnow()
        run.updated_at = run.finished_at
        run.progress.current_stage_index = None
        run.progress.current_stage_name = None
        run.progress.completed_stages = len(
            [item for item in run.step_results if item.status == PerfTestStepStatus.COMPLETED]
        )
        run.progress.total_stages = len(run.step_results)
        run.progress.last_heartbeat_at = run.finished_at
        run.progress.elapsed_sec = self._calculate_elapsed_from_created(run, run.finished_at)
        if any(item.status == PerfTestStepStatus.FAILED for item in run.step_results):
            run.status = PerfTestRunStatus.FAILED
        else:
            run.status = PerfTestRunStatus.COMPLETED
        run.aggregate_metrics = self._aggregate_metrics(run.step_results)

    def _finalize_run_failure(self, run: PerfTestRunDetail, message: str) -> None:
        current_stage_name = run.progress.current_stage_name
        current_stage_index = run.progress.current_stage_index
        run.finished_at = self._utcnow()
        run.updated_at = run.finished_at
        run.status = PerfTestRunStatus.FAILED
        run.progress.completed_stages = len(
            [item for item in run.step_results if item.status == PerfTestStepStatus.COMPLETED]
        )
        run.progress.total_stages = len(run.step_results)
        run.progress.last_heartbeat_at = run.finished_at
        run.progress.elapsed_sec = self._calculate_elapsed_from_created(run, run.finished_at)
        if len(run.error_samples) < 200:
            run.error_samples.append(
                PerfFailureSample(
                    sample_id=f"{run.run_id}-fatal",
                    stage_name=current_stage_name,
                    step_index=current_stage_index,
                    error_type="run_execution_error",
                    message=message,
                    occurred_at=run.finished_at,
                )
            )
        run.aggregate_metrics = self._aggregate_metrics(run.step_results)

    def _finalize_run_cancelled(self, run: PerfTestRunDetail, reason: str) -> None:
        now = self._utcnow()
        current_stage_index = run.progress.current_stage_index
        if current_stage_index is not None and 0 <= current_stage_index < len(run.step_results):
            current_stage = run.step_results[current_stage_index]
            if current_stage.status == PerfTestStepStatus.RUNNING:
                current_stage.status = PerfTestStepStatus.CANCELLED
                current_stage.finished_at = now
                if current_stage.started_at is not None:
                    current_stage.metrics = self._finalize_metrics(
                        current=current_stage.metrics,
                        latencies_ms=list(current_stage.latency_samples_ms),
                        wall_time_ms=max((now - current_stage.started_at).total_seconds() * 1000, 0.0),
                    )

        for stage in run.step_results:
            if stage.status == PerfTestStepStatus.PENDING:
                stage.status = PerfTestStepStatus.CANCELLED
                stage.finished_at = now

        run.finished_at = now
        run.updated_at = now
        run.status = PerfTestRunStatus.CANCELLED
        run.progress.current_stage_index = None
        run.progress.current_stage_name = None
        run.progress.completed_stages = len(
            [item for item in run.step_results if item.status == PerfTestStepStatus.COMPLETED]
        )
        run.progress.total_stages = len(run.step_results)
        run.progress.last_heartbeat_at = now
        run.progress.elapsed_sec = self._calculate_elapsed_from_created(run, now)
        if len(run.error_samples) < 200:
            run.error_samples.append(
                PerfFailureSample(
                    sample_id=f"{run.run_id}-cancelled",
                    stage_name=None,
                    step_index=current_stage_index,
                    error_type="run_cancelled",
                    message=reason,
                    occurred_at=now,
                )
            )
        run.aggregate_metrics = self._aggregate_metrics(run.step_results)

    def _finalize_metrics(
        self,
        *,
        current: PerfMetrics,
        latencies_ms: list[float],
        wall_time_ms: float,
    ) -> PerfMetrics:
        total_requests = current.total_requests
        success_count = current.success_count
        failure_count = current.failure_count
        avg_ms = (sum(latencies_ms) / len(latencies_ms)) if latencies_ms else None
        return PerfMetrics(
            total_requests=total_requests,
            success_count=success_count,
            failure_count=failure_count,
            success_rate=(success_count / total_requests) if total_requests else 0.0,
            rps=(total_requests / (wall_time_ms / 1000)) if wall_time_ms > 0 else 0.0,
            avg_ms=avg_ms,
            p50_ms=self._percentile(latencies_ms, 50),
            p95_ms=self._percentile(latencies_ms, 95),
            p99_ms=self._percentile(latencies_ms, 99),
            max_ms=max(latencies_ms) if latencies_ms else None,
            timeout_count=current.timeout_count,
            status_code_breakdown=dict(current.status_code_breakdown),
            error_type_breakdown=dict(current.error_type_breakdown),
        )

    def _aggregate_metrics(self, stages: list[PerfStageResult]) -> PerfMetrics:
        total_requests = sum(stage.metrics.total_requests for stage in stages)
        success_count = sum(stage.metrics.success_count for stage in stages)
        failure_count = sum(stage.metrics.failure_count for stage in stages)
        timeout_count = sum(stage.metrics.timeout_count for stage in stages)
        wall_time_ms = sum(
            (stage.finished_at - stage.started_at).total_seconds() * 1000
            for stage in stages
            if stage.started_at is not None and stage.finished_at is not None
        )
        latency_samples_ms: list[float] = []
        status_code_breakdown: dict[str, int] = {}
        error_type_breakdown: dict[str, int] = {}
        max_candidates: list[float] = []

        for stage in stages:
            latency_samples_ms.extend(stage.latency_samples_ms)
            if stage.metrics.max_ms is not None:
                max_candidates.append(stage.metrics.max_ms)
            for code, count in stage.metrics.status_code_breakdown.items():
                status_code_breakdown[code] = status_code_breakdown.get(code, 0) + count
            for error_type, count in stage.metrics.error_type_breakdown.items():
                error_type_breakdown[error_type] = error_type_breakdown.get(error_type, 0) + count

        avg_ms = (sum(latency_samples_ms) / len(latency_samples_ms)) if latency_samples_ms else None
        return PerfMetrics(
            total_requests=total_requests,
            success_count=success_count,
            failure_count=failure_count,
            success_rate=(success_count / total_requests) if total_requests else 0.0,
            rps=(total_requests / (wall_time_ms / 1000)) if wall_time_ms > 0 else 0.0,
            avg_ms=avg_ms,
            p50_ms=self._percentile(latency_samples_ms, 50),
            p95_ms=self._percentile(latency_samples_ms, 95),
            p99_ms=self._percentile(latency_samples_ms, 99),
            max_ms=max(max_candidates) if max_candidates else None,
            timeout_count=timeout_count,
            status_code_breakdown=status_code_breakdown,
            error_type_breakdown=error_type_breakdown,
        )

    def _validate_message_response(self, *, case: PerfTestCaseDefinition, response: httpx.Response) -> str | None:
        payload = self._json_or_none(response)
        if not isinstance(payload, dict):
            return "message response is not a JSON object"

        snapshot = payload.get("snapshot")
        analysis = payload.get("analysis")
        if case.expectations.required_graph_status:
            if not isinstance(snapshot, dict):
                return "message response does not include snapshot"
            current_graph = snapshot.get("current_graph")
            if not isinstance(current_graph, dict):
                return "message response does not include snapshot.current_graph"
            actual_status = current_graph.get("status")
            if actual_status != case.expectations.required_graph_status:
                return (
                    f"expected graph status {case.expectations.required_graph_status}, "
                    f"got {actual_status!r}"
                )

        if case.expectations.required_primary_intent_code:
            primary_intent_code = self._extract_primary_intent_code(analysis)
            if primary_intent_code != case.expectations.required_primary_intent_code:
                return (
                    f"expected primary intent {case.expectations.required_primary_intent_code}, "
                    f"got {primary_intent_code!r}"
                )

        if case.expectations.required_slot_keys or case.expectations.required_slot_values:
            slot_memory = self._extract_analysis_slot_memory(
                analysis=analysis,
                intent_code=case.expectations.required_primary_intent_code,
            )
            if slot_memory is None:
                return "message response does not include analysis slot_memory"
            missing_keys = [
                slot_key
                for slot_key in case.expectations.required_slot_keys
                if slot_key not in slot_memory
            ]
            if missing_keys:
                return f"missing expected slot keys: {', '.join(missing_keys)}"
            for slot_key, expected_value in case.expectations.required_slot_values.items():
                actual_value = slot_memory.get(slot_key)
                if actual_value != expected_value:
                    return f"expected slot {slot_key}={expected_value!r}, got {actual_value!r}"

        required_fragments = case.expectations.required_message_contains
        if required_fragments:
            candidate_text = (
                self._extract_snapshot_message(snapshot)
                or self._extract_analysis_text(analysis)
                or self._response_excerpt(response)
            )
            missing = [fragment for fragment in required_fragments if fragment not in candidate_text]
            if missing:
                return f"missing expected response fragments: {', '.join(missing)}"
        return None

    def _extract_snapshot_message(self, snapshot: Any) -> str:
        if not isinstance(snapshot, dict):
            return ""
        messages = snapshot.get("messages")
        if not isinstance(messages, list) or not messages:
            return ""
        last_message = messages[-1]
        if not isinstance(last_message, dict):
            return ""
        content = last_message.get("content")
        return content if isinstance(content, str) else ""

    def _extract_analysis_text(self, analysis: Any) -> str:
        if not isinstance(analysis, dict):
            return ""
        content = analysis.get("content")
        return content if isinstance(content, str) else ""

    def _extract_primary_intent_code(self, analysis: Any) -> str | None:
        if not isinstance(analysis, dict):
            return None
        recognition = analysis.get("recognition")
        if not isinstance(recognition, dict):
            return None
        primary = recognition.get("primary")
        if not isinstance(primary, list) or not primary:
            return None
        first_match = primary[0]
        if not isinstance(first_match, dict):
            return None
        intent_code = first_match.get("intent_code")
        return intent_code if isinstance(intent_code, str) else None

    def _extract_analysis_slot_memory(
        self,
        *,
        analysis: Any,
        intent_code: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(analysis, dict):
            return None
        slot_nodes = analysis.get("slot_nodes")
        if not isinstance(slot_nodes, list):
            return None
        selected_node: dict[str, Any] | None = None
        for node in slot_nodes:
            if not isinstance(node, dict):
                continue
            node_intent_code = node.get("intent_code")
            if intent_code and node_intent_code == intent_code:
                selected_node = node
                break
            if selected_node is None:
                selected_node = node
        if not isinstance(selected_node, dict):
            return None
        slot_memory = selected_node.get("slot_memory")
        return slot_memory if isinstance(slot_memory, dict) else None

    def _response_excerpt(self, response: httpx.Response) -> str:
        return response.text.strip()[:2000]

    def _json_or_none(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    def _build_run_id(self) -> str:
        return f"perf_{uuid4().hex[:12]}"

    def _utcnow(self) -> datetime:
        return datetime.now(UTC)

    def _calculate_elapsed_from_created(self, run: PerfTestRunDetail, now: datetime) -> float:
        return max((now - run.created_at).total_seconds(), 0.0)

    def _resolve_case(
        self,
        case_id: str,
        case_override: PerfTestCaseRuntimeOverride | None,
    ) -> PerfTestCaseDefinition:
        base_case = self._case_catalog.get_case(case_id)
        if case_override is None:
            return base_case

        merged_payload = base_case.model_dump(mode="python")
        if case_override.session_request is not None:
            merged_payload["session_request"] = dict(case_override.session_request)
        if case_override.message_request is not None:
            merged_payload["message_request"] = dict(case_override.message_request)
        if case_override.expectations is not None:
            merged_expectations = base_case.expectations.model_dump(mode="python")
            for key, value in case_override.expectations.model_dump(exclude_none=True).items():
                merged_expectations[key] = value
            merged_payload["expectations"] = merged_expectations
        return PerfTestCaseDefinition.model_validate(merged_payload)

    def _percentile(self, values: list[float], percentile: int) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * (percentile / 100)
        lower_index = int(position)
        upper_index = min(lower_index + 1, len(ordered) - 1)
        lower_value = ordered[lower_index]
        upper_value = ordered[upper_index]
        weight = position - lower_index
        return lower_value + (upper_value - lower_value) * weight
