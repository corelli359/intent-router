from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, FastAPI

from intent_agents.account_balance_service import AccountBalanceAgentRequest, AccountBalanceAgentService
from intent_agents.common import AgentExecutionResponse, AgentLLMSettings, LangChainJsonObjectRunner


@lru_cache
def get_account_balance_settings() -> AgentLLMSettings:
    return AgentLLMSettings.from_env(prefix="ACCOUNT_BALANCE_AGENT", service_name="account-balance-agent")


@lru_cache
def get_account_balance_service() -> AccountBalanceAgentService:
    settings = get_account_balance_settings()
    resolver = LangChainJsonObjectRunner(settings) if settings.connection_ready else None  # noqa: F821
    return AccountBalanceAgentService(resolver=resolver)


def create_app() -> FastAPI:
    app = FastAPI(title="Account Balance Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_account_balance_settings()
        return {
            "status": "ok",
            "service": settings.service_name,
            "llm_ready": settings.connection_ready,
        }

    @app.post("/api/agent/run", response_model=AgentExecutionResponse)
    async def run_agent(
        request: AccountBalanceAgentRequest,
        service: AccountBalanceAgentService = Depends(get_account_balance_service),
    ) -> AgentExecutionResponse:
        return await service.handle(request)

    return app


app = create_app()
