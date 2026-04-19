from __future__ import annotations

from functools import lru_cache
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse

from .support import (
    AgentCancelRequest,
    AgentCancelResponse,
    AgentExecutionResponse,
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
        """Run the transfer money agent and return SSE stream."""
        return StreamingResponse(
            service.handle_stream(request),
            media_type="text/event-stream",
        )

    @app.post("/api/agent/cancel", response_model=AgentCancelResponse)
    async def cancel_agent(request: AgentCancelRequest) -> AgentCancelResponse:
        return AgentCancelResponse(status="cancelled")

    return app


app = create_app()
