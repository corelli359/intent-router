from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any
from uuid import uuid4

from router_core.agent_client import AgentClient, StreamingAgentClient
from router_core.context_builder import ContextBuilder
from router_core.domain import (
    ChatMessage,
    IntentDefinition,
    RouterSnapshot,
    Task,
    TaskEvent,
    TaskStatus,
    utc_now,
)
from router_core.orchestrator import LongTermMemoryStore
from router_core.recognizer import IntentRecognizer, RecognitionResult
from router_core.v2_domain import (
    ExecutionGraphState,
    GraphCondition,
    GraphNodeState,
    GraphNodeStatus,
    GraphRouterSnapshot,
    GraphSessionState,
    GraphStatus,
)
from router_core.v2_planner import (
    BasicTurnInterpreter,
    IntentGraphPlanner,
    SequentialIntentGraphPlanner,
    TurnDecisionPayload,
    TurnInterpreter,
)


logger = logging.getLogger(__name__)

TERMINAL_NODE_STATUSES = {
    GraphNodeStatus.COMPLETED,
    GraphNodeStatus.FAILED,
    GraphNodeStatus.CANCELLED,
    GraphNodeStatus.SKIPPED,
}
ACTIVE_NODE_STATUSES = {
    GraphNodeStatus.RUNNING,
    GraphNodeStatus.WAITING_USER_INPUT,
    GraphNodeStatus.WAITING_CONFIRMATION,
}


@dataclass(slots=True)
class GraphRouterOrchestratorConfig:
    intent_switch_threshold: float = 0.80
    agent_timeout_seconds: float = 60.0


class GraphSessionStore:
    def __init__(self, long_term_memory: LongTermMemoryStore | None = None) -> None:
        self._sessions: dict[str, GraphSessionState] = {}
        self.long_term_memory = long_term_memory or LongTermMemoryStore()

    def create(self, cust_id: str, session_id: str | None = None) -> GraphSessionState:
        resolved_session_id = session_id or f"session_v2_{uuid4().hex[:10]}"
        session = GraphSessionState(session_id=resolved_session_id, cust_id=cust_id)
        self._sessions[resolved_session_id] = session
        return session

    def get(self, session_id: str) -> GraphSessionState:
        return self._sessions[session_id]

    def get_or_create(self, session_id: str | None, cust_id: str) -> GraphSessionState:
        if session_id is None:
            return self.create(cust_id=cust_id)
        if session_id not in self._sessions:
            self._sessions[session_id] = GraphSessionState(session_id=session_id, cust_id=cust_id)
        session = self._sessions[session_id]
        if session.cust_id != cust_id:
            session = GraphSessionState(session_id=session_id, cust_id=cust_id)
            self._sessions[session_id] = session
        if session.is_expired():
            self.long_term_memory.promote_session(self._compat_session_view(session))
            session = GraphSessionState(session_id=session.session_id, cust_id=session.cust_id)
            self._sessions[session_id] = session
        return session

    def _compat_session_view(self, session: GraphSessionState) -> Any:
        class _Compat:
            def __init__(self, source: GraphSessionState) -> None:
                self.session_id = source.session_id
                self.cust_id = source.cust_id
                self.messages = source.messages
                self.tasks = source.tasks

        return _Compat(session)


class _NoopIntentRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        return RecognitionResult(primary=[], candidates=[])


class GraphRouterOrchestrator:
    def __init__(
        self,
        publish_event: Callable[[TaskEvent], Any],
        session_store: GraphSessionStore | None = None,
        intent_catalog: Any | None = None,
        recognizer: IntentRecognizer | None = None,
        planner: IntentGraphPlanner | None = None,
        turn_interpreter: TurnInterpreter | None = None,
        context_builder: ContextBuilder | None = None,
        agent_client: AgentClient | None = None,
        config: GraphRouterOrchestratorConfig | None = None,
    ) -> None:
        self.publish_event = publish_event
        self.session_store = session_store or GraphSessionStore()
        self.intent_catalog = intent_catalog
        self.recognizer = recognizer or _NoopIntentRecognizer()
        self.planner = planner or SequentialIntentGraphPlanner()
        self.turn_interpreter = turn_interpreter or BasicTurnInterpreter()
        self.context_builder = context_builder or ContextBuilder()
        self.agent_client = agent_client or StreamingAgentClient()
        self.config = config or GraphRouterOrchestratorConfig()
        if self.intent_catalog is None:
            class _FallbackCatalog:
                def list_active(self) -> list[IntentDefinition]:
                    return []

                def get_fallback_intent(self) -> IntentDefinition | None:
                    return None

            self.intent_catalog = _FallbackCatalog()

    def create_session(self, cust_id: str, session_id: str | None = None) -> GraphSessionState:
        return self.session_store.create(cust_id=cust_id, session_id=session_id)

    def snapshot(self, session_id: str) -> GraphRouterSnapshot:
        session = self.session_store.get(session_id)
        return GraphRouterSnapshot(
            session_id=session.session_id,
            cust_id=session.cust_id,
            messages=list(session.messages),
            candidate_intents=list(session.candidate_intents),
            current_graph=session.current_graph.model_copy(deep=True) if session.current_graph is not None else None,
            pending_graph=session.pending_graph.model_copy(deep=True) if session.pending_graph is not None else None,
            active_node_id=session.active_node_id,
            expires_at=session.expires_at,
        )

    async def handle_user_message(self, session_id: str, cust_id: str, content: str) -> GraphRouterSnapshot:
        session = self.session_store.get_or_create(session_id, cust_id)
        session.messages.append(ChatMessage(role="user", content=content))
        session.touch()

        if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
            await self._handle_pending_graph_turn(session, content)
            return self.snapshot(session.session_id)

        waiting_node = self._get_waiting_node(session)
        if waiting_node is not None:
            await self._handle_waiting_node_turn(session, waiting_node, content)
            return self.snapshot(session.session_id)

        await self._route_new_message(session, content)
        return self.snapshot(session.session_id)

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
    ) -> GraphRouterSnapshot:
        session = self.session_store.get_or_create(session_id, cust_id)
        if source not in {None, "router", "graph"}:
            raise ValueError(f"Unsupported action source: {source}")

        if action_code in {"confirm_graph", "confirm_plan"}:
            await self._confirm_pending_graph(session, graph_id=task_id, confirm_token=confirm_token)
            return self.snapshot(session.session_id)
        if action_code in {"cancel_graph", "cancel_plan"}:
            await self._cancel_pending_graph(session, graph_id=task_id, confirm_token=confirm_token)
            return self.snapshot(session.session_id)
        if action_code == "cancel_node":
            await self._cancel_current_node(session, reason=(payload or {}).get("reason") or "用户取消当前节点")
            return self.snapshot(session.session_id)

        raise ValueError(f"Unsupported action_code: {action_code}")

    async def _route_new_message(
        self,
        session: GraphSessionState,
        content: str,
        *,
        recognition: Any | None = None,
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ) -> None:
        if recognition is None:
            context = self._build_session_context(session)
            recognition = await self._recognize_message(
                session,
                content,
                recent_messages=context["recent_messages"],
                long_term_memory=context["long_term_memory"],
                emit_events=True,
            )
        else:
            if recent_messages is None:
                recent_messages = []
            if long_term_memory is None:
                long_term_memory = []
        session.candidate_intents = recognition.candidates
        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        matches = [match for match in recognition.primary if match.intent_code in active_intents]

        if not matches:
            fallback_intent = self._fallback_intent()
            if fallback_intent is None:
                await self._publish_no_match_hint(session)
                return
            matches = [type("Match", (), {"intent_code": fallback_intent.intent_code, "confidence": 0.0})()]
            active_intents[fallback_intent.intent_code] = fallback_intent

        graph = await self.planner.plan(
            message=content,
            matches=matches,
            intents_by_code=active_intents,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
        )
        if len(graph.nodes) > 1:
            graph.touch(GraphStatus.WAITING_CONFIRMATION)
            session.pending_graph = graph
            session.active_node_id = None
            await self._publish_pending_graph(session)
            return

        session.pending_graph = None
        session.current_graph = graph
        self._activate_graph(graph)
        await self._publish_graph_state(session, "graph.created", "已创建执行图")
        await self._drain_graph(session, graph.source_message)

    async def _recognize_message(
        self,
        session: GraphSessionState,
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

        recognition = await self.recognizer.recognize(
            message=content,
            intents=self.intent_catalog.list_active(),
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            on_delta=publish_recognition_delta if emit_events else None,
        )
        if emit_events:
            primary_intents = [match.intent_code for match in recognition.primary]
            await self._publish(
                TaskEvent(
                    event="recognition.completed",
                    task_id="recognition",
                    session_id=session.session_id,
                    intent_code="recognition",
                    status=TaskStatus.COMPLETED,
                    message=(
                        f"意图识别完成: {', '.join(primary_intents)}"
                        if primary_intents
                        else "意图识别完成: 未命中主意图"
                    ),
                    payload={
                        "cust_id": session.cust_id,
                        "primary": [match.model_dump() for match in recognition.primary],
                        "candidates": [match.model_dump() for match in recognition.candidates],
                    },
                )
            )
        return recognition

    def _activate_graph(self, graph: ExecutionGraphState) -> None:
        graph.actions = []
        self._refresh_node_states(graph)
        if graph.status == GraphStatus.WAITING_CONFIRMATION:
            graph.touch(GraphStatus.RUNNING)
        else:
            graph.touch(self._graph_status(graph))

    async def _drain_graph(self, session: GraphSessionState, seed_input: str) -> None:
        graph = session.current_graph
        if graph is None:
            await self._publish_session_state(session, "session.idle")
            return

        while True:
            self._refresh_node_states(graph)
            waiting_node = self._get_waiting_node(session)
            if waiting_node is not None:
                session.active_node_id = waiting_node.node_id
                await self._publish_session_state(
                    session,
                    "session.waiting_confirmation"
                    if waiting_node.status == GraphNodeStatus.WAITING_CONFIRMATION
                    else "session.waiting_user_input",
                )
                await self._emit_graph_progress(session)
                return

            next_node = self._next_ready_node(graph)
            if next_node is None:
                session.active_node_id = None
                await self._emit_graph_progress(session)
                await self._publish_session_state(session, "session.idle")
                return

            session.active_node_id = next_node.node_id
            await self._run_node(session, graph, next_node, seed_input)

            if next_node.status in TERMINAL_NODE_STATUSES:
                continue
            if next_node.status in {GraphNodeStatus.WAITING_USER_INPUT, GraphNodeStatus.WAITING_CONFIRMATION}:
                await self._emit_graph_progress(session)
                await self._publish_session_state(
                    session,
                    "session.waiting_confirmation"
                    if next_node.status == GraphNodeStatus.WAITING_CONFIRMATION
                    else "session.waiting_user_input",
                )
                return
            logger.warning(
                "Node %s (%s) exited run loop with unexpected status %s",
                next_node.node_id,
                next_node.intent_code,
                next_node.status,
            )
            return

    async def _run_node(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        user_input: str,
    ) -> None:
        task = self._get_task(session, node.task_id)
        created_new_task = task is None
        if task is None:
            task = await self._create_task_for_node(session, graph, node)

        previous_initial_source_input = task.input_context.get("initial_source_input")
        if not (isinstance(previous_initial_source_input, str) and previous_initial_source_input):
            previous_initial_source_input = node.source_fragment or graph.source_message
        effective_user_input = (node.source_fragment or user_input) if created_new_task else user_input
        task.input_context = self._build_session_context(session, task=task)
        task.input_context.update(
            {
                "graph_id": graph.graph_id,
                "graph_version": graph.version,
                "node_id": node.node_id,
                "source_input": effective_user_input,
                "initial_source_input": previous_initial_source_input,
            }
        )

        task.touch(TaskStatus.DISPATCHING)
        node.slot_memory = dict(task.slot_memory)
        node.touch(GraphNodeStatus.RUNNING)
        graph.touch(GraphStatus.RUNNING)
        await self._publish_node_state(session, graph, node, task.status, "node.dispatching", "节点开始分发")

        task.touch(TaskStatus.RUNNING)
        await self._publish_node_state(session, graph, node, task.status, "node.running", "节点执行中")

        try:
            async with asyncio.timeout(self.config.agent_timeout_seconds):
                async for chunk in self.agent_client.stream(task, effective_user_input):
                    await self._handle_agent_chunk(session, graph, node, task, chunk)
                    if chunk.status in {
                        TaskStatus.WAITING_USER_INPUT,
                        TaskStatus.WAITING_CONFIRMATION,
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                    }:
                        break
        except TimeoutError:
            await self._fail_node(
                session,
                graph,
                node,
                task,
                (
                    f"节点执行超时（{self.config.agent_timeout_seconds:.0f}s），"
                    "已自动终止，请稍后重试"
                ),
                payload={"timeout_seconds": self.config.agent_timeout_seconds},
            )

    async def _create_task_for_node(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
    ) -> Task:
        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        intent = active_intents.get(node.intent_code)
        if intent is None:
            raise ValueError(f"Intent {node.intent_code} is no longer active")

        context = self._build_session_context(session)
        context.update(
            {
                "source_input": node.source_fragment or graph.source_message,
                "initial_source_input": node.source_fragment or graph.source_message,
                "graph_id": graph.graph_id,
                "graph_version": graph.version,
                "node_id": node.node_id,
            }
        )
        task = Task(
            session_id=session.session_id,
            intent_code=intent.intent_code,
            agent_url=intent.agent_url,
            intent_name=intent.name,
            intent_description=intent.description,
            intent_examples=intent.examples,
            request_schema=intent.request_schema,
            field_mapping=intent.field_mapping,
            confidence=node.confidence,
            input_context=context,
            slot_memory=dict(node.slot_memory),
        )
        task.touch(TaskStatus.CREATED)
        session.tasks.append(task)
        node.task_id = task.task_id
        await self._publish_node_state(session, graph, node, task.status, "node.created", f"创建节点 {node.intent_code}")
        return task

    async def _handle_agent_chunk(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        task: Task,
        chunk: Any,
    ) -> None:
        task.touch(chunk.status)
        node.slot_memory = dict(task.slot_memory)
        node.output_payload = dict(chunk.payload)
        node_status = self._node_status_for_task_status(chunk.status)
        node.touch(node_status)

        if chunk.status in {
            TaskStatus.WAITING_USER_INPUT,
            TaskStatus.WAITING_CONFIRMATION,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        } and chunk.content:
            session.messages.append(ChatMessage(role="assistant", content=chunk.content, created_at=utc_now()))
            session.touch()

        event_name = {
            TaskStatus.WAITING_USER_INPUT: "node.waiting_user_input",
            TaskStatus.WAITING_CONFIRMATION: "node.waiting_confirmation",
            TaskStatus.COMPLETED: "node.completed",
            TaskStatus.FAILED: "node.failed",
        }.get(chunk.status, "node.message")

        await self._publish(
            TaskEvent(
                event=event_name,
                task_id=node.node_id,
                session_id=session.session_id,
                intent_code=node.intent_code,
                status=chunk.status,
                message=chunk.content,
                ishandover=chunk.ishandover,
                payload=self._normalize_interaction_payload(
                    {
                        **dict(chunk.payload),
                        "cust_id": session.cust_id,
                        "graph": self._graph_payload(graph),
                        "node": self._node_payload(node),
                    },
                    source="agent",
                ),
            )
        )
        self._refresh_node_states(graph)
        await self._emit_graph_progress(session)

    async def _fail_node(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        task: Task,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        task.touch(TaskStatus.FAILED)
        node.touch(GraphNodeStatus.FAILED)
        node.output_payload = dict(payload or {})
        session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
        session.touch()
        await self._publish(
            TaskEvent(
                event="node.failed",
                task_id=node.node_id,
                session_id=session.session_id,
                intent_code=node.intent_code,
                status=TaskStatus.FAILED,
                message=message,
                ishandover=True,
                payload={"cust_id": session.cust_id, **(payload or {}), "graph": self._graph_payload(graph), "node": self._node_payload(node)},
            )
        )
        self._refresh_node_states(graph)
        await self._emit_graph_progress(session)

    async def _resume_waiting_node(
        self,
        session: GraphSessionState,
        node: GraphNodeState,
        content: str,
    ) -> None:
        graph = session.current_graph
        if graph is None:
            return
        await self._publish(
            TaskEvent(
                event="node.resuming",
                task_id=node.node_id,
                session_id=session.session_id,
                intent_code=node.intent_code,
                status=TaskStatus.RESUMING,
                message="恢复当前节点执行",
                payload={"cust_id": session.cust_id, "graph": self._graph_payload(graph), "node": self._node_payload(node)},
            )
        )
        await self._run_node(session, graph, node, content)
        await self._drain_graph(session, graph.source_message)

    async def _handle_pending_graph_turn(self, session: GraphSessionState, content: str) -> None:
        pending_graph = session.pending_graph
        if pending_graph is None:
            return
        recognition = await self._recognize_message(
            session,
            content,
            recent_messages=[],
            long_term_memory=[],
            emit_events=False,
        )
        decision = await self.turn_interpreter.interpret_pending_graph(
            message=content,
            pending_graph=pending_graph,
            recognition=recognition,
        )
        if decision.action == "confirm_pending_graph":
            await self._confirm_pending_graph(session, graph_id=None, confirm_token=None)
            return
        if decision.action == "cancel_pending_graph":
            await self._cancel_pending_graph(session, graph_id=None, confirm_token=None)
            return
        if decision.action == "replan":
            session.pending_graph = None
            await self._route_new_message(
                session,
                content,
                recognition=recognition,
                recent_messages=[],
                long_term_memory=[],
            )
            return
        await self._publish_graph_waiting_hint(session)

    async def _handle_waiting_node_turn(
        self,
        session: GraphSessionState,
        waiting_node: GraphNodeState,
        content: str,
    ) -> None:
        recognition = await self._recognize_message(
            session,
            content,
            recent_messages=[],
            long_term_memory=[],
            emit_events=False,
        )
        graph = session.current_graph
        if graph is None:
            return
        decision = await self.turn_interpreter.interpret_waiting_node(
            message=content,
            waiting_node=waiting_node,
            current_graph=graph,
            recognition=recognition,
        )
        if decision.action == "resume_current":
            await self._resume_waiting_node(session, waiting_node, content)
            return
        if decision.action == "cancel_current":
            await self._cancel_current_node(session, reason=decision.reason or "用户取消当前节点")
            await self._drain_graph(session, graph.source_message)
            return
        if decision.action == "replan":
            await self._cancel_current_graph(session, reason=decision.reason or "检测到用户修改了目标，准备重规划")
            await self._route_new_message(
                session,
                content,
                recognition=recognition,
                recent_messages=[],
                long_term_memory=[],
            )
            return
        await self._publish_session_state(session, "session.waiting_user_input")

    async def _cancel_current_node(self, session: GraphSessionState, *, reason: str) -> None:
        graph = session.current_graph
        node = self._get_waiting_node(session)
        if graph is None or node is None:
            raise ValueError("No waiting node to cancel")
        task = self._get_task(session, node.task_id)
        if task is not None and task.status in {TaskStatus.WAITING_USER_INPUT, TaskStatus.WAITING_CONFIRMATION}:
            try:
                await self.agent_client.cancel(session.session_id, task.task_id, task.agent_url)
            except Exception as exc:
                logger.warning("Failed to cancel node task %s: %s", task.task_id, exc)
            task.touch(TaskStatus.CANCELLED)
        node.touch(GraphNodeStatus.CANCELLED, blocking_reason=reason)
        await self._publish_node_state(session, graph, node, TaskStatus.CANCELLED, "node.cancelled", reason)
        self._refresh_node_states(graph)
        await self._emit_graph_progress(session)

    async def _cancel_current_graph(self, session: GraphSessionState, *, reason: str) -> None:
        graph = session.current_graph
        if graph is None:
            return
        for node in graph.nodes:
            if node.status in TERMINAL_NODE_STATUSES:
                continue
            task = self._get_task(session, node.task_id)
            if task is not None and task.status in {TaskStatus.WAITING_USER_INPUT, TaskStatus.WAITING_CONFIRMATION}:
                try:
                    await self.agent_client.cancel(session.session_id, task.task_id, task.agent_url)
                except Exception as exc:
                    logger.warning("Failed to cancel graph task %s: %s", task.task_id, exc)
                task.touch(TaskStatus.CANCELLED)
            node.touch(GraphNodeStatus.CANCELLED, blocking_reason=reason)
        graph.touch(GraphStatus.CANCELLED)
        session.active_node_id = None
        await self._publish_graph_state(session, "graph.cancelled", reason, status=TaskStatus.CANCELLED)

    async def _confirm_pending_graph(
        self,
        session: GraphSessionState,
        *,
        graph_id: str | None,
        confirm_token: str | None,
    ) -> None:
        graph = session.pending_graph
        if graph is None or graph.status != GraphStatus.WAITING_CONFIRMATION:
            raise ValueError("No pending graph to confirm")
        if graph_id not in {None, "session", graph.graph_id}:
            raise ValueError("Invalid graph id for confirmation")
        if confirm_token is not None and confirm_token != graph.confirm_token:
            raise ValueError("Invalid graph confirm token")

        session.pending_graph = None
        session.current_graph = graph
        self._activate_graph(graph)
        await self._publish_graph_state(session, "graph.confirmed", "执行图已确认，开始执行")
        await self._drain_graph(session, graph.source_message)

    async def _cancel_pending_graph(
        self,
        session: GraphSessionState,
        *,
        graph_id: str | None,
        confirm_token: str | None,
    ) -> None:
        graph = session.pending_graph
        if graph is None or graph.status != GraphStatus.WAITING_CONFIRMATION:
            raise ValueError("No pending graph to cancel")
        if graph_id not in {None, "session", graph.graph_id}:
            raise ValueError("Invalid graph id for cancellation")
        if confirm_token is not None and confirm_token != graph.confirm_token:
            raise ValueError("Invalid graph confirm token")

        graph.touch(GraphStatus.CANCELLED)
        graph.actions = []
        await self._publish(
            TaskEvent(
                event="graph.cancelled",
                task_id=graph.graph_id,
                session_id=session.session_id,
                intent_code="graph",
                status=TaskStatus.CANCELLED,
                message="已取消执行图",
                ishandover=True,
                payload=self._normalize_interaction_payload(
                    {"cust_id": session.cust_id, "graph": self._graph_payload(graph)},
                    source="router",
                ),
            )
        )
        session.pending_graph = None

    async def _publish_pending_graph(self, session: GraphSessionState) -> None:
        graph = session.pending_graph
        if graph is None:
            return
        await self._publish(
            TaskEvent(
                event="graph.proposed",
                task_id=graph.graph_id,
                session_id=session.session_id,
                intent_code="graph",
                status=TaskStatus.WAITING_CONFIRMATION,
                message="请确认执行图",
                ishandover=False,
                payload=self._normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "graph": self._graph_payload(graph, include_actions=True, pending=True),
                        "interaction": self._graph_interaction(graph, pending=True),
                    },
                    source="router",
                ),
            )
        )

    async def _publish_graph_waiting_hint(self, session: GraphSessionState) -> None:
        graph = session.pending_graph
        if graph is None:
            return
        await self._publish(
            TaskEvent(
                event="graph.waiting_confirmation",
                task_id=graph.graph_id,
                session_id=session.session_id,
                intent_code="graph",
                status=TaskStatus.WAITING_CONFIRMATION,
                message="当前有待确认的执行图，请先确认或取消",
                ishandover=False,
                payload=self._normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "graph": self._graph_payload(graph, include_actions=True, pending=True),
                        "interaction": self._graph_interaction(graph, pending=True),
                    },
                    source="router",
                ),
            )
        )

    async def _publish_no_match_hint(self, session: GraphSessionState) -> None:
        message = "暂未识别到明确事项，请换一种说法或补充更多上下文。"
        session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
        session.touch()
        await self._publish(
            TaskEvent(
                event="graph.unrecognized",
                task_id="graph",
                session_id=session.session_id,
                intent_code="graph",
                status=TaskStatus.COMPLETED,
                message=message,
                ishandover=True,
                payload={"cust_id": session.cust_id},
            )
        )
        await self._publish_session_state(session, "session.idle")

    async def _publish_graph_state(
        self,
        session: GraphSessionState,
        event: str,
        message: str,
        *,
        status: TaskStatus | None = None,
    ) -> None:
        graph = session.pending_graph if event in {"graph.proposed", "graph.waiting_confirmation"} else session.current_graph
        if graph is None:
            return
        resolved_status = status or self._task_status_for_graph(graph.status)
        await self._publish(
            TaskEvent(
                event=event,
                task_id=graph.graph_id,
                session_id=session.session_id,
                intent_code="graph",
                status=resolved_status,
                message=message,
                ishandover=resolved_status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
                payload=self._normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "graph": self._graph_payload(
                            graph,
                            include_actions=graph.status == GraphStatus.WAITING_CONFIRMATION,
                            pending=graph is session.pending_graph,
                        ),
                        "interaction": self._graph_interaction(
                            graph,
                            pending=graph is session.pending_graph,
                        ),
                    },
                    source="router",
                ),
            )
        )

    async def _publish_node_state(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        task_status: TaskStatus,
        event: str,
        message: str,
    ) -> None:
        await self._publish(
            TaskEvent(
                event=event,
                task_id=node.node_id,
                session_id=session.session_id,
                intent_code=node.intent_code,
                status=task_status,
                message=message,
                payload={"cust_id": session.cust_id, "graph": self._graph_payload(graph), "node": self._node_payload(node)},
            )
        )

    async def _publish_session_state(self, session: GraphSessionState, event: str) -> None:
        payload = {
            "cust_id": session.cust_id,
            "active_node_id": session.active_node_id,
            "candidate_intents": [match.model_dump() for match in session.candidate_intents],
            "expires_at": session.expires_at.isoformat(),
        }
        if session.current_graph is not None:
            payload["graph"] = self._graph_payload(session.current_graph)
        if session.pending_graph is not None:
            payload["pending_graph"] = self._graph_payload(session.pending_graph, include_actions=True, pending=True)
        await self._publish(
            TaskEvent(
                event=event,
                task_id="session",
                session_id=session.session_id,
                intent_code="session",
                status=TaskStatus.RUNNING if session.active_node_id else TaskStatus.COMPLETED,
                message="会话状态更新",
                payload=payload,
            )
        )

    async def _emit_graph_progress(self, session: GraphSessionState) -> None:
        graph = session.current_graph
        if graph is None:
            return
        graph.touch(self._graph_status(graph))
        event_name = self._graph_event_name(graph.status)
        await self._publish_graph_state(session, event_name, self._graph_message(graph.status))

    def _refresh_node_states(self, graph: ExecutionGraphState) -> None:
        for node in graph.nodes:
            if node.status in TERMINAL_NODE_STATUSES | ACTIVE_NODE_STATUSES:
                continue
            incoming_edges = graph.incoming_edges(node.node_id)
            if not incoming_edges:
                node.touch(GraphNodeStatus.READY)
                continue

            should_skip = False
            all_ready = True
            blocking_reason = "等待上游节点完成"
            for edge in incoming_edges:
                source = graph.node_by_id(edge.source_node_id)
                if source.status in {GraphNodeStatus.FAILED, GraphNodeStatus.CANCELLED, GraphNodeStatus.SKIPPED}:
                    should_skip = True
                    blocking_reason = edge.label or "上游节点未满足依赖"
                    break
                expected_statuses = (
                    edge.condition.expected_statuses
                    if edge.condition is not None and edge.condition.expected_statuses
                    else [GraphNodeStatus.COMPLETED.value]
                )
                if source.status.value in expected_statuses:
                    if edge.condition is not None and (
                        edge.condition.left_key is not None or edge.condition.expression is not None
                    ):
                        if self._condition_matches_from_condition(source, edge.condition):
                            continue
                        should_skip = True
                        blocking_reason = edge.label or "条件依赖未满足"
                        break
                    continue
                if source.status in TERMINAL_NODE_STATUSES:
                    should_skip = True
                    blocking_reason = edge.label or "条件依赖未满足"
                    break
                all_ready = False

            if should_skip:
                node.touch(GraphNodeStatus.SKIPPED, blocking_reason=blocking_reason)
            elif all_ready:
                node.touch(GraphNodeStatus.READY)
            else:
                node.touch(GraphNodeStatus.BLOCKED, blocking_reason=blocking_reason)

    def _condition_matches_from_condition(self, source: GraphNodeState, condition: GraphCondition | None) -> bool:
        if condition is None or condition.left_key is None or condition.operator is None:
            return False
        current_value = source.output_payload.get(condition.left_key)
        if current_value is None:
            return False
        threshold = condition.right_value
        operator = condition.operator
        if isinstance(current_value, (int, float)) and isinstance(threshold, (int, float)):
            left = float(current_value)
            right = float(threshold)
        else:
            left = current_value
            right = threshold
        try:
            if operator == ">":
                return left > right
            if operator == ">=":
                return left >= right
            if operator == "<":
                return left < right
            if operator == "<=":
                return left <= right
            return left == right
        except TypeError:
            return False

    def _graph_status(self, graph: ExecutionGraphState) -> GraphStatus:
        statuses = [node.status for node in graph.nodes]
        if not statuses:
            return GraphStatus.COMPLETED
        if any(status == GraphNodeStatus.WAITING_CONFIRMATION for status in statuses):
            return GraphStatus.WAITING_CONFIRMATION_NODE
        if any(status == GraphNodeStatus.WAITING_USER_INPUT for status in statuses):
            return GraphStatus.WAITING_USER_INPUT
        if any(status in {GraphNodeStatus.READY, GraphNodeStatus.BLOCKED, GraphNodeStatus.RUNNING} for status in statuses):
            completed = any(status == GraphNodeStatus.COMPLETED for status in statuses)
            return GraphStatus.PARTIALLY_COMPLETED if completed else GraphStatus.RUNNING
        if all(status in {GraphNodeStatus.CANCELLED, GraphNodeStatus.SKIPPED} for status in statuses):
            return GraphStatus.CANCELLED
        if all(status in {GraphNodeStatus.COMPLETED, GraphNodeStatus.SKIPPED} for status in statuses):
            return GraphStatus.COMPLETED
        if any(status == GraphNodeStatus.FAILED for status in statuses):
            completed = any(status == GraphNodeStatus.COMPLETED for status in statuses)
            return GraphStatus.PARTIALLY_COMPLETED if completed else GraphStatus.FAILED
        if any(status == GraphNodeStatus.CANCELLED for status in statuses):
            completed = any(status == GraphNodeStatus.COMPLETED for status in statuses)
            return GraphStatus.PARTIALLY_COMPLETED if completed else GraphStatus.CANCELLED
        return GraphStatus.RUNNING

    def _next_ready_node(self, graph: ExecutionGraphState) -> GraphNodeState | None:
        ready_nodes = [node for node in graph.nodes if node.status == GraphNodeStatus.READY]
        if not ready_nodes:
            return None
        ready_nodes.sort(key=lambda node: (node.position, node.created_at))
        return ready_nodes[0]

    def _get_waiting_node(self, session: GraphSessionState) -> GraphNodeState | None:
        graph = session.current_graph
        if graph is None:
            return None
        waiting_nodes = [
            node
            for node in graph.nodes
            if node.status in {GraphNodeStatus.WAITING_USER_INPUT, GraphNodeStatus.WAITING_CONFIRMATION}
        ]
        if not waiting_nodes:
            return None
        waiting_nodes.sort(key=lambda node: node.updated_at, reverse=True)
        return waiting_nodes[0]

    def _get_task(self, session: GraphSessionState, task_id: str | None) -> Task | None:
        if task_id is None:
            return None
        for task in session.tasks:
            if task.task_id == task_id:
                return task
        return None

    def _build_session_context(self, session: GraphSessionState, task: Task | None = None) -> dict[str, Any]:
        long_term_memory = self.session_store.long_term_memory.recall(session.cust_id)
        return self.context_builder.build_task_context(session, task=task, long_term_memory=long_term_memory)

    def _fallback_intent(self) -> IntentDefinition | None:
        getter = getattr(self.intent_catalog, "get_fallback_intent", None)
        if getter is None:
            return None
        return getter()

    def _node_status_for_task_status(self, status: TaskStatus) -> GraphNodeStatus:
        mapping = {
            TaskStatus.CREATED: GraphNodeStatus.DRAFT,
            TaskStatus.QUEUED: GraphNodeStatus.READY,
            TaskStatus.DISPATCHING: GraphNodeStatus.RUNNING,
            TaskStatus.RUNNING: GraphNodeStatus.RUNNING,
            TaskStatus.WAITING_USER_INPUT: GraphNodeStatus.WAITING_USER_INPUT,
            TaskStatus.WAITING_CONFIRMATION: GraphNodeStatus.WAITING_CONFIRMATION,
            TaskStatus.RESUMING: GraphNodeStatus.RUNNING,
            TaskStatus.COMPLETED: GraphNodeStatus.COMPLETED,
            TaskStatus.FAILED: GraphNodeStatus.FAILED,
            TaskStatus.CANCELLED: GraphNodeStatus.CANCELLED,
        }
        return mapping[status]

    def _task_status_for_graph(self, status: GraphStatus) -> TaskStatus:
        mapping = {
            GraphStatus.DRAFT: TaskStatus.CREATED,
            GraphStatus.WAITING_CONFIRMATION: TaskStatus.WAITING_CONFIRMATION,
            GraphStatus.RUNNING: TaskStatus.RUNNING,
            GraphStatus.WAITING_USER_INPUT: TaskStatus.WAITING_USER_INPUT,
            GraphStatus.WAITING_CONFIRMATION_NODE: TaskStatus.WAITING_CONFIRMATION,
            GraphStatus.PARTIALLY_COMPLETED: TaskStatus.RUNNING,
            GraphStatus.COMPLETED: TaskStatus.COMPLETED,
            GraphStatus.FAILED: TaskStatus.FAILED,
            GraphStatus.CANCELLED: TaskStatus.CANCELLED,
        }
        return mapping[status]

    def _graph_event_name(self, status: GraphStatus) -> str:
        if status == GraphStatus.COMPLETED:
            return "graph.completed"
        if status == GraphStatus.FAILED:
            return "graph.failed"
        if status == GraphStatus.CANCELLED:
            return "graph.cancelled"
        return "graph.updated"

    def _graph_message(self, status: GraphStatus) -> str:
        if status == GraphStatus.COMPLETED:
            return "执行图已完成"
        if status == GraphStatus.FAILED:
            return "执行图执行失败"
        if status == GraphStatus.CANCELLED:
            return "执行图已取消"
        if status == GraphStatus.WAITING_USER_INPUT:
            return "执行图等待用户补充信息"
        if status == GraphStatus.WAITING_CONFIRMATION_NODE:
            return "执行图等待节点确认"
        return "执行图状态更新"

    def _graph_payload(
        self,
        graph: ExecutionGraphState,
        *,
        include_actions: bool = False,
        pending: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "graph_id": graph.graph_id,
            "source_message": graph.source_message,
            "summary": graph.summary,
            "version": graph.version,
            "status": graph.status.value,
            "confirm_token": graph.confirm_token if pending else None,
            "nodes": [self._node_payload(node) for node in graph.nodes],
            "edges": [edge.model_dump(mode="json") for edge in graph.edges],
        }
        if include_actions:
            payload["actions"] = [action.model_dump(mode="json") for action in graph.actions]
        return payload

    def _node_payload(self, node: GraphNodeState) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "intent_code": node.intent_code,
            "title": node.title,
            "confidence": node.confidence,
            "position": node.position,
            "source_fragment": node.source_fragment,
            "status": node.status.value,
            "task_id": node.task_id,
            "depends_on": list(node.depends_on),
            "blocking_reason": node.blocking_reason,
            "relation_reason": node.relation_reason,
            "slot_memory": dict(node.slot_memory),
            "output_payload": dict(node.output_payload),
            "updated_at": node.updated_at.isoformat(),
        }

    def _graph_interaction(self, graph: ExecutionGraphState, *, pending: bool) -> dict[str, Any]:
        return {
            "type": "graph_card",
            "card_type": "dynamic_graph",
            "title": "请确认执行图" if pending else "动态执行图",
            "summary": graph.summary,
            "version": graph.version,
            "graph_id": graph.graph_id,
            "confirm_token": graph.confirm_token if pending else None,
            "nodes": [self._node_payload(node) for node in graph.nodes],
            "edges": [edge.model_dump(mode="json") for edge in graph.edges],
            "actions": [action.model_dump(mode="json") for action in graph.actions] if pending else [],
        }

    def _normalize_interaction_payload(self, payload: dict[str, Any], *, source: str) -> dict[str, Any]:
        interaction = payload.get("interaction")
        if not isinstance(interaction, dict):
            return payload
        normalized = dict(payload)
        interaction_payload = dict(interaction)
        interaction_payload.setdefault("source", source)
        normalized["interaction"] = interaction_payload
        return normalized

    async def _publish(self, event: TaskEvent) -> None:
        result = self.publish_event(event)
        if result is not None and hasattr(result, "__await__"):
            await result
