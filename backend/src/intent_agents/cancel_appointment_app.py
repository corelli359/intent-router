from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, FastAPI

from intent_agents.cancel_appointment_service import (
    CancelAppointmentAgentRequest,
    CancelAppointmentAgentService,
)
from intent_agents.common import AgentExecutionResponse, AgentLLMSettings, LangChainJsonObjectRunner


@lru_cache
def get_cancel_appointment_settings() -> AgentLLMSettings:
    return AgentLLMSettings.from_env(prefix="CANCEL_APPOINTMENT_AGENT", service_name="cancel-appointment-agent")


@lru_cache
def get_cancel_appointment_service() -> CancelAppointmentAgentService:
    settings = get_cancel_appointment_settings()
    resolver = LangChainJsonObjectRunner(settings) if settings.connection_ready else None
    return CancelAppointmentAgentService(resolver=resolver)


def create_app() -> FastAPI:
    app = FastAPI(title="Cancel Appointment Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_cancel_appointment_settings()
        return {
            "status": "ok",
            "service": settings.service_name,
            "llm_ready": settings.connection_ready,
        }

    @app.post("/api/agent/run", response_model=AgentExecutionResponse)
    async def run_agent(
        request: CancelAppointmentAgentRequest,
        service: CancelAppointmentAgentService = Depends(get_cancel_appointment_service),
    ) -> AgentExecutionResponse:
        return await service.handle(request)

    return app


app = create_app()
