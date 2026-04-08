from __future__ import annotations

import asyncio
import sys
from pathlib import Path


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from router_api.sse.broker import EventBroker  # noqa: E402
from router_core.domain import TaskEvent, TaskStatus  # noqa: E402


def test_event_broker_emits_heartbeat_before_real_event() -> None:
    async def run() -> None:
        broker = EventBroker(heartbeat_interval_seconds=0.01, max_idle_seconds=0.2)
        stream = broker.subscribe("session_heartbeat")

        heartbeat = await stream.__anext__()
        assert heartbeat.event == "heartbeat"

        await broker.publish(
            TaskEvent(
                event="task.completed",
                task_id="task_001",
                session_id="session_heartbeat",
                intent_code="query_account_balance",
                status=TaskStatus.COMPLETED,
                message="done",
            )
        )
        event = await stream.__anext__()
        assert event.event == "task.completed"

        await stream.aclose()

    asyncio.run(run())


def test_event_broker_drops_oldest_event_when_queue_is_full() -> None:
    async def run() -> None:
        broker = EventBroker(max_queue_size=1)
        queue = broker.register("session_overflow")

        await broker.publish(
            TaskEvent(
                event="task.created",
                task_id="task_001",
                session_id="session_overflow",
                intent_code="query_account_balance",
                status=TaskStatus.CREATED,
                message="created",
            )
        )
        await broker.publish(
            TaskEvent(
                event="task.completed",
                task_id="task_001",
                session_id="session_overflow",
                intent_code="query_account_balance",
                status=TaskStatus.COMPLETED,
                message="completed",
            )
        )

        event = queue.get_nowait()
        assert event.event == "task.completed"
        broker.unregister("session_overflow", queue)

    asyncio.run(run())
