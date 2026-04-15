from __future__ import annotations

import asyncio
from enum import StrEnum
import json
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from starlette.responses import StreamingResponse

from router_service.api.dependencies import get_event_broker, get_orchestrator
from router_service.api.sse.broker import EventBroker
from router_service.core.shared.domain import IntentMatch, TaskEvent, TaskStatus
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphEdge,
    GraphNodeState,
    GuidedSelectionPayload,
    ProactiveRecommendationPayload,
    RecommendationContextPayload,
)
from router_service.core.graph.orchestrator import GraphRouterOrchestrator, MessageAnalysisResult


router = APIRouter(tags=["router"])


class MessageExecutionMode(StrEnum):
    """Execution behavior selected for one message request."""

    EXECUTE = "execute"
    ANALYZE_ONLY = "analyze_only"


class MessageAnalysisMode(StrEnum):
    """Depth of analysis requested for analyze-only message calls."""

    FULL = "full"
    INTENT_ONLY = "intent_only"


class CreateSessionResponse(BaseModel):
    """Response returned after creating a router session."""

    session_id: str
    cust_id: str


class CreateSessionRequest(BaseModel):
    """Optional payload used when callers want to control session creation."""

    cust_id: str | None = None
    session_id: str | None = None


class MessageRequest(BaseModel):
    """Unified message payload for both plain dialog and structured router inputs."""

    content: str | None = None
    message: str | None = None
    execution_mode: MessageExecutionMode = Field(default=MessageExecutionMode.EXECUTE, alias="executionMode")
    analysis_mode: MessageAnalysisMode = Field(default=MessageAnalysisMode.FULL, alias="analysisMode")
    guided_selection: GuidedSelectionPayload | None = Field(default=None, alias="guidedSelection")
    recommendation_context: RecommendationContextPayload | None = Field(default=None, alias="recommendationContext")
    proactive_recommendation: ProactiveRecommendationPayload | None = Field(default=None, alias="proactiveRecommendation")
    cust_id: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "MessageRequest":
        """Normalize alias fields and require either message content or guided selection."""
        self.content = self.content or self.message or ""
        if not self.content and (self.guided_selection is None or not self.guided_selection.selected_intents):
            raise ValueError("content/message or guided_selection is required")
        return self


class RecognitionAnalysis(BaseModel):
    """Recognition buckets returned by analyze-only mode."""

    primary: list[IntentMatch] = Field(default_factory=list)
    candidates: list[IntentMatch] = Field(default_factory=list)


class MessageAnalysisPayload(BaseModel):
    """Structured analyze-only payload for intent and slot verification."""

    session_id: str
    cust_id: str
    content: str
    no_match: bool = False
    recognition: RecognitionAnalysis
    graph: ExecutionGraphState | None = None
    slot_nodes: list[GraphNodeState] = Field(default_factory=list)
    conditional_edges: list[GraphEdge] = Field(default_factory=list)


class ActionRequest(BaseModel):
    """Action payload accepted from direct API callers or the graph UI layer."""

    action_code: str | None = None
    actionCode: str | None = None
    source: str | None = None
    task_id: str | None = None
    taskId: str | None = None
    confirm_token: str | None = None
    confirmToken: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    cust_id: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "ActionRequest":
        """Normalize camelCase aliases into the canonical action request shape."""
        resolved_code = self.action_code or self.actionCode
        if not resolved_code:
            raise ValueError("action_code is required")
        self.action_code = resolved_code
        self.task_id = self.task_id or self.taskId
        self.confirm_token = self.confirm_token or self.confirmToken
        return self


def _resolve_action_cust_id(
    orchestrator: GraphRouterOrchestrator,
    session_id: str,
    request: ActionRequest,
) -> str:
    """Resolve the customer id for an action request from payload or existing session."""
    if request.cust_id:
        return request.cust_id
    try:
        return orchestrator.snapshot(session_id).cust_id
    except KeyError:
        return "cust_demo"


def _resolve_message_cust_id(
    orchestrator: GraphRouterOrchestrator,
    session_id: str,
    request: MessageRequest,
) -> str:
    """Resolve the customer id for a message request from payload or existing session."""
    if request.cust_id:
        return request.cust_id
    try:
        return orchestrator.snapshot(session_id).cust_id
    except KeyError:
        return "cust_demo"


def _encode_sse(event_name: str, payload: dict[str, object]) -> str:
    """Encode one router event as an SSE frame."""
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {body}\n\n"


def _build_message_analysis_payload(result: MessageAnalysisResult) -> MessageAnalysisPayload:
    """Convert orchestrator analysis output into an API response model."""
    graph = result.graph.model_copy(deep=True) if result.graph is not None else None
    return MessageAnalysisPayload(
        session_id=result.session_id,
        cust_id=result.cust_id,
        content=result.content,
        no_match=result.no_match,
        recognition=RecognitionAnalysis(
            primary=list(result.recognition.primary),
            candidates=list(result.recognition.candidates),
        ),
        graph=graph,
        slot_nodes=list(graph.nodes) if graph is not None else [],
        conditional_edges=[edge for edge in graph.edges if edge.condition is not None] if graph is not None else [],
    )


@router.post("/sessions", response_model=CreateSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: CreateSessionRequest | None = None,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
) -> CreateSessionResponse:
    """Create a router session for direct API or frontend callers."""
    cust_id = request.cust_id if request and request.cust_id else "cust_demo"
    session = orchestrator.create_session(cust_id=cust_id, session_id=request.session_id if request else None)
    return CreateSessionResponse(session_id=session.session_id, cust_id=session.cust_id)


@router.get("/sessions/{session_id}")
async def get_session_snapshot(
    session_id: str,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
):
    """Return the current router snapshot for one session."""
    try:
        return orchestrator.snapshot(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


@router.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: str,
    request: MessageRequest,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
):
    """Submit one user message turn to the router and return the updated snapshot."""
    # Message APIs are the main entry for intent dialog. They can be called by a
    # frontend chat page, a test harness, or a backend integration that wants to
    # drive the router directly without rendering any UI.
    resolved_cust_id = _resolve_message_cust_id(orchestrator, session_id, request)
    try:
        if request.execution_mode == MessageExecutionMode.ANALYZE_ONLY:
            analysis = await orchestrator.analyze_user_message(
                session_id=session_id,
                cust_id=resolved_cust_id,
                content=request.content or "",
                analysis_mode=request.analysis_mode,
                guided_selection=request.guided_selection,
                recommendation_context=request.recommendation_context,
                proactive_recommendation=request.proactive_recommendation,
            )
            return {"ok": True, "analysis": _build_message_analysis_payload(analysis).model_dump(mode="json")}
        snapshot = await orchestrator.handle_user_message(
            session_id=session_id,
            cust_id=resolved_cust_id,
            content=request.content or "",
            guided_selection=request.guided_selection,
            recommendation_context=request.recommendation_context,
            proactive_recommendation=request.proactive_recommendation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "snapshot": snapshot.model_dump(mode="json")}


@router.post("/sessions/{session_id}/messages/analyze")
async def analyze_message(
    session_id: str,
    request: MessageRequest,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
):
    """Analyze one message turn without executing downstream agents."""
    resolved_cust_id = _resolve_message_cust_id(orchestrator, session_id, request)
    try:
        analysis = await orchestrator.analyze_user_message(
            session_id=session_id,
            cust_id=resolved_cust_id,
            content=request.content or "",
            analysis_mode=request.analysis_mode,
            guided_selection=request.guided_selection,
            recommendation_context=request.recommendation_context,
            proactive_recommendation=request.proactive_recommendation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "analysis": _build_message_analysis_payload(analysis).model_dump(mode="json")}


@router.post("/sessions/{session_id}/actions")
async def post_action(
    session_id: str,
    request: ActionRequest,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
):
    """Submit one explicit graph action and return the updated snapshot."""
    # Action APIs mutate the router state machine directly. Typical callers are
    # the graph UI, an orchestration service, or tests that need to confirm/cancel
    # a pending graph or interrupt the current waiting node.
    resolved_cust_id = _resolve_action_cust_id(orchestrator, session_id, request)
    try:
        snapshot = await orchestrator.handle_action(
            session_id=session_id,
            cust_id=resolved_cust_id,
            action_code=request.action_code or "",
            source=request.source,
            task_id=request.task_id,
            confirm_token=request.confirm_token,
            payload=request.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "snapshot": snapshot.model_dump(mode="json")}


@router.post("/sessions/{session_id}/actions/stream")
async def post_action_stream(
    session_id: str,
    request: ActionRequest,
    http_request: Request,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
    broker: EventBroker = Depends(get_event_broker),
) -> StreamingResponse:
    """Execute one graph action while streaming router events over SSE."""
    resolved_cust_id = _resolve_action_cust_id(orchestrator, session_id, request)

    async def event_generator():
        """Yield SSE frames for the action execution lifecycle."""
        # The broker queue must be registered before the action task starts,
        # otherwise early graph/node events could be missed by the client.
        queue = broker.register(session_id)
        processing_task = asyncio.create_task(
            orchestrator.handle_action(
                session_id=session_id,
                cust_id=resolved_cust_id,
                action_code=request.action_code or "",
                source=request.source,
                task_id=request.task_id,
                confirm_token=request.confirm_token,
                payload=request.payload,
            )
        )
        try:
            while True:
                if await http_request.is_disconnected():
                    break
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

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions/{session_id}/messages/stream")
async def post_message_stream(
    session_id: str,
    request: MessageRequest,
    http_request: Request,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
    broker: EventBroker = Depends(get_event_broker),
) -> StreamingResponse:
    """Execute one message turn while streaming router events over SSE."""
    if request.execution_mode == MessageExecutionMode.ANALYZE_ONLY:
        raise HTTPException(status_code=400, detail="analyze_only is not supported on the stream endpoint")
    resolved_cust_id = _resolve_message_cust_id(orchestrator, session_id, request)

    async def event_generator():
        """Yield SSE frames for the message processing lifecycle."""
        # Streaming and non-streaming message APIs hit the same orchestrator path.
        # The only difference is whether the caller also subscribes to router events
        # while the turn is being processed.
        queue = broker.register(session_id)
        processing_task = asyncio.create_task(
            orchestrator.handle_user_message(
                session_id=session_id,
                cust_id=resolved_cust_id,
                content=request.content or "",
                guided_selection=request.guided_selection,
                recommendation_context=request.recommendation_context,
                proactive_recommendation=request.proactive_recommendation,
            )
        )
        try:
            while True:
                if await http_request.is_disconnected():
                    break
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
    request: Request,
    broker: EventBroker = Depends(get_event_broker),
) -> StreamingResponse:
    """Subscribe to router events for one session without triggering a new action."""
    async def event_generator():
        """Yield heartbeat and session events until the client disconnects."""
        subscription = broker.subscribe(session_id)
        initial_heartbeat = TaskEvent(
            event="heartbeat",
            task_id="session",
            session_id=session_id,
            intent_code="session",
            status=TaskStatus.RUNNING,
            message="heartbeat",
        )
        try:
            yield _encode_sse(initial_heartbeat.event, initial_heartbeat.model_dump(mode="json"))
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(subscription.__anext__(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                except StopAsyncIteration:
                    break
                yield _encode_sse(event.event, event.model_dump(mode="json"))
        finally:
            await subscription.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
