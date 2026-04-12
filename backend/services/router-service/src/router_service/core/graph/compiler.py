from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from router_service.core.domain import IntentDefinition, IntentMatch
from router_service.core.intent_understanding_service import IntentUnderstandingService
from router_service.core.recognizer import RecognitionResult
from router_service.core.slot_grounding import normalize_structured_slot_memory
from router_service.core.slot_resolution_service import SlotResolutionService
from router_service.core.graph_domain import (
    ExecutionGraphState,
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
from router_service.core.graph_semantics import repair_unexecutable_condition_edges
from router_service.core.graph_planner import IntentGraphPlanner


SessionContextBuilder = Callable[[GraphSessionState], dict[str, Any]]
RecentMessageSanitizer = Callable[[list[str]], list[str]]


@dataclass(slots=True)
class GraphCompilationResult:
    recognition: RecognitionResult
    graph: ExecutionGraphState | None
    no_match: bool = False


class GraphCompiler:
    def __init__(
        self,
        *,
        intent_catalog: Any,
        planner: IntentGraphPlanner,
        understanding_service: IntentUnderstandingService,
        slot_resolution_service: SlotResolutionService,
    ) -> None:
        self.intent_catalog = intent_catalog
        self.planner = planner
        self.understanding_service = understanding_service
        self.slot_resolution_service = slot_resolution_service

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
    ) -> GraphCompilationResult:
        graph: ExecutionGraphState | None = None
        if recognition is None and (recent_messages is None or long_term_memory is None):
            context = build_session_context(session)
            recent_messages = context["recent_messages"]
            long_term_memory = context["long_term_memory"]
        else:
            recent_messages = recent_messages or []
            long_term_memory = long_term_memory or []
        recent_messages = sanitize_recent_messages_for_planning(recent_messages)
        recent_messages = self.augment_recent_messages_with_recommendations(
            recent_messages,
            recommendation_context=recommendation_context,
        )

        if self.understanding_service.has_graph_builder:
            build_result = await self.understanding_service.build_graph_from_message(
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
            recognition = await self.understanding_service.recognize_message(
                session,
                content,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                emit_events=True,
            )

        recognition = recognition or RecognitionResult(primary=[], candidates=[])
        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        matches = [match for match in recognition.primary if match.intent_code in active_intents]

        if not matches:
            fallback_intent = self.fallback_intent()
            if fallback_intent is None:
                return GraphCompilationResult(recognition=recognition, graph=None, no_match=True)
            matches = [IntentMatch(intent_code=fallback_intent.intent_code, confidence=0.0, reason="fallback")]
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
        self.slot_resolution_service.apply_proactive_slot_defaults(
            graph,
            selected_items=proactive_defaults or [],
            proactive_recommendation=proactive_recommendation,
            intents_by_code=active_intents,
        )
        if not skip_history_prefill:
            self.slot_resolution_service.apply_history_prefill_policy(
                session,
                graph,
                source_message=content,
                intents_by_code=active_intents,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
            )

        return GraphCompilationResult(recognition=recognition, graph=graph, no_match=False)

    def build_guided_selection_graph(
        self,
        *,
        content: str,
        guided_selection: GuidedSelectionPayload,
    ) -> ExecutionGraphState:
        active_intents = {intent.intent_code: intent for intent in self.intent_catalog.list_active()}
        graph = ExecutionGraphState(
            source_message=content,
            summary=self.guided_selection_summary(guided_selection),
            status=GraphStatus.DRAFT,
            actions=[],
        )

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

        repair_unexecutable_condition_edges(graph=graph, intents_by_code=active_intents)
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
        )

    def guided_selection_display_content(self, guided_selection: GuidedSelectionPayload | None) -> str:
        if guided_selection is None or not guided_selection.selected_intents:
            return ""
        titles = [selected.title or selected.intent_code for selected in guided_selection.selected_intents]
        return f"已选择推荐事项：{'、'.join(titles)}"

    def guided_selection_from_proactive_items(
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

    def augment_recent_messages_with_recommendations(
        self,
        recent_messages: list[str],
        *,
        recommendation_context: RecommendationContextPayload | None,
    ) -> list[str]:
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
        return [
            *recent_messages,
            self.proactive_recommendation_context_summary(proactive_recommendation),
            self.proactive_selection_summary(selected_items),
        ]

    def recommendation_context_summary(self, recommendation_context: RecommendationContextPayload) -> str:
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

    def fallback_intent(self) -> IntentDefinition | None:
        getter = getattr(self.intent_catalog, "get_fallback_intent", None)
        if getter is None:
            return None
        return getter()
