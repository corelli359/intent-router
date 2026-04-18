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
    async def handle_stream(self, request: FallbackAgentRequest) -> "AsyncIterator[str]":
        """Handle the request and yield SSE formatted events."""
        response = await self.handle(request)

        # Build the final output payload
        output_payload = {
            "event": response.event,
            "content": response.content,
            "ishandover": response.ishandover,
            "status": response.status,
            "slot_memory": response.slot_memory,
            "payload": response.payload,
        }

        # Yield the "结束" (end) node with the final result
        end_event = AgentStreamEvent.from_node_output(
            node_id="end",
            node_title="结束",
            output=output_payload,
        )
        yield end_event.to_sse(event="message")

        # Yield the done event
        yield "event:done\ndata:[DONE]\n\n"

