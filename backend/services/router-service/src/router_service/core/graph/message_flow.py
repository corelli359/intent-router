from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from router_service.core.shared.domain import ChatMessage, utc_now
from router_service.core.graph.compiler import GraphCompiler
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphNodeState,
    GraphRouterSnapshot,
    GraphSessionState,
    GraphStatus,
    GuidedSelectionPayload,
    ProactiveRecommendationItem,
    ProactiveRecommendationPayload,
    ProactiveRecommendationRouteMode,
    RecommendationContextPayload,
)
from router_service.core.graph.session_store import GraphSessionStore
from router_service.core.graph.state_sync import GraphStateSync
from router_service.core.recognition.understanding_service import IntentUnderstandingService
from router_service.core.support.llm_client import llm_exception_is_retryable
from router_service.core.graph.recommendation_router import ProactiveRecommendationRouter
from router_service.core.support.trace_logging import current_trace_id, router_stage


logger = logging.getLogger(__name__)

PLANNING_RECENT_MESSAGE_PREFIXES = (
    "user:",
    "[FRONTEND_RECOMMENDATION_CONTEXT]",
    "[PROACTIVE_RECOMMENDATION_CONTEXT]",
    "[PROACTIVE_RECOMMENDATION_SELECTION]",
)

TERMINAL_GRAPH_STATUSES = {
    GraphStatus.READY_FOR_DISPATCH,
    GraphStatus.COMPLETED,
    GraphStatus.PARTIALLY_COMPLETED,
    GraphStatus.FAILED,
    GraphStatus.CANCELLED,
}


class GraphMessageFlow:
    """Owns all router decisions triggered by a new user message.

    One incoming turn can enter the system in several states:
    - free dialog with no active graph
    - pending graph waiting for graph-level confirmation
    - current graph with a node waiting for more slots/confirmation
    - guided selection from recommendation UI
    - proactive recommendation payload from an upstream recommender
    """

    def __init__(
        self,
        *,
        session_store: GraphSessionStore,
        graph_compiler: GraphCompiler,
        understanding_service: IntentUnderstandingService,
        recommendation_router: ProactiveRecommendationRouter,
        state_sync: GraphStateSync,
        snapshot_session: Callable[[str], GraphRouterSnapshot],
        get_waiting_node: Callable[[GraphSessionState], GraphNodeState | None],
        build_session_context: Callable[[GraphSessionState], dict[str, Any]],
        activate_graph: Callable[[ExecutionGraphState], None],
        drain_graph: Callable[[GraphSessionState, str], Awaitable[None]],
        cancel_pending_graph: Callable[[GraphSessionState, str | None, str | None], Awaitable[None]],
        cancel_current_graph: Callable[[GraphSessionState, str], Awaitable[None]],
        confirm_pending_graph: Callable[[GraphSessionState, str | None, str | None], Awaitable[None]],
        resume_waiting_node: Callable[[GraphSessionState, GraphNodeState, str], Awaitable[None]],
        cancel_current_node: Callable[[GraphSessionState, str], Awaitable[None]],
        session_business_limit: int = 5,
        memory_recall_limit: int = 20,
    ) -> None:
        """Initialize message-driven routing with graph compilation and control callbacks."""
        self.session_store = session_store
        self.graph_compiler = graph_compiler
        self.understanding_service = understanding_service
        self.recommendation_router = recommendation_router
        self.state_sync = state_sync
        self.snapshot_session = snapshot_session
        self.get_waiting_node = get_waiting_node
        self.build_session_context = build_session_context
        self.activate_graph = activate_graph
        self.drain_graph = drain_graph
        self.cancel_pending_graph = cancel_pending_graph
        self.cancel_current_graph = cancel_current_graph
        self.confirm_pending_graph = confirm_pending_graph
        self.resume_waiting_node = resume_waiting_node
        self.cancel_current_node = cancel_current_node
        self.session_business_limit = session_business_limit
        self.memory_recall_limit = memory_recall_limit

    def _ensure_session_memory_warm(self, session: GraphSessionState) -> None:
        """Warm the session-scoped memory workset when the backing store supports it."""
        ensure_session_memory = getattr(self.session_store, "ensure_session_memory", None)
        if callable(ensure_session_memory):
            ensure_session_memory(session)
            return
        memory_runtime = getattr(self.session_store, "memory_runtime", None)
        if memory_runtime is None:
            return
        ensure_runtime_memory = getattr(memory_runtime, "ensure_session_memory", None)
        if callable(ensure_runtime_memory):
            ensure_runtime_memory(
                session_id=session.session_id,
                cust_id=session.cust_id,
                recall_limit=self.memory_recall_limit,
            )

    def _attach_business(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        *,
        pending: bool,
    ) -> None:
        """Attach a new business object and trim the live workset when possible."""
        session.attach_business(
            graph,
            router_only_mode=session.router_only_mode,
            pending=pending,
        )
        session.enforce_business_limit(self.session_business_limit)

    async def handle_user_message(
        self,
        session_id: str,
        cust_id: str,
        content: str,
        *,
        router_only: bool = False,
        guided_selection: GuidedSelectionPayload | None = None,
        recommendation_context: RecommendationContextPayload | None = None,
        proactive_recommendation: ProactiveRecommendationPayload | None = None,
        return_snapshot: bool = True,
        emit_events: bool = False,
    ) -> GraphRouterSnapshot | None:
        """Entry point for all message-driven routing."""
        with router_stage(
            logger,
            "message_flow.handle_user_message",
            router_only=router_only,
            has_guided_selection=guided_selection is not None,
            has_recommendation_context=recommendation_context is not None,
            has_proactive_recommendation=proactive_recommendation is not None,
            emit_events=emit_events,
        ):
            session = self.session_store.get_or_create(session_id, cust_id)
            self._ensure_session_memory_warm(session)
            session.last_diagnostics = []
            if session.current_graph is None and session.pending_graph is None:
                session.restore_latest_suspended_business()
            session.set_router_only_mode(router_only)
            message_content = content.strip()
            display_content = message_content or self.graph_compiler.guided_selection_display_content(guided_selection)
            if display_content:
                session.messages.append(ChatMessage(role="user", content=display_content))
            session.touch()

            try:
                if proactive_recommendation is not None:
                    await self.handle_proactive_recommendation_turn(
                        session,
                        content=message_content,
                        proactive_recommendation=proactive_recommendation,
                        emit_events=emit_events,
                    )
                    return self.snapshot_session(session.session_id) if return_snapshot else None

                if guided_selection is not None:
                    await self.handle_guided_selection_turn(
                        session,
                        content=message_content,
                        guided_selection=guided_selection,
                    )
                    return self.snapshot_session(session.session_id) if return_snapshot else None

                if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
                    await self.handle_pending_graph_turn(session, message_content, emit_events=emit_events)
                    return self.snapshot_session(session.session_id) if return_snapshot else None

                waiting_node = self.get_waiting_node(session)
                if waiting_node is not None:
                    await self.handle_waiting_node_turn(
                        session,
                        waiting_node,
                        message_content,
                        emit_events=emit_events,
                    )
                    return self.snapshot_session(session.session_id) if return_snapshot else None

                await self.route_new_message(
                    session,
                    message_content,
                    recommendation_context=recommendation_context,
                    emit_events=emit_events,
                )
            except Exception as exc:
                if not llm_exception_is_retryable(exc):
                    raise
                logger.debug("Graph router LLM is temporarily unavailable", exc_info=True)
                session.messages.append(
                    ChatMessage(
                        role="assistant",
                        content="当前意图识别服务繁忙，请稍后重试。",
                        created_at=utc_now(),
                    )
                )
                session.touch()
            logger.debug(
                "Message flow result (trace_id=%s, session_id=%s, current_graph_status=%s, pending_graph_status=%s, active_node_id=%s)",
                current_trace_id(),
                session.session_id,
                session.current_graph.status.value if session.current_graph is not None else None,
                session.pending_graph.status.value if session.pending_graph is not None else None,
                session.active_node_id,
            )
            return self.snapshot_session(session.session_id) if return_snapshot else None

    async def handle_proactive_recommendation_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        proactive_recommendation: ProactiveRecommendationPayload,
        emit_events: bool = False,
    ) -> None:
        """Handle one proactive recommendation turn from the upstream recommender."""
        with router_stage(
            logger,
            "message_flow.handle_proactive_recommendation_turn",
            item_count=len(proactive_recommendation.items),
        ):
            if not proactive_recommendation.items:
                raise ValueError("proactive_recommendation.items is required")
            if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
                session.suspend_pending_business(reason="用户切换到主动推荐事项处理")
            if session.current_graph is not None and session.current_graph.status not in TERMINAL_GRAPH_STATUSES:
                session.suspend_focus_business(reason="用户切换到主动推荐事项处理")

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
                await self.state_sync.publish_session_state(session, "session.idle")
                return

            if decision.route_mode == ProactiveRecommendationRouteMode.SWITCH_TO_FREE_DIALOG:
                await self.route_new_message(session, content, emit_events=emit_events)
                return

            if not selected_items:
                await self.state_sync.publish_no_match_hint(session)
                return

            if (
                decision.route_mode == ProactiveRecommendationRouteMode.DIRECT_EXECUTE
                and any(not item.allow_direct_execute for item in selected_items)
            ):
                decision.route_mode = ProactiveRecommendationRouteMode.INTERACTIVE_GRAPH

            if decision.route_mode == ProactiveRecommendationRouteMode.DIRECT_EXECUTE:
                guided_selection = self.graph_compiler.guided_selection_from_proactive_items(selected_items)
                await self.route_guided_selection(session, content="", guided_selection=guided_selection)
                return

            await self.route_proactive_interactive_graph(
                session,
                content=content,
                proactive_recommendation=proactive_recommendation,
                selected_items=selected_items,
                emit_events=emit_events,
            )

    async def handle_guided_selection_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        guided_selection: GuidedSelectionPayload,
    ) -> None:
        """Replace any in-flight graph state with a deterministic selected-intent graph."""
        with router_stage(
            logger,
            "message_flow.handle_guided_selection_turn",
            selected_intent_count=len(guided_selection.selected_intents),
        ):
            if not guided_selection.selected_intents:
                raise ValueError("guided_selection.selected_intents is required")
            if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
                session.suspend_pending_business(reason="用户切换为引导式已选意图执行")
            if session.current_graph is not None and session.current_graph.status not in TERMINAL_GRAPH_STATUSES:
                session.suspend_focus_business(reason="用户切换为引导式已选意图执行")
            await self.route_guided_selection(session, content=content, guided_selection=guided_selection)

    async def route_new_message(
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
        emit_events: bool = False,
    ) -> None:
        """Compile and route a normal free-form user message."""
        with router_stage(
            logger,
            "message_flow.route_new_message",
            recognition_hint=recognition is not None,
            recommendation_context=recommendation_context is not None,
            proactive_defaults=len(proactive_defaults or []),
            skip_history_prefill=skip_history_prefill,
        ):
            compile_result = await self.graph_compiler.compile_message(
                session,
                content,
                build_session_context=self.build_session_context,
                sanitize_recent_messages_for_planning=self.sanitize_recent_messages_for_planning,
                recognition=recognition,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                recommendation_context=recommendation_context,
                proactive_defaults=proactive_defaults,
                proactive_recommendation=proactive_recommendation,
                skip_history_prefill=skip_history_prefill,
                exclude_current_turn_from_context=True,
                emit_events=emit_events,
            )
            session.candidate_intents = compile_result.recognition.candidates
            session.last_diagnostics = list(compile_result.diagnostics or [])
            graph = compile_result.graph
            if compile_result.no_match or graph is None:
                await self.state_sync.publish_no_match_hint(session)
                return
            if graph.status == GraphStatus.WAITING_CONFIRMATION:
                graph.touch(GraphStatus.WAITING_CONFIRMATION)
                self._attach_business(session, graph, pending=True)
                await self.state_sync.publish_pending_graph(session)
                return

            self._attach_business(session, graph, pending=False)
            self.activate_graph(graph)
            await self.state_sync.publish_graph_state(session, "graph.created", "已创建执行图")
            await self.drain_graph(session, graph.source_message)

    async def route_guided_selection(
        self,
        session: GraphSessionState,
        *,
        content: str,
        guided_selection: GuidedSelectionPayload,
    ) -> None:
        """Build and start a graph directly from UI-selected intents."""
        with router_stage(
            logger,
            "message_flow.route_guided_selection",
            selected_intent_count=len(guided_selection.selected_intents),
        ):
            graph = self.graph_compiler.build_guided_selection_graph(
                content=content,
                guided_selection=guided_selection,
            )
            session.candidate_intents = []
            session.last_diagnostics = []
            self._attach_business(session, graph, pending=False)
            self.activate_graph(graph)
            await self.state_sync.publish_graph_state(session, "graph.created", "已根据所选意图创建执行图")
            await self.drain_graph(session, graph.source_message)

    async def route_proactive_interactive_graph(
        self,
        session: GraphSessionState,
        *,
        content: str,
        proactive_recommendation: ProactiveRecommendationPayload,
        selected_items: list[ProactiveRecommendationItem],
        emit_events: bool = False,
    ) -> None:
        """Compile a graph from proactive items while preserving recommendation defaults."""
        with router_stage(
            logger,
            "message_flow.route_proactive_interactive_graph",
            selected_item_count=len(selected_items),
        ):
            compile_result = await self.graph_compiler.compile_proactive_interactive_graph(
                session,
                content=content,
                proactive_recommendation=proactive_recommendation,
                selected_items=selected_items,
                build_session_context=self.build_session_context,
                sanitize_recent_messages_for_planning=self.sanitize_recent_messages_for_planning,
                emit_events=emit_events,
            )
            session.candidate_intents = compile_result.recognition.candidates
            session.last_diagnostics = list(compile_result.diagnostics or [])
            graph = compile_result.graph
            if compile_result.no_match or graph is None:
                await self.state_sync.publish_no_match_hint(session)
                return
            if graph.status == GraphStatus.WAITING_CONFIRMATION:
                graph.touch(GraphStatus.WAITING_CONFIRMATION)
                self._attach_business(session, graph, pending=True)
                await self.state_sync.publish_pending_graph(session)
                return

            self._attach_business(session, graph, pending=False)
            self.activate_graph(graph)
            await self.state_sync.publish_graph_state(session, "graph.created", "已创建执行图")
            await self.drain_graph(session, graph.source_message)

    async def handle_pending_graph_turn(
        self,
        session: GraphSessionState,
        content: str,
        *,
        emit_events: bool = False,
    ) -> None:
        """Interpret a turn while a proposed graph is waiting for confirmation."""
        with router_stage(logger, "message_flow.handle_pending_graph_turn"):
            pending_graph = session.pending_graph
            if pending_graph is None:
                return
            turn_result = await self.understanding_service.interpret_pending_graph_turn(
                session,
                content=content,
                pending_graph=pending_graph,
            )
            session.last_diagnostics = list(turn_result.diagnostics or [])
            decision = turn_result.decision
            logger.debug(
                "Pending graph turn decision (trace_id=%s, session_id=%s, action=%s, reason=%s)",
                current_trace_id(),
                session.session_id,
                decision.action,
                decision.reason,
            )
            if decision.action == "confirm_pending_graph":
                await self.confirm_pending_graph(session, graph_id=None, confirm_token=None)
                return
            if decision.action == "cancel_pending_graph":
                await self.cancel_pending_graph(session, graph_id=None, confirm_token=None)
                return
            if decision.action == "replan":
                session.suspend_pending_business(reason=decision.reason or "检测到新的主业务，挂起待确认业务")
                await self.route_new_message(
                    session,
                    content,
                    recognition=turn_result.recognition,
                    recent_messages=[],
                    long_term_memory=[],
                    emit_events=emit_events,
                )
                return
            await self.state_sync.publish_graph_waiting_hint(session)

    async def handle_waiting_node_turn(
        self,
        session: GraphSessionState,
        waiting_node: GraphNodeState,
        content: str,
        *,
        emit_events: bool = False,
    ) -> None:
        """Interpret a turn while the current node is blocked on user input."""
        with router_stage(
            logger,
            "message_flow.handle_waiting_node_turn",
            waiting_node_id=waiting_node.node_id,
            waiting_intent_code=waiting_node.intent_code,
        ):
            graph = session.current_graph
            if graph is None:
                return
            turn_result = await self.understanding_service.interpret_waiting_node_turn(
                session,
                content=content,
                waiting_node=waiting_node,
                current_graph=graph,
            )
            session.last_diagnostics = list(turn_result.diagnostics or [])
            decision = turn_result.decision
            logger.debug(
                "Waiting node turn decision (trace_id=%s, session_id=%s, node_id=%s, action=%s, reason=%s)",
                current_trace_id(),
                session.session_id,
                waiting_node.node_id,
                decision.action,
                decision.reason,
            )
            if decision.action == "resume_current":
                await self.resume_waiting_node(session, waiting_node, content)
                return
            if decision.action == "cancel_current":
                await self.cancel_current_node(session, reason=decision.reason or "用户取消当前节点")
                await self.drain_graph(session, content)
                return
            if decision.action == "replan":
                session.suspend_focus_business(reason=decision.reason or "检测到新的主业务，挂起当前业务")
                await self.route_new_message(
                    session,
                    content,
                    recognition=turn_result.recognition,
                    recent_messages=[],
                    long_term_memory=[],
                    emit_events=emit_events,
                )
                return
            await self.state_sync.publish_session_state(session, "session.waiting_user_input")

    def sanitize_recent_messages_for_planning(self, recent_messages: list[str]) -> list[str]:
        """Keep only planning-safe synthetic messages instead of full chat history."""
        if not recent_messages:
            return []
        return [
            entry
            for entry in recent_messages
            if any(entry.startswith(prefix) for prefix in PLANNING_RECENT_MESSAGE_PREFIXES)
        ]
