from __future__ import annotations

import asyncio
from contextlib import suppress
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
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
public_router = APIRouter(tags=["router"])


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

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    cust_id: str | None = Field(default=None, alias="custId")
    session_id: str | None = Field(default=None, alias="sessionId")


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

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    content: str | None = None
    message: str | None = None
    txt: str | None = None
    execution_mode: MessageExecutionMode = Field(default=MessageExecutionMode.EXECUTE, alias="executionMode")
    guided_selection: GuidedSelectionPayload | None = Field(default=None, alias="guidedSelection")
    recommendation_context: RecommendationContextPayload | None = Field(default=None, alias="recommendationContext")
    proactive_recommendation: ProactiveRecommendationPayload | None = Field(default=None, alias="proactiveRecommendation")
    config_variables: list[ConfigVariableInput] = Field(default_factory=list)
    cust_id: str | None = Field(default=None, alias="custId")

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


class ProtocolMessageRequest(BaseModel):
    """Production message contract exposed through the unified `/api/v1/message` entry."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    session_id: str = Field(alias="sessionId", min_length=1)
    txt: str = ""
    config_variables: list[ConfigVariableInput] = Field(default_factory=list)
    execution_mode: MessageExecutionMode = Field(default=MessageExecutionMode.EXECUTE, alias="executionMode")
    cust_id: str | None = Field(default=None, alias="custId")
    stream: bool = True

    @model_validator(mode="after")
    def normalize(self) -> "ProtocolMessageRequest":
        """Require the upstream protocol message text and normalize whitespace-only payloads."""
        self.txt = self.txt or ""
        if not self.txt.strip():
            raise ValueError("txt is required")
        return self


class TaskCompletionRequest(BaseModel):
    """Assistant-facing fixed-path task completion callback contract."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    session_id: str = Field(alias="sessionId", min_length=1)
    task_id: str = Field(alias="taskId", min_length=1)
    completion_signal: int = Field(alias="completionSignal", ge=1, le=2)


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


def _resolve_session_cust_id(
    orchestrator: GraphRouterOrchestrator,
    session_id: str,
    cust_id: str | None,
) -> str:
    """Resolve one customer id from payload first, then existing session state."""
    if cust_id:
        return cust_id
    try:
        return _session_or_snapshot(orchestrator, session_id).cust_id
    except KeyError:
        return "cust_demo"


def _resolve_live_session(orchestrator: GraphRouterOrchestrator, session_id: str) -> object:
    """Return the live in-memory session required by the production message contract."""
    session_store = getattr(orchestrator, "session_store", None)
    getter = getattr(session_store, "get", None)
    if getter is None:
        raise RuntimeError("router live session store is unavailable")
    return getter(session_id)


def _resolve_message_cust_id(
    orchestrator: GraphRouterOrchestrator,
    session_id: str,
    request: MessageRequest,
) -> str:
    """Resolve the customer id for a message request from payload or existing session."""
    return _resolve_session_cust_id(orchestrator, session_id, request.cust_id)


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

    status_priority = {
        GraphNodeStatus.WAITING_USER_INPUT: 0,
        GraphNodeStatus.WAITING_CONFIRMATION: 1,
        GraphNodeStatus.WAITING_ASSISTANT_COMPLETION: 2,
        GraphNodeStatus.RUNNING: 3,
        GraphNodeStatus.READY_FOR_DISPATCH: 4,
        GraphNodeStatus.COMPLETED: 5,
        GraphNodeStatus.FAILED: 6,
        GraphNodeStatus.CANCELLED: 7,
        GraphNodeStatus.SKIPPED: 8,
    }
    candidates = [node for node in graph.nodes if node.status in status_priority]
    if candidates:
        return min(
            candidates,
            key=lambda item: (
                status_priority.get(item.status, 99),
                -item.updated_at.timestamp(),
                item.position,
            ),
        )
    return max(graph.nodes, key=lambda item: item.updated_at)


def _graphs_for_task_lookup(session: object) -> list[ExecutionGraphState]:
    """Return distinct live graphs that may still own the requested task."""
    graphs: list[ExecutionGraphState] = []
    seen_graph_ids: set[str] = set()
    for candidate in (
        getattr(session, "current_graph", None),
        getattr(session, "pending_graph", None),
    ):
        if candidate is None or candidate.graph_id in seen_graph_ids:
            continue
        graphs.append(candidate)
        seen_graph_ids.add(candidate.graph_id)
    for business in getattr(session, "business_objects", []) or []:
        graph = getattr(business, "graph", None)
        if graph is None or graph.graph_id in seen_graph_ids:
            continue
        graphs.append(graph)
        seen_graph_ids.add(graph.graph_id)
    return graphs


def _graph_and_node_for_task(session: object, task_id: str) -> tuple[ExecutionGraphState, GraphNodeState] | None:
    """Resolve one live graph/node pair by task id."""
    for graph in _graphs_for_task_lookup(session):
        for node in graph.nodes:
            if node.task_id == task_id:
                return graph, node
    return None


def _last_assistant_message(session: object) -> str:
    """Return the most recent assistant message content when present."""
    messages = getattr(session, "messages", []) or []
    for item in reversed(messages):
        if getattr(item, "role", None) == "assistant" and getattr(item, "content", None):
            return str(item.content)
    return ""


def _assistant_task_status(node_status: str | GraphNodeStatus | None) -> str:
    """Map router node status into the assistant-facing task-list status."""
    if isinstance(node_status, GraphNodeStatus):
        value = node_status.value
    elif node_status is None:
        value = ""
    else:
        value = str(node_status)

    if value in {
        GraphNodeStatus.COMPLETED.value,
    }:
        return "completed"
    if value in {
        GraphNodeStatus.FAILED.value,
    }:
        return "failed"
    if value in {
        GraphNodeStatus.CANCELLED.value,
        GraphNodeStatus.SKIPPED.value,
    }:
        return "cancelled"
    if value in {
        GraphNodeStatus.RUNNING.value,
    }:
        return "running"
    return "waiting"


def _assistant_effective_status(
    *,
    node_status: str | GraphNodeStatus | None,
) -> str:
    """Resolve the assistant-facing status from Router-owned node state."""
    if isinstance(node_status, GraphNodeStatus):
        node_status_value = node_status.value
    elif node_status is None:
        node_status_value = ""
    else:
        node_status_value = str(node_status)

    return node_status_value


def _assistant_task_name(
    *,
    task_id: str | None,
    node_id: str | None,
    intent_code: str | None,
    position: int | None = None,
) -> str:
    """Return the stable task identifier exposed to the assistant-facing protocol."""
    if task_id not in (None, ""):
        return str(task_id)
    if intent_code not in (None, ""):
        if position is not None:
            return f"{intent_code}#{position}"
        return str(intent_code)
    if node_id not in (None, ""):
        return str(node_id)
    return "task"


def _assistant_task_list_from_graph(graph: ExecutionGraphState | None) -> list[dict[str, str]]:
    """Build the assistant-facing task list from one live graph object."""
    if graph is None:
        return []
    return [
        {
            "name": _assistant_task_name(
                task_id=node.task_id,
                node_id=node.node_id,
                intent_code=node.intent_code,
                position=node.position,
            ),
            "status": _assistant_task_status(node.status),
        }
        for node in sorted(graph.nodes, key=lambda item: (item.position, item.created_at))
    ]


def _assistant_task_list_from_graph_payload(graph_payload: dict[str, Any] | None) -> list[dict[str, str]]:
    """Build the assistant-facing task list from a serialized graph payload."""
    if not isinstance(graph_payload, dict):
        return []
    nodes = graph_payload.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [
        {
            "name": _assistant_task_name(
                task_id=str(item.get("task_id")) if item.get("task_id") not in (None, "") else None,
                node_id=str(item.get("node_id")) if item.get("node_id") not in (None, "") else None,
                intent_code=str(item.get("intent_code")) if item.get("intent_code") not in (None, "") else None,
                position=item.get("position") if isinstance(item.get("position"), int) else None,
            ),
            "status": _assistant_task_status(str(item.get("status") or "")),
        }
        for item in nodes
        if isinstance(item, dict)
    ]


def _assistant_default_node_id(
    *,
    status: str,
    explicit_node_id: str | None = None,
) -> str:
    """Resolve the assistant-facing node id while preserving agent-provided values when present."""
    if explicit_node_id not in (None, ""):
        return str(explicit_node_id)
    if status in {"waiting_user_input", "waiting_confirmation", "waiting_assistant_completion", "completed", "failed"}:
        return "end"
    return ""


def _assistant_router_placeholder_message(
    *,
    status: str,
    blocking_reason: str | None = None,
    graph_status: str | None = None,
    fallback: str = "",
) -> str:
    """Resolve the assistant-facing message from router state instead of agent text."""
    if blocking_reason not in (None, ""):
        return str(blocking_reason)

    normalized_graph_status = str(graph_status or "")
    if normalized_graph_status == "partially_completed":
        return "执行图部分完成，存在已完成节点之外的未执行或异常终止节点"
    if normalized_graph_status == "failed" or status == "failed":
        return "执行图执行失败"
    if normalized_graph_status == "cancelled" or status == "cancelled":
        return "执行图已取消"
    if normalized_graph_status == "completed" or status == "completed":
        return "执行图已完成"

    if status in {"waiting_user_input", "waiting_confirmation", "waiting_assistant_completion", "ready_for_dispatch"} and fallback:
        return fallback
    if normalized_graph_status == "waiting_user_input" or status == "waiting_user_input":
        return "执行图等待用户补充信息"
    if normalized_graph_status in {"waiting_confirmation", "waiting_confirmation_node"} or status == "waiting_confirmation":
        return "执行图等待节点确认"
    if normalized_graph_status == "waiting_assistant_completion" or status == "waiting_assistant_completion":
        return "执行图等待助手确认完成态"
    if normalized_graph_status == "ready_for_dispatch" or status == "ready_for_dispatch":
        return "路由识别完成，已具备执行条件；当前为 router_only 模式，未调用执行 agent"
    return fallback


def _assistant_event_message(
    *,
    event_message: str | None,
    agent_output: dict[str, Any],
    status: str,
    blocking_reason: str | None = None,
    graph_status: str | None = None,
) -> str:
    """Preserve agent-originated stream text while keeping router placeholders for router events."""
    if agent_output:
        for value in (agent_output.get("message"), agent_output.get("content"), event_message):
            if value not in (None, ""):
                return str(value)
    return _assistant_router_placeholder_message(
        status=status,
        blocking_reason=blocking_reason,
        graph_status=graph_status,
        fallback=event_message or "",
    )


def _assistant_synthesized_transfer_data(
    *,
    intent_code: str,
    slot_memory: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build the transfer `data[].answer` fallback from router-visible slots."""
    if intent_code != "AG_TRANS":
        return []
    normalized_payload = dict(payload or {})
    amount = slot_memory.get("amount")
    payee_name = slot_memory.get("payee_name")
    if amount in (None, "") and normalized_payload.get("amount") not in (None, ""):
        amount = normalized_payload.get("amount")
    if payee_name in (None, "") and normalized_payload.get("payee_name") not in (None, ""):
        payee_name = normalized_payload.get("payee_name")
    if amount in (None, "") or payee_name in (None, ""):
        return []
    return [
        {
            "isSubAgent": "True",
            "typIntent": "mbpTransfer",
            "answer": f"||{'' if amount is None else amount}|{'' if payee_name is None else payee_name}|",
        }
    ]


def _assistant_completion_fields(
    *,
    status: str,
    node_status: str | GraphNodeStatus | None,
    agent_output: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Resolve the unified completion state/reason for one assistant-facing payload."""
    normalized_output = dict(agent_output or {})
    explicit_state = normalized_output.get("completion_state")
    if isinstance(explicit_state, bool):
        explicit_state = int(explicit_state)
    if isinstance(explicit_state, int) and explicit_state in {0, 1, 2}:
        explicit_reason = normalized_output.get("completion_reason")
        if explicit_reason not in (None, ""):
            return explicit_state, str(explicit_reason)
        if explicit_state == 2:
            return 2, "agent_final_done"
        if explicit_state == 1:
            return 1, "agent_partial_done"
        return 0, "running"

    if isinstance(node_status, GraphNodeStatus):
        node_status_value = node_status.value
    elif node_status is None:
        node_status_value = status
    else:
        node_status_value = str(node_status)

    if status in {"waiting_user_input"} or node_status_value == GraphNodeStatus.WAITING_USER_INPUT.value:
        return 0, "router_waiting_user_input"
    if status in {"waiting_confirmation"} or node_status_value == GraphNodeStatus.WAITING_CONFIRMATION.value:
        return 0, "router_waiting_confirmation"
    if (
        status in {"waiting_assistant_completion"}
        or node_status_value == GraphNodeStatus.WAITING_ASSISTANT_COMPLETION.value
    ):
        return 1, "agent_partial_done"
    if status in {"ready_for_dispatch"} or node_status_value == GraphNodeStatus.READY_FOR_DISPATCH.value:
        return 0, "router_ready_for_dispatch"
    if status in {"running", "dispatching"} or node_status_value == GraphNodeStatus.RUNNING.value:
        return 0, "running"
    if status == "failed" or node_status_value == GraphNodeStatus.FAILED.value:
        return 2, "router_error"
    if status == "cancelled" or node_status_value == GraphNodeStatus.CANCELLED.value:
        return 2, "assistant_cancel"
    return 0, "running"


def _assistant_output_template(
    *,
    status: str,
    current_task: str = "",
    completion_state: int = 0,
    completion_reason: str = "",
    node_id: str = "",
    intent_code: str = "",
    is_handover: bool = False,
    handover_reason: str = "",
    message: str = "",
    data: list[Any] | None = None,
    slot_memory: dict[str, Any] | None = None,
    task_list: list[dict[str, str]] | None = None,
    error_code: str | RouterErrorCode | None = None,
    stage: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable assistant-facing payload shared by non-stream and SSE responses."""
    output: dict[str, Any] = {
        "current_task": current_task,
        "task_list": list(task_list or []),
        "completion_state": completion_state,
        "completion_reason": completion_reason,
        "node_id": node_id,
        "intent_code": intent_code,
        "status": status,
        "isHandOver": is_handover,
        "handOverReason": handover_reason,
        "message": message,
        "data": list(data or []),
        "slot_memory": dict(slot_memory or {}),
    }
    if error_code is not None:
        output["errorCode"] = str(error_code)
    if stage is not None:
        output["stage"] = stage
    if details:
        output["details"] = dict(details)
    return output


def _assistant_output_from_node(
    *,
    node: GraphNodeState,
    graph: ExecutionGraphState | None,
    message: str,
) -> dict[str, Any]:
    """Build the assistant-facing payload from live graph/node runtime objects."""
    agent_output = dict(getattr(node, "_agent_output", {}) or {})
    slot_memory = dict(node.slot_memory)
    output_payload = dict(node.output_payload)
    status = _assistant_effective_status(
        node_status=node.status,
    )
    is_handover = bool(
        agent_output["isHandOver"]
        if "isHandOver" in agent_output
        else node.status in {GraphNodeStatus.COMPLETED, GraphNodeStatus.FAILED}
    )
    if "handOverReason" in agent_output and agent_output["handOverReason"] not in (None, ""):
        handover_reason = str(agent_output["handOverReason"])
    elif node.status == GraphNodeStatus.WAITING_USER_INPUT:
        handover_reason = "waiting_user_input"
    elif node.status == GraphNodeStatus.WAITING_CONFIRMATION:
        handover_reason = "waiting_confirmation"
    elif node.status == GraphNodeStatus.WAITING_ASSISTANT_COMPLETION:
        handover_reason = "waiting_assistant_completion"
    elif node.status == GraphNodeStatus.COMPLETED:
        handover_reason = "completed"
    else:
        handover_reason = node.status.value
    data = agent_output.get("data")
    if not isinstance(data, list):
        data = _assistant_synthesized_transfer_data(
            intent_code=node.intent_code,
            slot_memory=slot_memory,
            payload=output_payload,
        )
    completion_override = None
    override_getter = getattr(node, "completion_override", None)
    if callable(override_getter):
        completion_override = override_getter()
    if completion_override is None:
        completion_state, completion_reason = _assistant_completion_fields(
            status=status,
            node_status=node.status,
            agent_output=agent_output,
        )
    else:
        completion_state, completion_reason = completion_override
    return _assistant_output_template(
        current_task=_assistant_task_name(
            task_id=node.task_id,
            node_id=node.node_id,
            intent_code=node.intent_code,
            position=node.position,
        ),
        task_list=_assistant_task_list_from_graph(graph),
        completion_state=completion_state,
        completion_reason=completion_reason,
        node_id=_assistant_default_node_id(
            status=status,
            explicit_node_id=str(agent_output.get("node_id")) if agent_output.get("node_id") not in (None, "") else None,
        ),
        intent_code=str(agent_output.get("intent_code") or node.intent_code),
        status=status,
        is_handover=is_handover,
        handover_reason=handover_reason,
        message=_assistant_router_placeholder_message(
            status=status,
            blocking_reason=node.blocking_reason,
            graph_status=graph.status.value if graph is not None else None,
            fallback=message,
        ),
        data=data,
        slot_memory=slot_memory,
    )


def _assistant_output_from_event(event: TaskEvent) -> dict[str, Any] | None:
    """Translate one internal router task event into the assistant-facing SSE payload."""
    payload = dict(event.payload or {})
    agent_output = payload.get("agent_output")
    if not isinstance(agent_output, dict):
        agent_output = {}

    allowed_events = {
        "graph.unrecognized",
        "node.message",
        "node.waiting_user_input",
        "node.waiting_confirmation",
        "node.waiting_assistant_completion",
        "node.ready_for_dispatch",
        "node.completed",
        "node.failed",
        "node.cancelled",
    }
    if event.event not in allowed_events:
        return None

    graph_payload = payload.get("graph")
    graph_payload = graph_payload if isinstance(graph_payload, dict) else None
    node_payload = payload.get("node")
    node_payload = node_payload if isinstance(node_payload, dict) else None

    if event.event == "graph.unrecognized":
        return _assistant_output_template(
            status="unrecognized",
            completion_state=2,
            completion_reason="router_no_match",
            node_id="end",
            handover_reason="unrecognized",
            message=event.message or "",
        )

    if node_payload is None:
        return None

    slot_memory = dict(node_payload.get("slot_memory") or {})
    output_payload = dict(node_payload.get("output_payload") or {})
    status = _assistant_effective_status(
        node_status=str(node_payload.get("status") or ""),
    )
    is_handover = bool(
        agent_output["isHandOver"]
        if "isHandOver" in agent_output
        else (event.ishandover if isinstance(event.ishandover, bool) else status in {"completed", "failed"})
    )
    if "handOverReason" in agent_output and agent_output["handOverReason"] not in (None, ""):
        handover_reason = str(agent_output["handOverReason"])
    elif status in {"waiting_user_input", "waiting_confirmation", "waiting_assistant_completion"}:
        handover_reason = status
    elif status == "completed":
        handover_reason = "completed"
    else:
        handover_reason = status
    data = agent_output.get("data")
    intent_code = str(agent_output.get("intent_code") or node_payload.get("intent_code") or event.intent_code)
    if not isinstance(data, list):
        data = _assistant_synthesized_transfer_data(
            intent_code=intent_code,
            slot_memory=slot_memory,
            payload=output_payload,
        )
    completion_state, completion_reason = _assistant_completion_fields(
        status=status,
        node_status=str(node_payload.get("status") or ""),
        agent_output=agent_output,
    )
    return _assistant_output_template(
        current_task=_assistant_task_name(
            task_id=str(node_payload.get("task_id")) if node_payload.get("task_id") not in (None, "") else None,
            node_id=str(node_payload.get("node_id")) if node_payload.get("node_id") not in (None, "") else None,
            intent_code=intent_code,
            position=node_payload.get("position") if isinstance(node_payload.get("position"), int) else None,
        ),
        task_list=_assistant_task_list_from_graph_payload(graph_payload),
        completion_state=completion_state,
        completion_reason=completion_reason,
        node_id=_assistant_default_node_id(
            status=status,
            explicit_node_id=str(agent_output.get("node_id")) if agent_output.get("node_id") not in (None, "") else None,
        ),
        intent_code=intent_code,
        status=status,
        is_handover=is_handover,
        handover_reason=handover_reason,
        message=_assistant_event_message(
            event_message=event.message,
            agent_output=agent_output,
            status=status,
            blocking_reason=str(node_payload.get("blocking_reason")) if node_payload.get("blocking_reason") not in (None, "") else None,
            graph_status=str(graph_payload.get("status") or "") if graph_payload is not None else None,
        ),
        data=data,
        slot_memory=slot_memory,
    )


def _assistant_output_payload(session: object) -> dict[str, Any]:
    """Build the assistant-facing response payload from live router/session state."""
    graph = _response_graph(session)
    node = _response_node(session)
    if node is None:
        message = _last_assistant_message(session)
        if message:
            return _assistant_output_template(
                status="unrecognized",
                completion_state=2,
                completion_reason="router_no_match",
                node_id="end",
                handover_reason="unrecognized",
                message=message,
            )
        return _assistant_output_template(status="idle")

    message = node.blocking_reason or _last_assistant_message(session)
    return _assistant_output_from_node(node=node, graph=graph, message=message)


def _assistant_response_envelope(session: object) -> dict[str, Any]:
    """Build the assistant-facing `ok + output` envelope from live router/session state."""
    return {
        "ok": True,
        "output": _assistant_output_payload(session),
    }


def _assistant_response_envelope_for_task(session: object, task_id: str) -> dict[str, Any]:
    """Build the assistant-facing envelope for one specific task id."""
    resolved = _graph_and_node_for_task(session, task_id)
    if resolved is None:
        return _assistant_response_envelope(session)
    graph, node = resolved
    return {
        "ok": True,
        "output": _assistant_output_from_node(
            node=node,
            graph=graph,
            message="",
        ),
    }


def _assistant_llm_unavailable_output(exc: LLMServiceUnavailableError) -> dict[str, Any]:
    """Build the assistant-facing failure payload for semantic-model outages."""
    return _assistant_output_template(
        status="failed",
        completion_state=2,
        completion_reason="router_error",
        node_id="end",
        is_handover=False,
        handover_reason="failed",
        message=str(exc),
        error_code=RouterErrorCode.ROUTER_LLM_UNAVAILABLE,
        stage=exc.stage,
        details=dict(exc.details),
    )


def _assistant_bad_request_output(message: str) -> dict[str, Any]:
    """Build the assistant-facing failure payload for request validation errors."""
    return _assistant_output_template(
        status="failed",
        completion_state=2,
        completion_reason="router_error",
        node_id="end",
        handover_reason="failed",
        message=message,
        error_code=RouterErrorCode.ROUTER_BAD_REQUEST,
    )


def _encode_done_sse() -> str:
    """Encode the assistant-facing terminal SSE frame."""
    return "event: done\ndata: [DONE]\n\n"


async def _assistant_message_json_response(
    *,
    session_id: str,
    content: str,
    execution_mode: MessageExecutionMode,
    config_variables: list[ConfigVariableInput],
    cust_id: str | None,
    orchestrator: GraphRouterOrchestrator,
) -> AssistantOutputResponse:
    """Process one assistant-protocol turn and always return `ok + output` without snapshots."""
    resolved_cust_id = _resolve_session_cust_id(orchestrator, session_id, cust_id)
    upstream_config_variables, upstream_slots_data = _split_upstream_config_variables(config_variables)
    try:
        serialized_handler = getattr(orchestrator, "handle_user_message_serialized", None)
        if callable(serialized_handler):
            serialized_response = await serialized_handler(
                session_id=session_id,
                cust_id=resolved_cust_id,
                content=content,
                serializer=_assistant_response_envelope,
                assistant_protocol=True,
                router_only=execution_mode == MessageExecutionMode.ROUTER_ONLY,
                upstream_config_variables=upstream_config_variables,
                upstream_slots_data=upstream_slots_data,
                emit_events=False,
            )
            return AssistantOutputResponse(**serialized_response)
        await orchestrator.handle_user_message(
            session_id=session_id,
            cust_id=resolved_cust_id,
            content=content,
            assistant_protocol=True,
            router_only=execution_mode == MessageExecutionMode.ROUTER_ONLY,
            upstream_config_variables=upstream_config_variables,
            upstream_slots_data=upstream_slots_data,
            return_snapshot=False,
            emit_events=False,
        )
        session = _resolve_live_session(orchestrator, session_id)
        return AssistantOutputResponse(**_assistant_response_envelope(session))
    except LLMServiceUnavailableError as exc:
        return AssistantOutputResponse(
            ok=False,
            output=_assistant_llm_unavailable_output(exc),
        )
    except ValueError as exc:
        return AssistantOutputResponse(
            ok=False,
            output=_assistant_bad_request_output(str(exc)),
        )
    except Exception as exc:
        return AssistantOutputResponse(
            ok=False,
            output=_assistant_output_template(
                status="failed",
                completion_state=2,
                completion_reason="router_error",
                node_id="end",
                handover_reason="failed",
                message=str(exc),
                error_code=RouterErrorCode.ROUTER_INTERNAL_ERROR,
            ),
        )


def _assistant_message_stream_response(
    *,
    session_id: str,
    content: str,
    execution_mode: MessageExecutionMode,
    config_variables: list[ConfigVariableInput],
    cust_id: str | None,
    http_request: Request,
    orchestrator: GraphRouterOrchestrator,
    broker: EventBroker,
) -> StreamingResponse:
    """Process one assistant-protocol turn and always return the minimal SSE contract."""
    resolved_cust_id = _resolve_session_cust_id(orchestrator, session_id, cust_id)
    upstream_config_variables, upstream_slots_data = _split_upstream_config_variables(config_variables)

    async def event_generator():
        queue = broker.register(session_id)
        processing_task = asyncio.create_task(
            orchestrator.handle_user_message(
                session_id=session_id,
                cust_id=resolved_cust_id,
                content=content,
                assistant_protocol=True,
                router_only=execution_mode == MessageExecutionMode.ROUTER_ONLY,
                upstream_config_variables=upstream_config_variables,
                upstream_slots_data=upstream_slots_data,
                return_snapshot=False,
                emit_events=True,
            )
        )
        try:
            while True:
                if await http_request.is_disconnected():
                    break
                if processing_task.done() and queue.empty():
                    try:
                        await processing_task
                    except LLMServiceUnavailableError as exc:
                        yield _encode_sse("message", _assistant_llm_unavailable_output(exc))
                        break
                    except ValueError as exc:
                        yield _encode_sse("message", _assistant_bad_request_output(str(exc)))
                        break
                    except Exception as exc:
                        yield _encode_sse(
                            "message",
                            _assistant_output_template(
                                status="failed",
                                completion_state=2,
                                completion_reason="router_error",
                                node_id="end",
                                handover_reason="failed",
                                message=str(exc),
                                error_code=RouterErrorCode.ROUTER_INTERNAL_ERROR,
                            ),
                        )
                        break
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                payload = _assistant_output_from_event(event)
                if payload is None:
                    continue
                yield _encode_sse("message", payload)
        finally:
            broker.unregister(session_id, queue)
            if not processing_task.done():
                processing_task.cancel()
                with suppress(asyncio.CancelledError):
                    await processing_task
            if not await http_request.is_disconnected():
                yield _encode_done_sse()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
    if request.uses_assistant_protocol:
        return await _assistant_message_json_response(
            session_id=session_id,
            content=request.content or "",
            execution_mode=request.execution_mode,
            config_variables=request.config_variables,
            cust_id=request.cust_id,
            orchestrator=orchestrator,
        )
    resolved_cust_id = _resolve_message_cust_id(orchestrator, session_id, request)
    upstream_config_variables, upstream_slots_data = _split_upstream_config_variables(request.config_variables)
    try:
        serialized_handler = getattr(orchestrator, "handle_user_message_serialized", None)
        if callable(serialized_handler):
            serialized_response = await serialized_handler(
                session_id=session_id,
                cust_id=resolved_cust_id,
                content=request.content or "",
                serializer=_serialize_session,
                assistant_protocol=False,
                router_only=request.execution_mode == MessageExecutionMode.ROUTER_ONLY,
                guided_selection=request.guided_selection,
                recommendation_context=request.recommendation_context,
                proactive_recommendation=request.proactive_recommendation,
                upstream_config_variables=upstream_config_variables,
                upstream_slots_data=upstream_slots_data,
                emit_events=False,
            )
            snapshot = serialized_response
        else:
            response_state = await orchestrator.handle_user_message(
                session_id=session_id,
                cust_id=resolved_cust_id,
                content=request.content or "",
                assistant_protocol=False,
                router_only=request.execution_mode == MessageExecutionMode.ROUTER_ONLY,
                guided_selection=request.guided_selection,
                recommendation_context=request.recommendation_context,
                proactive_recommendation=request.proactive_recommendation,
                upstream_config_variables=upstream_config_variables,
                upstream_slots_data=upstream_slots_data,
                emit_events=False,
            )
            snapshot = _serialize_response_snapshot(response_state)
    except LLMServiceUnavailableError as exc:
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
    if request.uses_assistant_protocol:
        return _assistant_message_stream_response(
            session_id=session_id,
            content=request.content or "",
            execution_mode=request.execution_mode,
            config_variables=request.config_variables,
            cust_id=request.cust_id,
            http_request=http_request,
            orchestrator=orchestrator,
            broker=broker,
        )
    resolved_cust_id = _resolve_message_cust_id(orchestrator, session_id, request)
    upstream_config_variables, upstream_slots_data = _split_upstream_config_variables(request.config_variables)

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
                assistant_protocol=request.uses_assistant_protocol,
                router_only=request.execution_mode == MessageExecutionMode.ROUTER_ONLY,
                guided_selection=request.guided_selection,
                recommendation_context=request.recommendation_context,
                proactive_recommendation=request.proactive_recommendation,
                upstream_config_variables=upstream_config_variables,
                upstream_slots_data=upstream_slots_data,
                return_snapshot=False,
                emit_events=True,
            )
        )
        try:
            while True:
                if await http_request.is_disconnected():
                    break
                if processing_task.done() and queue.empty():
                    try:
                        await processing_task
                    except LLMServiceUnavailableError as exc:
                        if request.uses_assistant_protocol:
                            yield _encode_sse("message", _assistant_llm_unavailable_output(exc))
                            break
                        raise
                    except ValueError as exc:
                        if request.uses_assistant_protocol:
                            yield _encode_sse("message", _assistant_bad_request_output(str(exc)))
                            break
                        raise
                    except Exception as exc:
                        if request.uses_assistant_protocol:
                            yield _encode_sse(
                                "message",
                                _assistant_output_template(
                                    status="failed",
                                    completion_state=2,
                                    completion_reason="router_error",
                                    node_id="end",
                                    handover_reason="failed",
                                    message=str(exc),
                                    error_code=RouterErrorCode.ROUTER_INTERNAL_ERROR,
                                ),
                            )
                            break
                        raise
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


@public_router.post("/v1/message", response_model=None)
async def post_protocol_message(
    request: ProtocolMessageRequest,
    http_request: Request,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
    broker: EventBroker = Depends(get_event_broker),
) -> StreamingResponse | AssistantOutputResponse:
    """Unified production message entrypoint driven entirely by the request body contract."""
    if request.stream:
        return _assistant_message_stream_response(
            session_id=request.session_id,
            content=request.txt,
            execution_mode=request.execution_mode,
            config_variables=request.config_variables,
            cust_id=request.cust_id,
            http_request=http_request,
            orchestrator=orchestrator,
            broker=broker,
        )
    return await _assistant_message_json_response(
        session_id=request.session_id,
        content=request.txt,
        execution_mode=request.execution_mode,
        config_variables=request.config_variables,
        cust_id=request.cust_id,
        orchestrator=orchestrator,
    )


@public_router.post("/v1/task/completion", response_model=None)
async def post_task_completion(
    request: TaskCompletionRequest,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
) -> AssistantOutputResponse:
    """Advance one task's unified completion state through the fixed assistant callback API."""
    try:
        serialized_handler = getattr(orchestrator, "handle_task_completion_serialized", None)
        if callable(serialized_handler):
            serialized_response = await serialized_handler(
                session_id=request.session_id,
                task_id=request.task_id,
                completion_signal=request.completion_signal,
                serializer=lambda session: _assistant_response_envelope_for_task(session, request.task_id),
                emit_events=False,
            )
            return AssistantOutputResponse(**serialized_response)
        live_handler = getattr(orchestrator, "handle_task_completion", None)
        if callable(live_handler):
            await live_handler(
                session_id=request.session_id,
                task_id=request.task_id,
                completion_signal=request.completion_signal,
                emit_events=False,
            )
            session = _resolve_live_session(orchestrator, request.session_id)
            return AssistantOutputResponse(**_assistant_response_envelope_for_task(session, request.task_id))
        raise RuntimeError("router task completion handler is unavailable")
    except KeyError as exc:
        missing_key = str(exc.args[0]) if exc.args else ""
        if missing_key == request.session_id:
            raise RouterApiException(
                status_code=status.HTTP_404_NOT_FOUND,
                code=RouterErrorCode.ROUTER_SESSION_NOT_FOUND,
                message="Router session not found",
                details={"session_id": request.session_id},
            ) from exc
        raise RouterApiException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=RouterErrorCode.ROUTER_TASK_NOT_FOUND,
            message="Router task not found",
            details={"session_id": request.session_id, "task_id": request.task_id},
        ) from exc
    except ValueError as exc:
        raise RouterApiException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code=RouterErrorCode.ROUTER_BAD_REQUEST,
            message=str(exc),
            details={
                "session_id": request.session_id,
                "task_id": request.task_id,
                "completion_signal": request.completion_signal,
            },
        ) from exc


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
