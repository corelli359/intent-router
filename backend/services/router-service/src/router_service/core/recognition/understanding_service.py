from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any

from router_service.core.shared.diagnostics import (
    RouterDiagnostic,
    RouterDiagnosticCode,
    diagnostic,
    merge_diagnostics,
)
from router_service.core.support.llm_client import llm_exception_is_retryable
from router_service.core.support.trace_logging import current_trace_id, router_stage
from router_service.core.support.context_builder import ContextBuilder
from router_service.core.recognition.recognizer import (
    IntentRecognizer,
    NullIntentRecognizer,
    RecognitionResult,
)
from router_service.core.shared.graph_domain import ExecutionGraphState, GraphNodeState, GraphSessionState
from router_service.core.graph.builder import GraphBuildResult, IntentGraphBuilder
from router_service.core.graph.planner import TurnDecisionPayload, TurnInterpreter


logger = logging.getLogger(__name__)


def _callable_supports_keyword(callable_obj: object, keyword: str) -> bool:
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return True
    return keyword in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


@dataclass(slots=True)
class TurnInterpretationResult:
    """Combined result of turn interpretation and its supporting recognition output."""

    decision: TurnDecisionPayload
    recognition: RecognitionResult
    diagnostics: list[RouterDiagnostic] | None = None


class IntentUnderstandingService:
    """Bridges raw LLM semantics with router-friendly recognition/decision outputs."""

    def __init__(
        self,
        *,
        intent_catalog: Any,
        recognizer: IntentRecognizer,
        graph_builder: IntentGraphBuilder | None,
        turn_interpreter: TurnInterpreter,
        event_publisher: Any,
        context_builder: ContextBuilder | None = None,
        long_term_memory_store: Any | None = None,
    ) -> None:
        """Initialize the understanding service with recognition, planning, and event dependencies."""
        self.intent_catalog = intent_catalog
        self.recognizer = recognizer
        self.graph_builder = graph_builder
        self.turn_interpreter = turn_interpreter
        self.event_publisher = event_publisher
        self.context_builder = context_builder or ContextBuilder()
        self.long_term_memory_store = long_term_memory_store

    @property
    def has_graph_builder(self) -> bool:
        """Return whether unified graph building is enabled."""
        return self.graph_builder is not None

    async def recognize_message(
        self,
        session: GraphSessionState,
        content: str,
        *,
        recent_messages: list[str],
        long_term_memory: list[str],
        recommend_task: list[dict[str, Any]] | None = None,
        emit_events: bool,
    ) -> RecognitionResult:
        """Run recognition and optionally stream semantic progress events."""
        with router_stage(
            logger,
            "understanding.recognize_message",
            emit_events=emit_events,
            recent_message_count=len(recent_messages),
            long_term_memory_count=len(long_term_memory),
        ):
            if emit_events:
                await self.event_publisher.publish_recognition_started(session)

            async def publish_recognition_delta(delta: str) -> None:
                """Forward streamed recognition deltas to the event publisher when enabled."""
                if not emit_events or not delta:
                    return
                await self.event_publisher.publish_recognition_delta(session, delta=delta)

            recognize_kwargs: dict[str, Any] = {
                "message": content,
                "intents": self.intent_catalog.active_intents_by_code().values(),
                "recent_messages": recent_messages,
                "long_term_memory": long_term_memory,
                "on_delta": publish_recognition_delta if emit_events else None,
            }
            if _callable_supports_keyword(self.recognizer.recognize, "recommend_task"):
                recognize_kwargs["recommend_task"] = recommend_task
            recognition = await self.recognizer.recognize(**recognize_kwargs)
            if emit_events:
                await self.event_publisher.publish_recognition_completed(session, recognition=recognition)
            logger.debug(
                "Recognition result (trace_id=%s, session_id=%s, primary_intents=%s, candidate_intents=%s)",
                current_trace_id(),
                session.session_id,
                len(recognition.primary),
                len(recognition.candidates),
            )
            return recognition

    async def build_graph_from_message(
        self,
        session: GraphSessionState,
        content: str,
        *,
        recent_messages: list[str],
        long_term_memory: list[str],
        recognition: RecognitionResult | None,
        recommend_task: list[dict[str, Any]] | None = None,
        emit_events: bool,
    ) -> GraphBuildResult:
        """Run the optional unified graph-builder path when enabled."""
        with router_stage(
            logger,
            "understanding.build_graph_from_message",
            emit_events=emit_events,
            has_recognition_hint=recognition is not None,
            recent_message_count=len(recent_messages),
            long_term_memory_count=len(long_term_memory),
        ):
            if emit_events:
                await self.event_publisher.publish_graph_builder_started(session)

            async def publish_graph_builder_delta(delta: str) -> None:
                """Forward streamed graph-builder deltas to the event publisher when enabled."""
                if not emit_events or not delta:
                    return
                await self.event_publisher.publish_graph_builder_delta(session, delta=delta)

            if self.graph_builder is None:
                raise RuntimeError("graph_builder is not configured")
            build_kwargs: dict[str, Any] = {
                "message": content,
                "intents": self.intent_catalog.active_intents_by_code().values(),
                "recent_messages": recent_messages,
                "long_term_memory": long_term_memory,
                "recognition": recognition,
                "on_delta": publish_graph_builder_delta if emit_events else None,
            }
            if _callable_supports_keyword(self.graph_builder.build, "recommend_task"):
                build_kwargs["recommend_task"] = recommend_task
            result = await self.graph_builder.build(**build_kwargs)
            if emit_events:
                await self.event_publisher.publish_graph_builder_completed(session, result=result)
            logger.debug(
                "Graph builder result (trace_id=%s, session_id=%s, primary_intents=%s, graph_nodes=%s, graph_status=%s)",
                current_trace_id(),
                session.session_id,
                len(result.recognition.primary),
                len(result.graph.nodes) if result.graph is not None else 0,
                result.graph.status.value if result.graph is not None else None,
            )
            return result

    async def interpret_pending_graph_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        pending_graph: ExecutionGraphState,
    ) -> TurnInterpretationResult:
        """Interpret a user turn while the router is waiting for graph-level confirmation."""
        with router_stage(
            logger,
            "understanding.interpret_pending_graph_turn",
            pending_graph_id=pending_graph.graph_id,
        ):
            recognition = await self._recognize_turn_message(
                session=session,
                content=content,
                mode="pending_graph",
            )
            decision = await self.turn_interpreter.interpret_pending_graph(
                message=content,
                pending_graph=pending_graph,
                recognition=recognition,
            )
            logger.debug(
                "Pending graph interpretation result (trace_id=%s, session_id=%s, action=%s, target_intent_code=%s)",
                current_trace_id(),
                session.session_id,
                decision.action,
                decision.target_intent_code,
            )
            return TurnInterpretationResult(
                decision=decision,
                recognition=recognition,
                diagnostics=merge_diagnostics(recognition.diagnostics),
            )

    async def interpret_waiting_node_turn(
        self,
        session: GraphSessionState,
        *,
        waiting_node: GraphNodeState,
        current_graph: ExecutionGraphState,
        content: str,
    ) -> TurnInterpretationResult:
        """Interpret a user turn while a concrete node is waiting for more input."""
        with router_stage(
            logger,
            "understanding.interpret_waiting_node_turn",
            graph_id=current_graph.graph_id,
            waiting_node_id=waiting_node.node_id,
            waiting_intent_code=waiting_node.intent_code,
        ):
            recognition = await self._recognize_turn_message(
                session=session,
                content=content,
                mode="waiting_node",
            )
            decision = await self.turn_interpreter.interpret_waiting_node(
                message=content,
                waiting_node=waiting_node,
                current_graph=current_graph,
                recognition=recognition,
            )
            logger.debug(
                "Waiting node interpretation result (trace_id=%s, session_id=%s, node_id=%s, action=%s, target_intent_code=%s)",
                current_trace_id(),
                session.session_id,
                waiting_node.node_id,
                decision.action,
                decision.target_intent_code,
            )
            return TurnInterpretationResult(
                decision=decision,
                recognition=recognition,
                diagnostics=merge_diagnostics(recognition.diagnostics),
            )

    def _resolve_fast_recognizer(self) -> IntentRecognizer | None:
        """Use a local fast path only when semantic recognition is fully unavailable."""
        return self.recognizer if isinstance(self.recognizer, NullIntentRecognizer) else None

    def _turn_recognition_unavailable(
        self,
        *,
        mode: str,
        error_type: str,
        path: str,
    ) -> RecognitionResult:
        """Return a conservative empty recognition result for blocked-turn handling."""
        message = (
            "待确认图阶段识别服务暂时不可用，已保守保持等待"
            if mode == "pending_graph"
            else "补槽阶段识别服务暂时不可用，已保守继续当前节点"
        )
        return RecognitionResult(
            primary=[],
            candidates=[],
            diagnostics=[
                diagnostic(
                    RouterDiagnosticCode.TURN_RECOGNITION_RETRYABLE_UNAVAILABLE,
                    source="turn_interpreter",
                    message=message,
                    details={
                        "error_type": error_type,
                        "mode": mode,
                        "path": path,
                    },
                )
            ],
        )

    def _blocked_turn_recent_messages(
        self,
        *,
        session: GraphSessionState,
        content: str,
    ) -> list[str]:
        """Return prior-turn context for blocked-turn recognition without duplicating the current input."""
        recent_messages = self.context_builder.build_recent_messages(session)
        current_turn_entry = f"user: {content.strip()}"
        if content.strip() and recent_messages and recent_messages[-1].strip() == current_turn_entry:
            return recent_messages[:-1]
        return recent_messages

    def _blocked_turn_long_term_memory(self, session: GraphSessionState) -> list[str]:
        """Return recalled customer memory for blocked-turn recognition when available."""
        if self.long_term_memory_store is None:
            return []
        return list(self.long_term_memory_store.recall(session.cust_id))

    async def _recognize_turn_message(
        self,
        *,
        session: GraphSessionState,
        content: str,
        mode: str,
    ) -> RecognitionResult:
        """Recognize blocked-turn follow-ups via the lightest available path."""
        recent_messages = self._blocked_turn_recent_messages(session=session, content=content)
        long_term_memory = self._blocked_turn_long_term_memory(session)
        fast_recognizer = self._resolve_fast_recognizer()
        if fast_recognizer is not None:
            try:
                return await fast_recognizer.recognize(
                    message=content,
                    intents=self.intent_catalog.active_intents_by_code().values(),
                    recent_messages=recent_messages,
                    long_term_memory=long_term_memory,
                )
            except Exception as exc:
                logger.debug(
                    "Fast-path turn recognition unavailable, using conservative empty result",
                    exc_info=True,
                )
                return self._turn_recognition_unavailable(
                    mode=mode,
                    error_type=type(exc).__name__,
                    path="fast",
                )

        try:
            return await self.recognize_message(
                session,
                content,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                emit_events=False,
            )
        except Exception as exc:
            if not llm_exception_is_retryable(exc):
                raise
            logger.debug(
                "Turn recognition unavailable, falling back to conservative empty result",
                exc_info=True,
            )
            return self._turn_recognition_unavailable(
                mode=mode,
                error_type=type(exc).__name__,
                path="full",
            )
