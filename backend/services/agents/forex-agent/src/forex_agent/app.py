from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, FastAPI

from .support import (
    AgentCancelRequest,
    AgentCancelResponse,
    AgentExecutionResponse,
    AgentLLMSettings,
    LangChainJsonObjectRunner,
)
from .service import ForexExchangeAgentRequest, ForexExchangeAgentService


@lru_cache
def get_forex_exchange_settings() -> AgentLLMSettings:
    return AgentLLMSettings.from_env(
        prefix="FOREX_EXCHANGE_AGENT",
        service_name="forex-exchange-agent",
    )


@lru_cache
def get_forex_exchange_service() -> ForexExchangeAgentService:
    settings = get_forex_exchange_settings()
    resolver = LangChainJsonObjectRunner(settings) if settings.connection_ready else None
    return ForexExchangeAgentService(resolver=resolver)


def create_app() -> FastAPI:
    app = FastAPI(title="Forex Exchange Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_forex_exchange_settings()
        return {
            "status": "ok",
            "service": settings.service_name,
            "llm_ready": settings.connection_ready,
        }

    @app.post("/api/agent/run", response_model=AgentExecutionResponse)
    async def run_agent(
        request: ForexExchangeAgentRequest,
        service: ForexExchangeAgentService = Depends(get_forex_exchange_service),
    ) -> AgentExecutionResponse:
        return await service.handle(request)

    @app.post("/api/agent/cancel", response_model=AgentCancelResponse)
    async def cancel_agent(request: AgentCancelRequest) -> AgentCancelResponse:
        del request
        return AgentCancelResponse(status="cancelled")

    return app


app = create_app()
