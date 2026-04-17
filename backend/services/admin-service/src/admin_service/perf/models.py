from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class PerfTestRunStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PerfTestStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PerfTestStepPlan(BaseModel):
    step_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    concurrency: int = Field(ge=1, le=10_000)
    duration_sec: float = Field(gt=0, le=86_400)
    warmup_sec: float = Field(default=0.0, ge=0, le=86_400)
    request_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    cooldown_sec: float = Field(default=0.0, ge=0, le=86_400)
    timeout_ms: int | None = Field(default=None, ge=1, le=300_000)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "duration_sec" not in normalized and "durationSec" in normalized:
            normalized["duration_sec"] = normalized["durationSec"]
        if "warmup_sec" not in normalized and "warmupSec" in normalized:
            normalized["warmup_sec"] = normalized["warmupSec"]
        if "request_limit" not in normalized and "requestLimit" in normalized:
            normalized["request_limit"] = normalized["requestLimit"]
        if "cooldown_sec" not in normalized and "cooldownSec" in normalized:
            normalized["cooldown_sec"] = normalized["cooldownSec"]
        if "timeout_ms" not in normalized and "timeoutMs" in normalized:
            normalized["timeout_ms"] = normalized["timeoutMs"]
        if "request_limit" not in normalized and "total_requests" in normalized:
            normalized["request_limit"] = normalized["total_requests"]
        if "request_limit" not in normalized and "totalRequests" in normalized:
            normalized["request_limit"] = normalized["totalRequests"]
        return normalized

    @model_validator(mode="after")
    def populate_defaults(self) -> "PerfTestStepPlan":
        if self.warmup_sec > self.duration_sec:
            raise ValueError("warmup_sec must not exceed duration_sec")
        if not self.step_id:
            self.step_id = f"c{self.concurrency}-d{int(self.duration_sec)}"
        if not self.name:
            self.name = f"{self.concurrency} concurrency / {self.duration_sec:g}s"
        return self


class PerfTestExpectation(BaseModel):
    session_status_code: int = Field(default=201, ge=100, le=599)
    message_status_code: int = Field(default=200, ge=100, le=599)
    required_graph_status: str | None = Field(default=None, min_length=1, max_length=128)
    required_message_contains: list[str] = Field(default_factory=list)
    required_primary_intent_code: str | None = Field(default=None, min_length=1, max_length=128)
    required_slot_keys: list[str] = Field(default_factory=list)
    required_slot_values: dict[str, Any] = Field(default_factory=dict)


class PerfTestCaseDefinition(BaseModel):
    case_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=4000)
    category: str | None = Field(default=None, max_length=128)
    tags: list[str] = Field(default_factory=list)
    target_route: str | None = Field(default=None, max_length=512)
    notes: list[str] = Field(default_factory=list)
    session_request: dict[str, Any] = Field(default_factory=dict)
    message_request: dict[str, Any] = Field(default_factory=dict)
    default_steps: list[PerfTestStepPlan] = Field(default_factory=list)
    expectations: PerfTestExpectation = Field(default_factory=PerfTestExpectation)

    @model_validator(mode="before")
    @classmethod
    def normalize_default_steps(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "default_steps" not in normalized and "default_plan" in normalized:
            normalized["default_steps"] = normalized["default_plan"]
        return normalized


class PerfMetrics(BaseModel):
    total_requests: int = 0
    success_count: int = 0
    failure_count: int = 0
    success_rate: float = 0.0
    rps: float = 0.0
    avg_ms: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    max_ms: float | None = None
    timeout_count: int = 0
    status_code_breakdown: dict[str, int] = Field(default_factory=dict)
    error_type_breakdown: dict[str, int] = Field(default_factory=dict)


class PerfFailureSample(BaseModel):
    sample_id: str
    stage_name: str | None = Field(default=None, max_length=128)
    step_index: int | None = Field(default=None, ge=0)
    status_code: int | None = Field(default=None, ge=100, le=599)
    error_type: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    latency_ms: float | None = Field(default=None, ge=0)
    request_summary: str | None = Field(default=None, max_length=4000)
    occurred_at: datetime


class PerfRunProgress(BaseModel):
    completed_stages: int = Field(default=0, ge=0)
    total_stages: int = Field(default=0, ge=0)
    current_stage_index: int | None = Field(default=None, ge=0)
    current_stage_name: str | None = Field(default=None, max_length=128)
    elapsed_sec: float | None = Field(default=None, ge=0)
    last_heartbeat_at: datetime | None = None


class PerfStageResult(BaseModel):
    stage_id: str
    step_index: int = Field(ge=0)
    name: str
    status: PerfTestStepStatus = PerfTestStepStatus.PENDING
    concurrency: int = Field(ge=1)
    duration_sec: float = Field(gt=0)
    warmup_sec: float = Field(default=0.0, ge=0)
    request_limit: int | None = Field(default=None, ge=1)
    cooldown_sec: float = Field(default=0.0, ge=0)
    timeout_ms: int | None = Field(default=None, ge=1)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metrics: PerfMetrics = Field(default_factory=PerfMetrics)
    failure_samples: list[PerfFailureSample] = Field(default_factory=list)
    latency_samples_ms: list[float] = Field(default_factory=list, exclude=True, repr=False)


class PerfTestRunDetail(BaseModel):
    run_id: str
    case_id: str
    case_name: str
    status: PerfTestRunStatus
    target_base_url: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: PerfRunProgress = Field(default_factory=PerfRunProgress)
    aggregate_metrics: PerfMetrics = Field(default_factory=PerfMetrics)
    ladder_steps: list[PerfTestStepPlan] = Field(default_factory=list)
    step_results: list[PerfStageResult] = Field(default_factory=list)
    error_samples: list[PerfFailureSample] = Field(default_factory=list)


class PerfTestRunSummary(BaseModel):
    run_id: str
    case_id: str
    case_name: str
    status: PerfTestRunStatus
    started_at: datetime | None = None
    updated_at: datetime
    finished_at: datetime | None = None
    current_stage_index: int | None = Field(default=None, ge=0)
    total_stages: int = Field(default=0, ge=0)
    aggregate_metrics: PerfMetrics = Field(default_factory=PerfMetrics)

    @classmethod
    def from_run(cls, run: PerfTestRunDetail) -> "PerfTestRunSummary":
        return cls(
            run_id=run.run_id,
            case_id=run.case_id,
            case_name=run.case_name,
            status=run.status,
            started_at=run.started_at,
            updated_at=run.updated_at,
            finished_at=run.finished_at,
            current_stage_index=run.progress.current_stage_index,
            total_stages=run.progress.total_stages,
            aggregate_metrics=run.aggregate_metrics.model_copy(deep=True),
        )


class PerfTestCreateRunRequest(BaseModel):
    case_id: str = Field(min_length=1, max_length=128)
    ladder_steps: list[PerfTestStepPlan] | None = None
    max_failed_samples: int = Field(default=20, ge=1, le=200)

    @model_validator(mode="before")
    @classmethod
    def normalize_ladder_steps(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "ladder_steps" not in normalized and "step_plan" in normalized:
            normalized["ladder_steps"] = normalized["step_plan"]
        return normalized


class PerfTestCaseListResponse(BaseModel):
    items: list[PerfTestCaseDefinition]
    total: int


class PerfTestRunListResponse(BaseModel):
    items: list[PerfTestRunSummary]
    total: int


class PerfTestCaseNotFoundError(KeyError):
    pass


class PerfTestRunNotFoundError(KeyError):
    pass
