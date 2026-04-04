from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator

from router_core.domain import TaskEvent


class EventBroker:
    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[TaskEvent]]] = defaultdict(list)

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
        try:
            while True:
                yield await queue.get()
        finally:
            self.unregister(session_id, queue)
