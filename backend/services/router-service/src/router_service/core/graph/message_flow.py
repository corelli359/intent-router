from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from router_service.core.domain import ChatMessage, utc_now
from router_service.core.graph_compiler import GraphCompiler
from router_service.core.graph_domain import (
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
from router_service.core.intent_understanding_service import IntentUnderstandingService
from router_service.core.llm_client import llm_exception_is_retryable
from router_service.core.recommendation_router import ProactiveRecommendationRouter


logger = logging.getLogger(__name__)

PLANNING_RECENT_MESSAGE_PREFIXES = (
    "user:",
    "[FRONTEND_RECOMMENDATION_CONTEXT]",
    "[PROACTIVE_RECOMMENDATION_CONTEXT]",
    "[PROACTIVE_RECOMMENDATION_SELECTION]",
)

TERMINAL_GRAPH_STATUSES = {
    GraphStatus.COMPLETED,
    GraphStatus.PARTIALLY_COMPLETED,
    GraphStatus.FAILED,
    GraphStatus.CANCELLED,
}


class GraphMessageFlow:
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
    ) -> None:
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
                await self.handle_proactive_recommendation_turn(
                    session,
                    content=message_content,
                    proactive_recommendation=proactive_recommendation,
                )
                return self.snapshot_session(session.session_id)

            if guided_selection is not None:
                await self.handle_guided_selection_turn(
                    session,
                    content=message_content,
                    guided_selection=guided_selection,
                )
                return self.snapshot_session(session.session_id)

            if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
                await self.handle_pending_graph_turn(session, message_content)
                return self.snapshot_session(session.session_id)

            waiting_node = self.get_waiting_node(session)
            if waiting_node is not None:
                await self.handle_waiting_node_turn(session, waiting_node, message_content)
                return self.snapshot_session(session.session_id)

            await self.route_new_message(
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
        return self.snapshot_session(session.session_id)

    async def handle_proactive_recommendation_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> None:
        if not proactive_recommendation.items:
            raise ValueError("proactive_recommendation.items is required")
        if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
            await self.cancel_pending_graph(session, graph_id=None, confirm_token=None)
        if session.current_graph is not None and session.current_graph.status not in TERMINAL_GRAPH_STATUSES:
            await self.cancel_current_graph(session, reason="用户切换到主动推荐事项处理")

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
            await self.route_new_message(session, content)
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
        )

    async def handle_guided_selection_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        guided_selection: GuidedSelectionPayload,
    ) -> None:
        if not guided_selection.selected_intents:
            raise ValueError("guided_selection.selected_intents is required")
        if session.pending_graph is not None and session.pending_graph.status == GraphStatus.WAITING_CONFIRMATION:
            await self.cancel_pending_graph(session, graph_id=None, confirm_token=None)
        if session.current_graph is not None and session.current_graph.status not in TERMINAL_GRAPH_STATUSES:
            await self.cancel_current_graph(session, reason="用户切换为引导式已选意图执行")
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
    ) -> None:
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
        )
        session.candidate_intents = compile_result.recognition.candidates
        graph = compile_result.graph
        if compile_result.no_match or graph is None:
            await self.state_sync.publish_no_match_hint(session)
            return
        if graph.status == GraphStatus.WAITING_CONFIRMATION:
            graph.touch(GraphStatus.WAITING_CONFIRMATION)
            session.pending_graph = graph
            session.current_graph = None
            session.active_node_id = None
            await self.state_sync.publish_pending_graph(session)
            return

        session.pending_graph = None
        session.current_graph = graph
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
        graph = self.graph_compiler.build_guided_selection_graph(
            content=content,
            guided_selection=guided_selection,
        )
        session.candidate_intents = []
        session.pending_graph = None
        session.current_graph = graph
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
    ) -> None:
        compile_result = await self.graph_compiler.compile_proactive_interactive_graph(
            session,
            content=content,
            proactive_recommendation=proactive_recommendation,
            selected_items=selected_items,
            build_session_context=self.build_session_context,
            sanitize_recent_messages_for_planning=self.sanitize_recent_messages_for_planning,
        )
        session.candidate_intents = compile_result.recognition.candidates
        graph = compile_result.graph
        if compile_result.no_match or graph is None:
            await self.state_sync.publish_no_match_hint(session)
            return
        if graph.status == GraphStatus.WAITING_CONFIRMATION:
            graph.touch(GraphStatus.WAITING_CONFIRMATION)
            session.pending_graph = graph
            session.current_graph = None
            session.active_node_id = None
            await self.state_sync.publish_pending_graph(session)
            return

        session.pending_graph = None
        session.current_graph = graph
        self.activate_graph(graph)
        await self.state_sync.publish_graph_state(session, "graph.created", "已创建执行图")
        await self.drain_graph(session, graph.source_message)

    async def handle_pending_graph_turn(self, session: GraphSessionState, content: str) -> None:
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
            await self.confirm_pending_graph(session, graph_id=None, confirm_token=None)
            return
        if decision.action == "cancel_pending_graph":
            await self.cancel_pending_graph(session, graph_id=None, confirm_token=None)
            return
        if decision.action == "replan":
            session.pending_graph = None
            await self.route_new_message(
                session,
                content,
                recognition=turn_result.recognition,
                recent_messages=[],
                long_term_memory=[],
            )
            return
        await self.state_sync.publish_graph_waiting_hint(session)

    async def handle_waiting_node_turn(
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
            await self.resume_waiting_node(session, waiting_node, content)
            return
        if decision.action == "cancel_current":
            await self.cancel_current_node(session, reason=decision.reason or "用户取消当前节点")
            await self.drain_graph(session, content)
            return
        if decision.action == "replan":
            await self.cancel_current_graph(session, reason=decision.reason or "检测到用户修改了目标，准备重规划")
            await self.route_new_message(
                session,
                content,
                recognition=turn_result.recognition,
                recent_messages=[],
                long_term_memory=[],
            )
            return
        await self.state_sync.publish_session_state(session, "session.waiting_user_input")

    def sanitize_recent_messages_for_planning(self, recent_messages: list[str]) -> list[str]:
        if not recent_messages:
            return []
        return [
            entry
            for entry in recent_messages
            if any(entry.startswith(prefix) for prefix in PLANNING_RECENT_MESSAGE_PREFIXES)
        ]
