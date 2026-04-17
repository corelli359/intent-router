from __future__ import annotations

from collections.abc import AsyncIterator

from router_service.core.shared.domain import AgentStreamChunk, Task


ROUTER_AGENT_BARRIER_ENABLED_ENV = "ROUTER_AGENT_BARRIER_ENABLED"


class AgentBarrierTriggeredError(RuntimeError):
    """Raised when perf mode blocks a call that would perform real agent I/O."""


def build_agent_barrier_error(
    *,
    intent_code: str,
    agent_url: str | None,
) -> AgentBarrierTriggeredError:
    """Build a stable, operator-friendly error for blocked agent calls."""
    return AgentBarrierTriggeredError(
        f"{ROUTER_AGENT_BARRIER_ENABLED_ENV}=true blocked a real agent call "
        f"(intent_code={intent_code}, agent_url={agent_url or 'unset'}). "
        "Perf traffic must use router_only paths and must not trigger agent I/O."
    )


def agent_barrier_triggered(exc: BaseException) -> bool:
    """Return whether the raised exception came from the perf agent barrier."""
    return isinstance(exc, AgentBarrierTriggeredError)


class BarrierAgentClient:
    """Fail-fast agent client used by perf environments to block downstream I/O."""

    async def stream(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        """Raise immediately instead of reaching a real downstream agent."""
        del user_input
        if False:  # pragma: no cover
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="blocked",
                content="blocked",
                ishandover=False,
                status=task.status,
            )
        raise build_agent_barrier_error(
            intent_code=task.intent_code,
            agent_url=task.agent_url,
        )

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        """No-op because no downstream agent task should ever be created."""
        del session_id, task_id, agent_url
        return None

    async def close(self) -> None:
        """Nothing to close for the barrier client."""
        return None
