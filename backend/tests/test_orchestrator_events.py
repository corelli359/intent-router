from __future__ import annotations

import asyncio
import sys
from pathlib import Path


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from router_core.orchestrator import RouterOrchestrator  # noqa: E402
from router_core.agent_client import MockStreamingAgentClient  # noqa: E402
from router_core.domain import IntentDefinition  # noqa: E402


class StaticCatalog:
    def __init__(self) -> None:
        self._intents = [
            IntentDefinition(
                intent_code="query_order_status",
                name="查询订单状态",
                description="查询订单状态",
                examples=["帮我查下订单"],
                keywords=["订单", "状态"],
                agent_url="mock://query_order_status",
                dispatch_priority=100,
                primary_threshold=0.68,
                candidate_threshold=0.45,
            )
        ]

    def list_active(self) -> list[IntentDefinition]:
        return list(self._intents)

    def priorities(self) -> dict[str, int]:
        return {intent.intent_code: intent.dispatch_priority for intent in self._intents}


def test_orchestrator_publishes_recognition_then_task_events() -> None:
    async def run() -> None:
        events = []

        def publish(event) -> None:
            events.append(event)

        orchestrator = RouterOrchestrator(
            publish_event=publish,
            intent_catalog=StaticCatalog(),
            agent_client=MockStreamingAgentClient(),
        )
        session = orchestrator.create_session(cust_id="cust_demo")
        await orchestrator.handle_user_message(session.session_id, "cust_demo", "帮我查下订单")

        event_names = [event.event for event in events]
        assert "recognition.started" in event_names
        assert "recognition.completed" in event_names
        assert "task.created" in event_names
        assert "task.dispatching" in event_names
        assert "task.running" in event_names
        assert "task.waiting_user_input" in event_names
        assert event_names.index("recognition.started") < event_names.index("recognition.completed")
        assert event_names.index("recognition.completed") < event_names.index("task.created")
        assert event_names.index("task.created") < event_names.index("task.dispatching")

    asyncio.run(run())
