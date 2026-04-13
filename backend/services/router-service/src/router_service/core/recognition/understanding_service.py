from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from router_service.core.support.llm_client import llm_exception_is_retryable
from router_service.core.recognition.recognizer import IntentRecognizer, RecognitionResult
from router_service.core.shared.graph_domain import ExecutionGraphState, GraphNodeState, GraphSessionState
from router_service.core.graph.builder import GraphBuildResult, IntentGraphBuilder
from router_service.core.graph.planner import TurnDecisionPayload, TurnInterpreter


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnInterpretationResult:
    """Combined result of turn interpretation and its supporting recognition output."""

    decision: TurnDecisionPayload
    recognition: RecognitionResult


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
    ) -> None:
        """Initialize the understanding service with recognition, planning, and event dependencies."""
        self.intent_catalog = intent_catalog
        self.recognizer = recognizer
        self.graph_builder = graph_builder
        self.turn_interpreter = turn_interpreter
        self.event_publisher = event_publisher

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
        emit_events: bool,
    ) -> RecognitionResult:
        """Run recognition and optionally stream semantic progress events."""
        if emit_events:
            await self.event_publisher.publish_recognition_started(session)

        async def publish_recognition_delta(delta: str) -> None:
            """Forward streamed recognition deltas to the event publisher when enabled."""
            if not emit_events or not delta:
                return
            await self.event_publisher.publish_recognition_delta(session, delta=delta)

        recognition = await self.recognizer.recognize(
            message=content,
            intents=self.intent_catalog.active_intents_by_code().values(),
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            on_delta=publish_recognition_delta if emit_events else None,
        )
        if emit_events:
            await self.event_publisher.publish_recognition_completed(session, recognition=recognition)
        return recognition

    async def build_graph_from_message(
        self,
        session: GraphSessionState,
        content: str,
        *,
        recent_messages: list[str],
        long_term_memory: list[str],
        recognition: RecognitionResult | None,
        emit_events: bool,
    ) -> GraphBuildResult:
        """Run the optional unified graph-builder path when enabled."""
        if emit_events:
            await self.event_publisher.publish_graph_builder_started(session)

        async def publish_graph_builder_delta(delta: str) -> None:
            """Forward streamed graph-builder deltas to the event publisher when enabled."""
            if not emit_events or not delta:
                return
            await self.event_publisher.publish_graph_builder_delta(session, delta=delta)

        if self.graph_builder is None:
            raise RuntimeError("graph_builder is not configured")
        result = await self.graph_builder.build(
            message=content,
            intents=self.intent_catalog.active_intents_by_code().values(),
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            recognition=recognition,
            on_delta=publish_graph_builder_delta if emit_events else None,
        )
        if emit_events:
            await self.event_publisher.publish_graph_builder_completed(session, result=result)
        return result

    async def interpret_pending_graph_turn(
        self,
        session: GraphSessionState,
        *,
        content: str,
        pending_graph: ExecutionGraphState,
    ) -> TurnInterpretationResult:
        """Interpret a user turn while the router is waiting for graph-level confirmation."""
        try:
            recognition = await self.recognize_message(
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
        return TurnInterpretationResult(decision=decision, recognition=recognition)

    async def interpret_waiting_node_turn(
        self,
        session: GraphSessionState,
        *,
        waiting_node: GraphNodeState,
        current_graph: ExecutionGraphState,
        content: str,
    ) -> TurnInterpretationResult:
        """Interpret a user turn while a concrete node is waiting for more input."""
        try:
            recognition = await self.recognize_message(
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
        decision = await self.turn_interpreter.interpret_waiting_node(
            message=content,
            waiting_node=waiting_node,
            current_graph=current_graph,
            recognition=recognition,
        )
        return TurnInterpretationResult(decision=decision, recognition=recognition)
