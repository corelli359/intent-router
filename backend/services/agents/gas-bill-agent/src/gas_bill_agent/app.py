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
from .service import (
    GasBillPaymentAgentRequest,
    GasBillPaymentAgentService,
)


@lru_cache
def get_gas_bill_payment_settings() -> AgentLLMSettings:
    return AgentLLMSettings.from_env(
        prefix="GAS_BILL_PAYMENT_AGENT",
        service_name="gas-bill-payment-agent",
    )


@lru_cache
def get_gas_bill_payment_service() -> GasBillPaymentAgentService:
    settings = get_gas_bill_payment_settings()
    resolver = LangChainJsonObjectRunner(settings) if settings.connection_ready else None
    return GasBillPaymentAgentService(resolver=resolver)


def create_app() -> FastAPI:
    app = FastAPI(title="Gas Bill Payment Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_gas_bill_payment_settings()
        return {
            "status": "ok",
            "service": settings.service_name,
            "llm_ready": settings.connection_ready,
        }

    @app.post("/api/agent/run", response_model=AgentExecutionResponse)
    async def run_agent(
        request: GasBillPaymentAgentRequest,
        service: GasBillPaymentAgentService = Depends(get_gas_bill_payment_service),
    ) -> AgentExecutionResponse:
        return await service.handle(request)

    @app.post("/api/agent/cancel", response_model=AgentCancelResponse)
    async def cancel_agent(request: AgentCancelRequest) -> AgentCancelResponse:
        del request
        return AgentCancelResponse(status="cancelled")

    return app


app = create_app()
