from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from router_core.agent_client import AgentClient, MockStreamingAgentClient
from router_core.context_builder import ContextBuilder
from router_core.domain import (
    ChatMessage,
    CustomerMemory,
    IntentDefinition,
    LongTermMemoryEntry,
    RouterSnapshot,
    SessionState,
    Task,
    TaskEvent,
    TaskStatus,
)
from router_core.recognizer import IntentRecognizer, SimpleIntentRecognizer
from router_core.task_queue import next_runnable_task, queue_pending_tasks, waiting_task


class LongTermMemoryStore:
    def __init__(self) -> None:
        self._customers: dict[str, CustomerMemory] = {}

    def get_or_create(self, cust_id: str) -> CustomerMemory:
        if cust_id not in self._customers:
            self._customers[cust_id] = CustomerMemory(cust_id=cust_id)
        return self._customers[cust_id]

    def recall(self, cust_id: str, limit: int = 10) -> list[str]:
        memory = self.get_or_create(cust_id)
        return [entry.content for entry in memory.facts[-limit:]]

    def promote_session(self, session: SessionState) -> None:
        memory = self.get_or_create(session.cust_id)
        for message in session.messages[-5:]:
            memory.remember(
                LongTermMemoryEntry(
                    cust_id=session.cust_id,
                    memory_type="session_message",
                    content=f"{message.role}: {message.content}",
                    source_session_id=session.session_id,
                )
            )
        for task in session.tasks:
            if not task.slot_memory:
                continue
            slot_pairs = ", ".join(f"{key}={value}" for key, value in sorted(task.slot_memory.items()))
            memory.remember(
                LongTermMemoryEntry(
                    cust_id=session.cust_id,
                    memory_type="task_slot_memory",
                    content=f"{task.intent_code}: {slot_pairs}",
                    source_session_id=session.session_id,
                )
            )


class SessionStore:
    def __init__(self, long_term_memory: LongTermMemoryStore | None = None) -> None:
        self._sessions: dict[str, SessionState] = {}
        self.long_term_memory = long_term_memory or LongTermMemoryStore()

    def create(self, cust_id: str, session_id: str | None = None) -> SessionState:
        resolved_session_id = session_id or f"session_{uuid4().hex[:10]}"
        session = SessionState(session_id=resolved_session_id, cust_id=cust_id)
        self._sessions[resolved_session_id] = session
        return session

    def get(self, session_id: str) -> SessionState:
        return self._sessions[session_id]

    def get_or_create(self, session_id: str | None, cust_id: str) -> SessionState:
        if session_id is None:
            return self.create(cust_id=cust_id)
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(session_id=session_id, cust_id=cust_id)
        session = self._sessions[session_id]
        if session.cust_id != cust_id:
            session = SessionState(session_id=session_id, cust_id=cust_id)
            self._sessions[session_id] = session
        if session.is_expired():
            self.long_term_memory.promote_session(session)
            session = SessionState(session_id=session.session_id, cust_id=session.cust_id)
            self._sessions[session_id] = session
        return session


class RouterOrchestrator:
    def __init__(
        self,
        publish_event: Callable[[TaskEvent], Any],
        session_store: SessionStore | None = None,
        intent_catalog: Any | None = None,
        recognizer: IntentRecognizer | None = None,
        context_builder: ContextBuilder | None = None,
        agent_client: AgentClient | None = None,
    ) -> None:
        self.publish_event = publish_event
        self.session_store = session_store or SessionStore()
        self.intent_catalog = intent_catalog
        self.recognizer = recognizer or SimpleIntentRecognizer()
        self.context_builder = context_builder or ContextBuilder()
        self.agent_client = agent_client or MockStreamingAgentClient()
        if self.intent_catalog is None:
            from router_core.demo_intents import DEMO_INTENTS

            class _FallbackCatalog:
                def list_active(self) -> list[IntentDefinition]:
                    return list(DEMO_INTENTS)

                def priorities(self) -> dict[str, int]:
                    return {intent.intent_code: intent.dispatch_priority for intent in self.list_active()}

            self.intent_catalog = _FallbackCatalog()

    def create_session(self, cust_id: str, session_id: str | None = None) -> SessionState:
        return self.session_store.create(cust_id=cust_id, session_id=session_id)

    def snapshot(self, session_id: str) -> RouterSnapshot:
        session = self.session_store.get(session_id)
        return RouterSnapshot(
            session_id=session.session_id,
            cust_id=session.cust_id,
            messages=list(session.messages),
            tasks=list(session.tasks),
            candidate_intents=list(session.candidate_intents),
            active_task_id=session.active_task_id,
            expires_at=session.expires_at,
        )

    async def handle_user_message(self, session_id: str, cust_id: str, content: str) -> RouterSnapshot:
        session = self.session_store.get_or_create(session_id, cust_id)
        session.messages.append(ChatMessage(role="user", content=content))
        session.touch()

        current_waiting_task = waiting_task(session)
        if current_waiting_task is not None:
            current_waiting_task.touch(TaskStatus.RESUMING)
            session.active_task_id = current_waiting_task.task_id
            await self._publish(
                TaskEvent(
                    event="task.resuming",
                    task_id=current_waiting_task.task_id,
                    session_id=session.session_id,
                    intent_code=current_waiting_task.intent_code,
                    status=current_waiting_task.status,
                    message="恢复原任务执行",
                    payload={"cust_id": session.cust_id},
                )
            )
            await self._run_task(session, current_waiting_task, content)
            await self._drain_queue(session)
            return self.snapshot(session.session_id)

        long_term_memory = self.session_store.long_term_memory.recall(session.cust_id)
        context = self.context_builder.build_task_context(session, task=None, long_term_memory=long_term_memory)
        await self._publish(
            TaskEvent(
                event="recognition.started",
                task_id="recognition",
                session_id=session.session_id,
                intent_code="recognition",
                status=TaskStatus.RUNNING,
                message="开始意图识别",
                payload={"cust_id": session.cust_id},
            )
        )

        async def publish_recognition_delta(delta: str) -> None:
            if not delta:
                return
            await self._publish(
                TaskEvent(
                    event="recognition.delta",
                    task_id="recognition",
                    session_id=session.session_id,
                    intent_code="recognition",
                    status=TaskStatus.RUNNING,
                    message=delta,
                    payload={"cust_id": session.cust_id},
                )
            )

        recognition = await self.recognizer.recognize(
            message=content,
            intents=self.intent_catalog.list_active(),
            recent_messages=context["recent_messages"],
            long_term_memory=context["long_term_memory"],
            on_delta=publish_recognition_delta,
        )
        session.candidate_intents = recognition.candidates
        primary_intent_codes = [match.intent_code for match in recognition.primary]
        await self._publish(
            TaskEvent(
                event="recognition.completed",
                task_id="recognition",
                session_id=session.session_id,
                intent_code="recognition",
                status=TaskStatus.COMPLETED,
                message=(
                    f"意图识别完成: {', '.join(primary_intent_codes)}"
                    if primary_intent_codes
                    else "意图识别完成: 未命中主意图"
                ),
                payload={
                    "cust_id": session.cust_id,
                    "primary": [match.model_dump() for match in recognition.primary],
                    "candidates": [match.model_dump() for match in recognition.candidates],
                },
            )
        )

        existing_intents = {
            task.intent_code
            for task in session.tasks
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        }
        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        for match in recognition.primary:
            if match.intent_code in existing_intents:
                continue
            intent = active_intents[match.intent_code]
            task = Task(
                session_id=session.session_id,
                intent_code=intent.intent_code,
                agent_url=intent.agent_url,
                intent_name=intent.name,
                intent_description=intent.description,
                intent_examples=intent.examples,
                request_schema=intent.request_schema,
                field_mapping=intent.field_mapping,
                confidence=match.confidence,
                input_context=context,
            )
            task.touch(TaskStatus.CREATED)
            session.tasks.append(task)
            await self._publish(
                TaskEvent(
                    event="task.created",
                    task_id=task.task_id,
                    session_id=session.session_id,
                    intent_code=task.intent_code,
                    status=task.status,
                    message=f"创建任务 {task.intent_code}",
                    payload={"confidence": task.confidence, "cust_id": session.cust_id},
                )
            )

        queue_pending_tasks(session, self.intent_catalog.priorities())
        await self._publish_session_state(session, "session.recognized")
        await self._drain_queue(session)
        return self.snapshot(session.session_id)

    async def _drain_queue(self, session: SessionState) -> None:
        while True:
            next_task = next_runnable_task(session, self.intent_catalog.priorities())
            if next_task is None:
                session.active_task_id = None
                await self._publish_session_state(session, "session.idle")
                return
            session.active_task_id = next_task.task_id
            await self._run_task(session, next_task, session.messages[-1].content)
            if next_task.status == TaskStatus.WAITING_USER_INPUT:
                await self._publish_session_state(session, "session.waiting_user_input")
                return

    async def _run_task(self, session: SessionState, task: Task, user_input: str) -> None:
        task.input_context = self.context_builder.build_task_context(
            session=session,
            task=task,
            long_term_memory=self.session_store.long_term_memory.recall(session.cust_id),
        )
        task.touch(TaskStatus.DISPATCHING)
        await self._publish_task_state(task, session, "task.dispatching", "任务开始分发")

        task.touch(TaskStatus.RUNNING)
        await self._publish_task_state(task, session, "task.running", "任务执行中")

        async for chunk in self.agent_client.stream(task, user_input):
            task.touch(chunk.status)
            event_name = {
                TaskStatus.WAITING_USER_INPUT: "task.waiting_user_input",
                TaskStatus.COMPLETED: "task.completed",
                TaskStatus.FAILED: "task.failed",
            }.get(chunk.status, "task.message")
            await self._publish(
                TaskEvent(
                    event=event_name,
                    task_id=task.task_id,
                    session_id=session.session_id,
                    intent_code=task.intent_code,
                    status=task.status,
                    message=chunk.content,
                    ishandover=chunk.ishandover,
                    payload={**chunk.payload, "cust_id": session.cust_id},
                )
            )
            if chunk.status == TaskStatus.COMPLETED and task.slot_memory:
                slot_pairs = ", ".join(f"{key}={value}" for key, value in sorted(task.slot_memory.items()))
                self.session_store.long_term_memory.get_or_create(session.cust_id).remember(
                    LongTermMemoryEntry(
                        cust_id=session.cust_id,
                        memory_type="task_completion",
                        content=f"{task.intent_code}: {slot_pairs}",
                        source_session_id=session.session_id,
                    )
                )
            if chunk.status in {TaskStatus.WAITING_USER_INPUT, TaskStatus.COMPLETED, TaskStatus.FAILED}:
                break

    async def _publish_task_state(self, task: Task, session: SessionState, event: str, message: str) -> None:
        await self._publish(
            TaskEvent(
                event=event,
                task_id=task.task_id,
                session_id=session.session_id,
                intent_code=task.intent_code,
                status=task.status,
                message=message,
                payload={"cust_id": session.cust_id},
            )
        )

    async def _publish_session_state(self, session: SessionState, event: str) -> None:
        payload = {
            "cust_id": session.cust_id,
            "active_task_id": session.active_task_id,
            "queued_task_ids": [task.task_id for task in session.tasks if task.status == TaskStatus.QUEUED],
            "candidate_intents": [match.model_dump() for match in session.candidate_intents],
            "expires_at": session.expires_at.isoformat(),
        }
        await self._publish(
            TaskEvent(
                event=event,
                task_id="session",
                session_id=session.session_id,
                intent_code="session",
                status=TaskStatus.RUNNING if session.active_task_id else TaskStatus.COMPLETED,
                message="会话状态更新",
                payload=payload,
            )
        )

    async def _publish(self, event: TaskEvent) -> None:
        result = self.publish_event(event)
        if result is not None and hasattr(result, "__await__"):
            await result
