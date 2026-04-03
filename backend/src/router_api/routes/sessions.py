from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, model_validator
from starlette.responses import StreamingResponse

from router_api.dependencies import get_event_broker, get_orchestrator
from router_api.sse.broker import EventBroker
from router_core.orchestrator import RouterOrchestrator


router = APIRouter(prefix="/api/router", tags=["router"])


class CreateSessionResponse(BaseModel):
    session_id: str
    cust_id: str


class CreateSessionRequest(BaseModel):
    cust_id: str | None = None
    session_id: str | None = None


class MessageRequest(BaseModel):
    content: str | None = None
    message: str | None = None
    cust_id: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "MessageRequest":
        if self.content is None and self.message is None:
            raise ValueError("content or message is required")
        self.content = self.content or self.message
        self.cust_id = self.cust_id or "cust_demo"
        return self


@router.post("/sessions", response_model=CreateSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: CreateSessionRequest | None = None,
    orchestrator: RouterOrchestrator = Depends(get_orchestrator),
) -> CreateSessionResponse:
    cust_id = request.cust_id if request and request.cust_id else "cust_demo"
    session = orchestrator.create_session(cust_id=cust_id, session_id=request.session_id if request else None)
    return CreateSessionResponse(session_id=session.session_id, cust_id=session.cust_id)


@router.get("/sessions/{session_id}")
async def get_session_snapshot(
    session_id: str,
    orchestrator: RouterOrchestrator = Depends(get_orchestrator),
):
    try:
        return orchestrator.snapshot(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


@router.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: str,
    request: MessageRequest,
    orchestrator: RouterOrchestrator = Depends(get_orchestrator),
):
    snapshot = await orchestrator.handle_user_message(
        session_id=session_id,
        cust_id=request.cust_id or "cust_demo",
        content=request.content or "",
    )
    return {"ok": True, "snapshot": snapshot.model_dump(mode="json")}


@router.get("/sessions/{session_id}/events")
async def stream_events(
    session_id: str,
    broker: EventBroker = Depends(get_event_broker),
) -> StreamingResponse:
    async def event_generator():
        async for event in broker.subscribe(session_id):
            payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
            yield f"event: {event.event}\ndata: {payload}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
