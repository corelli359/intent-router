from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, FastAPI

from intent_agents.common import AgentExecutionResponse, AgentLLMSettings, LangChainJsonObjectRunner
from intent_agents.order_status_service import OrderStatusAgentRequest, OrderStatusAgentService


@lru_cache
def get_order_status_settings() -> AgentLLMSettings:
    return AgentLLMSettings.from_env(prefix="ORDER_STATUS_AGENT", service_name="order-status-agent")


@lru_cache
def get_order_status_service() -> OrderStatusAgentService:
    settings = get_order_status_settings()
    resolver = LangChainJsonObjectRunner(settings) if settings.connection_ready else None
    return OrderStatusAgentService(resolver=resolver)


def create_app() -> FastAPI:
    app = FastAPI(title="Order Status Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_order_status_settings()
        return {
            "status": "ok",
            "service": settings.service_name,
            "llm_ready": settings.connection_ready,
        }

    @app.post("/api/agent/run", response_model=AgentExecutionResponse)
    async def run_agent(
        request: OrderStatusAgentRequest,
        service: OrderStatusAgentService = Depends(get_order_status_service),
    ) -> AgentExecutionResponse:
        return await service.handle(request)

    return app


app = create_app()
