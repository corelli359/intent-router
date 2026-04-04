from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from admin_api.dependencies import get_intent_repository
from admin_api.schemas import IntentCreateRequest, IntentListResponse, IntentResponse, IntentUpdateRequest
from models.intent import IntentPayload, IntentStatus
from persistence.intent_repository import (
    IntentAlreadyExistsError,
    IntentNotFoundError,
    IntentRepository,
)

router = APIRouter(prefix="/admin/intents", tags=["admin-intents"])


@router.get("", response_model=IntentListResponse)
def list_intents(
    status_filter: IntentStatus | None = None,
    repository: IntentRepository = Depends(get_intent_repository),
) -> IntentListResponse:
    records = repository.list_intents(status_filter)
    items = [IntentResponse.from_record(record) for record in records]
    return IntentListResponse(items=items, total=len(items))


@router.get("/{intent_code}", response_model=IntentResponse)
def get_intent(
    intent_code: str,
    repository: IntentRepository = Depends(get_intent_repository),
) -> IntentResponse:
    try:
        return IntentResponse.from_record(repository.get_intent(intent_code))
    except IntentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("", response_model=IntentResponse, status_code=status.HTTP_201_CREATED)
def create_intent(
    request: IntentCreateRequest,
    repository: IntentRepository = Depends(get_intent_repository),
) -> IntentResponse:
    try:
        payload = IntentPayload(**request.model_dump())
        return IntentResponse.from_record(repository.create_intent(payload))
    except IntentAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put("/{intent_code}", response_model=IntentResponse)
def update_intent(
    intent_code: str,
    request: IntentUpdateRequest,
    repository: IntentRepository = Depends(get_intent_repository),
) -> IntentResponse:
    try:
        payload = IntentPayload(**request.model_dump())
        return IntentResponse.from_record(repository.update_intent(intent_code, payload))
    except IntentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IntentAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete(
    "/{intent_code}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_intent(
    intent_code: str,
    repository: IntentRepository = Depends(get_intent_repository),
) -> Response:
    try:
        repository.delete_intent(intent_code)
    except IntentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _set_intent_status(
    repository: IntentRepository,
    intent_code: str,
    next_status: IntentStatus,
) -> IntentResponse:
    current = repository.get_intent(intent_code)
    payload_data = current.model_dump(
        exclude={
            "created_at",
            "updated_at",
        }
    )
    payload_data["status"] = next_status
    payload = IntentPayload(
        **payload_data,
    )
    return IntentResponse.from_record(repository.update_intent(intent_code, payload))


@router.post("/{intent_code}/activate", response_model=IntentResponse)
def activate_intent(
    intent_code: str,
    repository: IntentRepository = Depends(get_intent_repository),
) -> IntentResponse:
    try:
        return _set_intent_status(repository, intent_code, IntentStatus.ACTIVE)
    except IntentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IntentAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{intent_code}/deactivate", response_model=IntentResponse)
def deactivate_intent(
    intent_code: str,
    repository: IntentRepository = Depends(get_intent_repository),
) -> IntentResponse:
    try:
        return _set_intent_status(repository, intent_code, IntentStatus.INACTIVE)
    except IntentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IntentAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
