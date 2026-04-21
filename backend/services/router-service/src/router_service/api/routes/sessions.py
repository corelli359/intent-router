from __future__ import annotations

import asyncio
from contextlib import suppress
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field, model_validator
from starlette.responses import StreamingResponse

from router_service.api.errors import RouterApiException, RouterErrorCode
from router_service.api.dependencies import get_event_broker, get_orchestrator
from router_service.api.sse.broker import EventBroker
from router_service.core.shared.domain import IntentMatch, TaskEvent, TaskStatus
from router_service.core.shared.diagnostics import RouterDiagnostic
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphEdge,
    GraphNodeState,
    GraphNodeStatus,
    GraphRouterSnapshot,
    GuidedSelectionPayload,
    ProactiveRecommendationPayload,
    RecommendationContextPayload,
)
from router_service.core.graph.orchestrator import GraphRouterOrchestrator
from router_service.core.support.llm_client import LLMServiceUnavailableError
from router_service.core.support.json_codec import JSONDecodeError, json_dumps, json_loads


router = APIRouter(tags=["router"])


class MessageExecutionMode(StrEnum):
    """Execution behavior selected for one message request."""

    EXECUTE = "execute"
    ROUTER_ONLY = "router_only"


class CreateSessionResponse(BaseModel):
    """Response returned after creating a router session."""

    session_id: str
    cust_id: str


class CreateSessionRequest(BaseModel):
    """Optional payload used when callers want to control session creation."""

    cust_id: str | None = None
    session_id: str | None = None


class SessionSnapshotResponse(BaseModel):
    """Standard response envelope for snapshot-returning session mutations."""

    ok: bool = True
    snapshot: GraphRouterSnapshot


class AssistantOutputResponse(BaseModel):
    """Assistant-facing response envelope for the new upstream protocol."""

    ok: bool = True
    output: dict[str, Any] = Field(default_factory=dict)


class ConfigVariableInput(BaseModel):
    """One upstream `config_variables` item passed through the router."""

    name: str
    value: Any = ""

    def parsed_json_value(self) -> Any:
        """Parse JSON-like string values when possible for structured router hints."""
        if isinstance(self.value, str):
            text = self.value.strip()
            if not text:
                return None
            try:
                return json_loads(text)
            except JSONDecodeError:
                return self.value
        return self.value


class MessageRequest(BaseModel):
    """Unified message payload for both plain dialog and structured router inputs."""

    content: str | None = None
    message: str | None = None
    txt: str | None = None
    execution_mode: MessageExecutionMode = Field(default=MessageExecutionMode.EXECUTE, alias="executionMode")
    guided_selection: GuidedSelectionPayload | None = Field(default=None, alias="guidedSelection")
    recommendation_context: RecommendationContextPayload | None = Field(default=None, alias="recommendationContext")
    proactive_recommendation: ProactiveRecommendationPayload | None = Field(default=None, alias="proactiveRecommendation")
    config_variables: list[ConfigVariableInput] = Field(default_factory=list)
    cust_id: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "MessageRequest":
        """Normalize alias fields and require either message content or guided selection."""
        self.content = self.content or self.message or self.txt or ""
        if not self.content and (self.guided_selection is None or not self.guided_selection.selected_intents):
            raise ValueError("content/message or guided_selection is required")
        return self

    @property
    def uses_assistant_protocol(self) -> bool:
        """Return whether the request follows the v0.3 assistant-facing envelope."""
        return self.txt is not None or bool(self.config_variables)


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


def _session_or_snapshot(orchestrator: GraphRouterOrchestrator, session_id: str, fallback: object | None = None) -> object:
    """Return the live in-memory session when available, otherwise fall back to snapshot APIs."""
    if fallback is not None:
        return fallback
    session_store = getattr(orchestrator, "session_store", None)
    getter = getattr(session_store, "get", None)
    if getter is not None:
        return getter(session_id)
    return orchestrator.snapshot(session_id)


def _resolve_action_cust_id(
    orchestrator: GraphRouterOrchestrator,
    session_id: str,
    request: ActionRequest,
) -> str:
    """Resolve the customer id for an action request from payload or existing session."""
    if request.cust_id:
        return request.cust_id
    try:
        return _session_or_snapshot(orchestrator, session_id).cust_id
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
        return _session_or_snapshot(orchestrator, session_id).cust_id
    except KeyError:
        return "cust_demo"


def _serialize_match(match: IntentMatch) -> dict[str, object]:
    """Serialize one intent match without copying the surrounding graph/session tree."""
    return {
        "intent_code": match.intent_code,
        "confidence": match.confidence,
        "reason": match.reason,
    }


def _serialize_diagnostic(diagnostic: RouterDiagnostic) -> dict[str, object]:
    """Serialize one router diagnostic."""
    return {
        "code": diagnostic.code,
        "source": diagnostic.source,
        "message": diagnostic.message,
        "details": dict(diagnostic.details),
    }


def _serialize_graph(graph: ExecutionGraphState | None) -> dict[str, object] | None:
    """Serialize one graph directly from the live runtime object."""
    if graph is None:
        return None
    return {
        "graph_id": graph.graph_id,
        "source_message": graph.source_message,
        "summary": graph.summary,
        "version": graph.version,
        "status": graph.status.value,
        "confirm_token": graph.confirm_token,
        "nodes": [_serialize_node(node) for node in graph.nodes],
        "edges": [_serialize_edge(edge) for edge in graph.edges],
        "actions": [_serialize_action(action) for action in graph.actions],
        "diagnostics": [_serialize_diagnostic(item) for item in graph.diagnostics],
        "created_at": graph.created_at.isoformat(),
        "updated_at": graph.updated_at.isoformat(),
    }


def _serialize_action(action: object) -> dict[str, object]:
    """Serialize one graph action."""
    return {
        "code": action.code,
        "label": action.label,
    }


def _serialize_edge(edge: GraphEdge) -> dict[str, object]:
    """Serialize one graph edge."""
    return {
        "edge_id": edge.edge_id,
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "relation_type": edge.relation_type.value,
        "label": edge.label,
        "condition": _serialize_condition(edge.condition),
    }


def _serialize_condition(condition: object | None) -> dict[str, object] | None:
    """Serialize one edge condition."""
    if condition is None:
        return None
    return {
        "source_node_id": condition.source_node_id,
        "expected_statuses": list(condition.expected_statuses),
        "left_key": condition.left_key,
        "operator": condition.operator,
        "right_value": condition.right_value,
    }


def _serialize_node(node: GraphNodeState) -> dict[str, object]:
    """Serialize one graph node."""
    return {
        "node_id": node.node_id,
        "intent_code": node.intent_code,
        "title": node.title,
        "confidence": node.confidence,
        "position": node.position,
        "source_fragment": node.source_fragment,
        "status": node.status.value,
        "task_id": node.task_id,
        "depends_on": list(node.depends_on),
        "blocking_reason": node.blocking_reason,
        "skip_reason_code": node.skip_reason_code,
        "relation_reason": node.relation_reason,
        "slot_memory": dict(node.slot_memory),
        "slot_bindings": [_serialize_slot_binding(binding) for binding in node.slot_bindings],
        "history_slot_keys": list(node.history_slot_keys),
        "diagnostics": [_serialize_diagnostic(item) for item in node.diagnostics],
        "output_payload": dict(node.output_payload),
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
    }


def _serialize_slot_binding(binding: object) -> dict[str, object]:
    """Serialize one slot binding."""
    return {
        "slot_key": binding.slot_key,
        "value": binding.value,
        "source": binding.source.value,
        "source_text": binding.source_text,
        "confidence": binding.confidence,
        "is_modified": binding.is_modified,
    }


def _serialize_message(message: object) -> dict[str, object]:
    """Serialize one chat message."""
    return {
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
    }


def _serialize_session_payload(
    orchestrator: GraphRouterOrchestrator,
    session_id: str,
    *,
    fallback: object | None = None,
) -> dict[str, object]:
    """Serialize the current session state directly from live in-memory objects."""
    session = _session_or_snapshot(orchestrator, session_id, fallback=fallback)
    return _serialize_session(session)


def _serialize_session(session: object) -> dict[str, object]:
    """Serialize one session-like object into the API response shape."""
    return {
        "session_id": session.session_id,
        "cust_id": session.cust_id,
        "messages": [_serialize_message(item) for item in session.messages],
        "candidate_intents": [_serialize_match(item) for item in session.candidate_intents],
        "last_diagnostics": [_serialize_diagnostic(item) for item in session.last_diagnostics],
        "shared_slot_memory": dict(getattr(session, "shared_slot_memory", {}) or {}),
        "current_graph": _serialize_graph(session.current_graph),
        "pending_graph": _serialize_graph(session.pending_graph),
        "active_node_id": session.active_node_id,
        "expires_at": session.expires_at.isoformat(),
    }


def _serialize_response_snapshot(snapshot: object) -> dict[str, object]:
    """Normalize fallback snapshot objects into the public API response shape."""
    if isinstance(snapshot, dict):
        return snapshot
    model_dump = getattr(snapshot, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return _serialize_session(snapshot)


def _encode_sse(event_name: str, payload: dict[str, object]) -> str:
    """Encode one router event as an SSE frame."""
    body = json_dumps(payload)
    return f"event: {event_name}\ndata: {body}\n\n"


def _split_upstream_config_variables(
    config_variables: list[ConfigVariableInput],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split ordinary passthrough config variables from special `slots_data`."""
    passthrough: dict[str, Any] = {}
    slots_data: dict[str, Any] = {}
    for item in config_variables:
        if item.name == "slots_data":
            parsed = item.parsed_json_value()
            if isinstance(parsed, dict):
                slots_data = dict(parsed)
            continue
        passthrough[item.name] = item.value
    return passthrough, slots_data


def _response_graph(session: object) -> ExecutionGraphState | None:
    """Resolve the graph that should be used to build the assistant-facing response."""
    current_graph = getattr(session, "current_graph", None)
    if current_graph is not None:
        return current_graph
    return getattr(session, "pending_graph", None)


def _response_node(session: object) -> GraphNodeState | None:
    """Resolve the most relevant node for one assistant-facing response."""
    graph = _response_graph(session)
    if graph is None or not graph.nodes:
        return None

    active_node_id = getattr(session, "active_node_id", None)
    if active_node_id:
        with suppress(KeyError):
            return graph.node_by_id(active_node_id)

    preferred_statuses = {
        GraphNodeStatus.WAITING_USER_INPUT,
        GraphNodeStatus.WAITING_CONFIRMATION,
        GraphNodeStatus.READY_FOR_DISPATCH,
        GraphNodeStatus.COMPLETED,
        GraphNodeStatus.FAILED,
    }
    candidates = [node for node in graph.nodes if node.status in preferred_statuses]
    if candidates:
        return max(candidates, key=lambda item: item.updated_at)
    return max(graph.nodes, key=lambda item: item.updated_at)


def _assistant_uses_multi_intent_graph(session: object) -> bool:
    """Return whether the current assistant-facing response has entered multi-intent graph mode."""
    graph = _response_graph(session)
    return graph is not None and len(graph.nodes) > 1


def _last_assistant_message(session: object) -> str:
    """Return the most recent assistant message content when present."""
    messages = getattr(session, "messages", []) or []
    for item in reversed(messages):
        if getattr(item, "role", None) == "assistant" and getattr(item, "content", None):
            return str(item.content)
    return ""


def _synthesized_transfer_data(node: GraphNodeState) -> list[dict[str, str]]:
    """Build the transfer-agent `data[].answer` fallback from router-visible slots."""
    slot_memory = dict(node.slot_memory)
    amount = slot_memory.get("amount")
    payee_name = slot_memory.get("payee_name")
    if amount in (None, "") and node.output_payload.get("amount") not in (None, ""):
        amount = node.output_payload.get("amount")
    if payee_name in (None, "") and node.output_payload.get("payee_name") not in (None, ""):
        payee_name = node.output_payload.get("payee_name")
    if amount in (None, "") and payee_name in (None, ""):
        return []
    return [
        {
            "isSubAgent": "True",
            "typIntent": "mbpTransfer",
            "answer": f"||{'' if amount is None else amount}|{'' if payee_name is None else payee_name}|",
        }
    ]


def _assistant_output_payload(session: object) -> dict[str, Any]:
    """Build the assistant-facing response payload from live router/session state."""
    node = _response_node(session)
    if node is None:
        message = _last_assistant_message(session)
        return {
            "status": "completed" if message else "idle",
            "message": message,
        }

    agent_output = dict(getattr(node, "_agent_output", {}) or {})
    if agent_output:
        output: dict[str, Any] = {}
        if "isHandOver" in agent_output:
            output["isHandOver"] = agent_output["isHandOver"]
        else:
            output["isHandOver"] = node.status in {GraphNodeStatus.COMPLETED, GraphNodeStatus.FAILED}
        if "handOverReason" in agent_output and agent_output["handOverReason"] not in (None, ""):
            output["handOverReason"] = agent_output["handOverReason"]
        elif node.status == GraphNodeStatus.WAITING_USER_INPUT:
            output["handOverReason"] = "waiting_user_input"
        elif node.status == GraphNodeStatus.COMPLETED:
            output["handOverReason"] = "completed"
        data = agent_output.get("data")
        if isinstance(data, list):
            output["data"] = data
        elif node.intent_code == "AG_TRANS":
            output["data"] = _synthesized_transfer_data(node)
        output["intent_code"] = node.intent_code
        return output

    message = node.blocking_reason or _last_assistant_message(session)
    return {
        "intent_code": node.intent_code,
        "status": node.status.value,
        "message": message,
        "slot_memory": dict(node.slot_memory),
    }


def _assistant_multi_intent_unsupported_output(session: object) -> dict[str, Any]:
    """Build the assistant-facing failure payload for multi-intent graphs not yet exposed in production."""
    graph = _response_graph(session)
    output: dict[str, Any] = {
        "status": "failed",
        "message": "当前生产协议暂仅支持单意图场景，多意图输出协议待定。",
        "errorCode": RouterErrorCode.ROUTER_MULTI_INTENT_UNSUPPORTED,
        "route_mode": "multi_intent",
    }
    if graph is not None:
        output["graph_status"] = graph.status.value
        intent_codes = [node.intent_code for node in graph.nodes]
        if intent_codes:
            output["intent_codes"] = intent_codes
    return output


def _assistant_response_envelope(session: object) -> dict[str, Any]:
    """Build the assistant-facing `ok + output` envelope from live router/session state."""
    if _assistant_uses_multi_intent_graph(session):
        return {
            "ok": False,
            "output": _assistant_multi_intent_unsupported_output(session),
        }
    return {
        "ok": True,
        "output": _assistant_output_payload(session),
    }


def _assistant_llm_unavailable_output(exc: LLMServiceUnavailableError) -> dict[str, Any]:
    """Build the assistant-facing failure payload for semantic-model outages."""
    output: dict[str, Any] = {
        "status": "failed",
        "message": str(exc),
        "errorCode": RouterErrorCode.ROUTER_LLM_UNAVAILABLE,
        "stage": exc.stage,
    }
    if exc.details:
        output["details"] = dict(exc.details)
    return output


@router.post("/sessions", response_model=CreateSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: CreateSessionRequest | None = None,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
) -> CreateSessionResponse:
    """Create a router session for direct API or frontend callers."""
    cust_id = request.cust_id if request and request.cust_id else "cust_demo"
    session = orchestrator.create_session(cust_id=cust_id, session_id=request.session_id if request else None)
    return CreateSessionResponse(session_id=session.session_id, cust_id=session.cust_id)


@router.get("/sessions/{session_id}", response_model=GraphRouterSnapshot)
async def get_session_snapshot(
    session_id: str,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
) -> dict[str, object]:
    """Return the current router snapshot for one session."""
    try:
        return _serialize_session_payload(orchestrator, session_id)
    except KeyError as exc:
        raise RouterApiException(
            status_code=404,
            code=RouterErrorCode.ROUTER_SESSION_NOT_FOUND,
            message="session not found",
            details={"session_id": session_id},
        ) from exc


@router.post(
    "/sessions/{session_id}/messages",
    response_model=SessionSnapshotResponse | AssistantOutputResponse,
)
async def post_message(
    session_id: str,
    request: MessageRequest,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
) -> SessionSnapshotResponse | AssistantOutputResponse:
    """Submit one user message turn to the router and return the updated snapshot."""
    # Message APIs are the main entry for intent dialog. They can be called by a
    # frontend chat page, a test harness, or a backend integration that wants to
    # drive the router directly without rendering any UI.
    resolved_cust_id = _resolve_message_cust_id(orchestrator, session_id, request)
    upstream_config_variables, upstream_slots_data = _split_upstream_config_variables(request.config_variables)
    try:
        serialized_handler = getattr(orchestrator, "handle_user_message_serialized", None)
        if callable(serialized_handler):
            serializer = (
                _assistant_response_envelope
                if request.uses_assistant_protocol
                else _serialize_session
            )
            serialized_response = await serialized_handler(
                session_id=session_id,
                cust_id=resolved_cust_id,
                content=request.content or "",
                serializer=serializer,
                router_only=request.execution_mode == MessageExecutionMode.ROUTER_ONLY,
                guided_selection=request.guided_selection,
                recommendation_context=request.recommendation_context,
                proactive_recommendation=request.proactive_recommendation,
                upstream_config_variables=upstream_config_variables,
                upstream_slots_data=upstream_slots_data,
                emit_events=False,
            )
            if request.uses_assistant_protocol:
                return AssistantOutputResponse(**serialized_response)
            snapshot = serialized_response
        else:
            response_state = await orchestrator.handle_user_message(
                session_id=session_id,
                cust_id=resolved_cust_id,
                content=request.content or "",
                router_only=request.execution_mode == MessageExecutionMode.ROUTER_ONLY,
                guided_selection=request.guided_selection,
                recommendation_context=request.recommendation_context,
                proactive_recommendation=request.proactive_recommendation,
                upstream_config_variables=upstream_config_variables,
                upstream_slots_data=upstream_slots_data,
                emit_events=False,
            )
            if request.uses_assistant_protocol:
                return AssistantOutputResponse(**_assistant_response_envelope(response_state))
            snapshot = _serialize_response_snapshot(response_state)
    except LLMServiceUnavailableError as exc:
        if request.uses_assistant_protocol:
            return AssistantOutputResponse(
                ok=False,
                output=_assistant_llm_unavailable_output(exc),
            )
        raise RouterApiException(
            status_code=503,
            code=RouterErrorCode.ROUTER_LLM_UNAVAILABLE,
            message=str(exc),
            details={
                "stage": exc.stage,
                **dict(exc.details),
            },
        ) from exc
    except ValueError as exc:
        raise RouterApiException(
            status_code=400,
            code=RouterErrorCode.ROUTER_BAD_REQUEST,
            message=str(exc),
        ) from exc
    return SessionSnapshotResponse(snapshot=snapshot)


@router.post("/sessions/{session_id}/actions", response_model=SessionSnapshotResponse)
async def post_action(
    session_id: str,
    request: ActionRequest,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
) -> SessionSnapshotResponse:
    """Submit one explicit graph action and return the updated snapshot."""
    # Action APIs mutate the router state machine directly. Typical callers are
    # the graph UI, an orchestration service, or tests that need to confirm/cancel
    # a pending graph or interrupt the current waiting node.
    resolved_cust_id = _resolve_action_cust_id(orchestrator, session_id, request)
    try:
        serialized_handler = getattr(orchestrator, "handle_action_serialized", None)
        if callable(serialized_handler):
            snapshot = await serialized_handler(
                session_id=session_id,
                cust_id=resolved_cust_id,
                action_code=request.action_code or "",
                serializer=_serialize_session,
                source=request.source,
                task_id=request.task_id,
                confirm_token=request.confirm_token,
                payload=request.payload,
            )
        else:
            snapshot = _serialize_response_snapshot(
                await orchestrator.handle_action(
                    session_id=session_id,
                    cust_id=resolved_cust_id,
                    action_code=request.action_code or "",
                    source=request.source,
                    task_id=request.task_id,
                    confirm_token=request.confirm_token,
                    payload=request.payload,
                )
            )
    except ValueError as exc:
        raise RouterApiException(
            status_code=400,
            code=RouterErrorCode.ROUTER_BAD_REQUEST,
            message=str(exc),
        ) from exc
    return SessionSnapshotResponse(snapshot=snapshot)


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
                return_snapshot=False,
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
                router_only=request.execution_mode == MessageExecutionMode.ROUTER_ONLY,
                guided_selection=request.guided_selection,
                recommendation_context=request.recommendation_context,
                proactive_recommendation=request.proactive_recommendation,
                upstream_config_variables=_split_upstream_config_variables(request.config_variables)[0],
                upstream_slots_data=_split_upstream_config_variables(request.config_variables)[1],
                return_snapshot=False,
                emit_events=True,
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
