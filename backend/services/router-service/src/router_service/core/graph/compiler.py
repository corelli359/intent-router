from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Callable, Literal

from router_service.core.shared.domain import IntentDefinition, IntentMatch
from router_service.core.shared.diagnostics import (
    RouterDiagnostic,
    RouterDiagnosticCode,
    diagnostic,
    merge_diagnostics,
)
from router_service.core.recognition.understanding_service import IntentUnderstandingService
from router_service.core.recognition.recognizer import RecognitionResult
from router_service.core.slots.grounding import normalize_structured_slot_memory
from router_service.core.slots.resolution_service import SlotResolutionService
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphAction,
    GraphEdge,
    GraphEdgeType,
    GraphNodeState,
    GraphSessionState,
    GraphStatus,
    GuidedSelectionPayload,
    ProactiveRecommendationItem,
    ProactiveRecommendationPayload,
    RecommendationContextPayload,
    SlotBindingSource,
)
from router_service.core.graph.semantics import repair_unexecutable_condition_edges
from router_service.core.graph.planner import IntentGraphPlanner, SequentialIntentGraphPlanner
from router_service.core.support.trace_logging import current_trace_id, router_stage
from router_service.models.intent import GraphConfirmPolicy


SessionContextBuilder = Callable[[GraphSessionState], dict[str, Any]]
RecentMessageSanitizer = Callable[[list[str]], list[str]]
GraphPlanningPolicy = Literal["always", "never", "multi_intent_only", "auto"]

logger = logging.getLogger(__name__)

_COMPLEX_GRAPH_SIGNAL_PATTERNS = (
    re.compile(r"(如果|若|要是).*(就|则)"),
    re.compile(r"先.+再"),
    re.compile(r"(然后|之后|接着|随后|同时|并且|另外|顺便|分别)"),
    re.compile(r"再(给|转|查|做|办|交|缴|买|换|付|执行)"),
    re.compile(r"还(给|转|查|做|办|交|缴|买|换|付|大于|小于|超过|低于|剩|够)"),
)


@dataclass(slots=True)
class GraphCompilationResult:
    """Result of compiling one turn into recognition plus an optional graph."""

    recognition: RecognitionResult
    graph: ExecutionGraphState | None
    no_match: bool = False
    diagnostics: list[RouterDiagnostic] | None = None


class GraphCompiler:
    """Compiles one user turn into either a pending graph or an executable graph."""

    def __init__(
        self,
        *,
        intent_catalog: Any,
        planner: IntentGraphPlanner,
        understanding_service: IntentUnderstandingService,
        slot_resolution_service: SlotResolutionService,
        planning_policy: GraphPlanningPolicy = "always",
        fallback_planner: IntentGraphPlanner | None = None,
    ) -> None:
        """Initialize the compiler with catalog, planning, understanding, and slot services."""
        self.intent_catalog = intent_catalog
        self.planner = planner
        self.understanding_service = understanding_service
        self.slot_resolution_service = slot_resolution_service
        self.planning_policy = planning_policy
        self.fallback_planner = fallback_planner or SequentialIntentGraphPlanner()

    async def compile_message(
        self,
        session: GraphSessionState,
        content: str,
        *,
        build_session_context: SessionContextBuilder,
        sanitize_recent_messages_for_planning: RecentMessageSanitizer,
        recognition: RecognitionResult | None = None,
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
        recommendation_context: RecommendationContextPayload | None = None,
        proactive_defaults: list[ProactiveRecommendationItem] | None = None,
        proactive_recommendation: ProactiveRecommendationPayload | None = None,
        skip_history_prefill: bool = False,
        exclude_current_turn_from_context: bool = False,
        emit_events: bool = True,
    ) -> GraphCompilationResult:
        """Compile a free-form user message into graph state.

        Pipeline order:
        1. collect session context
        2. run unified builder or recognizer
        3. choose active/fallback intents
        4. plan graph if builder did not already return one
        5. repair condition edges
        6. inject proactive defaults and optional history prefill
        """
        with router_stage(
            logger,
            "compiler.compile_message",
            planning_policy=self.planning_policy,
            has_recognition_hint=recognition is not None,
            has_recent_messages=recent_messages is not None,
            has_long_term_memory=long_term_memory is not None,
            has_recommendation_context=recommendation_context is not None,
            proactive_defaults=len(proactive_defaults or []),
            skip_history_prefill=skip_history_prefill,
            exclude_current_turn_from_context=exclude_current_turn_from_context,
        ):
            graph: ExecutionGraphState | None = None
            if recognition is None and (recent_messages is None or long_term_memory is None):
                context = build_session_context(session)
                recent_messages = context["recent_messages"]
                long_term_memory = context["long_term_memory"]
            else:
                recent_messages = recent_messages or []
                long_term_memory = long_term_memory or []
            if exclude_current_turn_from_context:
                recent_messages = self._exclude_inflight_user_message(
                    message=content,
                    recent_messages=recent_messages,
                )
            recent_messages = sanitize_recent_messages_for_planning(recent_messages)
            recent_messages = self.augment_recent_messages_with_recommendations(
                recent_messages,
                recommendation_context=recommendation_context,
            )

            if self.understanding_service.has_graph_builder and self.planning_policy == "always":
                build_result = await self.understanding_service.build_graph_from_message(
                    session,
                    content,
                    recent_messages=recent_messages,
                    long_term_memory=long_term_memory,
                    recognition=recognition,
                    emit_events=emit_events,
                )
                recognition = build_result.recognition
                graph = build_result.graph
            elif recognition is None:
                recognition = await self.understanding_service.recognize_message(
                    session,
                    content,
                    recent_messages=recent_messages,
                    long_term_memory=long_term_memory,
                    emit_events=emit_events,
                )

            recognition = recognition or RecognitionResult(primary=[], candidates=[], diagnostics=[])
            active_intent_index = self.intent_catalog.active_intents_by_code()
            matches = [match for match in recognition.primary if match.intent_code in active_intent_index]
            intents_by_code = dict(active_intent_index)
            diagnostics: list[RouterDiagnostic] = list(recognition.diagnostics or [])

            if not matches:
                fallback_intent = self.fallback_intent()
                if fallback_intent is None:
                    diagnostics = merge_diagnostics(
                        diagnostics,
                        [
                            diagnostic(
                                RouterDiagnosticCode.ROUTER_NO_MATCH,
                                source="compiler",
                                message="当前消息未识别到可执行意图",
                                details={"content": content},
                            )
                        ],
                    )
                    logger.debug(
                        "Compiler result (trace_id=%s, session_id=%s, no_match=%s, primary_intents=%s, candidate_intents=%s)",
                        current_trace_id(),
                        session.session_id,
                        True,
                        len(recognition.primary),
                        len(recognition.candidates),
                    )
                    return GraphCompilationResult(
                        recognition=recognition,
                        graph=None,
                        no_match=True,
                        diagnostics=diagnostics,
                    )
                matches = [IntentMatch(intent_code=fallback_intent.intent_code, confidence=0.0, reason="fallback")]
                intents_by_code[fallback_intent.intent_code] = fallback_intent

            if graph is None:
                graph = await self._plan_graph(
                    message=content,
                    matches=matches,
                    intents_by_code=intents_by_code,
                    recent_messages=recent_messages,
                    long_term_memory=long_term_memory,
                )
            diagnostics = merge_diagnostics(
                diagnostics,
                graph.diagnostics,
            )
            repair_unexecutable_condition_edges(graph=graph, intents_by_code=intents_by_code)
            self.slot_resolution_service.apply_proactive_slot_defaults(
                graph,
                selected_items=proactive_defaults or [],
                proactive_recommendation=proactive_recommendation,
                intents_by_code=intents_by_code,
            )
            if not skip_history_prefill:
                self.slot_resolution_service.apply_history_prefill_policy(
                    session,
                    graph,
                    source_message=content,
                    intents_by_code=intents_by_code,
                    recent_messages=recent_messages,
                    long_term_memory=long_term_memory,
                )

            logger.debug(
                "Compiler result (trace_id=%s, session_id=%s, primary_intents=%s, candidate_intents=%s, graph_nodes=%s, graph_status=%s)",
                current_trace_id(),
                session.session_id,
                len(recognition.primary),
                len(recognition.candidates),
                len(graph.nodes),
                graph.status.value,
            )
            return GraphCompilationResult(
                recognition=recognition,
                graph=graph,
                no_match=False,
                diagnostics=diagnostics,
            )

    def _exclude_inflight_user_message(
        self,
        *,
        message: str,
        recent_messages: list[str],
    ) -> list[str]:
        """Drop the current turn when it was already appended to the session transcript.

        `/messages` stores the incoming user turn on the session before graph compilation
        so the final transcript keeps the right user/assistant ordering. Recognition and
        planning should still see only prior turns in `recent_messages`; otherwise the
        current message is duplicated once in `message` and once in `recent_messages`.
        """
        normalized_message = message.strip()
        if not normalized_message or not recent_messages:
            return recent_messages
        current_turn_entry = f"user: {normalized_message}"
        if recent_messages[-1].strip() == current_turn_entry:
            return recent_messages[:-1]
        return recent_messages

    async def recognize_only(
        self,
        session: GraphSessionState,
        content: str,
        *,
        build_session_context: SessionContextBuilder,
        sanitize_recent_messages_for_planning: RecentMessageSanitizer,
        recommendation_context: RecommendationContextPayload | None = None,
        emit_events: bool = False,
    ) -> RecognitionResult:
        """Run intent recognition only, without planning, graph building, or slot hydration."""
        with router_stage(
            logger,
            "compiler.recognize_only",
            has_recommendation_context=recommendation_context is not None,
        ):
            context = build_session_context(session)
            recent_messages = sanitize_recent_messages_for_planning(context["recent_messages"])
            recent_messages = self.augment_recent_messages_with_recommendations(
                recent_messages,
                recommendation_context=recommendation_context,
            )
            recognition = await self.understanding_service.recognize_message(
                session,
                content,
                recent_messages=recent_messages,
                long_term_memory=context["long_term_memory"],
                emit_events=emit_events,
            )
            logger.debug(
                "Recognize-only result (trace_id=%s, session_id=%s, primary_intents=%s, candidate_intents=%s)",
                current_trace_id(),
                session.session_id,
                len(recognition.primary),
                len(recognition.candidates),
            )
            return recognition

    async def _plan_graph(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
    ) -> ExecutionGraphState:
        """Choose between heavy planning and deterministic fallback planning."""
        use_heavy_planner = self._should_use_heavy_planner(message=message, matches=matches)
        planner = self.planner if use_heavy_planner else self.fallback_planner
        with router_stage(
            logger,
            "compiler.plan_graph",
            planner=planner.__class__.__name__,
            use_heavy_planner=use_heavy_planner,
            match_count=len(matches),
        ):
            graph = await planner.plan(
                message=message,
                matches=matches,
                intents_by_code=intents_by_code,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
            )
            if planner is self.fallback_planner:
                self._apply_single_node_confirmation_policy(graph=graph, intents_by_code=intents_by_code)
            logger.debug(
                "Planner result (trace_id=%s, planner=%s, graph_id=%s, graph_nodes=%s, graph_status=%s)",
                current_trace_id(),
                planner.__class__.__name__,
                graph.graph_id,
                len(graph.nodes),
                graph.status.value,
            )
            return graph

    def _should_use_heavy_planner(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
    ) -> bool:
        """Return whether this turn should go through the heavier LLM planning path."""
        if self.planning_policy == "always":
            return True
        if self.planning_policy == "never":
            return False
        if len(matches) > 1:
            return True
        if self.planning_policy == "multi_intent_only":
            return False
        return self._message_has_complex_graph_signal(message)

    def _message_has_complex_graph_signal(self, message: str) -> bool:
        """Detect conditions, sequencing, or repeated actions in a single-intent turn."""
        normalized = " ".join(part for part in message.split() if part).strip()
        if not normalized:
            return False
        return any(pattern.search(normalized) for pattern in _COMPLEX_GRAPH_SIGNAL_PATTERNS)

    def _apply_single_node_confirmation_policy(
        self,
        *,
        graph: ExecutionGraphState,
        intents_by_code: dict[str, IntentDefinition],
    ) -> None:
        """Honor per-intent confirmation hints after deterministic single-node compilation."""
        if len(graph.nodes) != 1:
            return
        intent = intents_by_code.get(graph.nodes[0].intent_code)
        if intent is None:
            return
        confirm_policy = intent.graph_build_hints.confirm_policy
        if confirm_policy == GraphConfirmPolicy.ALWAYS:
            graph.touch(GraphStatus.WAITING_CONFIRMATION)
            if not graph.actions:
                graph.actions = [
                    GraphAction(code="confirm_graph", label="开始执行"),
                    GraphAction(code="cancel_graph", label="取消"),
                ]
            return
        if confirm_policy == GraphConfirmPolicy.NEVER:
            graph.touch(GraphStatus.DRAFT)
            graph.actions = []

    def build_guided_selection_graph(
        self,
        *,
        content: str,
        guided_selection: GuidedSelectionPayload,
    ) -> ExecutionGraphState:
        """Build a deterministic graph from already-selected intents.

        This bypasses recognition because the upstream recommendation/UI layer has
        already decided which intents should enter the graph.
        """
        active_intent_index = self.intent_catalog.active_intents_by_code()
        graph = ExecutionGraphState(
            source_message=content,
            summary=self.guided_selection_summary(guided_selection),
            status=GraphStatus.DRAFT,
            actions=[],
        )

        for index, selected in enumerate(guided_selection.selected_intents):
            intent = active_intent_index.get(selected.intent_code)
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
                    slot_bindings=self.slot_resolution_service.structured_slot_bindings(
                        slot_memory=slot_memory,
                        source=SlotBindingSource.RECOMMENDATION,
                        source_text=selected.title or selected.source_fragment or "",
                        confidence=1.0,
                    ),
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

        repair_unexecutable_condition_edges(graph=graph, intents_by_code=active_intent_index)
        return graph

    async def compile_proactive_interactive_graph(
        self,
        session: GraphSessionState,
        *,
        content: str,
        proactive_recommendation: ProactiveRecommendationPayload,
        selected_items: list[ProactiveRecommendationItem],
        build_session_context: SessionContextBuilder,
        sanitize_recent_messages_for_planning: RecentMessageSanitizer,
    ) -> GraphCompilationResult:
        """Compile a graph from proactive selections while preserving recommendation context."""
        context = build_session_context(session)
        return await self.compile_message(
            session,
            content,
            build_session_context=build_session_context,
            sanitize_recent_messages_for_planning=sanitize_recent_messages_for_planning,
            recognition=self.recognition_from_proactive_items(selected_items),
            recent_messages=self.augment_recent_messages_with_proactive_selection(
                context["recent_messages"],
                proactive_recommendation=proactive_recommendation,
                selected_items=selected_items,
            ),
            long_term_memory=context["long_term_memory"],
            proactive_defaults=selected_items,
            proactive_recommendation=proactive_recommendation,
            skip_history_prefill=True,
            exclude_current_turn_from_context=True,
        )

    def guided_selection_display_content(self, guided_selection: GuidedSelectionPayload | None) -> str:
        """Build the synthetic user-visible content for guided-selection turns."""
        if guided_selection is None or not guided_selection.selected_intents:
            return ""
        titles = [selected.title or selected.intent_code for selected in guided_selection.selected_intents]
        return f"已选择推荐事项：{'、'.join(titles)}"

    def guided_selection_from_proactive_items(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> GuidedSelectionPayload:
        """Convert selected proactive items into a guided-selection payload."""
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

    def augment_recent_messages_with_recommendations(
        self,
        recent_messages: list[str],
        *,
        recommendation_context: RecommendationContextPayload | None,
    ) -> list[str]:
        """Append non-binding recommendation hints for recognition/planning only."""
        if recommendation_context is None or not recommendation_context.intents:
            return recent_messages
        return [
            *recent_messages,
            self.recommendation_context_summary(recommendation_context),
        ]

    def augment_recent_messages_with_proactive_selection(
        self,
        recent_messages: list[str],
        *,
        proactive_recommendation: ProactiveRecommendationPayload,
        selected_items: list[ProactiveRecommendationItem],
    ) -> list[str]:
        """Append proactive context plus the subset already selected by the user/router."""
        return [
            *recent_messages,
            self.proactive_recommendation_context_summary(proactive_recommendation),
            self.proactive_selection_summary(selected_items),
        ]

    def recommendation_context_summary(self, recommendation_context: RecommendationContextPayload) -> str:
        """Render recommendation context into planning-safe synthetic messages."""
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

    def proactive_recommendation_context_summary(
        self,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> str:
        """Render proactive recommendation context into planning-safe synthetic messages."""
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

    def proactive_selection_summary(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> str:
        """Render the selected proactive items into planning-safe synthetic messages."""
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

    def guided_selection_summary(self, guided_selection: GuidedSelectionPayload) -> str:
        """Build the graph summary used for guided-selection graphs."""
        titles = [selected.title or selected.intent_code for selected in guided_selection.selected_intents]
        return (
            f"已按用户选择生成执行图：{'、'.join(titles)}"
            if titles
            else "已按用户选择生成执行图"
        )

    def recognition_from_proactive_items(
        self,
        selected_items: list[ProactiveRecommendationItem],
    ) -> RecognitionResult:
        """Convert selected proactive items into deterministic recognition output."""
        matches: list[IntentMatch] = []
        for index, item in enumerate(selected_items):
            matches.append(
                IntentMatch(
                    intent_code=item.intent_code,
                    confidence=max(0.5, round(0.99 - (index * 0.01), 2)),
                    reason="selected_from_proactive_recommendation",
                )
            )
        return RecognitionResult(primary=matches, candidates=[], diagnostics=[])

    def fallback_intent(self) -> IntentDefinition | None:
        """Return the configured fallback intent from the intent catalog, if any."""
        getter = getattr(self.intent_catalog, "get_fallback_intent", None)
        if getter is None:
            return None
        return getter()
