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
from router_service.core.shared.domain import TaskEvent
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphNodeState,
    GraphNodeStatus,
)
from router_service.core.graph.orchestrator import GraphRouterOrchestrator
from router_service.core.support.agent_barrier import AgentBarrierTriggeredError
from router_service.core.support.llm_client import LLMServiceUnavailableError
from router_service.core.support.json_codec import JSONDecodeError, json_dumps, json_loads


public_router = APIRouter(tags=["router"])


class MessageExecutionMode(StrEnum):
    """Execution behavior selected for one message request."""

    EXECUTE = "execute"
    ROUTER_ONLY = "router_only"


class AssistantOutputResponse(BaseModel):
    """Assistant-facing v0.5 response envelope."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    ok: bool = True
    current_task: str = ""
    task_list: list[dict[str, str]] = Field(default_factory=list)
    status: str = ""
    intent_code: str = ""
    completion_state: int = 0
    completion_reason: str = ""
    slot_memory: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, alias="errorCode")
    stage: str | None = None
    details: dict[str, Any] | None = None


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
    stream: bool = True


def _resolve_session_cust_id(
    orchestrator: GraphRouterOrchestrator,
    session_id: str,
    cust_id: str | None,
) -> str:
    """Resolve one customer id from payload first, then live in-memory session state."""
    if cust_id:
        return cust_id
    session_store = getattr(orchestrator, "session_store", None)
    getter = getattr(session_store, "get", None)
    if getter is None:
        return "cust_demo"
    try:
        return getter(session_id).cust_id
    except KeyError:
        return "cust_demo"


def _resolve_live_session(orchestrator: GraphRouterOrchestrator, session_id: str) -> object:
    """Return the live in-memory session required by the production message contract."""
    session_store = getattr(orchestrator, "session_store", None)
    getter = getattr(session_store, "get", None)
    if getter is None:
        raise RuntimeError("router live session store is unavailable")
    return getter(session_id)


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
    del node_id
    if task_id not in (None, ""):
        return str(task_id)
    if intent_code not in (None, ""):
        if position is not None:
            return f"{intent_code}#{position}"
        return str(intent_code)
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


def _assistant_agent_output(agent_output: dict[str, Any] | None) -> dict[str, Any]:
    """Return the assistant-facing nested agent output without router-owned fields."""
    normalized_output = dict(agent_output or {})
    if not normalized_output:
        return {}
    normalized_output.pop("slot_memory", None)
    return normalized_output


def _assistant_completion_fields(
    *,
    status: str,
    node_status: str | GraphNodeStatus | None,
    explicit_state: int | None = None,
    explicit_reason: str | None = None,
) -> tuple[int, str]:
    """Resolve the router-owned completion state/reason for one assistant-facing payload."""
    if isinstance(explicit_state, bool):
        explicit_state = int(explicit_state)
    if isinstance(explicit_state, int) and explicit_state in {0, 1, 2}:
        if explicit_reason not in (None, ""):
            return explicit_state, str(explicit_reason)
        if explicit_state == 2 and status == "completed":
            return 2, "assistant_final_done"
        if explicit_state == 1 and status == "waiting_assistant_completion":
            return 1, "assistant_partial_done"
        return explicit_state, "running"

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
        return 1, "assistant_confirmation_required"
    if status in {"ready_for_dispatch"} or node_status_value == GraphNodeStatus.READY_FOR_DISPATCH.value:
        return 0, "router_ready_for_dispatch"
    if status in {"running", "dispatching"} or node_status_value == GraphNodeStatus.RUNNING.value:
        return 0, "running"
    if status == "completed" or node_status_value == GraphNodeStatus.COMPLETED.value:
        return 2, "completed"
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
    intent_code: str = "",
    message: str = "",
    slot_memory: dict[str, Any] | None = None,
    task_list: list[dict[str, str]] | None = None,
    output: dict[str, Any] | None = None,
    error_code: str | RouterErrorCode | None = None,
    stage: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable assistant-facing v0.5 payload shared by JSON and SSE responses."""
    response_payload: dict[str, Any] = {
        "current_task": current_task,
        "task_list": list(task_list or []),
        "status": status,
        "intent_code": intent_code,
        "completion_state": completion_state,
        "completion_reason": completion_reason,
        "slot_memory": dict(slot_memory or {}),
        "message": message,
        "output": dict(output or {}),
    }
    if error_code is not None:
        response_payload["errorCode"] = str(error_code)
    if stage is not None:
        response_payload["stage"] = stage
    if details:
        response_payload["details"] = dict(details)
    return response_payload


def _assistant_output_from_node(
    *,
    node: GraphNodeState,
    graph: ExecutionGraphState | None,
    message: str,
) -> dict[str, Any]:
    """Build the assistant-facing v0.5 payload from live graph/node runtime objects."""
    agent_output = dict(getattr(node, "_agent_output", {}) or {})
    slot_memory = dict(node.slot_memory)
    status = _assistant_effective_status(
        node_status=node.status,
    )
    completion_override = None
    override_getter = getattr(node, "completion_override", None)
    if callable(override_getter):
        completion_override = override_getter()
    completion_state, completion_reason = _assistant_completion_fields(
        status=status,
        node_status=node.status,
        explicit_state=completion_override[0] if completion_override is not None else None,
        explicit_reason=completion_override[1] if completion_override is not None else None,
    )
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
        intent_code=node.intent_code,
        status=status,
        message=_assistant_router_placeholder_message(
            status=status,
            blocking_reason=node.blocking_reason,
            graph_status=graph.status.value if graph is not None else None,
            fallback="",
        ),
        slot_memory=slot_memory,
        output=_assistant_agent_output(agent_output),
    )


def _assistant_node_payload_priority(node_payload: dict[str, Any]) -> tuple[int, int]:
    """Rank one serialized node payload by user-relevant state, then by graph order."""
    status_priority = {
        GraphNodeStatus.WAITING_USER_INPUT.value: 0,
        GraphNodeStatus.WAITING_CONFIRMATION.value: 1,
        GraphNodeStatus.WAITING_ASSISTANT_COMPLETION.value: 2,
        GraphNodeStatus.RUNNING.value: 3,
        GraphNodeStatus.READY_FOR_DISPATCH.value: 4,
        GraphNodeStatus.DRAFT.value: 5,
        GraphNodeStatus.READY.value: 6,
        GraphNodeStatus.BLOCKED.value: 7,
        GraphNodeStatus.COMPLETED.value: 8,
        GraphNodeStatus.FAILED.value: 9,
        GraphNodeStatus.CANCELLED.value: 10,
        GraphNodeStatus.SKIPPED.value: 11,
    }
    return (
        status_priority.get(str(node_payload.get("status") or ""), 99),
        node_payload.get("position") if isinstance(node_payload.get("position"), int) else 9999,
    )


def _assistant_response_node_payload(graph_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Resolve the most relevant serialized node from one graph payload."""
    if not isinstance(graph_payload, dict):
        return None
    nodes = graph_payload.get("nodes")
    if not isinstance(nodes, list):
        return None
    node_payloads = [item for item in nodes if isinstance(item, dict)]
    if not node_payloads:
        return None
    return min(node_payloads, key=_assistant_node_payload_priority)


def _assistant_output_from_graph_payload(
    *,
    graph_payload: dict[str, Any] | None,
    message: str,
    fallback_status: str = "",
    fallback_task_id: str | None = None,
) -> dict[str, Any]:
    """Build the assistant-facing payload from one serialized graph event payload."""
    node_payload = _assistant_response_node_payload(graph_payload)
    graph_status = str(graph_payload.get("status") or "") if isinstance(graph_payload, dict) else ""
    node_status = (
        str(node_payload.get("status") or "")
        if isinstance(node_payload, dict)
        else fallback_status or graph_status
    )
    status = _assistant_effective_status(node_status=node_status)
    completion_state, completion_reason = _assistant_completion_fields(
        status=status,
        node_status=node_status,
    )
    blocking_reason = (
        str(node_payload.get("blocking_reason"))
        if isinstance(node_payload, dict) and node_payload.get("blocking_reason") not in (None, "")
        else None
    )
    intent_code = (
        str(node_payload.get("intent_code") or "")
        if isinstance(node_payload, dict)
        else ""
    )
    current_task = _assistant_task_name(
        task_id=(
            str(node_payload.get("task_id"))
            if isinstance(node_payload, dict) and node_payload.get("task_id") not in (None, "")
            else (fallback_task_id if node_payload is None else None)
        ),
        node_id=(
            str(node_payload.get("node_id"))
            if isinstance(node_payload, dict) and node_payload.get("node_id") not in (None, "")
            else None
        ),
        intent_code=intent_code,
        position=(
            node_payload.get("position")
            if isinstance(node_payload, dict) and isinstance(node_payload.get("position"), int)
            else None
        ),
    )
    slot_memory = (
        dict(node_payload.get("slot_memory") or {})
        if isinstance(node_payload, dict)
        else {}
    )
    return _assistant_output_template(
        current_task=current_task,
        task_list=_assistant_task_list_from_graph_payload(graph_payload),
        completion_state=completion_state,
        completion_reason=completion_reason,
        intent_code=intent_code,
        status=status,
        message=_assistant_router_placeholder_message(
            status=status,
            blocking_reason=blocking_reason,
            graph_status=graph_status,
            fallback=message,
        ),
        slot_memory=slot_memory,
        output={},
    )


def _assistant_output_from_recognition_event(event: TaskEvent) -> dict[str, Any] | None:
    """Build one assistant-facing SSE frame once recognition has a stable result."""
    payload = dict(event.payload or {})
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, list):
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            if item.get("code") == "RECOGNIZER_LLM_FAILED":
                return None

    primary = payload.get("primary")
    primary = primary if isinstance(primary, list) else []
    candidates = payload.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    first_primary = next((item for item in primary if isinstance(item, dict)), None)
    intent_code = str(first_primary.get("intent_code") or "") if first_primary is not None else ""
    return _assistant_output_template(
        status="running",
        intent_code=intent_code,
        completion_state=0,
        completion_reason="intent_recognized",
        message=event.message or ("意图识别完成" if intent_code else "暂未识别到明确意图"),
        output={},
        stage="intent_recognition",
        details={
            "primary": primary,
            "candidates": candidates,
        },
    )


def _assistant_output_from_event(event: TaskEvent) -> dict[str, Any] | None:
    """Translate one internal router task event into the assistant-facing v0.5 SSE payload."""
    payload = dict(event.payload or {})
    agent_output = payload.get("agent_output")
    if not isinstance(agent_output, dict):
        agent_output = {}

    allowed_events = {
        "recognition.completed",
        "graph_builder.completed",
        "graph.unrecognized",
        "graph.proposed",
        "graph.waiting_confirmation",
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

    if event.event in {"recognition.completed", "graph_builder.completed"}:
        return _assistant_output_from_recognition_event(event)

    graph_payload = payload.get("graph")
    graph_payload = graph_payload if isinstance(graph_payload, dict) else None
    node_payload = payload.get("node")
    node_payload = node_payload if isinstance(node_payload, dict) else None

    if event.event == "graph.unrecognized":
        return _assistant_output_template(
            status="unrecognized",
            completion_state=2,
            completion_reason="router_no_match",
            message=event.message or "",
        )

    if event.event.startswith("graph."):
        return _assistant_output_from_graph_payload(
            graph_payload=graph_payload,
            message=event.message or "",
            fallback_status=event.status.value,
            fallback_task_id=event.task_id if event.task_id not in (None, "", "graph") else None,
        )

    if node_payload is None:
        return None

    slot_memory = dict(node_payload.get("slot_memory") or {})
    status = _assistant_effective_status(
        node_status=str(node_payload.get("status") or ""),
    )
    intent_code = str(node_payload.get("intent_code") or event.intent_code)
    completion_state, completion_reason = _assistant_completion_fields(
        status=status,
        node_status=str(node_payload.get("status") or ""),
        explicit_state=node_payload.get("completion_state") if isinstance(node_payload.get("completion_state"), int) else None,
        explicit_reason=(
            str(node_payload.get("completion_reason"))
            if node_payload.get("completion_reason") not in (None, "")
            else None
        ),
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
        intent_code=intent_code,
        status=status,
        message=_assistant_router_placeholder_message(
            status=status,
            blocking_reason=str(node_payload.get("blocking_reason")) if node_payload.get("blocking_reason") not in (None, "") else None,
            graph_status=str(graph_payload.get("status") or "") if graph_payload is not None else None,
            fallback="",
        ),
        slot_memory=slot_memory,
        output=_assistant_agent_output(agent_output),
    )


def _assistant_output_payload(session: object) -> dict[str, Any]:
    """Build the assistant-facing v0.5 payload from live router/session state."""
    graph = _response_graph(session)
    node = _response_node(session)
    if node is None:
        message = _last_assistant_message(session)
        if message:
            return _assistant_output_template(
                status="unrecognized",
                completion_state=2,
                completion_reason="router_no_match",
                message=message,
            )
        return _assistant_output_template(status="idle")

    message = node.blocking_reason or _last_assistant_message(session)
    return _assistant_output_from_node(node=node, graph=graph, message=message)


def _assistant_response_envelope(session: object) -> dict[str, Any]:
    """Build the assistant-facing `ok + top-level fields` envelope from live router/session state."""
    return {"ok": True, **_assistant_output_payload(session)}


def _assistant_response_envelope_for_task(session: object, task_id: str) -> dict[str, Any]:
    """Build the assistant-facing envelope for one specific task id."""
    resolved = _graph_and_node_for_task(session, task_id)
    if resolved is None:
        return _assistant_response_envelope(session)
    graph, node = resolved
    return {"ok": True, **_assistant_output_from_node(node=node, graph=graph, message="")}


def _assistant_llm_unavailable_output(exc: LLMServiceUnavailableError) -> dict[str, Any]:
    """Build the assistant-facing failure payload for semantic-model outages."""
    return _assistant_output_template(
        status="failed",
        completion_state=2,
        completion_reason="router_error",
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
        message=message,
        error_code=RouterErrorCode.ROUTER_BAD_REQUEST,
    )


def _encode_done_sse() -> str:
    """Encode the assistant-facing terminal SSE frame."""
    return "event: done\ndata: [DONE]\n\n"


def _assistant_sse_payload(*, ok: bool, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap one assistant SSE business payload with the top-level ok flag."""
    return {"ok": ok, **payload}


def _assistant_response_dict(response: dict[str, Any]) -> dict[str, Any]:
    """Normalize one assistant response dict and drop absent optional fields."""
    return AssistantOutputResponse(**response).model_dump(mode="json", by_alias=True, exclude_none=True)


async def _assistant_message_json_response(
    *,
    session_id: str,
    content: str,
    execution_mode: MessageExecutionMode,
    config_variables: list[ConfigVariableInput],
    cust_id: str | None,
    orchestrator: GraphRouterOrchestrator,
) -> dict[str, Any]:
    """Process one assistant-protocol turn and always return v0.5 top-level fields without snapshots."""
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
            return _assistant_response_dict(serialized_response)
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
        return _assistant_response_dict(_assistant_response_envelope(session))
    except LLMServiceUnavailableError as exc:
        return _assistant_response_dict({"ok": False, **_assistant_llm_unavailable_output(exc)})
    except AgentBarrierTriggeredError as exc:
        return _assistant_response_dict(
            {
                "ok": False,
                **_assistant_output_template(
                    status="failed",
                    completion_state=2,
                    completion_reason="router_error",
                    message=str(exc),
                    error_code=RouterErrorCode.ROUTER_AGENT_BARRIER_TRIGGERED,
                ),
            }
        )
    except ValueError as exc:
        return _assistant_response_dict({"ok": False, **_assistant_bad_request_output(str(exc))})
    except Exception as exc:
        return _assistant_response_dict(
            {
                "ok": False,
                **_assistant_output_template(
                status="failed",
                completion_state=2,
                completion_reason="router_error",
                message=str(exc),
                error_code=RouterErrorCode.ROUTER_INTERNAL_ERROR,
            ),
            }
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
                        yield _encode_sse(
                            "message",
                            _assistant_sse_payload(ok=False, payload=_assistant_llm_unavailable_output(exc)),
                        )
                        break
                    except AgentBarrierTriggeredError as exc:
                        yield _encode_sse(
                            "message",
                            _assistant_sse_payload(
                                ok=False,
                                payload=_assistant_output_template(
                                    status="failed",
                                    completion_state=2,
                                    completion_reason="router_error",
                                    message=str(exc),
                                    error_code=RouterErrorCode.ROUTER_AGENT_BARRIER_TRIGGERED,
                                ),
                            ),
                        )
                        break
                    except ValueError as exc:
                        yield _encode_sse(
                            "message",
                            _assistant_sse_payload(ok=False, payload=_assistant_bad_request_output(str(exc))),
                        )
                        break
                    except Exception as exc:
                        yield _encode_sse(
                            "message",
                            _assistant_sse_payload(
                                ok=False,
                                payload=_assistant_output_template(
                                    status="failed",
                                    completion_state=2,
                                    completion_reason="router_error",
                                    message=str(exc),
                                    error_code=RouterErrorCode.ROUTER_INTERNAL_ERROR,
                                ),
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
                yield _encode_sse("message", _assistant_sse_payload(ok=True, payload=payload))
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


async def _assistant_task_completion_json_response(
    *,
    request: TaskCompletionRequest,
    orchestrator: GraphRouterOrchestrator,
) -> dict[str, Any]:
    """Process one assistant task-completion callback and return the non-stream v0.5 payload."""
    serialized_handler = getattr(orchestrator, "handle_task_completion_serialized", None)
    if callable(serialized_handler):
        serialized_response = await serialized_handler(
            session_id=request.session_id,
            task_id=request.task_id,
            completion_signal=request.completion_signal,
            serializer=lambda session: _assistant_response_envelope_for_task(session, request.task_id),
            emit_events=False,
        )
        return _assistant_response_dict(serialized_response)
    live_handler = getattr(orchestrator, "handle_task_completion", None)
    if callable(live_handler):
        await live_handler(
            session_id=request.session_id,
            task_id=request.task_id,
            completion_signal=request.completion_signal,
            emit_events=False,
        )
        session = _resolve_live_session(orchestrator, request.session_id)
        return _assistant_response_dict(_assistant_response_envelope_for_task(session, request.task_id))
    raise RuntimeError("router task completion handler is unavailable")


def _assistant_task_completion_stream_response(
    *,
    request: TaskCompletionRequest,
    http_request: Request,
    orchestrator: GraphRouterOrchestrator,
    broker: EventBroker,
) -> StreamingResponse:
    """Stream router events produced by an assistant task-completion callback."""

    async def event_generator():
        queue = broker.register(request.session_id)
        processing_task = asyncio.create_task(
            orchestrator.handle_task_completion(
                session_id=request.session_id,
                task_id=request.task_id,
                completion_signal=request.completion_signal,
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
                    except KeyError:
                        yield _encode_sse(
                            "message",
                            _assistant_sse_payload(
                                ok=False,
                                payload=_assistant_output_template(
                                    status="failed",
                                    completion_state=2,
                                    completion_reason="router_error",
                                    message="Router task not found",
                                    error_code=RouterErrorCode.ROUTER_TASK_NOT_FOUND,
                                    details={
                                        "session_id": request.session_id,
                                        "task_id": request.task_id,
                                    },
                                ),
                            ),
                        )
                    except ValueError as exc:
                        yield _encode_sse(
                            "message",
                            _assistant_sse_payload(ok=False, payload=_assistant_bad_request_output(str(exc))),
                        )
                    except Exception as exc:
                        yield _encode_sse(
                            "message",
                            _assistant_sse_payload(
                                ok=False,
                                payload=_assistant_output_template(
                                    status="failed",
                                    completion_state=2,
                                    completion_reason="router_error",
                                    message=str(exc),
                                    error_code=RouterErrorCode.ROUTER_INTERNAL_ERROR,
                                ),
                            ),
                        )
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                payload = _assistant_output_from_event(event)
                if payload is None:
                    continue
                yield _encode_sse("message", _assistant_sse_payload(ok=True, payload=payload))
        finally:
            broker.unregister(request.session_id, queue)
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
    http_request: Request,
    orchestrator: GraphRouterOrchestrator = Depends(get_orchestrator),
    broker: EventBroker = Depends(get_event_broker),
) -> StreamingResponse | dict[str, Any]:
    """Advance one task's completion state through the assistant callback API."""
    try:
        if request.stream:
            return _assistant_task_completion_stream_response(
                request=request,
                http_request=http_request,
                orchestrator=orchestrator,
                broker=broker,
            )
        return await _assistant_task_completion_json_response(
            request=request,
            orchestrator=orchestrator,
        )
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
