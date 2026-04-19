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
from .service import AccountBalanceAgentRequest, AccountBalanceAgentService


@lru_cache
def get_settings() -> AgentLLMSettings:
    return AgentLLMSettings.from_env(prefix="ACCOUNT_BALANCE_AGENT", service_name="account-balance-agent")


@lru_cache
def get_service() -> AccountBalanceAgentService:
    settings = get_settings()
    resolver = LangChainJsonObjectRunner(settings) if settings.connection_ready else None
    return AccountBalanceAgentService(resolver=resolver)


def create_app() -> FastAPI:
    app = FastAPI(title="Account Balance Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_settings()
        return {
            "status": "ok",
            "service": settings.service_name,
            "llm_ready": settings.connection_ready,
        }

    @app.post("/api/agent/run")
    async def run_agent(
        request: AccountBalanceAgentRequest,
        service: AccountBalanceAgentService = Depends(get_service),
    ) -> StreamingResponse:
        """Run the agent and return SSE stream."""
        return StreamingResponse(
            service.handle_stream(request),
            media_type="text/event-stream",
        )

    @app.post("/api/agent/cancel", response_model=AgentCancelResponse)
    async def cancel_agent(request: AgentCancelRequest) -> AgentCancelResponse:
        return AgentCancelResponse(status="cancelled")

    return app


app = create_app()
