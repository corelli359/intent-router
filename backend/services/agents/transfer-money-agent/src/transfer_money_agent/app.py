from __future__ import annotations

import json
from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse

from .support import (
    AgentCancelRequest,
    AgentCancelResponse,
    AgentLLMSettings,
    LangChainJsonObjectRunner,
)
from .service import TransferMoneyAgentRequest, TransferMoneyAgentService


@lru_cache
def get_transfer_money_settings() -> AgentLLMSettings:
    return AgentLLMSettings.from_env(prefix="TRANSFER_MONEY_AGENT", service_name="transfer-money-agent")


@lru_cache
def get_transfer_money_service() -> TransferMoneyAgentService:
    settings = get_transfer_money_settings()
    resolver = LangChainJsonObjectRunner(settings) if settings.connection_ready else None
    return TransferMoneyAgentService(resolver=resolver)


def _sse_frame(*, event: str, data: str) -> bytes:
    return f"event:{event}\ndata:{data}\n\n".encode("utf-8")


async def _run_agent_stream(
    request: TransferMoneyAgentRequest,
    service: TransferMoneyAgentService,
) -> AsyncIterator[bytes]:
    result = await service.handle(request)
    yield _sse_frame(event="message", data=json.dumps(result.model_dump(), ensure_ascii=False))
    yield _sse_frame(event="done", data="[DONE]")


def create_app() -> FastAPI:
    app = FastAPI(title="Transfer Money Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_transfer_money_settings()
        return {
            "status": "ok",
            "service": settings.service_name,
            "llm_ready": settings.connection_ready,
        }

    @app.post("/api/agent/run")
    async def run_agent(
        request: TransferMoneyAgentRequest,
        service: TransferMoneyAgentService = Depends(get_transfer_money_service),
    ) -> StreamingResponse:
        return StreamingResponse(
            _run_agent_stream(request, service),
            media_type="text/event-stream",
        )

    @app.post("/api/agent/cancel", response_model=AgentCancelResponse)
    async def cancel_agent(request: AgentCancelRequest) -> AgentCancelResponse:
        return AgentCancelResponse(status="cancelled")

    return app


app = create_app()
