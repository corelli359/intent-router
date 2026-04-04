from __future__ import annotations

import asyncio
import json
from contextlib import suppress

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


def _encode_sse(event_name: str, payload: dict[str, object]) -> str:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {body}\n\n"


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


@router.post("/sessions/{session_id}/messages/stream")
async def post_message_stream(
    session_id: str,
    request: MessageRequest,
    orchestrator: RouterOrchestrator = Depends(get_orchestrator),
    broker: EventBroker = Depends(get_event_broker),
) -> StreamingResponse:
    async def event_generator():
        queue = broker.register(session_id)
        processing_task = asyncio.create_task(
            orchestrator.handle_user_message(
                session_id=session_id,
                cust_id=request.cust_id or "cust_demo",
                content=request.content or "",
            )
        )
        try:
            while True:
                if processing_task.done() and queue.empty():
                    await processing_task
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                yield _encode_sse(event.event, event.model_dump(mode="json"))
        finally:
            broker.unregister(session_id, queue)
            if not processing_task.done():
                processing_task.cancel()
                with suppress(asyncio.CancelledError):
                    await processing_task
            await orchestrator.cancel_waiting_tasks(session_id, reason="SSE stream disconnected")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}/events")
async def stream_events(
    session_id: str,
    broker: EventBroker = Depends(get_event_broker),
    orchestrator: RouterOrchestrator = Depends(get_orchestrator),
) -> StreamingResponse:
    async def event_generator():
        try:
            async for event in broker.subscribe(session_id):
                yield _encode_sse(event.event, event.model_dump(mode="json"))
        finally:
            await orchestrator.cancel_waiting_tasks(session_id, reason="SSE stream disconnected")

    return StreamingResponse(event_generator(), media_type="text/event-stream")
