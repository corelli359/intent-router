from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator

from router_service.core.shared.domain import TaskEvent, TaskStatus

logger = logging.getLogger(__name__)


class EventBroker:
    def __init__(
        self,
        *,
        heartbeat_interval_seconds: float = 15.0,
        max_idle_seconds: float = 300.0,
        max_queue_size: int = 500,
    ) -> None:
        self._queues: dict[str, list[asyncio.Queue[TaskEvent]]] = defaultdict(list)
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.max_idle_seconds = max_idle_seconds
        self._max_queue_size = max_queue_size

    def register(self, session_id: str) -> asyncio.Queue[TaskEvent]:
        queue = self._new_queue()
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
            await self._push_event(queue, event)

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

    def _new_queue(self) -> asyncio.Queue[TaskEvent]:
        if self._max_queue_size <= 0:
            return asyncio.Queue()
        return asyncio.Queue(maxsize=self._max_queue_size)

    async def _push_event(self, queue: asyncio.Queue[TaskEvent], event: TaskEvent) -> None:
        if self._max_queue_size > 0 and queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                logger.debug("event queue already drained")
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "dropping event for session %s because subscriber queue is full",
                event.session_id,
            )
