from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any, TypeVar

from router_service.core.support.agent_client import AgentClient, StreamingAgentClient
from router_service.core.support.context_builder import ContextBuilder
from router_service.core.shared.domain import (
    ChatMessage,
    IntentDefinition,
    IntentMatch,
    Task,
    TaskEvent,
    TaskStatus,
    utc_now,
)
from router_service.core.recognition.recognizer import IntentRecognizer, RecognitionResult
from router_service.core.graph.compiler import GraphCompiler, GraphPlanningPolicy
from router_service.core.recognition.understanding_service import IntentUnderstandingService
from router_service.core.slots.resolution_service import SlotResolutionService
from router_service.core.slots.understanding_validator import UnderstandingValidationResult, UnderstandingValidator
from router_service.core.shared.graph_domain import (
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
    RecommendationContextPayload,
    SlotBindingSource,
    SlotBindingState,
)
from router_service.core.graph.runtime import GraphRuntimeEngine
from router_service.core.graph.constants import TERMINAL_NODE_STATUSES
from router_service.core.graph.presentation import GraphEventPublisher, GraphSnapshotPresenter
from router_service.core.graph.builder import GraphBuildResult, IntentGraphBuilder
from router_service.core.graph.planner import (
    BasicTurnInterpreter,
    IntentGraphPlanner,
    SequentialIntentGraphPlanner,
    TurnInterpreter,
)
from router_service.core.graph.recommendation_router import (
    NullProactiveRecommendationRouter,
    ProactiveRecommendationRouter,
)
from router_service.core.graph.action_flow import GraphActionFlow
from router_service.core.graph.session_store import GraphSessionStore
from router_service.core.graph.state_sync import GraphStateSync
from router_service.core.graph.message_flow import GraphMessageFlow
from router_service.core.support.trace_logging import current_trace_id, router_stage, router_trace


logger = logging.getLogger(__name__)
SerializedResponseT = TypeVar("SerializedResponseT")


@dataclass(slots=True)
class GraphRouterOrchestratorConfig:
    """Runtime knobs controlling intent switching and agent timeout behavior."""

    intent_switch_threshold: float = 0.80
    agent_timeout_seconds: float = 60.0
    memory_recall_limit: int = 20
    session_task_limit: int = 5
    session_business_limit: int = 5
    max_drain_iterations: int | None = None
    drain_iteration_multiplier: int = 3
    drain_iteration_floor: int = 8


class _NoopIntentRecognizer:
    """Recognizer fallback that always returns an empty result."""

    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        """Return an empty recognition result without performing any semantic work."""
        return RecognitionResult(primary=[], candidates=[], diagnostics=[])


class GraphRouterOrchestrator:
    """Top-level router runtime composed from message flow, action flow, and runtime state.

    The orchestrator is intentionally thin: recognition/planning, action handling,
    and state/event synchronization are delegated into focused collaborators. What
    remains here is the end-to-end graph execution loop that connects session
    state, node/task lifecycle, slot validation, and streaming agent responses.
    """

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
        planning_policy: GraphPlanningPolicy = "always",
        graph_compiler: GraphCompiler | None = None,
        state_sync: GraphStateSync | None = None,
        action_flow: GraphActionFlow | None = None,
        message_flow: GraphMessageFlow | None = None,
    ) -> None:
        """Assemble the orchestrator and lazily wire all default collaborators."""
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
                """Minimal catalog used when no intent catalog dependency is provided."""

                def list_active(self) -> list[IntentDefinition]:
                    """Return an empty active intent list."""
                    return []

                def get_fallback_intent(self) -> IntentDefinition | None:
                    """Return no fallback intent."""
                    return None

            self.intent_catalog = _FallbackCatalog()
        self.slot_resolution_service = slot_resolution_service or SlotResolutionService()
        self.state_sync = state_sync or GraphStateSync(
            runtime_engine=self.runtime_engine,
            presenter=self.presenter,
            event_publisher=self.event_publisher,
            slot_resolution_service=self.slot_resolution_service,
        )
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
            planning_policy=planning_policy,
        )
        self.action_flow = action_flow or GraphActionFlow(
            session_store=self.session_store,
            agent_client=self.agent_client,
            event_publisher=self.event_publisher,
            snapshot_session=self.snapshot,
            get_waiting_node=self._get_waiting_node,
            get_task=self._get_task,
            activate_graph=self._activate_graph,
            drain_graph=self._drain_graph,
            publish_node_state=self._publish_node_state,
            refresh_graph_state=self._refresh_graph_state,
            emit_graph_progress=self._emit_graph_progress,
            publish_graph_state=self._publish_graph_state,
        )
        self.message_flow = message_flow or GraphMessageFlow(
            session_store=self.session_store,
            graph_compiler=self.graph_compiler,
            recommendation_router=self.recommendation_router,
            understanding_service=self.understanding_service,
            state_sync=self.state_sync,
            snapshot_session=self.snapshot,
            get_waiting_node=self._get_waiting_node,
            build_session_context=self._build_session_context,
            activate_graph=self._activate_graph,
            drain_graph=self._drain_graph,
            cancel_pending_graph=self._cancel_pending_graph,
            cancel_current_graph=self._cancel_current_graph,
            confirm_pending_graph=self._confirm_pending_graph,
            resume_waiting_node=self._resume_waiting_node,
            cancel_current_node=self._cancel_current_node,
            session_business_limit=self.config.session_business_limit,
        )

    def create_session(self, cust_id: str, session_id: str | None = None) -> GraphSessionState:
        """Create a new graph session in the backing session store."""
        return self.session_store.create(cust_id=cust_id, session_id=session_id)

    def snapshot(self, session_id: str) -> GraphRouterSnapshot:
        """Build a deep-enough read snapshot safe for API exposure."""
        session = self.session_store.get(session_id)
        return self._build_session_dump(session)

    def _build_session_dump(self, session: GraphSessionState) -> GraphRouterSnapshot:
        """Build one API-facing dump directly from the current live session object."""
        return GraphRouterSnapshot(
            session_id=session.session_id,
            cust_id=session.cust_id,
            messages=list(session.messages),
            candidate_intents=list(session.candidate_intents),
            last_diagnostics=list(session.last_diagnostics),
            shared_slot_memory=dict(session.shared_slot_memory),
            current_graph=session.current_graph.model_copy(deep=True) if session.current_graph is not None else None,
            pending_graph=session.pending_graph.model_copy(deep=True) if session.pending_graph is not None else None,
            active_node_id=session.active_node_id,
            expires_at=session.expires_at,
        )

    def _finalize_handover_business_with(
        self,
        session: GraphSessionState,
        serializer: Callable[[GraphSessionState], SerializedResponseT],
    ) -> SerializedResponseT | None:
        """Compact the current handover-ready business after preserving one response payload."""
        business = session.handover_business()
        if business is None:
            return None
        response_dump = serializer(session)
        session.finalize_business(business.business_id)
        session.touch()
        return response_dump

    def _finalize_handover_business(self, session: GraphSessionState) -> GraphRouterSnapshot | None:
        """Compact the current handover-ready business after preserving the response dump."""
        return self._finalize_handover_business_with(session, self._build_session_dump)

    def _graph_and_node_for_task(
        self,
        session: GraphSessionState,
        task_id: str,
    ) -> tuple[ExecutionGraphState, GraphNodeState] | None:
        """Resolve one live graph/node pair from a task id."""
        seen_graph_ids: set[str] = set()
        graph_candidates = [
            session.current_graph,
            session.pending_graph,
            *[business.graph for business in session.business_objects],
        ]
        for graph in graph_candidates:
            if graph is None or graph.graph_id in seen_graph_ids:
                continue
            seen_graph_ids.add(graph.graph_id)
            for node in graph.nodes:
                if node.task_id == task_id:
                    return graph, node
        return None

    async def _apply_task_completion_signal(
        self,
        session: GraphSessionState,
        *,
        task_id: str,
        completion_signal: int,
        emit_events: bool,
    ) -> None:
        """Apply one assistant-originated completion signal to a live task/node."""
        if completion_signal not in {1, 2}:
            raise ValueError(f"Unsupported completionSignal: {completion_signal}")
        resolved = self._graph_and_node_for_task(session, task_id)
        if resolved is None:
            raise KeyError(task_id)
        graph, node = resolved
        task = self._get_task(session, task_id)
        override_getter = getattr(node, "apply_completion_signal", None)
        if not callable(override_getter):
            raise RuntimeError("graph node completion signal hook is unavailable")
        completion_state, _completion_reason = override_getter(
            source="assistant",
            signal=completion_signal,
        )
        if completion_state < 2:
            return

        if task is not None and task.status in {
            TaskStatus.DISPATCHING,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_USER_INPUT,
            TaskStatus.WAITING_CONFIRMATION,
            TaskStatus.WAITING_ASSISTANT_COMPLETION,
            TaskStatus.READY_FOR_DISPATCH,
            TaskStatus.RESUMING,
        }:
            try:
                await self.agent_client.cancel(session.session_id, task.task_id, task.agent_url)
            except Exception as exc:
                logger.warning("Failed to cancel task %s after assistant completion: %s", task.task_id, exc)
            task.touch(TaskStatus.COMPLETED)
        elif task is not None and task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            task.touch(TaskStatus.COMPLETED)

        node.touch(GraphNodeStatus.COMPLETED)
        if emit_events:
            await self.event_publisher.publish_node_runtime_event(
                session,
                graph,
                node,
                task_status=TaskStatus.COMPLETED,
                event="node.completed",
                message="任务完成态已由助手确认",
                ishandover=True,
                source="assistant",
            )
        await self._refresh_graph_state(session, graph)
        await self._emit_graph_progress(session)

    async def handle_user_message(
        self,
        session_id: str,
        cust_id: str,
        content: str,
        *,
        assistant_protocol: bool = False,
        router_only: bool = False,
        guided_selection: GuidedSelectionPayload | None = None,
        recommendation_context: RecommendationContextPayload | None = None,
        proactive_recommendation: ProactiveRecommendationPayload | None = None,
        upstream_config_variables: dict[str, Any] | None = None,
        upstream_slots_data: dict[str, Any] | None = None,
        return_snapshot: bool = True,
        emit_events: bool = True,
    ) -> GraphRouterSnapshot | None:
        """Delegate one user message turn into the message-flow state machine."""
        trace_details = {
            "router_only": router_only,
            "has_guided_selection": guided_selection is not None,
            "has_recommendation_context": recommendation_context is not None,
            "has_proactive_recommendation": proactive_recommendation is not None,
            "emit_events": emit_events,
        }
        with router_trace(
            logger,
            entrypoint="handle_user_message",
            session_id=session_id,
            cust_id=cust_id,
            content=content,
            details=trace_details,
        ):
            with self.event_publisher.event_scope(emit_events):
                async with self.session_store.session_lock(session_id):
                    with router_stage(logger, "orchestrator.handle_user_message", **trace_details):
                        message_flow_kwargs: dict[str, Any] = {
                            "assistant_protocol": assistant_protocol,
                            "router_only": router_only,
                            "guided_selection": guided_selection,
                            "recommendation_context": recommendation_context,
                            "proactive_recommendation": proactive_recommendation,
                            "return_snapshot": False,
                            "emit_events": emit_events,
                        }
                        if upstream_config_variables is not None:
                            message_flow_kwargs["upstream_config_variables"] = upstream_config_variables
                        if upstream_slots_data is not None:
                            message_flow_kwargs["upstream_slots_data"] = upstream_slots_data
                        await self.message_flow.handle_user_message(
                            session_id,
                            cust_id,
                            content,
                            **message_flow_kwargs,
                        )
                    session = self.session_store.get(session_id)
                    snapshot = self._finalize_handover_business(session)
                    if snapshot is None and return_snapshot:
                        snapshot = self._build_session_dump(session)
            logger.debug(
                "Router message snapshot (trace_id=%s, session_id=%s, current_graph_status=%s, pending_graph_status=%s, active_node_id=%s, candidate_intents=%s)",
                current_trace_id(),
                session_id,
                snapshot.current_graph.status.value if snapshot is not None and snapshot.current_graph is not None else None,
                snapshot.pending_graph.status.value if snapshot is not None and snapshot.pending_graph is not None else None,
                snapshot.active_node_id if snapshot is not None else None,
                len(snapshot.candidate_intents) if snapshot is not None else len(session.candidate_intents),
            )
            return snapshot if return_snapshot or snapshot is not None else None

    async def handle_task_completion(
        self,
        *,
        session_id: str,
        task_id: str,
        completion_signal: int,
        emit_events: bool = False,
    ) -> GraphRouterSnapshot | None:
        """Apply one assistant completion callback and optionally return a session snapshot."""
        with self.event_publisher.event_scope(emit_events):
            async with self.session_store.session_lock(session_id):
                session = self.session_store.get(session_id)
                await self._apply_task_completion_signal(
                    session,
                    task_id=task_id,
                    completion_signal=completion_signal,
                    emit_events=emit_events,
                )
                snapshot = self._finalize_handover_business(session)
                if snapshot is None:
                    snapshot = self._build_session_dump(session)
        return snapshot

    async def handle_task_completion_serialized(
        self,
        *,
        session_id: str,
        task_id: str,
        completion_signal: int,
        serializer: Callable[[GraphSessionState], SerializedResponseT],
        emit_events: bool = False,
    ) -> SerializedResponseT:
        """Apply one assistant completion callback and serialize the response while locked."""
        with self.event_publisher.event_scope(emit_events):
            async with self.session_store.session_lock(session_id):
                session = self.session_store.get(session_id)
                await self._apply_task_completion_signal(
                    session,
                    task_id=task_id,
                    completion_signal=completion_signal,
                    emit_events=emit_events,
                )
                serialized = self._finalize_handover_business_with(session, serializer)
                if serialized is None:
                    serialized = serializer(session)
        return serialized

    async def handle_user_message_serialized(
        self,
        *,
        session_id: str,
        cust_id: str,
        content: str,
        serializer: Callable[[GraphSessionState], SerializedResponseT],
        router_only: bool = False,
        assistant_protocol: bool = False,
        guided_selection: GuidedSelectionPayload | None = None,
        recommendation_context: RecommendationContextPayload | None = None,
        proactive_recommendation: ProactiveRecommendationPayload | None = None,
        upstream_config_variables: dict[str, Any] | None = None,
        upstream_slots_data: dict[str, Any] | None = None,
        emit_events: bool = False,
    ) -> SerializedResponseT:
        """Process one user message and serialize the response while the session is still locked."""
        trace_details = {
            "router_only": router_only,
            "has_guided_selection": guided_selection is not None,
            "has_recommendation_context": recommendation_context is not None,
            "has_proactive_recommendation": proactive_recommendation is not None,
            "emit_events": emit_events,
        }
        with router_trace(
            logger,
            entrypoint="handle_user_message_serialized",
            session_id=session_id,
            cust_id=cust_id,
            content=content,
            details=trace_details,
        ):
            with self.event_publisher.event_scope(emit_events):
                async with self.session_store.session_lock(session_id):
                    with router_stage(logger, "orchestrator.handle_user_message_serialized", **trace_details):
                        message_flow_kwargs: dict[str, Any] = {
                            "assistant_protocol": assistant_protocol,
                            "router_only": router_only,
                            "guided_selection": guided_selection,
                            "recommendation_context": recommendation_context,
                            "proactive_recommendation": proactive_recommendation,
                            "return_snapshot": False,
                            "emit_events": emit_events,
                        }
                        if upstream_config_variables is not None:
                            message_flow_kwargs["upstream_config_variables"] = upstream_config_variables
                        if upstream_slots_data is not None:
                            message_flow_kwargs["upstream_slots_data"] = upstream_slots_data
                        await self.message_flow.handle_user_message(
                            session_id,
                            cust_id,
                            content,
                            **message_flow_kwargs,
                        )
                    session = self.session_store.get(session_id)
                    serialized = self._finalize_handover_business_with(session, serializer)
                    if serialized is None:
                        serialized = serializer(session)
        return serialized

    async def _handle_proactive_recommendation_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> None:
        """Delegate proactive recommendation turns into the message flow."""
        await self.message_flow.handle_proactive_recommendation_turn(
            session,
            content=content,
            proactive_recommendation=proactive_recommendation,
        )

    async def _handle_guided_selection_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        guided_selection: GuidedSelectionPayload,
    ) -> None:
        """Delegate guided-selection turns into the message flow."""
        await self.message_flow.handle_guided_selection_turn(
            session,
            content=content,
            guided_selection=guided_selection,
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
        return_snapshot: bool = True,
        emit_events: bool = True,
    ) -> GraphRouterSnapshot | None:
        """Delegate one explicit graph action into the action-flow state machine."""
        with self.event_publisher.event_scope(emit_events):
            async with self.session_store.session_lock(session_id):
                await self.action_flow.handle_action(
                    session_id=session_id,
                    cust_id=cust_id,
                    action_code=action_code,
                    source=source,
                    task_id=task_id,
                    confirm_token=confirm_token,
                    payload=payload,
                    return_snapshot=False,
                )
                session = self.session_store.get(session_id)
                snapshot = self._finalize_handover_business(session)
                if snapshot is None and return_snapshot:
                    snapshot = self._build_session_dump(session)
        return snapshot if return_snapshot or snapshot is not None else None

    async def handle_action_serialized(
        self,
        *,
        session_id: str,
        cust_id: str,
        action_code: str,
        serializer: Callable[[GraphSessionState], SerializedResponseT],
        source: str | None = None,
        task_id: str | None = None,
        confirm_token: str | None = None,
        payload: dict[str, Any] | None = None,
        emit_events: bool = False,
    ) -> SerializedResponseT:
        """Process one explicit graph action and serialize the response while the session is still locked."""
        with self.event_publisher.event_scope(emit_events):
            async with self.session_store.session_lock(session_id):
                await self.action_flow.handle_action(
                    session_id=session_id,
                    cust_id=cust_id,
                    action_code=action_code,
                    source=source,
                    task_id=task_id,
                    confirm_token=confirm_token,
                    payload=payload,
                    return_snapshot=False,
                )
                session = self.session_store.get(session_id)
                serialized = self._finalize_handover_business_with(session, serializer)
                if serialized is None:
                    serialized = serializer(session)
        return serialized

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
        """Delegate free-form message routing into the message flow."""
        await self.message_flow.route_new_message(
            session,
            content,
            recognition=recognition,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            recommendation_context=recommendation_context,
            proactive_defaults=proactive_defaults,
            proactive_recommendation=proactive_recommendation,
            skip_history_prefill=skip_history_prefill,
        )

    async def _route_guided_selection(
        self,
        session: GraphSessionState,
        *,
        content: str,
        guided_selection: GuidedSelectionPayload,
    ) -> None:
        """Delegate guided-selection routing into the message flow."""
        await self.message_flow.route_guided_selection(
            session,
            content=content,
            guided_selection=guided_selection,
        )

    async def _route_proactive_interactive_graph(
        self,
        session: GraphSessionState,
        *,
        content: str,
        proactive_recommendation: ProactiveRecommendationPayload,
        selected_items: list[ProactiveRecommendationItem],
    ) -> None:
        """Delegate proactive interactive graph routing into the message flow."""
        await self.message_flow.route_proactive_interactive_graph(
            session,
            content=content,
            proactive_recommendation=proactive_recommendation,
            selected_items=selected_items,
        )

    async def _recognize_message(
        self,
        session: GraphSessionState,
        content: str,
        *,
        recent_messages: list[str],
        long_term_memory: list[str],
        emit_events: bool,
    ) -> Any:
        """Delegate message recognition into the understanding service."""
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
        """Delegate unified graph building into the understanding service."""
        return await self.understanding_service.build_graph_from_message(
            session,
            content,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            recognition=recognition,
            emit_events=emit_events,
        )

    def _activate_graph(self, graph: ExecutionGraphState) -> None:
        """Delegate graph activation into the runtime engine."""
        self.runtime_engine.activate_graph(graph)

    async def _drain_graph(self, session: GraphSessionState, seed_input: str) -> None:
        """Keep dispatching nodes until the graph blocks or reaches a terminal state.

        This is the core execution loop:
        1. recompute runtime-derived node states
        2. stop if the graph is waiting for user input/confirmation
        3. otherwise pick the next ready node and run it
        4. repeat until no ready node remains
        """
        with router_stage(
            logger,
            "orchestrator.drain_graph",
            router_only=session.router_only_mode,
            current_graph_id=session.current_graph.graph_id if session.current_graph is not None else None,
        ):
            if session.router_only_mode:
                await self._drain_graph_router_only(session, seed_input)
                return
            graph = session.current_graph
            if graph is None:
                await self._publish_session_state(session, "session.idle")
                return

            iterations = 0
            max_iterations = self._resolve_max_drain_iterations(graph)
            while True:
                iterations += 1
                if iterations > max_iterations:
                    await self._fail_drain_graph(
                        session,
                        graph,
                        max_iterations=max_iterations,
                    )
                    return
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

    async def _drain_graph_router_only(self, session: GraphSessionState, seed_input: str) -> None:
        """Advance the graph only through router understanding, stopping before agent execution."""
        with router_stage(
            logger,
            "orchestrator.drain_graph_router_only",
            current_graph_id=session.current_graph.graph_id if session.current_graph is not None else None,
        ):
            graph = session.current_graph
            if graph is None:
                await self._publish_session_state(session, "session.idle")
                return

            iterations = 0
            max_iterations = self._resolve_max_drain_iterations(graph)
            while True:
                iterations += 1
                if iterations > max_iterations:
                    await self._fail_drain_graph(
                        session,
                        graph,
                        max_iterations=max_iterations,
                    )
                    return
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
                if not await self._prepare_node_router_only(session, graph, next_node, dispatch_input=seed_input):
                    await self._emit_graph_progress(session)
                    await self._publish_session_state(session, "session.waiting_user_input")
                    return

                session.active_node_id = None
                await self._publish_session_state(session, "session.ready_for_dispatch")
                return

    def _resolve_max_drain_iterations(self, graph: ExecutionGraphState) -> int:
        """Return the guardrail threshold for one graph drain loop."""
        if self.config.max_drain_iterations is not None:
            return max(1, self.config.max_drain_iterations)
        return max(
            1,
            self.config.drain_iteration_floor,
            len(graph.nodes) * self.config.drain_iteration_multiplier,
        )

    async def _fail_drain_graph(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        *,
        max_iterations: int,
    ) -> None:
        """Fail the current graph when the drain loop exceeds its guardrail."""
        status_summary = {
            node.node_id: node.status.value
            for node in graph.nodes
        }
        message = (
            "执行图执行失败：graph drain 超过最大迭代次数"
            f"（{max_iterations}），疑似存在未收敛的节点状态"
        )
        logger.error(
            "Graph drain iteration guard tripped for session=%s graph=%s max_iterations=%s node_statuses=%s",
            session.session_id,
            graph.graph_id,
            max_iterations,
            status_summary,
        )
        graph.touch(GraphStatus.FAILED)
        graph.summary = f"{graph.summary}；{message}" if graph.summary else message
        session.active_node_id = None
        session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
        session.touch()
        await self._publish_graph_state(session, "graph.failed", message, status=TaskStatus.FAILED)
        await self._publish_session_state(session, "session.idle")

    async def _run_node(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        user_input: str,
    ) -> None:
        """Dispatch one node to its backing intent agent and consume streamed chunks."""
        with router_stage(
            logger,
            "orchestrator.run_node",
            graph_id=graph.graph_id,
            node_id=node.node_id,
            intent_code=node.intent_code,
            node_status=node.status.value,
        ):
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
        """Validate slot readiness and create the agent task for a ready node."""
        active_intents = self.intent_catalog.active_intents_by_code()
        intent = active_intents.get(node.intent_code)
        if intent is None:
            raise ValueError(f"Intent {node.intent_code} is no longer active")

        if self._intent_requires_slot_understanding(intent):
            validation = await self._validate_node_understanding(
                session,
                graph,
                node,
                intent=intent,
                current_message=dispatch_input,
            )
            if not validation.can_dispatch:
                # Slot fill stays in the router layer. The downstream agent only does
                # defensive slot checking and business execution.
                await self._mark_node_waiting_for_slots(session, graph, node, validation)
                return None
            session.last_diagnostics = list(validation.diagnostics or [])
        else:
            session.last_diagnostics = []
            node.diagnostics = []
            node.history_slot_keys = []

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
        session.enforce_task_limit(self.config.session_task_limit)
        node.task_id = task.task_id
        await self._publish_node_state(session, graph, node, task.status, "node.created", f"创建节点 {node.intent_code}")
        return task

    async def _prepare_node_router_only(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        *,
        dispatch_input: str,
    ) -> bool:
        """Validate router understanding and stop once the node is ready to dispatch."""
        active_intents = self.intent_catalog.active_intents_by_code()
        intent = active_intents.get(node.intent_code)
        if intent is None:
            raise ValueError(f"Intent {node.intent_code} is no longer active")

        if self._intent_requires_slot_understanding(intent):
            validation = await self._validate_node_understanding(
                session,
                graph,
                node,
                intent=intent,
                current_message=dispatch_input,
            )
            if not validation.can_dispatch:
                await self._mark_node_waiting_for_slots(session, graph, node, validation)
                return False
        else:
            session.last_diagnostics = []
            node.diagnostics = []
            node.history_slot_keys = []
        await self._mark_node_ready_for_dispatch(session, graph, node)
        return True

    async def _handle_agent_chunk(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        task: Task,
        chunk: Any,
    ) -> None:
        """Project each streamed agent chunk back into node/session/graph state."""
        raw_task_status = chunk.status
        node.slot_memory = dict(task.slot_memory)
        node.output_payload = dict(chunk.payload)
        node._agent_output = dict(getattr(chunk, "output", {}) or {})
        explicit_completion_state = node._agent_output.get("completion_state")
        if isinstance(explicit_completion_state, bool):
            explicit_completion_state = int(explicit_completion_state)
        if isinstance(explicit_completion_state, int) and explicit_completion_state in {0, 1, 2}:
            apply_completion_signal = getattr(node, "apply_completion_signal", None)
            if callable(apply_completion_signal):
                apply_completion_signal(source="agent", signal=explicit_completion_state)

        effective_task_status = raw_task_status
        if explicit_completion_state == 1 and raw_task_status == TaskStatus.COMPLETED:
            effective_task_status = TaskStatus.WAITING_ASSISTANT_COMPLETION

        task.touch(effective_task_status)
        node_status = self._node_status_for_task_status(effective_task_status)
        node.touch(node_status)

        if effective_task_status in {
            TaskStatus.WAITING_USER_INPUT,
            TaskStatus.WAITING_CONFIRMATION,
            TaskStatus.WAITING_ASSISTANT_COMPLETION,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        } and chunk.content:
            session.messages.append(ChatMessage(role="assistant", content=chunk.content, created_at=utc_now()))
            session.touch()

        event_name = {
            TaskStatus.WAITING_USER_INPUT: "node.waiting_user_input",
            TaskStatus.WAITING_CONFIRMATION: "node.waiting_confirmation",
            TaskStatus.WAITING_ASSISTANT_COMPLETION: "node.waiting_assistant_completion",
            TaskStatus.COMPLETED: "node.completed",
            TaskStatus.FAILED: "node.failed",
        }.get(effective_task_status, "node.message")

        await self.event_publisher.publish_node_runtime_event(
            session,
            graph,
            node,
            task_status=effective_task_status,
            event=event_name,
            message=chunk.content,
            ishandover=chunk.ishandover,
            payload={
                **dict(chunk.payload),
                "agent_output": dict(chunk.output),
            },
            source="agent",
        )
        if effective_task_status in {
            TaskStatus.WAITING_USER_INPUT,
            TaskStatus.WAITING_CONFIRMATION,
            TaskStatus.WAITING_ASSISTANT_COMPLETION,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        }:
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
        """Mark one node as failed and publish the corresponding runtime events."""
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
        """Run router-side slot extraction/validation before agent dispatch."""
        with router_stage(
            logger,
            "orchestrator.validate_node_understanding",
            graph_id=graph.graph_id,
            node_id=node.node_id,
            intent_code=intent.intent_code,
        ):
            long_term_memory = self.session_store.long_term_memory.recall(
                session.cust_id,
                limit=self.config.memory_recall_limit,
            )
            memory_candidates = list(long_term_memory)
            history_slot_values = self._history_slot_values(
                session,
                long_term_memory=long_term_memory,
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
            node.diagnostics = list(validation.diagnostics or [])
            logger.debug(
                "Router node understanding result (trace_id=%s, graph_id=%s, node_id=%s, intent_code=%s, can_dispatch=%s, missing_required_slots=%s, ambiguous_slot_keys=%s, invalid_slot_keys=%s)",
                current_trace_id(),
                graph.graph_id,
                node.node_id,
                intent.intent_code,
                validation.can_dispatch,
                validation.missing_required_slots,
                validation.ambiguous_slot_keys,
                validation.invalid_slot_keys,
            )
            return validation

    async def _mark_node_waiting_for_slots(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        validation: UnderstandingValidationResult,
    ) -> None:
        """Block the node in router space when required slots are still missing/ambiguous."""
        message = validation.prompt_message or "请补充当前事项所需信息"
        node.task_id = None
        node.touch(GraphNodeStatus.WAITING_USER_INPUT, blocking_reason=message)
        node.diagnostics = list(validation.diagnostics or [])
        graph.touch(GraphStatus.WAITING_USER_INPUT)
        session.last_diagnostics = list(validation.diagnostics or [])
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

    async def _mark_node_ready_for_dispatch(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
    ) -> None:
        """Freeze the node at the router boundary once intent and slots are ready."""
        message = (
            f"路由识别完成：事项「{node.title}」已具备执行条件，"
            "当前为 router_only 模式，未调用执行 agent"
        )
        node.task_id = None
        node.output_payload = {
            "router_only": True,
            "dispatch_ready": True,
            "intent_code": node.intent_code,
            "slot_memory": dict(node.slot_memory),
        }
        node.touch(GraphNodeStatus.READY_FOR_DISPATCH, blocking_reason=message)
        graph.touch(GraphStatus.READY_FOR_DISPATCH)
        session.last_diagnostics = list(node.diagnostics or [])
        session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
        session.touch()
        await self._publish_node_state(
            session,
            graph,
            node,
            TaskStatus.READY_FOR_DISPATCH,
            "node.ready_for_dispatch",
            message,
        )
        await self._publish_graph_state(
            session,
            "graph.ready_for_dispatch",
            message,
            status=TaskStatus.READY_FOR_DISPATCH,
        )

    async def _resume_waiting_node(
        self,
        session: GraphSessionState,
        node: GraphNodeState,
        content: str,
    ) -> None:
        """Resume the same node after the user answered a router-side slot prompt."""
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
        if session.router_only_mode:
            node.touch(GraphNodeStatus.READY)
            graph.touch(GraphStatus.RUNNING)
            if not await self._prepare_node_router_only(session, graph, node, dispatch_input=content):
                session.active_node_id = node.node_id
                await self._publish_session_state(session, "session.waiting_user_input")
                return
            session.active_node_id = None
            await self._publish_session_state(session, "session.ready_for_dispatch")
            return
        await self._run_node(session, graph, node, content)
        await self._drain_graph(session, content)

    async def _handle_pending_graph_turn(self, session: GraphSessionState, content: str) -> None:
        """Delegate pending-graph follow-up turns into the message flow."""
        await self.message_flow.handle_pending_graph_turn(session, content)

    async def _handle_waiting_node_turn(
        self,
        session: GraphSessionState,
        waiting_node: GraphNodeState,
        content: str,
    ) -> None:
        """Delegate waiting-node follow-up turns into the message flow."""
        await self.message_flow.handle_waiting_node_turn(session, waiting_node, content)

    async def _cancel_current_node(self, session: GraphSessionState, *, reason: str) -> None:
        """Delegate current-node cancellation into the action flow."""
        await self.action_flow.cancel_current_node(session, reason=reason)

    async def _cancel_current_graph(self, session: GraphSessionState, *, reason: str) -> None:
        """Delegate current-graph cancellation into the action flow."""
        await self.action_flow.cancel_current_graph(session, reason=reason)

    async def _confirm_pending_graph(
        self,
        session: GraphSessionState,
        *,
        graph_id: str | None,
        confirm_token: str | None,
    ) -> None:
        """Delegate pending-graph confirmation into the action flow."""
        await self.action_flow.confirm_pending_graph(
            session,
            graph_id=graph_id,
            confirm_token=confirm_token,
        )

    async def _cancel_pending_graph(
        self,
        session: GraphSessionState,
        *,
        graph_id: str | None,
        confirm_token: str | None,
    ) -> None:
        """Delegate pending-graph cancellation into the action flow."""
        await self.action_flow.cancel_pending_graph(
            session,
            graph_id=graph_id,
            confirm_token=confirm_token,
        )

    async def _publish_pending_graph(self, session: GraphSessionState) -> None:
        """Delegate pending-graph publication into the state-sync layer."""
        await self.state_sync.publish_pending_graph(session)

    async def _publish_graph_waiting_hint(self, session: GraphSessionState) -> None:
        """Delegate pending-graph waiting hints into the state-sync layer."""
        await self.state_sync.publish_graph_waiting_hint(session)

    async def _publish_no_match_hint(self, session: GraphSessionState) -> None:
        """Delegate the no-match hint into the state-sync layer."""
        await self.state_sync.publish_no_match_hint(session)

    async def _publish_graph_state(
        self,
        session: GraphSessionState,
        event: str,
        message: str,
        *,
        status: TaskStatus | None = None,
        payload_overrides: dict[str, Any] | None = None,
    ) -> None:
        """Delegate graph-state publication into the state-sync layer."""
        await self.state_sync.publish_graph_state(
            session,
            event,
            message,
            status=status,
            payload_overrides=payload_overrides,
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
        """Delegate node-state publication into the state-sync layer."""
        await self.state_sync.publish_node_state(session, graph, node, task_status, event, message)

    async def _publish_session_state(self, session: GraphSessionState, event: str) -> None:
        """Delegate session-state publication into the state-sync layer."""
        await self.state_sync.publish_session_state(session, event)

    async def _emit_graph_progress(self, session: GraphSessionState) -> None:
        """Delegate graph-progress emission into the state-sync layer."""
        await self.state_sync.emit_graph_progress(session)

    def _refresh_node_states(self, graph: ExecutionGraphState) -> None:
        """Delegate node-state recomputation into the state-sync layer."""
        self.state_sync.refresh_node_states(graph)

    def _condition_matches_from_condition(self, source: GraphNodeState, condition: GraphCondition | None) -> bool:
        """Delegate condition evaluation into the state-sync/runtime layers."""
        return self.state_sync.condition_matches_from_condition(source, condition)

    def _graph_status(self, graph: ExecutionGraphState) -> GraphStatus:
        """Delegate graph-status aggregation into the state-sync/runtime layers."""
        return self.state_sync.graph_status(graph)

    def _next_ready_node(self, graph: ExecutionGraphState) -> GraphNodeState | None:
        """Delegate ready-node lookup into the state-sync/runtime layers."""
        return self.state_sync.next_ready_node(graph)

    def _get_waiting_node(self, session: GraphSessionState) -> GraphNodeState | None:
        """Delegate waiting-node lookup into the state-sync/runtime layers."""
        return self.state_sync.get_waiting_node(session)

    def _get_task(self, session: GraphSessionState, task_id: str | None) -> Task | None:
        """Return one task from the session by id when it exists."""
        if task_id is None:
            return None
        for task in session.tasks:
            if task.task_id == task_id:
                return task
        return None

    async def _refresh_graph_state(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        """Delegate graph-state recomputation into the state-sync layer."""
        await self.state_sync.refresh_graph_state(session, graph)

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
        """Delegate history slot prefill into the state-sync/slot-resolution layers."""
        self.state_sync.apply_history_prefill_policy(
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
        """Delegate historical slot lookup into the state-sync/slot-resolution layers."""
        return self.state_sync.history_slot_values(
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
        """Delegate structured slot-binding creation into the state-sync layer."""
        return self.state_sync.structured_slot_bindings(
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
        """Delegate node slot-binding reconstruction into the state-sync layer."""
        self.state_sync.rebuild_node_slot_bindings(
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
        """Build the context payload sent to downstream agents for one graph node."""
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
        """Assemble recent messages plus long-term memory for recognition and agents."""
        long_term_memory = self.session_store.long_term_memory.recall(
            session.cust_id,
            limit=self.config.memory_recall_limit,
        )
        return self.context_builder.build_task_context(session, task=task, long_term_memory=long_term_memory)

    def _intent_requires_slot_understanding(self, intent: IntentDefinition) -> bool:
        """Return whether the router still needs to run slot extraction/validation for this intent."""
        return bool(intent.slot_schema)

    def _sanitize_recent_messages_for_planning(self, recent_messages: list[str]) -> list[str]:
        """Delegate planning-message sanitization into the message flow."""
        return self.message_flow.sanitize_recent_messages_for_planning(recent_messages)

    def _fallback_intent(self) -> IntentDefinition | None:
        """Return the configured fallback intent from the catalog when available."""
        getter = getattr(self.intent_catalog, "get_fallback_intent", None)
        if getter is None:
            return None
        return getter()

    def _node_status_for_task_status(self, status: TaskStatus) -> GraphNodeStatus:
        """Delegate task-to-node status translation into the state-sync/runtime layers."""
        return self.state_sync.node_status_for_task_status(status)

    def _task_status_for_graph(self, status: GraphStatus) -> TaskStatus:
        """Delegate graph-to-task status translation into the state-sync/runtime layers."""
        return self.state_sync.task_status_for_graph(status)

    def _guided_selection_display_content(self, guided_selection: GuidedSelectionPayload | None) -> str:
        """Delegate guided-selection display rendering into the graph compiler."""
        return self.graph_compiler.guided_selection_display_content(guided_selection)

    def _guided_selection_from_proactive_items(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> GuidedSelectionPayload:
        """Delegate proactive-item conversion into a guided-selection payload."""
        return self.graph_compiler.guided_selection_from_proactive_items(selected_items)

    def _augment_recent_messages_with_recommendations(
        self,
        recent_messages: list[str],
        *,
        recommendation_context: RecommendationContextPayload | None,
    ) -> list[str]:
        """Delegate recommendation-context augmentation into the graph compiler."""
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
        """Delegate proactive-selection augmentation into the graph compiler."""
        return self.graph_compiler.augment_recent_messages_with_proactive_selection(
            recent_messages,
            proactive_recommendation=proactive_recommendation,
            selected_items=selected_items,
        )

    def _recommendation_context_summary(self, recommendation_context: RecommendationContextPayload) -> str:
        """Delegate recommendation-context rendering into the graph compiler."""
        return self.graph_compiler.recommendation_context_summary(recommendation_context)

    def _proactive_recommendation_context_summary(
        self,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> str:
        """Delegate proactive recommendation rendering into the graph compiler."""
        return self.graph_compiler.proactive_recommendation_context_summary(proactive_recommendation)

    def _proactive_selection_summary(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> str:
        """Delegate proactive selection rendering into the graph compiler."""
        return self.graph_compiler.proactive_selection_summary(selected_items)

    def _guided_selection_summary(self, guided_selection: GuidedSelectionPayload) -> str:
        """Delegate guided-selection summary rendering into the graph compiler."""
        return self.graph_compiler.guided_selection_summary(guided_selection)

    def _recognition_from_proactive_items(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> RecognitionResult:
        """Delegate proactive-item recognition synthesis into the graph compiler."""
        return self.graph_compiler.recognition_from_proactive_items(selected_items)

    def _apply_proactive_slot_defaults(
        self,
        graph: ExecutionGraphState,
        *,
        selected_items: list[ProactiveRecommendationItem],
        proactive_recommendation: ProactiveRecommendationPayload | None,
        intents_by_code: dict[str, IntentDefinition],
    ) -> None:
        """Delegate proactive slot-default injection into the state-sync layer."""
        self.state_sync.apply_proactive_slot_defaults(
            graph,
            selected_items=selected_items,
            proactive_recommendation=proactive_recommendation,
            intents_by_code=intents_by_code,
        )
