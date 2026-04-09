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
    IntentMatch,
    RouterSnapshot,
    Task,
    TaskEvent,
    TaskStatus,
    utc_now,
)
from router_core.llm_client import llm_exception_is_retryable
from router_core.memory_store import LongTermMemoryStore
from router_core.recognizer import IntentRecognizer, RecognitionResult
from router_core.slot_grounding import (
    apply_history_slot_values,
    normalize_slot_memory,
    normalize_structured_slot_memory,
)
from router_core.v2_domain import (
    ExecutionGraphState,
    GraphAction,
    GraphCondition,
    GraphEdge,
    GraphEdgeType,
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
)
from router_core.v2_graph_semantics import repair_unexecutable_condition_edges, resolve_output_value
from router_core.v2_graph_builder import GraphBuildResult, IntentGraphBuilder
from router_core.v2_planner import (
    BasicTurnInterpreter,
    IntentGraphPlanner,
    SequentialIntentGraphPlanner,
    TurnDecisionPayload,
    TurnInterpreter,
)
from router_core.v2_recommendation_router import (
    NullProactiveRecommendationRouter,
    ProactiveRecommendationRouter,
)


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
ACTIVE_NODE_STATUSES = {
    GraphNodeStatus.RUNNING,
    GraphNodeStatus.WAITING_USER_INPUT,
    GraphNodeStatus.WAITING_CONFIRMATION,
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
        graph_builder: IntentGraphBuilder | None = None,
        planner: IntentGraphPlanner | None = None,
        turn_interpreter: TurnInterpreter | None = None,
        recommendation_router: ProactiveRecommendationRouter | None = None,
        context_builder: ContextBuilder | None = None,
        agent_client: AgentClient | None = None,
        config: GraphRouterOrchestratorConfig | None = None,
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
        display_content = message_content or self._guided_selection_display_content(guided_selection)
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
            guided_selection = self._guided_selection_from_proactive_items(selected_items)
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
        graph: ExecutionGraphState | None = None
        if recognition is None and (recent_messages is None or long_term_memory is None):
            context = self._build_session_context(session)
            recent_messages = context["recent_messages"]
            long_term_memory = context["long_term_memory"]
        else:
            recent_messages = recent_messages or []
            long_term_memory = long_term_memory or []
        recent_messages = self._sanitize_recent_messages_for_planning(recent_messages)
        recent_messages = self._augment_recent_messages_with_recommendations(
            recent_messages,
            recommendation_context=recommendation_context,
        )

        if self.graph_builder is not None:
            build_result = await self._build_graph_from_message(
                session,
                content,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                recognition=recognition,
                emit_events=True,
            )
            recognition = build_result.recognition
            graph = build_result.graph
        elif recognition is None:
            recognition = await self._recognize_message(
                session,
                content,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                emit_events=True,
            )
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

        if graph is None:
            graph = await self.planner.plan(
                message=content,
                matches=matches,
                intents_by_code=active_intents,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
            )
        repair_unexecutable_condition_edges(graph=graph, intents_by_code=active_intents)
        self._apply_proactive_slot_defaults(
            graph,
            selected_items=proactive_defaults or [],
            proactive_recommendation=proactive_recommendation,
            intents_by_code=active_intents,
        )
        if not skip_history_prefill:
            self._apply_history_prefill_policy(
                session,
                graph,
                source_message=content,
                intents_by_code=active_intents,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
            )
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
        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        graph = ExecutionGraphState(
            source_message=content,
            summary=self._guided_selection_summary(guided_selection),
            status=GraphStatus.DRAFT,
            actions=[],
        )
        session.candidate_intents = []

        for index, selected in enumerate(guided_selection.selected_intents):
            intent = active_intents.get(selected.intent_code)
            if intent is None:
                raise ValueError(f"Selected intent is not active: {selected.intent_code}")
            slot_memory = normalize_structured_slot_memory(
                slot_memory=selected.slot_memory,
                slot_schema=intent.slot_schema,
            )
            graph.nodes.append(
                GraphNodeState(
                    intent_code=intent.intent_code,
                    title=selected.title or intent.name,
                    confidence=1.0,
                    position=index,
                    source_fragment=content or selected.source_fragment or "",
                    slot_memory=slot_memory,
                )
            )

        for index in range(1, len(graph.nodes)):
            previous = graph.nodes[index - 1]
            current = graph.nodes[index]
            current.depends_on.append(previous.node_id)
            current.relation_reason = "按已选顺序执行"
            graph.edges.append(
                GraphEdge(
                    source_node_id=previous.node_id,
                    target_node_id=current.node_id,
                    relation_type=GraphEdgeType.SEQUENTIAL,
                    label="按已选顺序执行",
                )
            )

        repair_unexecutable_condition_edges(graph=graph, intents_by_code=active_intents)
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
        context = self._build_session_context(session)
        await self._route_new_message(
            session,
            content,
            recognition=self._recognition_from_proactive_items(selected_items),
            recent_messages=self._augment_recent_messages_with_proactive_selection(
                context["recent_messages"],
                proactive_recommendation=proactive_recommendation,
                selected_items=selected_items,
            ),
            long_term_memory=context["long_term_memory"],
            proactive_defaults=selected_items,
            proactive_recommendation=proactive_recommendation,
            skip_history_prefill=True,
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
        if emit_events:
            await self._publish(
                TaskEvent(
                    event="graph_builder.started",
                    task_id="graph_builder",
                    session_id=session.session_id,
                    intent_code="graph_builder",
                    status=TaskStatus.RUNNING,
                    message="开始统一识别与建图",
                    payload={"cust_id": session.cust_id},
                )
            )

        async def publish_graph_builder_delta(delta: str) -> None:
            if not emit_events or not delta:
                return
            await self._publish(
                TaskEvent(
                    event="graph_builder.delta",
                    task_id="graph_builder",
                    session_id=session.session_id,
                    intent_code="graph_builder",
                    status=TaskStatus.RUNNING,
                    message=delta,
                    payload={"cust_id": session.cust_id},
                )
            )

        if self.graph_builder is None:
            raise RuntimeError("graph_builder is not configured")
        result = await self.graph_builder.build(
            message=content,
            intents=self.intent_catalog.list_active(),
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            recognition=recognition,
            on_delta=publish_graph_builder_delta if emit_events else None,
        )
        if emit_events:
            await self._publish(
                TaskEvent(
                    event="graph_builder.completed",
                    task_id="graph_builder",
                    session_id=session.session_id,
                    intent_code="graph_builder",
                    status=TaskStatus.COMPLETED,
                    message="统一识别与建图完成",
                    payload={
                        "cust_id": session.cust_id,
                        "primary": [match.model_dump() for match in result.recognition.primary],
                        "candidates": [match.model_dump() for match in result.recognition.candidates],
                        "graph": self._graph_payload(
                            result.graph,
                            include_actions=result.graph.status == GraphStatus.WAITING_CONFIRMATION,
                            pending=result.graph.status == GraphStatus.WAITING_CONFIRMATION,
                        ),
                    },
                )
            )
        return result

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
        if task is None:
            task = await self._create_task_for_node(session, graph, node)

        previous_initial_source_input = task.input_context.get("initial_source_input")
        if not (isinstance(previous_initial_source_input, str) and previous_initial_source_input):
            previous_initial_source_input = node.source_fragment or graph.source_message
        effective_user_input = (node.source_fragment or user_input) if created_new_task else user_input
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
    ) -> Task:
        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        intent = active_intents.get(node.intent_code)
        if intent is None:
            raise ValueError(f"Intent {node.intent_code} is no longer active")

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
        await self._refresh_graph_state(session, graph)
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
        await self._drain_graph(session, content)

    async def _handle_pending_graph_turn(self, session: GraphSessionState, content: str) -> None:
        pending_graph = session.pending_graph
        if pending_graph is None:
            return
        try:
            recognition = await self._recognize_message(
                session,
                content,
                recent_messages=[],
                long_term_memory=[],
                emit_events=False,
            )
        except Exception as exc:
            if not llm_exception_is_retryable(exc):
                raise
            logger.warning("Pending graph recognition unavailable, falling back to conservative wait", exc_info=True)
            recognition = RecognitionResult(primary=[], candidates=[])
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
        try:
            recognition = await self._recognize_message(
                session,
                content,
                recent_messages=[],
                long_term_memory=[],
                emit_events=False,
            )
        except Exception as exc:
            if not llm_exception_is_retryable(exc):
                raise
            logger.warning("Waiting node recognition unavailable, continuing current node conservatively", exc_info=True)
            recognition = RecognitionResult(primary=[], candidates=[])
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
            await self._drain_graph(session, content)
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
        previous_status = graph.status
        graph.touch(self._graph_status(graph))
        event_name = self._graph_event_name(graph.status)
        message = self._graph_message(graph)
        if self._should_append_graph_terminal_message(graph, previous_status):
            session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
            session.touch()
        await self._publish_graph_state(session, event_name, message)

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
            skip_reason_code: str | None = None
            for edge in incoming_edges:
                source = graph.node_by_id(edge.source_node_id)
                if source.status == GraphNodeStatus.FAILED:
                    should_skip = True
                    blocking_reason = edge.label or "上游节点未满足依赖"
                    skip_reason_code = GraphNodeSkipReason.UPSTREAM_FAILED.value
                    break
                if source.status == GraphNodeStatus.CANCELLED:
                    should_skip = True
                    blocking_reason = edge.label or "上游节点未满足依赖"
                    skip_reason_code = GraphNodeSkipReason.UPSTREAM_CANCELLED.value
                    break
                if source.status == GraphNodeStatus.SKIPPED:
                    should_skip = True
                    blocking_reason = edge.label or "上游节点未满足依赖"
                    skip_reason_code = GraphNodeSkipReason.UPSTREAM_SKIPPED.value
                    break
                expected_statuses = (
                    edge.condition.expected_statuses
                    if edge.condition is not None and edge.condition.expected_statuses
                    else [GraphNodeStatus.COMPLETED.value]
                )
                if source.status.value in expected_statuses:
                    if edge.condition is not None and edge.condition.left_key is not None:
                        if self._condition_matches_from_condition(source, edge.condition):
                            continue
                        should_skip = True
                        blocking_reason = edge.label or "条件依赖未满足"
                        skip_reason_code = GraphNodeSkipReason.CONDITION_NOT_MET.value
                        break
                    continue
                if source.status in TERMINAL_NODE_STATUSES:
                    should_skip = True
                    blocking_reason = edge.label or "条件依赖未满足"
                    skip_reason_code = GraphNodeSkipReason.CONDITION_NOT_MET.value
                    break
                all_ready = False

            if should_skip:
                node.touch(
                    GraphNodeStatus.SKIPPED,
                    blocking_reason=blocking_reason,
                    skip_reason_code=skip_reason_code,
                )
            elif all_ready:
                node.touch(GraphNodeStatus.READY)
            else:
                node.touch(GraphNodeStatus.BLOCKED, blocking_reason=blocking_reason)

    def _condition_matches_from_condition(self, source: GraphNodeState, condition: GraphCondition | None) -> bool:
        if condition is None or condition.left_key is None or condition.operator is None:
            return False
        current_value = resolve_output_value(source.output_payload, condition.left_key)
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
            return GraphStatus.RUNNING
        if all(status in {GraphNodeStatus.CANCELLED, GraphNodeStatus.SKIPPED} for status in statuses):
            return GraphStatus.CANCELLED
        if all(status in {GraphNodeStatus.COMPLETED, GraphNodeStatus.SKIPPED} for status in statuses):
            return (
                GraphStatus.COMPLETED
                if self._all_skipped_nodes_are_condition_unmet(graph)
                else GraphStatus.PARTIALLY_COMPLETED
            )
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

    async def _refresh_graph_state(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        previous_statuses = {node.node_id: node.status for node in graph.nodes}
        self._refresh_node_states(graph)
        graph_status = self._graph_status(graph)

        for node in graph.nodes:
            previous_status = previous_statuses.get(node.node_id)
            if previous_status == node.status or node.status != GraphNodeStatus.SKIPPED:
                continue
            message = self._skipped_node_message(node)
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
        history_nodes: list[GraphNodeState] = []
        history_texts = [*recent_messages, *long_term_memory]
        history_slot_values = self._history_slot_values(session, long_term_memory=long_term_memory)

        for node in graph.nodes:
            intent = intents_by_code.get(node.intent_code)
            if intent is None:
                continue
            slot_memory, history_slot_keys = normalize_slot_memory(
                slot_memory=node.slot_memory,
                slot_schema=intent.slot_schema,
                grounding_text=f"{source_message}\n{node.source_fragment or ''}",
                history_texts=history_texts,
            )
            slot_memory, injected_history_keys = apply_history_slot_values(
                slot_memory=slot_memory,
                slot_schema=intent.slot_schema,
                history_slot_values=history_slot_values,
            )
            for slot_key in injected_history_keys:
                if slot_key not in history_slot_keys:
                    history_slot_keys.append(slot_key)
            node.slot_memory = slot_memory
            node.history_slot_keys = history_slot_keys
            if history_slot_keys:
                history_nodes.append(node)

        if not history_nodes:
            return

        history_notes = "；".join(
            f"{node.title} 复用历史槽位 {', '.join(node.history_slot_keys)}"
            for node in history_nodes
        )
        summary_note = f"检测到历史信息复用：{history_notes}，请确认后执行"
        summary = graph.summary.strip()
        if summary_note not in summary:
            graph.summary = f"{summary}。{summary_note}" if summary else summary_note
        graph.touch(GraphStatus.WAITING_CONFIRMATION)
        if not graph.actions:
            graph.actions = [
                GraphAction(code="confirm_graph", label="开始执行"),
                GraphAction(code="cancel_graph", label="取消"),
            ]

    def _history_slot_values(
        self,
        session: GraphSessionState,
        *,
        long_term_memory: list[str],
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}

        for task in reversed(session.tasks):
            if not task.slot_memory:
                continue
            for key, value in task.slot_memory.items():
                if key in values or value is None:
                    continue
                values[key] = value

        for entry in reversed(long_term_memory):
            if ":" not in entry or "=" not in entry:
                continue
            _, raw_pairs = entry.split(":", 1)
            for raw_pair in raw_pairs.split(","):
                if "=" not in raw_pair:
                    continue
                key, raw_value = raw_pair.split("=", 1)
                slot_key = key.strip()
                slot_value = raw_value.strip()
                if not slot_key or not slot_value or slot_key in values:
                    continue
                values[slot_key] = slot_value

        return values

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
                "graph": self._graph_payload(graph),
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
            GraphStatus.PARTIALLY_COMPLETED: TaskStatus.COMPLETED,
            GraphStatus.COMPLETED: TaskStatus.COMPLETED,
            GraphStatus.FAILED: TaskStatus.FAILED,
            GraphStatus.CANCELLED: TaskStatus.CANCELLED,
        }
        return mapping[status]

    def _graph_event_name(self, status: GraphStatus) -> str:
        if status == GraphStatus.COMPLETED:
            return "graph.completed"
        if status == GraphStatus.PARTIALLY_COMPLETED:
            return "graph.partially_completed"
        if status == GraphStatus.FAILED:
            return "graph.failed"
        if status == GraphStatus.CANCELLED:
            return "graph.cancelled"
        return "graph.updated"

    def _graph_message(self, graph: ExecutionGraphState) -> str:
        status = graph.status
        condition_skips = self._condition_skipped_nodes(graph)
        if status == GraphStatus.COMPLETED:
            if condition_skips:
                summaries = "；".join(
                    f"节点「{node.title}」因条件未满足未执行"
                    + (f"（{node.blocking_reason}）" if node.blocking_reason else "")
                    for node in condition_skips
                )
                return f"执行图已完成：{summaries}"
            return "执行图已完成"
        if status == GraphStatus.PARTIALLY_COMPLETED:
            return "执行图部分完成，存在已完成节点之外的未执行或异常终止节点"
        if status == GraphStatus.FAILED:
            return "执行图执行失败"
        if status == GraphStatus.CANCELLED:
            return "执行图已取消"
        if status == GraphStatus.WAITING_USER_INPUT:
            return "执行图等待用户补充信息"
        if status == GraphStatus.WAITING_CONFIRMATION_NODE:
            return "执行图等待节点确认"
        return "执行图状态更新"

    def _condition_skipped_nodes(self, graph: ExecutionGraphState) -> list[GraphNodeState]:
        return [
            node
            for node in graph.nodes
            if node.status == GraphNodeStatus.SKIPPED
            and node.skip_reason_code == GraphNodeSkipReason.CONDITION_NOT_MET.value
        ]

    def _all_skipped_nodes_are_condition_unmet(self, graph: ExecutionGraphState) -> bool:
        skipped_nodes = [node for node in graph.nodes if node.status == GraphNodeStatus.SKIPPED]
        return all(
            node.skip_reason_code == GraphNodeSkipReason.CONDITION_NOT_MET.value
            for node in skipped_nodes
        )

    def _guided_selection_display_content(self, guided_selection: GuidedSelectionPayload | None) -> str:
        if guided_selection is None or not guided_selection.selected_intents:
            return ""
        titles = [selected.title or selected.intent_code for selected in guided_selection.selected_intents]
        return f"已选择推荐事项：{'、'.join(titles)}"

    def _guided_selection_from_proactive_items(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> GuidedSelectionPayload:
        return GuidedSelectionPayload.model_validate(
            {
                "selectedIntents": [
                    {
                        "intentCode": item.intent_code,
                        "title": item.title,
                        "sourceFragment": item.title,
                        "slotMemory": item.slot_memory,
                    }
                    for item in selected_items
                ]
            }
        )

    def _augment_recent_messages_with_recommendations(
        self,
        recent_messages: list[str],
        *,
        recommendation_context: RecommendationContextPayload | None,
    ) -> list[str]:
        if recommendation_context is None or not recommendation_context.intents:
            return recent_messages
        return [
            *recent_messages,
            self._recommendation_context_summary(recommendation_context),
        ]

    def _augment_recent_messages_with_proactive_selection(
        self,
        recent_messages: list[str],
        *,
        proactive_recommendation: ProactiveRecommendationPayload,
        selected_items: list[ProactiveRecommendationItem],
    ) -> list[str]:
        return [
            *recent_messages,
            self._proactive_recommendation_context_summary(proactive_recommendation),
            self._proactive_selection_summary(selected_items),
        ]

    def _recommendation_context_summary(self, recommendation_context: RecommendationContextPayload) -> str:
        lines = [
            "[FRONTEND_RECOMMENDATION_CONTEXT] 以下是前端刚展示给用户的推荐候选事项；它们只是候选，不代表用户已经选择。",
        ]
        for index, item in enumerate(recommendation_context.intents, start=1):
            example = item.examples[0] if item.examples else ""
            lines.append(
                f"{index}. {item.title or item.intent_code} ({item.intent_code})"
                f" - {item.description or ''}".rstrip()
            )
            if example:
                lines.append(f"   例如：{example}")
        if recommendation_context.recommendation_id:
            lines.append(f"recommendation_id={recommendation_context.recommendation_id}")
        return "\n".join(lines)

    def _proactive_recommendation_context_summary(
        self,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> str:
        lines = [
            "[PROACTIVE_RECOMMENDATION_CONTEXT] 以下是系统本轮展示给用户的主动推荐事项；每项都带有原始默认要素。",
        ]
        if proactive_recommendation.intro_text:
            lines.append(f"intro_text={proactive_recommendation.intro_text}")
        if proactive_recommendation.shared_slot_memory:
            lines.append(f"shared_slot_memory={proactive_recommendation.shared_slot_memory}")
        for index, item in enumerate(proactive_recommendation.items, start=1):
            lines.append(
                f"{index}. {item.title} ({item.intent_code})"
                f" recommendation_item_id={item.recommendation_item_id}"
            )
            if item.description:
                lines.append(f"   description={item.description}")
            if item.slot_memory:
                lines.append(f"   slot_memory={item.slot_memory}")
        return "\n".join(lines)

    def _proactive_selection_summary(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> str:
        lines = [
            "[PROACTIVE_RECOMMENDATION_SELECTION] 以下推荐事项已由上游分流器选中；当前用户消息可能会修改其中部分要素或新增关系。",
        ]
        for index, item in enumerate(selected_items, start=1):
            lines.append(
                f"{index}. {item.title} ({item.intent_code})"
                f" recommendation_item_id={item.recommendation_item_id}"
            )
            if item.slot_memory:
                lines.append(f"   slot_memory={item.slot_memory}")
        return "\n".join(lines)

    def _guided_selection_summary(self, guided_selection: GuidedSelectionPayload) -> str:
        titles = [selected.title or selected.intent_code for selected in guided_selection.selected_intents]
        return (
            f"已按用户选择生成执行图：{'、'.join(titles)}"
            if titles
            else "已按用户选择生成执行图"
        )

    def _recognition_from_proactive_items(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> RecognitionResult:
        matches: list[IntentMatch] = []
        for index, item in enumerate(selected_items):
            matches.append(
                IntentMatch(
                    intent_code=item.intent_code,
                    confidence=max(0.5, round(0.99 - (index * 0.01), 2)),
                    reason="selected_from_proactive_recommendation",
                )
            )
        return RecognitionResult(primary=matches, candidates=[])

    def _apply_proactive_slot_defaults(
        self,
        graph: ExecutionGraphState,
        *,
        selected_items: list[ProactiveRecommendationItem],
        proactive_recommendation: ProactiveRecommendationPayload | None,
        intents_by_code: dict[str, IntentDefinition],
    ) -> None:
        if not selected_items and proactive_recommendation is None:
            return

        items_by_intent: dict[str, list[ProactiveRecommendationItem]] = {}
        for item in selected_items:
            items_by_intent.setdefault(item.intent_code, []).append(item)
        fallback_items_by_intent: dict[str, list[ProactiveRecommendationItem]] = {}
        if proactive_recommendation is not None:
            for item in proactive_recommendation.items:
                fallback_items_by_intent.setdefault(item.intent_code, []).append(item)
        shared_slot_memory = (
            dict(proactive_recommendation.shared_slot_memory)
            if proactive_recommendation is not None
            else {}
        )

        for node in graph.nodes:
            intent = intents_by_code.get(node.intent_code)
            if intent is None:
                continue
            allowed_slot_keys = {slot.slot_key for slot in intent.slot_schema}
            selected_item: ProactiveRecommendationItem | None = None
            candidates = items_by_intent.get(node.intent_code)
            if candidates:
                selected_item = candidates.pop(0)
            elif fallback_items_by_intent.get(node.intent_code):
                selected_item = fallback_items_by_intent[node.intent_code][0]

            merged_slot_memory: dict[str, Any] = {}
            if shared_slot_memory:
                merged_slot_memory.update(
                    {
                        key: value
                        for key, value in shared_slot_memory.items()
                        if key in allowed_slot_keys
                    }
                )
            if selected_item is not None and selected_item.slot_memory:
                merged_slot_memory.update(
                    {
                        key: value
                        for key, value in selected_item.slot_memory.items()
                        if key in allowed_slot_keys
                    }
                )
            merged_slot_memory.update(node.slot_memory)
            node.slot_memory = normalize_structured_slot_memory(
                slot_memory=merged_slot_memory,
                slot_schema=intent.slot_schema,
            )
            if selected_item is not None:
                if not node.title:
                    node.title = selected_item.title
                if not node.source_fragment:
                    node.source_fragment = selected_item.title

    def _should_append_graph_terminal_message(
        self,
        graph: ExecutionGraphState,
        previous_status: GraphStatus,
    ) -> bool:
        if graph.status not in {
            GraphStatus.COMPLETED,
            GraphStatus.PARTIALLY_COMPLETED,
            GraphStatus.FAILED,
            GraphStatus.CANCELLED,
        }:
            return False
        if previous_status == graph.status:
            return False
        return graph.status != GraphStatus.COMPLETED or bool(self._condition_skipped_nodes(graph))

    def _skipped_node_message(self, node: GraphNodeState) -> str:
        if node.skip_reason_code == GraphNodeSkipReason.CONDITION_NOT_MET.value:
            if node.blocking_reason:
                return f"节点「{node.title}」未执行：条件不满足（{node.blocking_reason}）"
            return f"节点「{node.title}」未执行：条件不满足"
        if node.blocking_reason:
            return f"节点「{node.title}」已跳过（{node.blocking_reason}）"
        return f"节点「{node.title}」已跳过"

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
            "skip_reason_code": node.skip_reason_code,
            "relation_reason": node.relation_reason,
            "slot_memory": dict(node.slot_memory),
            "history_slot_keys": list(node.history_slot_keys),
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
