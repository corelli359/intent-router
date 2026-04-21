from __future__ import annotations

import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class ConfigVariable(BaseModel):
    """One opaque config variable item forwarded to the router."""

    name: str
    value: Any = ""


class AssistantRunRequest(BaseModel):
    """Minimal assistant-to-router non-stream forwarding contract."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    txt: str = ""
    config_variables: list[ConfigVariable] = Field(default_factory=list)
    execution_mode: str = Field(default="execute", alias="executionMode")
    cust_id: str | None = Field(default=None, alias="custId")


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

    async def run(self, request: AssistantRunRequest) -> dict[str, Any]:
        """Forward one assistant request into the router message endpoint."""
        payload: dict[str, Any] = {
            "txt": request.txt,
            "config_variables": [item.model_dump(mode="json") for item in request.config_variables],
            "executionMode": request.execution_mode,
        }
        if request.cust_id:
            payload["cust_id"] = request.cust_id

        response = await self.http_client.post(
            f"{self.router_base_url}/api/router/v2/sessions/{request.session_id}/messages",
            json=payload,
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

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "service": "assistant-service"}

    @app.post("/api/assistant/run")
    async def run_assistant(
        request: AssistantRunRequest,
        service: RouterForwardService = Depends(get_router_forward_service),
    ) -> dict[str, Any]:
        return await service.run(request)

    return app


app = create_app()
