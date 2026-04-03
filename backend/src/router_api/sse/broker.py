from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator

from router_core.domain import TaskEvent


class EventBroker:
    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[TaskEvent]]] = defaultdict(list)

    async def publish(self, event: TaskEvent) -> None:
        for queue in list(self._queues[event.session_id]):
            await queue.put(event)

    async def subscribe(self, session_id: str) -> AsyncGenerator[TaskEvent, None]:
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
        self._queues[session_id].append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._queues[session_id].remove(queue)

