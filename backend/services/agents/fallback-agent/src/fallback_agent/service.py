from __future__ import annotations

from .support import AgentExecutionResponse, ConfigVariablesRequest


class FallbackAgentRequest(ConfigVariablesRequest):
    pass


class FallbackAgentService:
    async def handle(self, request: FallbackAgentRequest) -> AgentExecutionResponse:
        return AgentExecutionResponse.waiting(
            "当前业务已进入通用处理链路，但还没有配置专属执行能力。请补充更具体的办理诉求，或切换到人工客服。",
            payload={
                "agent": "fallback_general",
                "route_type": "fallback",
                "last_user_input": request.txt,
            },
        )
