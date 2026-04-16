from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .support import AgentConversationContext, AgentCustomer, AgentExecutionResponse


class FallbackAgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")
    input: str
    customer: AgentCustomer = Field(default_factory=AgentCustomer)
    conversation: AgentConversationContext = Field(default_factory=AgentConversationContext)


class FallbackAgentService:
    async def handle(self, request: FallbackAgentRequest) -> AgentExecutionResponse:
        return AgentExecutionResponse.waiting(
            "当前业务已进入通用处理链路，但还没有配置专属执行能力。请补充更具体的办理诉求，或切换到人工客服。",
            payload={
                "agent": "fallback_general",
                "route_type": "fallback",
                "last_user_input": request.input,
            },
        )
