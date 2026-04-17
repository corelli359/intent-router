from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from admin_service.api.dependencies import get_perf_test_service
from admin_service.perf.models import (
    PerfTestCaseListResponse,
    PerfTestCaseNotFoundError,
    PerfTestCreateRunRequest,
    PerfTestRunDetail,
    PerfTestRunListResponse,
    PerfTestRunNotFoundError,
)
from admin_service.perf.service import PerfTestService

router = APIRouter(prefix="/admin/perf-tests", tags=["admin-perf-tests"])


@router.get("/cases", response_model=PerfTestCaseListResponse)
def list_perf_test_cases(
    service: PerfTestService = Depends(get_perf_test_service),
) -> PerfTestCaseListResponse:
    items = service.list_cases()
    return PerfTestCaseListResponse(items=items, total=len(items))


@router.get("/runs", response_model=PerfTestRunListResponse)
def list_perf_test_runs(
    service: PerfTestService = Depends(get_perf_test_service),
) -> PerfTestRunListResponse:
    items = service.list_runs()
    return PerfTestRunListResponse(items=items, total=len(items))


@router.post("/runs", response_model=PerfTestRunDetail, status_code=status.HTTP_201_CREATED)
async def create_perf_test_run(
    request: PerfTestCreateRunRequest,
    service: PerfTestService = Depends(get_perf_test_service),
) -> PerfTestRunDetail:
    try:
        return await service.create_run(request)
    except PerfTestCaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/runs/{run_id}", response_model=PerfTestRunDetail)
def get_perf_test_run(
    run_id: str,
    service: PerfTestService = Depends(get_perf_test_service),
) -> PerfTestRunDetail:
    try:
        return service.get_run(run_id)
    except PerfTestRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
