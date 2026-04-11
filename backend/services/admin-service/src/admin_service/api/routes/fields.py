from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from admin_service.api.dependencies import get_field_repository, get_intent_repository
from admin_service.api.schemas import FieldCreateRequest, FieldListResponse, FieldResponse, FieldUpdateRequest
from admin_service.storage.field_repository import (
    IntentFieldAlreadyExistsError,
    IntentFieldNotFoundError,
    IntentFieldRepository,
)
from admin_service.storage.intent_repository import IntentRepository

router = APIRouter(prefix="/admin/fields", tags=["admin-fields"])


@router.get("", response_model=FieldListResponse)
def list_fields(repository: IntentFieldRepository = Depends(get_field_repository)) -> FieldListResponse:
    items = [FieldResponse.from_record(record) for record in repository.list_fields()]
    return FieldListResponse(items=items, total=len(items))


@router.get("/{field_code}", response_model=FieldResponse)
def get_field(
    field_code: str,
    repository: IntentFieldRepository = Depends(get_field_repository),
) -> FieldResponse:
    try:
        return FieldResponse.from_record(repository.get_field(field_code))
    except IntentFieldNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("", response_model=FieldResponse, status_code=status.HTTP_201_CREATED)
def create_field(
    request: FieldCreateRequest,
    repository: IntentFieldRepository = Depends(get_field_repository),
) -> FieldResponse:
    try:
        return FieldResponse.from_record(repository.create_field(request))
    except IntentFieldAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put("/{field_code}", response_model=FieldResponse)
def update_field(
    field_code: str,
    request: FieldUpdateRequest,
    repository: IntentFieldRepository = Depends(get_field_repository),
) -> FieldResponse:
    try:
        return FieldResponse.from_record(repository.update_field(field_code, request))
    except IntentFieldNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IntentFieldAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete("/{field_code}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_field(
    field_code: str,
    repository: IntentFieldRepository = Depends(get_field_repository),
    intent_repository: IntentRepository = Depends(get_intent_repository),
) -> Response:
    intents = intent_repository.list_intents()
    if any(
        slot.field_code == field_code
        for intent in intents
        for slot in intent.slot_schema
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Field is still referenced by registered intents: {field_code}",
        )
    try:
        repository.delete_field(field_code)
    except IntentFieldNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
