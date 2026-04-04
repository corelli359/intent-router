from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator

from router_core.domain import TaskEvent, TaskStatus


class EventBroker:
    def __init__(
        self,
        *,
        heartbeat_interval_seconds: float = 15.0,
        max_idle_seconds: float = 300.0,
    ) -> None:
        self._queues: dict[str, list[asyncio.Queue[TaskEvent]]] = defaultdict(list)
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.max_idle_seconds = max_idle_seconds

    def register(self, session_id: str) -> asyncio.Queue[TaskEvent]:
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
        self._queues[session_id].append(queue)
        return queue

    def unregister(self, session_id: str, queue: asyncio.Queue[TaskEvent]) -> None:
        queues = self._queues.get(session_id)
        if queues is None:
            return
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._queues.pop(session_id, None)

    async def publish(self, event: TaskEvent) -> None:
        for queue in list(self._queues[event.session_id]):
            await queue.put(event)

    async def subscribe(self, session_id: str) -> AsyncGenerator[TaskEvent, None]:
        queue = self.register(session_id)
        last_activity = asyncio.get_running_loop().time()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=self.heartbeat_interval_seconds)
                except asyncio.TimeoutError:
                    now = asyncio.get_running_loop().time()
                    if now - last_activity >= self.max_idle_seconds:
                        break
                    yield TaskEvent(
                        event="heartbeat",
                        task_id="session",
                        session_id=session_id,
                        intent_code="session",
                        status=TaskStatus.RUNNING,
                        message="heartbeat",
                    )
                    continue

                last_activity = asyncio.get_running_loop().time()
                yield event
        finally:
            self.unregister(session_id, queue)
