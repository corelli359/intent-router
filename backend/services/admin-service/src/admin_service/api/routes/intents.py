from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import ValidationError

from admin_service.api.dependencies import get_field_repository, get_intent_repository
from admin_service.api.schemas import IntentCreateRequest, IntentListResponse, IntentResponse, IntentUpdateRequest
from admin_service.models.intent import IntentFieldDefinition, IntentPayload, IntentSlotDefinition, IntentStatus
from admin_service.storage.field_repository import IntentFieldNotFoundError, IntentFieldRepository
from admin_service.storage.intent_repository import (
    IntentAlreadyExistsError,
    IntentNotFoundError,
    IntentRepository,
)

router = APIRouter(prefix="/admin/intents", tags=["admin-intents"])


def _resolve_field_catalog(
    *,
    inline_field_catalog: list[IntentFieldDefinition],
    slot_schema: list[IntentSlotDefinition],
    field_repository: IntentFieldRepository,
) -> list[IntentFieldDefinition]:
    inline_by_code = {field.field_code: field for field in inline_field_catalog}
    resolved: dict[str, IntentFieldDefinition] = {}

    for slot in slot_schema:
        field_code = slot.field_code.strip()
        if not field_code or field_code in resolved:
            continue
        try:
            resolved[field_code] = field_repository.get_field(field_code)
        except IntentFieldNotFoundError:
            inline_field = inline_by_code.get(field_code)
            if inline_field is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"slot field_code not found in admin field catalog: {field_code}",
                ) from None
            resolved[field_code] = inline_field

    for field_code, field in inline_by_code.items():
        resolved.setdefault(field_code, field)

    return list(resolved.values())


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
    field_repository: IntentFieldRepository = Depends(get_field_repository),
) -> IntentResponse:
    try:
        payload_data = request.model_dump()
        payload_data["field_catalog"] = [
            field.model_dump(mode="json")
            for field in _resolve_field_catalog(
                inline_field_catalog=request.field_catalog,
                slot_schema=request.slot_schema,
                field_repository=field_repository,
            )
        ]
        payload = IntentPayload(**payload_data)
        return IntentResponse.from_record(repository.create_intent(payload))
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except IntentAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put("/{intent_code}", response_model=IntentResponse)
def update_intent(
    intent_code: str,
    request: IntentUpdateRequest,
    repository: IntentRepository = Depends(get_intent_repository),
    field_repository: IntentFieldRepository = Depends(get_field_repository),
) -> IntentResponse:
    try:
        payload_data = request.model_dump()
        payload_data["field_catalog"] = [
            field.model_dump(mode="json")
            for field in _resolve_field_catalog(
                inline_field_catalog=request.field_catalog,
                slot_schema=request.slot_schema,
                field_repository=field_repository,
            )
        ]
        payload = IntentPayload(**payload_data)
        return IntentResponse.from_record(repository.update_intent(intent_code, payload))
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
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
