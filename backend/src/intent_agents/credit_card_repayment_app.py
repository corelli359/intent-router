from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, FastAPI

from intent_agents.common import (
    AgentCancelRequest,
    AgentCancelResponse,
    AgentExecutionResponse,
    AgentLLMSettings,
    LangChainJsonObjectRunner,
)
from intent_agents.credit_card_repayment_service import (
    CreditCardRepaymentAgentRequest,
    CreditCardRepaymentAgentService,
)


@lru_cache
def get_credit_card_repayment_settings() -> AgentLLMSettings:
    return AgentLLMSettings.from_env(
        prefix="CREDIT_CARD_REPAYMENT_AGENT",
        service_name="credit-card-repayment-agent",
    )


@lru_cache
def get_credit_card_repayment_service() -> CreditCardRepaymentAgentService:
    settings = get_credit_card_repayment_settings()
    resolver = LangChainJsonObjectRunner(settings) if settings.connection_ready else None
    return CreditCardRepaymentAgentService(resolver=resolver)


def create_app() -> FastAPI:
    app = FastAPI(title="Credit Card Repayment Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_credit_card_repayment_settings()
        return {
            "status": "ok",
            "service": settings.service_name,
            "llm_ready": settings.connection_ready,
        }

    @app.post("/api/agent/run", response_model=AgentExecutionResponse)
    async def run_agent(
        request: CreditCardRepaymentAgentRequest,
        service: CreditCardRepaymentAgentService = Depends(get_credit_card_repayment_service),
    ) -> AgentExecutionResponse:
        return await service.handle(request)

    @app.post("/api/agent/cancel", response_model=AgentCancelResponse)
    async def cancel_agent(request: AgentCancelRequest) -> AgentCancelResponse:
        del request
        return AgentCancelResponse(status="cancelled")

    return app


app = create_app()
