from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import re
from typing import Any
from uuid import uuid4

from router_core.agent_client import AgentClient, StreamingAgentClient
from router_core.context_builder import ContextBuilder
from router_core.domain import (
    ChatMessage,
    CustomerMemory,
    IntentDefinition,
    IntentMatch,
    LongTermMemoryEntry,
    RouterSnapshot,
    SessionPlan,
    SessionPlanItem,
    SessionPlanItemStatus,
    SessionPlanStatus,
    SessionState,
    Task,
    TaskEvent,
    TaskStatus,
    utc_now,
)
from router_core.recognizer import IntentRecognizer, SimpleIntentRecognizer
from router_core.task_queue import next_runnable_task, queue_pending_tasks


logger = logging.getLogger(__name__)

FAST_CANCEL_TERMS = ("取消", "算了", "不需要了", "不用了", "别了", "停止")
SWITCH_INTENT_TERMS = ("改成", "改为", "换成", "换为", "不要", "不查了", "不转了", "先别", "别")
CARD_NUMBER_RE = re.compile(r"\b\d{12,19}\b")
PHONE_LAST4_RE = re.compile(r"(?:后4位|后四位|尾号)\D*\d{4}")
FOUR_DIGITS_ONLY_RE = re.compile(r"^\D*(\d{4})\D*$")
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
AMOUNT_LABEL_RE = re.compile(r"(?:转账金额|金额)\D*(\d+(?:\.\d+)?)")
NAME_CUE_RE = re.compile(r"(?:给|向|转给|转账给)([\u4e00-\u9fffA-Za-z]{2,16})")


@dataclass(slots=True)
class RouterOrchestratorConfig:
    intent_switch_threshold: float = 0.80
    agent_timeout_seconds: float = 60.0


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
        config: RouterOrchestratorConfig | None = None,
    ) -> None:
        self.publish_event = publish_event
        self.session_store = session_store or SessionStore()
        self.intent_catalog = intent_catalog
        self.recognizer = recognizer or SimpleIntentRecognizer()
        self.context_builder = context_builder or ContextBuilder()
        self.agent_client = agent_client or StreamingAgentClient()
        self.config = config or RouterOrchestratorConfig()
        if self.intent_catalog is None:
            class _FallbackCatalog:
                def list_active(self) -> list[IntentDefinition]:
                    return []

                def get_fallback_intent(self) -> IntentDefinition | None:
                    return None

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
            pending_plan=session.pending_plan.model_copy(deep=True) if session.pending_plan is not None else None,
            active_task_id=session.active_task_id,
            expires_at=session.expires_at,
        )

    async def handle_user_message(self, session_id: str, cust_id: str, content: str) -> RouterSnapshot:
        session = self.session_store.get_or_create(session_id, cust_id)
        session.messages.append(ChatMessage(role="user", content=content))
        session.touch()

        if (
            session.pending_plan is not None
            and session.pending_plan.status == SessionPlanStatus.WAITING_CONFIRMATION
        ):
            if self._is_plan_confirm_message(content):
                await self._confirm_pending_plan(session, task_id="session", confirm_token=None)
                return self.snapshot(session.session_id)
            if self._contains_fast_cancel(content):
                await self._cancel_pending_plan(session, task_id="session", confirm_token=None)
                return self.snapshot(session.session_id)
            await self._publish_plan_waiting_hint(session)
            return self.snapshot(session.session_id)

        current_waiting_task = self._get_waiting_task(session)
        if current_waiting_task is not None:
            if await self._maybe_switch_waiting_task(session, current_waiting_task, content):
                return self.snapshot(session.session_id)
            await self._resume_waiting_task(session, current_waiting_task, content)
            return self.snapshot(session.session_id)

        await self._route_new_message(session, content)
        return self.snapshot(session.session_id)

    def _get_waiting_task(self, session: SessionState) -> Task | None:
        waiting = [
            task
            for task in session.tasks
            if task.status in {TaskStatus.WAITING_USER_INPUT, TaskStatus.WAITING_CONFIRMATION}
        ]
        if not waiting:
            return None
        waiting.sort(key=lambda task: task.updated_at, reverse=True)
        return waiting[0]

    async def _drain_queue(self, session: SessionState, user_input: str) -> None:
        while True:
            next_task = next_runnable_task(session, self.intent_catalog.priorities())
            if next_task is None:
                session.active_task_id = None
                await self._publish_session_state(session, "session.idle")
                return
            session.active_task_id = next_task.task_id
            await self._run_task(session, next_task, user_input)
            if next_task.status == TaskStatus.COMPLETED:
                continue
            if next_task.status == TaskStatus.FAILED:
                logger.warning(
                    "Task %s (%s) failed; continuing queue drain for session %s",
                    next_task.task_id,
                    next_task.intent_code,
                    session.session_id,
                )
                continue
            if next_task.status == TaskStatus.CANCELLED:
                logger.info(
                    "Task %s (%s) was cancelled; continuing queue drain for session %s",
                    next_task.task_id,
                    next_task.intent_code,
                    session.session_id,
                )
                continue
            if next_task.status in {TaskStatus.WAITING_USER_INPUT, TaskStatus.WAITING_CONFIRMATION}:
                await self._publish_session_state(
                    session,
                    "session.waiting_confirmation"
                    if next_task.status == TaskStatus.WAITING_CONFIRMATION
                    else "session.waiting_user_input",
                )
                return
            logger.warning(
                "Task %s (%s) ended _run_task with unexpected status %s; stopping queue drain",
                next_task.task_id,
                next_task.intent_code,
                next_task.status,
            )
            return

    async def _run_task(self, session: SessionState, task: Task, user_input: str) -> None:
        effective_user_input = user_input
        initial_source_input = task.input_context.get("initial_source_input")
        if not (isinstance(initial_source_input, str) and initial_source_input):
            previous_source_input = task.input_context.get("source_input")
            if isinstance(previous_source_input, str) and previous_source_input:
                initial_source_input = previous_source_input
        task.input_context = self.context_builder.build_task_context(
            session=session,
            task=task,
            long_term_memory=self.session_store.long_term_memory.recall(session.cust_id),
        )
        if isinstance(initial_source_input, str) and initial_source_input:
            task.input_context["initial_source_input"] = initial_source_input
        task.input_context["source_input"] = effective_user_input
        task.touch(TaskStatus.DISPATCHING)
        await self._publish_task_state(task, session, "task.dispatching", "任务开始分发")

        task.touch(TaskStatus.RUNNING)
        await self._publish_task_state(task, session, "task.running", "任务执行中")

        try:
            async with asyncio.timeout(self.config.agent_timeout_seconds):
                async for chunk in self.agent_client.stream(task, effective_user_input):
                    await self._handle_agent_chunk(session, task, chunk)
                    if chunk.status in {
                        TaskStatus.WAITING_USER_INPUT,
                        TaskStatus.WAITING_CONFIRMATION,
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                    }:
                        break
        except TimeoutError:
            timeout_message = (
                f"任务执行超时（{self.config.agent_timeout_seconds:.0f}s），"
                "已自动终止，请稍后重试"
            )
            await self._fail_task(
                session,
                task,
                timeout_message,
                payload={"timeout_seconds": self.config.agent_timeout_seconds},
            )

    async def _create_task(
        self,
        session: SessionState,
        context: dict[str, Any],
        *,
        intent: IntentDefinition,
        confidence: float,
        is_fallback: bool,
    ) -> Task:
        task_context = dict(context)
        source_input = task_context.get("source_input")
        if (
            "initial_source_input" not in task_context
            and isinstance(source_input, str)
            and source_input
        ):
            task_context["initial_source_input"] = source_input
        task = Task(
            session_id=session.session_id,
            intent_code=intent.intent_code,
            agent_url=intent.agent_url,
            intent_name=intent.name,
            intent_description=intent.description,
            intent_examples=intent.examples,
            request_schema=intent.request_schema,
            field_mapping=intent.field_mapping,
            confidence=confidence,
            input_context=task_context,
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
                payload={
                    "confidence": task.confidence,
                    "cust_id": session.cust_id,
                    "is_fallback": is_fallback,
                },
            )
        )
        return task

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
        await self._emit_plan_progress_if_needed(session)

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

    def _fallback_intent(self) -> IntentDefinition | None:
        getter = getattr(self.intent_catalog, "get_fallback_intent", None)
        if getter is None:
            return None
        return getter()

    async def cancel_waiting_tasks(self, session_id: str, reason: str = "SSE connection closed") -> None:
        try:
            session = self.session_store.get(session_id)
        except KeyError:
            return

        waiting_tasks = [
            task
            for task in session.tasks
            if task.status in {TaskStatus.WAITING_USER_INPUT, TaskStatus.WAITING_CONFIRMATION}
        ]
        for task in waiting_tasks:
            await self._cancel_task(session, task, reason=reason, notify_agent=True)

        if waiting_tasks:
            session.active_task_id = None
            await self._publish_session_state(session, "session.idle")

    async def _route_new_message(self, session: SessionState, content: str) -> None:
        context = self._build_session_context(session)
        context["source_input"] = content
        recognition = await self._recognize_message(
            session,
            content,
            recent_messages=context["recent_messages"],
            long_term_memory=context["long_term_memory"],
            emit_events=True,
        )
        should_run_queue = await self._dispatch_recognition(
            session,
            content,
            context=context,
            recognition=recognition,
            emit_recognition_completed=True,
        )
        if should_run_queue:
            await self._drain_queue(session, content)

    async def _resume_waiting_task(self, session: SessionState, task: Task, content: str) -> None:
        self._prepare_resuming_task(task, content)
        task.touch(TaskStatus.RESUMING)
        session.active_task_id = task.task_id
        await self._publish(
            TaskEvent(
                event="task.resuming",
                task_id=task.task_id,
                session_id=session.session_id,
                intent_code=task.intent_code,
                status=task.status,
                message="恢复原任务执行",
                payload={"cust_id": session.cust_id},
            )
        )
        await self._run_task(session, task, content)
        await self._drain_queue(session, content)

    async def _maybe_switch_waiting_task(
        self,
        session: SessionState,
        waiting_task: Task,
        content: str,
    ) -> bool:
        recognition_task = asyncio.create_task(
            self._recognize_message(
                session,
                content,
                recent_messages=[],
                long_term_memory=[],
                emit_events=False,
            )
        )
        recognition = await recognition_task
        fast_cancel = self._contains_fast_cancel(content)
        switch_match = self._intent_switch_match(waiting_task, recognition)
        if switch_match is not None and self._looks_like_slot_supplement(content) and not self._contains_explicit_switch_intent(content):
            return False

        if not fast_cancel and switch_match is None:
            return False
        if switch_match is not None:
            self._promote_switch_match(recognition, switch_match.intent_code)

        reason = (
            f"检测到用户切换意图至 {switch_match.intent_code}"
            if switch_match is not None
            else "用户取消当前待处理任务"
        )
        await self._cancel_waiting_and_queued_tasks(session, reason)

        if switch_match is None and self._is_pure_cancel_message(content):
            return True

        context = self._build_session_context(session)
        context["source_input"] = content
        should_run_queue = await self._dispatch_recognition(
            session,
            content,
            context=context,
            recognition=recognition,
            emit_recognition_completed=False,
        )
        if should_run_queue:
            await self._drain_queue(session, content)
        return True

    async def _recognize_message(
        self,
        session: SessionState,
        content: str,
        *,
        recent_messages: list[str],
        long_term_memory: list[str],
        emit_events: bool,
    ) -> Any:
        if emit_events:
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
            if not emit_events or not delta:
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

        return await self.recognizer.recognize(
            message=content,
            intents=self.intent_catalog.list_active(),
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            on_delta=publish_recognition_delta if emit_events else None,
        )

    async def _dispatch_recognition(
        self,
        session: SessionState,
        content: str,
        *,
        context: dict[str, Any],
        recognition: Any,
        emit_recognition_completed: bool,
    ) -> bool:
        session.candidate_intents = recognition.candidates
        existing_intents = {
            task.intent_code
            for task in session.tasks
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        }
        fallback_intent = self._fallback_intent()
        should_dispatch_fallback = (
            not recognition.primary
            and fallback_intent is not None
            and fallback_intent.intent_code not in existing_intents
        )
        primary_intent_codes = [match.intent_code for match in recognition.primary]
        if emit_recognition_completed:
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
                        else (
                            f"意图识别完成: 未命中主意图，转入兜底意图 {fallback_intent.intent_code}"
                            if should_dispatch_fallback and fallback_intent is not None
                            else "意图识别完成: 未命中主意图"
                        )
                    ),
                    payload={
                        "cust_id": session.cust_id,
                        "primary": [match.model_dump() for match in recognition.primary],
                        "candidates": [match.model_dump() for match in recognition.candidates],
                        "fallback_intent_code": (
                            fallback_intent.intent_code
                            if should_dispatch_fallback and fallback_intent is not None
                            else None
                        ),
                    },
                )
            )

        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        planned_matches = [
            match
            for match in recognition.primary
            if match.intent_code not in existing_intents and match.intent_code in active_intents
        ]
        if len(planned_matches) > 1:
            await self._propose_plan(
                session=session,
                content=content,
                matches=planned_matches,
                intents_by_code=active_intents,
            )
            return False

        for match in recognition.primary:
            if match.intent_code in existing_intents:
                continue
            intent = active_intents.get(match.intent_code)
            if intent is None:
                continue
            await self._create_task(session, context, intent=intent, confidence=match.confidence, is_fallback=False)

        if should_dispatch_fallback and fallback_intent is not None:
            await self._create_task(session, context, intent=fallback_intent, confidence=0.0, is_fallback=True)

        queue_pending_tasks(session, self.intent_catalog.priorities())
        await self._publish_session_state(session, "session.recognized")
        return True

    async def _handle_agent_chunk(self, session: SessionState, task: Task, chunk: Any) -> None:
        task.touch(chunk.status)
        event_name = {
            TaskStatus.WAITING_USER_INPUT: "task.waiting_user_input",
            TaskStatus.WAITING_CONFIRMATION: "task.waiting_confirmation",
            TaskStatus.COMPLETED: "task.completed",
            TaskStatus.FAILED: "task.failed",
        }.get(chunk.status, "task.message")
        event_time = utc_now()
        task_event = TaskEvent(
            event=event_name,
            task_id=task.task_id,
            session_id=session.session_id,
            intent_code=task.intent_code,
            status=task.status,
            message=chunk.content,
            ishandover=chunk.ishandover,
            payload=self._normalize_interaction_payload({**chunk.payload, "cust_id": session.cust_id}, source="agent"),
            created_at=event_time,
        )
        if chunk.status in {
            TaskStatus.WAITING_USER_INPUT,
            TaskStatus.WAITING_CONFIRMATION,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        } and chunk.content:
            session.messages.append(ChatMessage(role="assistant", content=chunk.content, created_at=event_time))
            session.touch()
        await self._publish(task_event)
        await self._emit_plan_progress_if_needed(session)
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

    async def _fail_task(
        self,
        session: SessionState,
        task: Task,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        task.touch(TaskStatus.FAILED)
        event_time = utc_now()
        session.messages.append(ChatMessage(role="assistant", content=message, created_at=event_time))
        session.touch()
        await self._publish(
            TaskEvent(
                event="task.failed",
                task_id=task.task_id,
                session_id=session.session_id,
                intent_code=task.intent_code,
                status=task.status,
                message=message,
                ishandover=True,
                payload={**(payload or {}), "cust_id": session.cust_id},
                created_at=event_time,
            )
        )
        await self._emit_plan_progress_if_needed(session)

    async def _cancel_waiting_and_queued_tasks(self, session: SessionState, reason: str) -> None:
        waiting_tasks = [
            task
            for task in session.tasks
            if task.status in {TaskStatus.WAITING_USER_INPUT, TaskStatus.WAITING_CONFIRMATION}
        ]
        queued_tasks = [task for task in session.tasks if task.status == TaskStatus.QUEUED]

        for task in waiting_tasks:
            await self._cancel_task(session, task, reason=reason, notify_agent=True)
        for task in queued_tasks:
            await self._cancel_task(session, task, reason=reason, notify_agent=False)

        session.active_task_id = None
        await self._publish_session_state(session, "session.idle")

    async def _cancel_task(
        self,
        session: SessionState,
        task: Task,
        *,
        reason: str,
        notify_agent: bool,
    ) -> None:
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return

        if notify_agent:
            try:
                await self.agent_client.cancel(session.session_id, task.task_id, task.agent_url)
            except Exception as exc:
                logger.warning(
                    "Failed to notify agent cancellation for task %s (%s): %s",
                    task.task_id,
                    task.intent_code,
                    exc,
                )

        task.touch(TaskStatus.CANCELLED)
        await self._publish(
            TaskEvent(
                event="task.cancelled",
                task_id=task.task_id,
                session_id=session.session_id,
                intent_code=task.intent_code,
                status=task.status,
                message=reason,
                payload={"cust_id": session.cust_id, "reason": reason},
            )
        )
        await self._emit_plan_progress_if_needed(session)

    def _build_session_context(self, session: SessionState, task: Task | None = None) -> dict[str, Any]:
        long_term_memory = self.session_store.long_term_memory.recall(session.cust_id)
        return self.context_builder.build_task_context(session, task=task, long_term_memory=long_term_memory)

    def _intent_switch_match(self, waiting_task: Task, recognition: Any) -> Any | None:
        switch_candidates = [
            match
            for match in [*recognition.primary, *recognition.candidates]
            if match.intent_code != waiting_task.intent_code
            and match.confidence >= self.config.intent_switch_threshold
        ]
        if not switch_candidates:
            return None
        switch_candidates.sort(key=lambda match: match.confidence, reverse=True)
        return switch_candidates[0]

    def _promote_switch_match(self, recognition: Any, intent_code: str) -> None:
        matching_candidate = next(
            (match for match in recognition.candidates if match.intent_code == intent_code),
            None,
        )
        if matching_candidate is None:
            return
        recognition.candidates = [
            match for match in recognition.candidates if match.intent_code != intent_code
        ]
        recognition.primary = [matching_candidate, *recognition.primary]

    def _prepare_resuming_task(self, task: Task, user_input: str) -> None:
        conflicting_keys = self._conflicting_slot_keys(task, user_input)
        for key in conflicting_keys:
            task.slot_memory.pop(key, None)

    def _conflicting_slot_keys(self, task: Task, user_input: str) -> set[str]:
        if not task.slot_memory:
            return set()

        has_name = NAME_CUE_RE.search(user_input) is not None
        has_card = CARD_NUMBER_RE.search(user_input) is not None
        has_phone = PHONE_LAST4_RE.search(user_input) is not None or FOUR_DIGITS_ONLY_RE.match(user_input.strip()) is not None
        has_amount = (
            AMOUNT_RE.search(user_input) is not None
            or AMOUNT_LABEL_RE.search(user_input) is not None
            or (
                any("amount" in key.lower() for key in task.slot_memory)
                and user_input.strip().isdigit()
                and len(user_input.strip()) != 4
            )
        )

        keys_to_clear: set[str] = set()
        for key in task.slot_memory:
            lowered = key.lower()
            if has_name and "name" in lowered:
                keys_to_clear.add(key)
                continue
            if has_card and ("card" in lowered or "account" in lowered):
                keys_to_clear.add(key)
                continue
            if has_phone and ("phone" in lowered or "mobile" in lowered):
                keys_to_clear.add(key)
                continue
            if has_amount and ("amount" in lowered or "money" in lowered):
                keys_to_clear.add(key)
        return keys_to_clear

    def _contains_fast_cancel(self, content: str) -> bool:
        normalized = re.sub(r"\s+", "", content)
        return any(term in normalized for term in FAST_CANCEL_TERMS)

    def _is_pure_cancel_message(self, content: str) -> bool:
        normalized = re.sub(r"[\s，,。.!！？、；;]", "", content)
        for term in FAST_CANCEL_TERMS:
            normalized = normalized.replace(term, "")
        return normalized == ""

    def _contains_explicit_switch_intent(self, content: str) -> bool:
        normalized = re.sub(r"\s+", "", content)
        return any(term in normalized for term in SWITCH_INTENT_TERMS)

    def _looks_like_slot_supplement(self, content: str) -> bool:
        normalized = content.strip()
        if not normalized:
            return False
        if CARD_NUMBER_RE.search(normalized):
            return True
        if PHONE_LAST4_RE.search(normalized) or FOUR_DIGITS_ONLY_RE.match(normalized):
            return True
        if AMOUNT_RE.search(normalized) or AMOUNT_LABEL_RE.search(normalized):
            return True
        if NAME_CUE_RE.search(normalized):
            return True
        return False

    async def _propose_plan(
        self,
        *,
        session: SessionState,
        content: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
    ) -> None:
        items: list[SessionPlanItem] = []
        for match in matches:
            intent = intents_by_code[match.intent_code]
            items.append(
                SessionPlanItem(
                    intent_code=intent.intent_code,
                    title=intent.name,
                    confidence=match.confidence,
                )
            )
        plan = SessionPlan(
            source_message=content,
            items=items,
            summary=f"识别到 {len(items)} 个事项，请确认后开始执行",
        )
        session.pending_plan = plan
        session.active_task_id = None
        plan.touch(SessionPlanStatus.WAITING_CONFIRMATION)
        await self._publish(
            TaskEvent(
                event="session.plan.proposed",
                task_id="session",
                session_id=session.session_id,
                intent_code="session",
                status=TaskStatus.WAITING_CONFIRMATION,
                message="请确认执行计划",
                ishandover=False,
                payload=self._normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "plan_id": plan.plan_id,
                        "plan_status": plan.status.value,
                        "items": [item.model_dump(mode="json") for item in plan.items],
                        "interaction": {
                            "type": "plan_card",
                            "card_type": "plan_confirm",
                            "title": plan.title,
                            "summary": plan.summary or f"识别到 {len(plan.items)} 个意图，请确认后开始执行",
                            "version": plan.version,
                            "plan_id": plan.plan_id,
                            "confirm_token": plan.confirm_token,
                            "items": [item.model_dump(mode="json") for item in plan.items],
                            "actions": [
                                {"code": "confirm_plan", "label": "开始执行"},
                                {"code": "cancel_plan", "label": "取消"},
                            ],
                        },
                    },
                    source="router",
                ),
            )
        )

    async def _publish_plan_waiting_hint(self, session: SessionState) -> None:
        plan = session.pending_plan
        if plan is None:
            return
        await self._publish(
            TaskEvent(
                event="session.plan.waiting_confirmation",
                task_id="session",
                session_id=session.session_id,
                intent_code="session",
                status=TaskStatus.WAITING_CONFIRMATION,
                message="当前有待确认的执行计划，请先确认或取消",
                ishandover=False,
                payload=self._normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "plan_id": plan.plan_id,
                        "plan_status": plan.status.value,
                        "interaction": {
                            "type": "plan_card",
                            "card_type": "plan_confirm",
                            "title": plan.title,
                            "summary": plan.summary or "请先确认当前执行计划",
                            "version": plan.version,
                            "plan_id": plan.plan_id,
                            "confirm_token": plan.confirm_token,
                            "items": [item.model_dump(mode="json") for item in plan.items],
                            "actions": [
                                {"code": "confirm_plan", "label": "开始执行"},
                                {"code": "cancel_plan", "label": "取消"},
                            ],
                        },
                    },
                    source="router",
                ),
            )
        )

    async def handle_action(
        self,
        *,
        session_id: str,
        cust_id: str,
        action_code: str,
        source: str | None = None,
        task_id: str | None = None,
        confirm_token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RouterSnapshot:
        session = self.session_store.get_or_create(session_id, cust_id)
        if source not in {None, "router"}:
            raise ValueError(f"Unsupported action source: {source}")

        if action_code == "confirm_plan":
            await self._confirm_pending_plan(session, task_id=task_id, confirm_token=confirm_token)
            return self.snapshot(session.session_id)

        if action_code == "cancel_plan":
            await self._cancel_pending_plan(session, task_id=task_id, confirm_token=confirm_token)
            return self.snapshot(session.session_id)

        raise ValueError(f"Unsupported action_code: {action_code}")

    async def _confirm_pending_plan(
        self,
        session: SessionState,
        *,
        task_id: str | None,
        confirm_token: str | None,
    ) -> None:
        plan = session.pending_plan
        if plan is None or plan.status != SessionPlanStatus.WAITING_CONFIRMATION:
            raise ValueError("No pending plan to confirm")
        if task_id not in {None, "session"}:
            raise ValueError("Plan action task_id must be 'session'")
        if confirm_token is not None and confirm_token != plan.confirm_token:
            raise ValueError("Invalid plan confirm token")

        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        context = self._build_session_context(session)
        context["source_input"] = plan.source_message
        for item in plan.items:
            intent = active_intents.get(item.intent_code)
            if intent is None:
                item.status = SessionPlanItemStatus.SKIPPED
                continue
            task = await self._create_task(
                session,
                context,
                intent=intent,
                confidence=item.confidence,
                is_fallback=False,
            )
            item.task_id = task.task_id
            item.status = SessionPlanItemStatus.PENDING

        plan.touch(SessionPlanStatus.RUNNING)
        await self._publish(
            TaskEvent(
                event="session.plan.confirmed",
                task_id="session",
                session_id=session.session_id,
                intent_code="session",
                status=TaskStatus.RUNNING,
                message="计划已确认，开始执行",
                ishandover=False,
                payload=self._normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "plan_id": plan.plan_id,
                        "plan_status": plan.status.value,
                        "items": [item.model_dump(mode="json") for item in plan.items],
                        "interaction": {
                            "type": "plan_card",
                            "card_type": "plan_confirm",
                            "title": plan.title,
                            "summary": plan.summary or f"识别到 {len(plan.items)} 个意图，开始按顺序执行",
                            "version": plan.version,
                            "plan_id": plan.plan_id,
                            "confirm_token": plan.confirm_token,
                            "items": [item.model_dump(mode="json") for item in plan.items],
                            "actions": [],
                        },
                    },
                    source="router",
                ),
            )
        )
        queue_pending_tasks(session, self.intent_catalog.priorities())
        await self._publish_session_state(session, "session.recognized")
        await self._drain_queue(session, plan.source_message)
        await self._emit_plan_progress_if_needed(session)

    async def _cancel_pending_plan(
        self,
        session: SessionState,
        *,
        task_id: str | None,
        confirm_token: str | None,
    ) -> None:
        plan = session.pending_plan
        if plan is None or plan.status != SessionPlanStatus.WAITING_CONFIRMATION:
            raise ValueError("No pending plan to cancel")
        if task_id not in {None, "session"}:
            raise ValueError("Plan action task_id must be 'session'")
        if confirm_token is not None and confirm_token != plan.confirm_token:
            raise ValueError("Invalid plan confirm token")

        plan.touch(SessionPlanStatus.CANCELLED)
        await self._publish(
            TaskEvent(
                event="session.plan.cancelled",
                task_id="session",
                session_id=session.session_id,
                intent_code="session",
                status=TaskStatus.CANCELLED,
                message="已取消执行计划",
                ishandover=True,
                payload=self._normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "plan_id": plan.plan_id,
                        "plan_status": plan.status.value,
                        "items": [item.model_dump(mode="json") for item in plan.items],
                        "interaction": {
                            "type": "plan_card",
                            "card_type": "plan_confirm",
                            "title": plan.title,
                            "summary": "执行计划已取消",
                            "version": plan.version,
                            "plan_id": plan.plan_id,
                            "confirm_token": plan.confirm_token,
                            "items": [item.model_dump(mode="json") for item in plan.items],
                            "actions": [],
                        },
                    },
                    source="router",
                ),
            )
        )
        session.pending_plan = None

    async def _emit_plan_progress_if_needed(self, session: SessionState) -> None:
        plan = session.pending_plan
        if plan is None or plan.status not in {
            SessionPlanStatus.RUNNING,
            SessionPlanStatus.PARTIALLY_COMPLETED,
        }:
            return

        task_by_id = {task.task_id: task for task in session.tasks}
        terminal_statuses = {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
        for item in plan.items:
            if item.task_id is None:
                continue
            task = task_by_id.get(item.task_id)
            if task is None:
                continue
            item.status = self._item_status_for_task(task.status)

        all_terminal = all(
            item.status.value in terminal_statuses or item.status == SessionPlanItemStatus.SKIPPED
            for item in plan.items
        )
        if all_terminal:
            plan.touch(SessionPlanStatus.COMPLETED)
            event_name = "session.plan.completed"
            event_status = TaskStatus.COMPLETED
            event_handover = True
            message = "执行计划已完成"
        else:
            has_completed = any(item.status == SessionPlanItemStatus.COMPLETED for item in plan.items)
            plan.touch(SessionPlanStatus.PARTIALLY_COMPLETED if has_completed else SessionPlanStatus.RUNNING)
            event_name = "session.plan.updated"
            event_status = TaskStatus.RUNNING
            event_handover = False
            message = "执行计划状态更新"

        await self._publish(
            TaskEvent(
                event=event_name,
                task_id="session",
                session_id=session.session_id,
                intent_code="session",
                status=event_status,
                message=message,
                ishandover=event_handover,
                payload=self._normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "plan_id": plan.plan_id,
                        "plan_status": plan.status.value,
                        "items": [item.model_dump(mode="json") for item in plan.items],
                        "interaction": {
                            "type": "plan_card",
                            "card_type": "plan_confirm",
                            "title": plan.title,
                            "summary": (
                                "执行计划已完成"
                                if all_terminal
                                else "Router 正在按顺序推进当前执行计划"
                            ),
                            "version": plan.version,
                            "plan_id": plan.plan_id,
                            "confirm_token": plan.confirm_token,
                            "items": [item.model_dump(mode="json") for item in plan.items],
                            "actions": [],
                        },
                    },
                    source="router",
                ),
            )
        )
        if all_terminal:
            session.pending_plan = None

    def _normalize_interaction_payload(self, payload: dict[str, Any], *, source: str) -> dict[str, Any]:
        interaction = payload.get("interaction")
        if not isinstance(interaction, dict):
            return payload
        normalized = dict(payload)
        interaction_payload = dict(interaction)
        interaction_payload.setdefault("source", source)
        normalized["interaction"] = interaction_payload
        return normalized

    def _item_status_for_task(self, status: TaskStatus) -> SessionPlanItemStatus:
        mapping = {
            TaskStatus.CREATED: SessionPlanItemStatus.PENDING,
            TaskStatus.QUEUED: SessionPlanItemStatus.PENDING,
            TaskStatus.DISPATCHING: SessionPlanItemStatus.RUNNING,
            TaskStatus.RUNNING: SessionPlanItemStatus.RUNNING,
            TaskStatus.WAITING_USER_INPUT: SessionPlanItemStatus.WAITING_USER_INPUT,
            TaskStatus.WAITING_CONFIRMATION: SessionPlanItemStatus.WAITING_CONFIRMATION,
            TaskStatus.RESUMING: SessionPlanItemStatus.RUNNING,
            TaskStatus.COMPLETED: SessionPlanItemStatus.COMPLETED,
            TaskStatus.FAILED: SessionPlanItemStatus.FAILED,
            TaskStatus.CANCELLED: SessionPlanItemStatus.CANCELLED,
        }
        return mapping[status]

    def _is_plan_confirm_message(self, content: str) -> bool:
        normalized = re.sub(r"[\s，,。.!！？、；;]", "", content)
        return normalized in {"确认", "确认执行", "开始", "开始执行", "执行", "执行吧", "好的", "好"}
