from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from router_service.core.agent_client import AgentClient, StreamingAgentClient
from router_service.core.context_builder import ContextBuilder
from router_service.core.domain import (
    ChatMessage,
    IntentDefinition,
    IntentMatch,
    Task,
    TaskEvent,
    TaskStatus,
    utc_now,
)
from router_service.core.llm_client import llm_exception_is_retryable
from router_service.core.memory_store import LongTermMemoryStore
from router_service.core.recognizer import IntentRecognizer, RecognitionResult
from router_service.core.graph_compiler import GraphCompiler
from router_service.core.intent_understanding_service import IntentUnderstandingService
from router_service.core.slot_resolution_service import SlotResolutionService
from router_service.core.understanding_validator import UnderstandingValidationResult, UnderstandingValidator
from router_service.core.graph_domain import (
    ExecutionGraphState,
    GraphCondition,
    GraphNodeState,
    GraphNodeSkipReason,
    GraphNodeStatus,
    GraphRouterSnapshot,
    GraphSessionState,
    GraphStatus,
    GuidedSelectionPayload,
    ProactiveRecommendationItem,
    ProactiveRecommendationPayload,
    ProactiveRecommendationRouteMode,
    RecommendationContextPayload,
    SlotBindingSource,
    SlotBindingState,
)
from router_service.core.graph_runtime import GraphRuntimeEngine
from router_service.core.graph_presentation import GraphEventPublisher, GraphSnapshotPresenter
from router_service.core.graph_builder import GraphBuildResult, IntentGraphBuilder
from router_service.core.graph_planner import (
    BasicTurnInterpreter,
    IntentGraphPlanner,
    SequentialIntentGraphPlanner,
    TurnInterpreter,
)
from router_service.core.recommendation_router import (
    NullProactiveRecommendationRouter,
    ProactiveRecommendationRouter,
)
from router_service.core.graph.session_store import GraphSessionStore


logger = logging.getLogger(__name__)

PLANNING_RECENT_MESSAGE_PREFIXES = (
    "user:",
    "[FRONTEND_RECOMMENDATION_CONTEXT]",
    "[PROACTIVE_RECOMMENDATION_CONTEXT]",
    "[PROACTIVE_RECOMMENDATION_SELECTION]",
)

TERMINAL_NODE_STATUSES = {
    GraphNodeStatus.COMPLETED,
    GraphNodeStatus.FAILED,
    GraphNodeStatus.CANCELLED,
    GraphNodeStatus.SKIPPED,
}
TERMINAL_GRAPH_STATUSES = {
    GraphStatus.COMPLETED,
    GraphStatus.PARTIALLY_COMPLETED,
    GraphStatus.FAILED,
    GraphStatus.CANCELLED,
}


@dataclass(slots=True)
class GraphRouterOrchestratorConfig:
    intent_switch_threshold: float = 0.80
    agent_timeout_seconds: float = 60.0


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
        graph_builder: IntentGraphBuilder | None = None,
        planner: IntentGraphPlanner | None = None,
        turn_interpreter: TurnInterpreter | None = None,
        recommendation_router: ProactiveRecommendationRouter | None = None,
        context_builder: ContextBuilder | None = None,
        agent_client: AgentClient | None = None,
        runtime_engine: GraphRuntimeEngine | None = None,
        presenter: GraphSnapshotPresenter | None = None,
        event_publisher: GraphEventPublisher | None = None,
        config: GraphRouterOrchestratorConfig | None = None,
        understanding_service: IntentUnderstandingService | None = None,
        understanding_validator: UnderstandingValidator | None = None,
        slot_resolution_service: SlotResolutionService | None = None,
        graph_compiler: GraphCompiler | None = None,
    ) -> None:
        self.publish_event = publish_event
        self.session_store = session_store or GraphSessionStore()
        self.intent_catalog = intent_catalog
        self.recognizer = recognizer or _NoopIntentRecognizer()
        self.graph_builder = graph_builder
        self.planner = planner or SequentialIntentGraphPlanner()
        self.turn_interpreter = turn_interpreter or BasicTurnInterpreter()
        self.recommendation_router = recommendation_router or NullProactiveRecommendationRouter()
        self.context_builder = context_builder or ContextBuilder()
        self.agent_client = agent_client or StreamingAgentClient()
        self.runtime_engine = runtime_engine or GraphRuntimeEngine()
        self.presenter = presenter or GraphSnapshotPresenter(self.runtime_engine)
        self.event_publisher = event_publisher or GraphEventPublisher(self.publish_event, self.presenter)
        self.config = config or GraphRouterOrchestratorConfig()
        if self.intent_catalog is None:
            class _FallbackCatalog:
                def list_active(self) -> list[IntentDefinition]:
                    return []

                def get_fallback_intent(self) -> IntentDefinition | None:
                    return None

            self.intent_catalog = _FallbackCatalog()
        self.slot_resolution_service = slot_resolution_service or SlotResolutionService()
        self.understanding_validator = understanding_validator or UnderstandingValidator()
        self.understanding_service = understanding_service or IntentUnderstandingService(
            intent_catalog=self.intent_catalog,
            recognizer=self.recognizer,
            graph_builder=self.graph_builder,
            turn_interpreter=self.turn_interpreter,
            event_publisher=self.event_publisher,
        )
        self.graph_compiler = graph_compiler or GraphCompiler(
            intent_catalog=self.intent_catalog,
            planner=self.planner,
            understanding_service=self.understanding_service,
            slot_resolution_service=self.slot_resolution_service,
        )

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

    async def handle_user_message(
        self,
        session_id: str,
        cust_id: str,
        content: str,
        *,
        guided_selection: GuidedSelectionPayload | None = None,
        recommendation_context: RecommendationContextPayload | None = None,
        proactive_recommendation: ProactiveRecommendationPayload | None = None,
    ) -> GraphRouterSnapshot:
        session = self.session_store.get_or_create(session_id, cust_id)
        message_content = content.strip()
        display_content = message_content or self.graph_compiler.guided_selection_display_content(guided_selection)
        if display_content:
            session.messages.append(ChatMessage(role="user", content=display_content))
        session.touch()

        try:
            if proactive_recommendation is not None:
                await self._handle_proactive_recommendation_turn(
                    session,
                    content=message_content,
                    proactive_recommendation=proactive_recommendation,
                )
                return self.snapshot(session.session_id)

            if guided_selection is not None:
                await self._handle_guided_selection_turn(session, content=message_content, guided_selection=guided_selection)
                return self.snapshot(session.session_id)

            if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
                await self._handle_pending_graph_turn(session, message_content)
                return self.snapshot(session.session_id)

            waiting_node = self._get_waiting_node(session)
            if waiting_node is not None:
                await self._handle_waiting_node_turn(session, waiting_node, message_content)
                return self.snapshot(session.session_id)

            await self._route_new_message(
                session,
                message_content,
                recommendation_context=recommendation_context,
            )
        except Exception as exc:
            if not llm_exception_is_retryable(exc):
                raise
            logger.warning("Graph router LLM is temporarily unavailable", exc_info=True)
            session.messages.append(
                ChatMessage(
                    role="assistant",
                    content="当前意图识别服务繁忙，请稍后重试。",
                    created_at=utc_now(),
                )
            )
            session.touch()
        return self.snapshot(session.session_id)

    async def _handle_proactive_recommendation_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> None:
        if not proactive_recommendation.items:
            raise ValueError("proactive_recommendation.items is required")
        if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
            await self._cancel_pending_graph(session, graph_id=None, confirm_token=None)
        if session.current_graph is not None and session.current_graph.status not in TERMINAL_GRAPH_STATUSES:
            await self._cancel_current_graph(session, reason="用户切换到主动推荐事项处理")

        decision = await self.recommendation_router.decide(
            message=content,
            proactive_recommendation=proactive_recommendation,
        )
        items_by_id = {item.recommendation_item_id: item for item in proactive_recommendation.items}
        selected_items = [
            items_by_id[recommendation_id]
            for recommendation_id in decision.selected_recommendation_ids
            if recommendation_id in items_by_id
        ]

        if decision.route_mode == ProactiveRecommendationRouteMode.NO_SELECTION:
            message = "好的，本次不执行这些推荐事项。"
            session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
            session.touch()
            await self._publish_session_state(session, "session.idle")
            return

        if decision.route_mode == ProactiveRecommendationRouteMode.SWITCH_TO_FREE_DIALOG:
            await self._route_new_message(session, content)
            return

        if not selected_items:
            await self._publish_no_match_hint(session)
            return

        if (
            decision.route_mode == ProactiveRecommendationRouteMode.DIRECT_EXECUTE
            and any(not item.allow_direct_execute for item in selected_items)
        ):
            decision.route_mode = ProactiveRecommendationRouteMode.INTERACTIVE_GRAPH

        if decision.route_mode == ProactiveRecommendationRouteMode.DIRECT_EXECUTE:
            guided_selection = self.graph_compiler.guided_selection_from_proactive_items(selected_items)
            await self._route_guided_selection(session, content="", guided_selection=guided_selection)
            return

        await self._route_proactive_interactive_graph(
            session,
            content=content,
            proactive_recommendation=proactive_recommendation,
            selected_items=selected_items,
        )

    async def _handle_guided_selection_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        guided_selection: GuidedSelectionPayload,
    ) -> None:
        if not guided_selection.selected_intents:
            raise ValueError("guided_selection.selected_intents is required")
        if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
            await self._cancel_pending_graph(session, graph_id=None, confirm_token=None)
        if session.current_graph is not None and session.current_graph.status not in TERMINAL_GRAPH_STATUSES:
            await self._cancel_current_graph(session, reason="用户切换为引导式已选意图执行")
        await self._route_guided_selection(session, content=content, guided_selection=guided_selection)

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
        recommendation_context: RecommendationContextPayload | None = None,
        proactive_defaults: list[ProactiveRecommendationItem] | None = None,
        proactive_recommendation: ProactiveRecommendationPayload | None = None,
        skip_history_prefill: bool = False,
    ) -> None:
        compile_result = await self.graph_compiler.compile_message(
            session,
            content,
            build_session_context=self._build_session_context,
            sanitize_recent_messages_for_planning=self._sanitize_recent_messages_for_planning,
            recognition=recognition,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            recommendation_context=recommendation_context,
            proactive_defaults=proactive_defaults,
            proactive_recommendation=proactive_recommendation,
            skip_history_prefill=skip_history_prefill,
        )
        session.candidate_intents = compile_result.recognition.candidates
        graph = compile_result.graph
        if compile_result.no_match or graph is None:
            await self._publish_no_match_hint(session)
            return
        if graph.status == GraphStatus.WAITING_CONFIRMATION:
            graph.touch(GraphStatus.WAITING_CONFIRMATION)
            session.pending_graph = graph
            session.current_graph = None
            session.active_node_id = None
            await self._publish_pending_graph(session)
            return

        session.pending_graph = None
        session.current_graph = graph
        self._activate_graph(graph)
        await self._publish_graph_state(session, "graph.created", "已创建执行图")
        await self._drain_graph(session, graph.source_message)

    async def _route_guided_selection(
        self,
        session: GraphSessionState,
        *,
        content: str,
        guided_selection: GuidedSelectionPayload,
    ) -> None:
        graph = self.graph_compiler.build_guided_selection_graph(
            content=content,
            guided_selection=guided_selection,
        )
        session.candidate_intents = []
        session.pending_graph = None
        session.current_graph = graph
        self._activate_graph(graph)
        await self._publish_graph_state(session, "graph.created", "已根据所选意图创建执行图")
        await self._drain_graph(session, graph.source_message)

    async def _route_proactive_interactive_graph(
        self,
        session: GraphSessionState,
        *,
        content: str,
        proactive_recommendation: ProactiveRecommendationPayload,
        selected_items: list[ProactiveRecommendationItem],
    ) -> None:
        compile_result = await self.graph_compiler.compile_proactive_interactive_graph(
            session,
            content=content,
            proactive_recommendation=proactive_recommendation,
            selected_items=selected_items,
            build_session_context=self._build_session_context,
            sanitize_recent_messages_for_planning=self._sanitize_recent_messages_for_planning,
        )
        session.candidate_intents = compile_result.recognition.candidates
        graph = compile_result.graph
        if compile_result.no_match or graph is None:
            await self._publish_no_match_hint(session)
            return
        if graph.status == GraphStatus.WAITING_CONFIRMATION:
            graph.touch(GraphStatus.WAITING_CONFIRMATION)
            session.pending_graph = graph
            session.current_graph = None
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
        return await self.understanding_service.recognize_message(
            session,
            content,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            emit_events=emit_events,
        )

    async def _build_graph_from_message(
        self,
        session: GraphSessionState,
        content: str,
        *,
        recent_messages: list[str],
        long_term_memory: list[str],
        recognition: RecognitionResult | None,
        emit_events: bool,
    ) -> GraphBuildResult:
        return await self.understanding_service.build_graph_from_message(
            session,
            content,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            recognition=recognition,
            emit_events=emit_events,
        )

    def _activate_graph(self, graph: ExecutionGraphState) -> None:
        self.runtime_engine.activate_graph(graph)

    async def _drain_graph(self, session: GraphSessionState, seed_input: str) -> None:
        graph = session.current_graph
        if graph is None:
            await self._publish_session_state(session, "session.idle")
            return

        while True:
            await self._refresh_graph_state(session, graph)
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
        node_was_waiting = node.status in {GraphNodeStatus.WAITING_USER_INPUT, GraphNodeStatus.WAITING_CONFIRMATION}
        if task is None:
            task = await self._create_task_for_node(
                session,
                graph,
                node,
                dispatch_input=user_input,
            )
            if task is None:
                return

        previous_initial_source_input = task.input_context.get("initial_source_input")
        if not (isinstance(previous_initial_source_input, str) and previous_initial_source_input):
            previous_initial_source_input = node.source_fragment or graph.source_message
        effective_user_input = (
            user_input
            if created_new_task and node_was_waiting
            else (node.source_fragment or user_input or graph.source_message)
            if created_new_task
            else user_input
        )
        task.input_context = self._build_graph_task_context(session, graph=graph, task=task)
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
        *,
        dispatch_input: str,
    ) -> Task | None:
        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        intent = active_intents.get(node.intent_code)
        if intent is None:
            raise ValueError(f"Intent {node.intent_code} is no longer active")

        validation = await self._validate_node_understanding(
            session,
            graph,
            node,
            intent=intent,
            current_message=dispatch_input,
        )
        if not validation.can_dispatch:
            await self._mark_node_waiting_for_slots(session, graph, node, validation)
            return None

        context = self._build_graph_task_context(session, graph=graph)
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

        await self.event_publisher.publish_node_runtime_event(
            session,
            graph,
            node,
            task_status=chunk.status,
            event=event_name,
            message=chunk.content,
            ishandover=chunk.ishandover,
            payload=dict(chunk.payload),
            source="agent",
        )
        await self._refresh_graph_state(session, graph)
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
        await self.event_publisher.publish_node_runtime_event(
            session,
            graph,
            node,
            task_status=TaskStatus.FAILED,
            event="node.failed",
            message=message,
            ishandover=True,
            payload=payload,
        )
        await self._refresh_graph_state(session, graph)
        await self._emit_graph_progress(session)

    async def _validate_node_understanding(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        *,
        intent: IntentDefinition,
        current_message: str,
    ) -> UnderstandingValidationResult:
        session_context = self._build_session_context(session)
        memory_candidates = list(session_context["long_term_memory"])
        history_slot_values = self._history_slot_values(
            session,
            long_term_memory=session_context["long_term_memory"],
        )
        for slot_key, value in history_slot_values.items():
            candidate = f"{slot_key}={value}"
            if candidate not in memory_candidates:
                memory_candidates.append(candidate)
        validation = await self.understanding_validator.validate_node(
            intent=intent,
            node=node,
            graph_source_message=graph.source_message,
            current_message=current_message,
            long_term_memory=memory_candidates,
        )
        node.slot_memory = dict(validation.slot_memory)
        node.slot_bindings = list(validation.slot_bindings)
        node.history_slot_keys = list(validation.history_slot_keys)
        return validation

    async def _mark_node_waiting_for_slots(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        validation: UnderstandingValidationResult,
    ) -> None:
        message = validation.prompt_message or "请补充当前事项所需信息"
        node.task_id = None
        node.touch(GraphNodeStatus.WAITING_USER_INPUT, blocking_reason=message)
        graph.touch(GraphStatus.WAITING_USER_INPUT)
        session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
        session.touch()
        await self._publish_node_state(
            session,
            graph,
            node,
            TaskStatus.WAITING_USER_INPUT,
            "node.waiting_user_input",
            message,
        )
        await self.event_publisher.publish_node_runtime_event(
            session,
            graph,
            node,
            task_status=TaskStatus.WAITING_USER_INPUT,
            event="node.understanding_blocked",
            message=message,
            payload={
                "missing_required_slots": list(validation.missing_required_slots),
                "ambiguous_slot_keys": list(validation.ambiguous_slot_keys),
                "invalid_slot_keys": list(validation.invalid_slot_keys),
            },
            source="router",
        )
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
        await self.event_publisher.publish_node_runtime_event(
            session,
            graph,
            node,
            task_status=TaskStatus.RESUMING,
            event="node.resuming",
            message="恢复当前节点执行",
        )
        await self._run_node(session, graph, node, content)
        await self._drain_graph(session, content)

    async def _handle_pending_graph_turn(self, session: GraphSessionState, content: str) -> None:
        pending_graph = session.pending_graph
        if pending_graph is None:
            return
        turn_result = await self.understanding_service.interpret_pending_graph_turn(
            session,
            content=content,
            pending_graph=pending_graph,
        )
        decision = turn_result.decision
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
                recognition=turn_result.recognition,
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
        graph = session.current_graph
        if graph is None:
            return
        turn_result = await self.understanding_service.interpret_waiting_node_turn(
            session,
            content=content,
            waiting_node=waiting_node,
            current_graph=graph,
        )
        decision = turn_result.decision
        if decision.action == "resume_current":
            await self._resume_waiting_node(session, waiting_node, content)
            return
        if decision.action == "cancel_current":
            await self._cancel_current_node(session, reason=decision.reason or "用户取消当前节点")
            await self._drain_graph(session, content)
            return
        if decision.action == "replan":
            await self._cancel_current_graph(session, reason=decision.reason or "检测到用户修改了目标，准备重规划")
            await self._route_new_message(
                session,
                content,
                recognition=turn_result.recognition,
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
        await self._refresh_graph_state(session, graph)
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
        await self.event_publisher.publish_graph_cancelled(session, graph)
        session.pending_graph = None

    async def _publish_pending_graph(self, session: GraphSessionState) -> None:
        graph = session.pending_graph
        if graph is None:
            return
        await self.event_publisher.publish_pending_graph(session, graph)

    async def _publish_graph_waiting_hint(self, session: GraphSessionState) -> None:
        graph = session.pending_graph
        if graph is None:
            return
        await self.event_publisher.publish_graph_waiting_hint(session, graph)

    async def _publish_no_match_hint(self, session: GraphSessionState) -> None:
        message = "暂未识别到明确事项，请换一种说法或补充更多上下文。"
        session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
        session.touch()
        await self.event_publisher.publish_unrecognized(session, message=message)
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
        await self.event_publisher.publish_graph_state(
            session,
            graph,
            event=event,
            message=message,
            status=status,
            pending=graph is session.pending_graph,
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
        await self.event_publisher.publish_node_state(
            session,
            graph,
            node,
            task_status=task_status,
            event=event,
            message=message,
        )

    async def _publish_session_state(self, session: GraphSessionState, event: str) -> None:
        await self.event_publisher.publish_session_state(session, event=event)

    async def _emit_graph_progress(self, session: GraphSessionState) -> None:
        graph = session.current_graph
        if graph is None:
            return
        previous_status = graph.status
        graph.touch(self._graph_status(graph))
        event_name = self.presenter.graph_event_name(graph.status)
        message = self.presenter.graph_message(graph)
        if self.presenter.should_append_graph_terminal_message(graph, previous_status):
            session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
            session.touch()
        await self._publish_graph_state(session, event_name, message)

    def _refresh_node_states(self, graph: ExecutionGraphState) -> None:
        self.runtime_engine.refresh_node_states(graph)

    def _condition_matches_from_condition(self, source: GraphNodeState, condition: GraphCondition | None) -> bool:
        return self.runtime_engine.condition_matches(source, condition)

    def _graph_status(self, graph: ExecutionGraphState) -> GraphStatus:
        return self.runtime_engine.graph_status(graph)

    def _next_ready_node(self, graph: ExecutionGraphState) -> GraphNodeState | None:
        return self.runtime_engine.next_ready_node(graph)

    def _get_waiting_node(self, session: GraphSessionState) -> GraphNodeState | None:
        return self.runtime_engine.waiting_node(session.current_graph)

    def _get_task(self, session: GraphSessionState, task_id: str | None) -> Task | None:
        if task_id is None:
            return None
        for task in session.tasks:
            if task.task_id == task_id:
                return task
        return None

    async def _refresh_graph_state(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        previous_statuses = {node.node_id: node.status for node in graph.nodes}
        self._refresh_node_states(graph)
        graph_status = self._graph_status(graph)

        for node in graph.nodes:
            previous_status = previous_statuses.get(node.node_id)
            if previous_status == node.status or node.status != GraphNodeStatus.SKIPPED:
                continue
            message = self.presenter.skipped_node_message(node)
            await self._publish_node_state(
                session,
                graph,
                node,
                TaskStatus.COMPLETED,
                "node.skipped",
                message,
            )
            if node.skip_reason_code == GraphNodeSkipReason.CONDITION_NOT_MET.value and graph_status not in {
                GraphStatus.COMPLETED,
                GraphStatus.PARTIALLY_COMPLETED,
                GraphStatus.FAILED,
                GraphStatus.CANCELLED,
            }:
                session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
                session.touch()

        waiting_node = self._get_waiting_node(session)
        session.active_node_id = waiting_node.node_id if waiting_node is not None else None

    def _apply_history_prefill_policy(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        *,
        source_message: str,
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
    ) -> None:
        self.slot_resolution_service.apply_history_prefill_policy(
            session,
            graph,
            source_message=source_message,
            intents_by_code=intents_by_code,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
        )

    def _history_slot_values(
        self,
        session: GraphSessionState,
        *,
        long_term_memory: list[str],
    ) -> dict[str, Any]:
        return self.slot_resolution_service.history_slot_values(
            session,
            long_term_memory=long_term_memory,
        )

    def _structured_slot_bindings(
        self,
        *,
        slot_memory: dict[str, Any],
        source: SlotBindingSource,
        source_text: str | None,
        confidence: float,
    ) -> list[SlotBindingState]:
        return self.slot_resolution_service.structured_slot_bindings(
            slot_memory=slot_memory,
            source=source,
            source_text=source_text,
            confidence=confidence,
        )

    def _rebuild_node_slot_bindings(
        self,
        node: GraphNodeState,
        *,
        preferred_sources: dict[str, SlotBindingSource] | None = None,
        source_text: str | None = None,
    ) -> None:
        self.slot_resolution_service.rebuild_node_slot_bindings(
            node,
            preferred_sources=preferred_sources,
            source_text=source_text,
        )

    def _build_graph_task_context(
        self,
        session: GraphSessionState,
        *,
        graph: ExecutionGraphState,
        task: Task | None = None,
    ) -> dict[str, Any]:
        context = self._build_session_context(session, task=task)
        context.update(
            {
                "graph": self.presenter.graph_payload(graph),
                "graph_summary": graph.summary,
                "completed_node_outputs": {
                    node.node_id: dict(node.output_payload)
                    for node in graph.nodes
                    if node.status == GraphNodeStatus.COMPLETED and node.output_payload
                },
            }
        )
        return context

    def _build_session_context(self, session: GraphSessionState, task: Task | None = None) -> dict[str, Any]:
        long_term_memory = self.session_store.long_term_memory.recall(session.cust_id)
        return self.context_builder.build_task_context(session, task=task, long_term_memory=long_term_memory)

    def _sanitize_recent_messages_for_planning(self, recent_messages: list[str]) -> list[str]:
        if not recent_messages:
            return []
        return [
            entry
            for entry in recent_messages
            if any(entry.startswith(prefix) for prefix in PLANNING_RECENT_MESSAGE_PREFIXES)
        ]

    def _fallback_intent(self) -> IntentDefinition | None:
        getter = getattr(self.intent_catalog, "get_fallback_intent", None)
        if getter is None:
            return None
        return getter()

    def _node_status_for_task_status(self, status: TaskStatus) -> GraphNodeStatus:
        return self.runtime_engine.node_status_for_task_status(status)

    def _task_status_for_graph(self, status: GraphStatus) -> TaskStatus:
        return self.runtime_engine.task_status_for_graph(status)

    def _guided_selection_display_content(self, guided_selection: GuidedSelectionPayload | None) -> str:
        return self.graph_compiler.guided_selection_display_content(guided_selection)

    def _guided_selection_from_proactive_items(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> GuidedSelectionPayload:
        return self.graph_compiler.guided_selection_from_proactive_items(selected_items)

    def _augment_recent_messages_with_recommendations(
        self,
        recent_messages: list[str],
        *,
        recommendation_context: RecommendationContextPayload | None,
    ) -> list[str]:
        return self.graph_compiler.augment_recent_messages_with_recommendations(
            recent_messages,
            recommendation_context=recommendation_context,
        )

    def _augment_recent_messages_with_proactive_selection(
        self,
        recent_messages: list[str],
        *,
        proactive_recommendation: ProactiveRecommendationPayload,
        selected_items: list[ProactiveRecommendationItem],
    ) -> list[str]:
        return self.graph_compiler.augment_recent_messages_with_proactive_selection(
            recent_messages,
            proactive_recommendation=proactive_recommendation,
            selected_items=selected_items,
        )

    def _recommendation_context_summary(self, recommendation_context: RecommendationContextPayload) -> str:
        return self.graph_compiler.recommendation_context_summary(recommendation_context)

    def _proactive_recommendation_context_summary(
        self,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> str:
        return self.graph_compiler.proactive_recommendation_context_summary(proactive_recommendation)

    def _proactive_selection_summary(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> str:
        return self.graph_compiler.proactive_selection_summary(selected_items)

    def _guided_selection_summary(self, guided_selection: GuidedSelectionPayload) -> str:
        return self.graph_compiler.guided_selection_summary(guided_selection)

    def _recognition_from_proactive_items(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> RecognitionResult:
        return self.graph_compiler.recognition_from_proactive_items(selected_items)

    def _apply_proactive_slot_defaults(
        self,
        graph: ExecutionGraphState,
        *,
        selected_items: list[ProactiveRecommendationItem],
        proactive_recommendation: ProactiveRecommendationPayload | None,
        intents_by_code: dict[str, IntentDefinition],
    ) -> None:
        self.slot_resolution_service.apply_proactive_slot_defaults(
            graph,
            selected_items=selected_items,
            proactive_recommendation=proactive_recommendation,
            intents_by_code=intents_by_code,
        )
