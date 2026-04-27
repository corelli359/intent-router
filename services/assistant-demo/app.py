from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_ROUTER_BASE_URL = "http://127.0.0.1:8024"
DEFAULT_TRANSFER_AGENT_BASE_URL = "http://127.0.0.1:8031"


class AssistantTurnRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    session_id: str = Field(alias="sessionId", min_length=1)
    message: str = Field(min_length=1)
    source: str = "user"
    scenario: str = "normal"
    user_profile: dict[str, Any] = Field(default_factory=dict, alias="userProfile")
    page_context: dict[str, Any] = Field(default_factory=dict, alias="pageContext")
    push_context: dict[str, Any] = Field(default_factory=dict, alias="pushContext")
    router_base_url: str = Field(default=DEFAULT_ROUTER_BASE_URL, alias="routerBaseUrl")
    transfer_agent_base_url: str = Field(default=DEFAULT_TRANSFER_AGENT_BASE_URL, alias="transferAgentBaseUrl")

    @model_validator(mode="after")
    def normalize(self) -> "AssistantTurnRequest":
        self.message = self.message.strip()
        self.router_base_url = self.router_base_url.rstrip("/") or DEFAULT_ROUTER_BASE_URL
        self.transfer_agent_base_url = self.transfer_agent_base_url.rstrip("/") or DEFAULT_TRANSFER_AGENT_BASE_URL
        return self


@dataclass(slots=True)
class AssistantSessionState:
    session_id: str
    active_task_id: str | None = None
    active_agent: str | None = None
    turn_count: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "active_task_id": self.active_task_id,
            "active_agent": self.active_agent,
            "turn_count": self.turn_count,
            "updated_at": self.updated_at,
        }


class AssistantRuntime:
    def __init__(self) -> None:
        self._sessions: dict[str, AssistantSessionState] = {}

    async def handle_turn(self, request: AssistantTurnRequest) -> dict[str, Any]:
        state = self._state(request.session_id)
        state.turn_count += 1
        state.updated_at = time.time()
        events: list[dict[str, Any]] = [
            {
                "type": "assistant.turn_received",
                "service": "assistant-service",
                "phase": "request",
                "title": "助手服务端接收用户消息",
                "summary": "前端只调用助手服务端；助手负责把上下文交给 Router。",
                "input": {
                    "message": request.message,
                    "source": request.source,
                    "scenario": request.scenario,
                    "has_push_context": bool(request.push_context),
                },
                "output": state.to_dict(),
            }
        ]
        router_output = await self._call_router(request=request, events=events)
        self._remember_router_task(state, router_output)

        agent_output: dict[str, Any] | None = None
        if self._should_call_transfer_agent(router_output=router_output, state=state):
            agent_output = await self._call_transfer_agent(
                request=request,
                task_id=state.active_task_id or _task_id(router_output) or "",
                events=events,
            )

        visible_output = self._visible_output(router_output=router_output, agent_output=agent_output, events=events)
        self._remember_visible_result(state, visible_output)
        assistant_message = self._assistant_message(router_output=router_output, agent_output=agent_output)
        events.append(
            {
                "type": "assistant.visible_result_generated",
                "service": "assistant-service",
                "phase": "callback",
                "title": "助手生成用户可见话术",
                "summary": assistant_message,
                "input": {
                    "router_status": router_output.get("status"),
                    "agent_status": agent_output.get("status") if agent_output else None,
                },
                "output": {"assistant_message": assistant_message},
            }
        )
        visible_output["events"] = [
            *events,
            *list(visible_output.get("events") or []),
        ]
        visible_output["assistant_message"] = assistant_message
        return {
            "session_id": request.session_id,
            "status": visible_output.get("status") or router_output.get("status") or "returned",
            "assistant_message": assistant_message,
            "assistant_state": state.to_dict(),
            "router_output": router_output,
            "agent_output": agent_output,
            "output": visible_output,
            "events": events,
        }

    def _state(self, session_id: str) -> AssistantSessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = AssistantSessionState(session_id=session_id)
        return self._sessions[session_id]

    async def _call_router(
        self,
        *,
        request: AssistantTurnRequest,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        url = f"{request.router_base_url}/api/router/v4/message"
        payload = {
            "session_id": request.session_id,
            "message": request.message,
            "source": request.source,
            "user_profile": dict(request.user_profile),
            "page_context": dict(request.page_context),
            "push_context": dict(request.push_context),
        }
        events.append(
            {
                "type": "assistant.router_request",
                "service": "assistant-service",
                "phase": "request",
                "title": "助手调用 Intent ReAct 服务",
                "summary": "助手服务端把用户表达、页面上下文和推送上下文提交给意图框架；框架从 intent.md 开始执行 ReAct。",
                "artifact": {"url": url},
                "input": payload,
            }
        )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            router_output = response.json()
        events.append(
            {
                "type": "assistant.router_response",
                "service": "assistant-service",
                "phase": "callback",
                "title": "助手收到 Router 返回",
                "summary": f"Router 返回状态：{router_output.get('status')}",
                "artifact": {"url": url},
                "output": router_output,
            }
        )
        return router_output

    async def _call_transfer_agent(
        self,
        *,
        request: AssistantTurnRequest,
        task_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        url = f"{request.transfer_agent_base_url}/api/transfer-agent/turn"
        payload = {
            "session_id": request.session_id,
            "task_id": task_id,
            "message": request.message,
            "router_base_url": request.router_base_url,
        }
        events.append(
            {
                "type": "assistant.agent_request",
                "service": "assistant-service",
                "phase": "request",
                "title": "助手调用执行 Agent",
                "summary": "业务补槽、确认和执行交给独立转账 Agent。",
                "artifact": {"url": url},
                "input": payload,
            }
        )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            agent_output = response.json()
        events.append(
            {
                "type": "assistant.agent_response",
                "service": "assistant-service",
                "phase": "callback",
                "title": "助手收到 Agent 返回",
                "summary": f"Agent 返回状态：{agent_output.get('status')}",
                "artifact": {"url": url},
                "output": agent_output,
            }
        )
        return agent_output

    def _remember_router_task(self, state: AssistantSessionState, router_output: dict[str, Any]) -> None:
        task_id = _task_id(router_output)
        target_agent = router_output.get("target_agent")
        if task_id:
            state.active_task_id = task_id
        if isinstance(target_agent, str) and target_agent:
            state.active_agent = target_agent
        if router_output.get("status") == "task_updated":
            state.active_task_id = None
            state.active_agent = None

    def _remember_visible_result(self, state: AssistantSessionState, visible_output: dict[str, Any]) -> None:
        if visible_output.get("status") != "task_updated":
            return
        if (
            visible_output.get("assistant_result_status") not in {"ready_for_assistant", "agent_output_abnormal"}
            and not isinstance(visible_output.get("agent_output"), dict)
        ):
            return
        state.active_task_id = None
        state.active_agent = None

    def _should_call_transfer_agent(
        self,
        *,
        router_output: dict[str, Any],
        state: AssistantSessionState,
    ) -> bool:
        if _router_handled_by_skill_runtime(router_output):
            return False
        return bool(
            state.active_task_id
            and (
                state.active_agent == "transfer-agent"
                or router_output.get("target_agent") == "transfer-agent"
                or router_output.get("scene_id") == "transfer"
            )
            and router_output.get("status") in {"dispatched", "forwarded"}
        )

    def _visible_output(
        self,
        *,
        router_output: dict[str, Any],
        agent_output: dict[str, Any] | None,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        router_update = agent_output.get("router_update") if isinstance(agent_output, dict) else None
        if isinstance(router_update, dict):
            visible = {
                **router_update,
                "prompt_report": router_update.get("prompt_report") or router_output.get("prompt_report"),
                "tasks": router_update.get("tasks") or router_output.get("tasks") or [],
                "events": [
                    *list(router_output.get("events") or []),
                    *list(agent_output.get("events") or []),
                    *list(router_update.get("events") or []),
                ],
            }
        else:
            visible = {
                **router_output,
                "events": [
                    *list(router_output.get("events") or []),
                    *list(agent_output.get("events") if isinstance(agent_output, dict) else []),
                ],
            }
        if isinstance(agent_output, dict) and agent_output.get("flow_nodes"):
            visible["agent_flow_nodes"] = list(agent_output.get("flow_nodes") or [])
        visible["assistant_events"] = list(events)
        return visible

    def _assistant_message(
        self,
        *,
        router_output: dict[str, Any],
        agent_output: dict[str, Any] | None,
    ) -> str:
        router_update = agent_output.get("router_update") if isinstance(agent_output, dict) else None
        agent_result = router_update.get("agent_output") if isinstance(router_update, dict) else None
        transfer_result = _transfer_result(agent_result) or _transfer_result(router_output.get("agent_output"))
        if transfer_result is not None:
            if transfer_result.get("status") == "success":
                return f"转账成功，已向{transfer_result.get('recipient')}转账{transfer_result.get('amount')}元。"
            return "转账处理已返回结果，请查看详情。"
        if _router_handled_by_skill_runtime(router_output):
            response = router_output.get("response")
            if isinstance(response, str) and response:
                return response
        if isinstance(agent_output, dict) and agent_output.get("assistant_message"):
            return str(agent_output["assistant_message"])
        status = router_output.get("status")
        if status == "dispatched":
            if router_output.get("scene_id") == "transfer":
                return "已进入转账办理流程。"
            return "好的，我正在为你处理。"
        if status == "forwarded":
            return "好的，继续为你处理。"
        if status == "planned":
            return "好的，我会分开处理这几个事项。"
        if status == "no_action":
            return "好的，这次先不处理。"
        if status == "clarification_required":
            return str(router_output.get("response") or "请再补充一点信息。")
        if status == "failed":
            return "这次没有处理成功，请稍后再试。"
        return "好的，我来处理。"


def _task_id(output: dict[str, Any]) -> str | None:
    task_id = output.get("task_id")
    if isinstance(task_id, str) and task_id:
        return task_id
    tasks = output.get("tasks")
    if isinstance(tasks, list) and tasks:
        first = tasks[0] if isinstance(tasks[0], dict) else {}
        for key in ("task_id", "agent_task_id"):
            value = first.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _transfer_result(agent_output: Any) -> dict[str, Any] | None:
    if not isinstance(agent_output, dict):
        return None
    data = agent_output.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if isinstance(item, dict) and item.get("type") == "transfer_result":
            return item
    return None


def _router_handled_by_skill_runtime(router_output: dict[str, Any]) -> bool:
    return any(
        isinstance(event, dict) and event.get("type") == "skill_react_decision"
        for event in router_output.get("events") or []
    )


runtime = AssistantRuntime()

app = FastAPI(
    title="Assistant Demo Service",
    version="0.1.0",
    default_response_class=ORJSONResponse,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3010",
        "http://127.0.0.1:3010",
        "http://localhost:3011",
        "http://127.0.0.1:3011",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/assistant/turn", response_model=None)
async def post_turn(request: AssistantTurnRequest) -> dict[str, Any]:
    return await runtime.handle_turn(request)
