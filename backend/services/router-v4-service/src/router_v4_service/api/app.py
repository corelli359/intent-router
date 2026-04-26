from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse

from router_v4_service.api.schemas import AgentOutputRequest, RouterV4MessageRequest
from router_v4_service.core.models import RouterV4Input
from router_v4_service.core.runtime import RouterV4Runtime


def build_runtime() -> RouterV4Runtime:
    return RouterV4Runtime()


def get_runtime(request: Request) -> RouterV4Runtime:
    runtime = getattr(request.app.state, "router_v4_runtime", None)
    if runtime is None:
        runtime = build_runtime()
        request.app.state.router_v4_runtime = runtime
    return runtime


def create_app() -> FastAPI:
    app = FastAPI(
        title="Intent Router V4 Service",
        version="0.1.0",
        default_response_class=ORJSONResponse,
    )
    app.state.router_v4_runtime = build_runtime()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/router/v4/sessions/{session_id}", response_model=None)
    async def get_session(session_id: str, http_request: Request) -> dict[str, Any]:
        runtime = get_runtime(http_request)
        return runtime.session_snapshot(session_id)

    @app.get("/api/router/v4/sessions/{session_id}/tasks/{task_id}", response_model=None)
    async def get_task(session_id: str, task_id: str, http_request: Request) -> dict[str, Any]:
        runtime = get_runtime(http_request)
        return runtime.task_snapshot(session_id, task_id)

    @app.get("/api/router/v4/sessions/{session_id}/graphs/{graph_id}", response_model=None)
    async def get_graph(session_id: str, graph_id: str, http_request: Request) -> dict[str, Any]:
        runtime = get_runtime(http_request)
        return runtime.graph_snapshot(session_id, graph_id)

    @app.post("/api/router/v4/message", response_model=None)
    async def post_message(request: RouterV4MessageRequest, http_request: Request) -> dict[str, Any]:
        runtime = get_runtime(http_request)
        output = runtime.handle_turn(
            RouterV4Input(
                session_id=request.session_id,
                message=request.message,
                user_profile=dict(request.user_profile),
                page_context=dict(request.page_context),
                agent_registry=request.agent_registry,
                source=request.source,
                push_context=dict(request.push_context),
            )
        )
        return output.to_dict()

    @app.post("/api/router/v4/agent-output", response_model=None)
    async def post_agent_output(request: AgentOutputRequest, http_request: Request) -> dict[str, Any]:
        runtime = get_runtime(http_request)
        output = runtime.handle_agent_output(
            session_id=request.session_id,
            task_id=request.task_id,
            agent_payload=request.agent_payload(),
        )
        return output.to_dict()

    return app


app = create_app()
