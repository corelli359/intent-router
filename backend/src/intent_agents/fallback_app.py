from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, FastAPI

from intent_agents.common import AgentExecutionResponse
from intent_agents.fallback_service import FallbackAgentRequest, FallbackAgentService


@lru_cache
def get_fallback_service() -> FallbackAgentService:
    return FallbackAgentService()


def create_app() -> FastAPI:
    app = FastAPI(title="Fallback Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "fallback-agent",
        }

    @app.post("/api/agent/run", response_model=AgentExecutionResponse)
    async def run_agent(
        request: FallbackAgentRequest,
        service: FallbackAgentService = Depends(get_fallback_service),
    ) -> AgentExecutionResponse:
        return await service.handle(request)

    return app


app = create_app()
