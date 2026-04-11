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
            "我还没识别到明确意图，请补充你要办理的业务，比如查订单或取消预约。",
            payload={
                "agent": "fallback_general",
                "route_type": "fallback",
                "last_user_input": request.input,
            },
        )
