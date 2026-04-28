from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse


class ConfigVariable(BaseModel):
    """One opaque config variable item forwarded to the router."""

    name: str
    value: Any = ""


class AssistantRunRequest(BaseModel):
    """Minimal assistant-to-router forwarding contract."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    txt: str = ""
    config_variables: list[ConfigVariable] = Field(default_factory=list)
    execution_mode: str = Field(default="execute", alias="executionMode")
    cust_id: str | None = Field(default=None, alias="custId")


class AssistantTaskCompletionRequest(BaseModel):
    """Assistant-to-router task completion confirmation contract."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")
    completion_signal: int = Field(alias="completionSignal", ge=1, le=2)


class RouterForwardService:
    """Thin HTTP forwarder from the assistant layer into the router."""

    def __init__(
        self,
        *,
        router_base_url: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.router_base_url = router_base_url.rstrip("/")
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    def _router_payload(self, request: AssistantRunRequest, *, stream: bool) -> dict[str, Any]:
        """Build the shared router payload while selecting the router-side response mode."""
        payload = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload["stream"] = stream
        return payload

    def _router_task_completion_payload(
        self,
        request: AssistantTaskCompletionRequest,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """Build one router completion-callback payload while selecting response mode."""
        payload = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload["stream"] = stream
        return payload

    async def run(self, request: AssistantRunRequest) -> dict[str, Any]:
        """Forward one assistant request into the router message endpoint."""
        response = await self.http_client.post(
            f"{self.router_base_url}/api/v1/message",
            json=self._router_payload(request, stream=False),
        )
        try:
            body = response.json()
        except ValueError:
            body = {
                "ok": False,
                "error": response.text,
            }
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=body)
        if not isinstance(body, dict):
            raise HTTPException(status_code=502, detail="router response must be a JSON object")
        return body

    async def stream(self, request: AssistantRunRequest) -> httpx.Response:
        """Open one streaming assistant request against the router SSE endpoint."""
        response = await self.http_client.send(
            self.http_client.build_request(
                "POST",
                f"{self.router_base_url}/api/v1/message",
                json=self._router_payload(request, stream=True),
                headers={"Accept": "text/event-stream"},
            ),
            stream=True,
        )
        if response.status_code >= 400:
            raw_body = await response.aread()
            await response.aclose()
            try:
                body: Any = response.json()
            except ValueError:
                body = raw_body.decode("utf-8", errors="replace")
            raise HTTPException(status_code=response.status_code, detail=body)
        return response

    async def complete_task(self, request: AssistantTaskCompletionRequest) -> dict[str, Any]:
        """Forward one assistant completion callback into the router completion endpoint."""
        response = await self.http_client.post(
            f"{self.router_base_url}/api/v1/task/completion",
            json=self._router_task_completion_payload(request, stream=False),
        )
        try:
            body = response.json()
        except ValueError:
            body = {
                "ok": False,
                "error": response.text,
            }
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=body)
        if not isinstance(body, dict):
            raise HTTPException(status_code=502, detail="router response must be a JSON object")
        return body

    async def complete_task_stream(self, request: AssistantTaskCompletionRequest) -> httpx.Response:
        """Open one streaming assistant completion callback against the router SSE endpoint."""
        response = await self.http_client.send(
            self.http_client.build_request(
                "POST",
                f"{self.router_base_url}/api/v1/task/completion",
                json=self._router_task_completion_payload(request, stream=True),
                headers={"Accept": "text/event-stream"},
            ),
            stream=True,
        )
        if response.status_code >= 400:
            raw_body = await response.aread()
            await response.aclose()
            try:
                body: Any = response.json()
            except ValueError:
                body = raw_body.decode("utf-8", errors="replace")
            raise HTTPException(status_code=response.status_code, detail=body)
        return response

    async def close(self) -> None:
        """Release the owned HTTP client when this service created it."""
        if self._owns_http_client:
            await self.http_client.aclose()


@lru_cache
def get_router_forward_service() -> RouterForwardService:
    router_base_url = os.getenv("ASSISTANT_ROUTER_BASE_URL", "http://127.0.0.1:8000")
    return RouterForwardService(router_base_url=router_base_url)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    try:
        yield
    finally:
        await get_router_forward_service().close()
        get_router_forward_service.cache_clear()


def create_app() -> FastAPI:
    app = FastAPI(title="Assistant Service", version="0.1.0", lifespan=_lifespan)

    def _stream_response_headers(response: httpx.Response) -> dict[str, str]:
        """Forward the router SSE headers needed by downstream clients."""
        allowed = {"cache-control", "connection", "x-accel-buffering"}
        return {name: value for name, value in response.headers.items() if name.lower() in allowed}

    async def _proxy_stream(response: httpx.Response) -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await response.aclose()

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "service": "assistant-service"}

    @app.post("/api/assistant/run")
    async def run_assistant(
        request: AssistantRunRequest,
        service: RouterForwardService = Depends(get_router_forward_service),
    ) -> dict[str, Any]:
        return await service.run(request)

    @app.post("/api/assistant/run/stream")
    async def run_assistant_stream(
        request: AssistantRunRequest,
        service: RouterForwardService = Depends(get_router_forward_service),
    ) -> StreamingResponse:
        response = await service.stream(request)
        media_type = response.headers.get("content-type", "text/event-stream").split(";", maxsplit=1)[0]
        return StreamingResponse(
            _proxy_stream(response),
            status_code=response.status_code,
            media_type=media_type,
            headers=_stream_response_headers(response),
        )

    @app.post("/api/assistant/task/completion")
    async def complete_assistant_task(
        request: AssistantTaskCompletionRequest,
        service: RouterForwardService = Depends(get_router_forward_service),
    ) -> dict[str, Any]:
        return await service.complete_task(request)

    @app.post("/api/assistant/task/completion/stream")
    async def complete_assistant_task_stream(
        request: AssistantTaskCompletionRequest,
        service: RouterForwardService = Depends(get_router_forward_service),
    ) -> StreamingResponse:
        response = await service.complete_task_stream(request)
        media_type = response.headers.get("content-type", "text/event-stream").split(";", maxsplit=1)[0]
        return StreamingResponse(
            _proxy_stream(response),
            status_code=response.status_code,
            media_type=media_type,
            headers=_stream_response_headers(response),
        )

    return app


app = create_app()
