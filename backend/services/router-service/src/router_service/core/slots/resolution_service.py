from __future__ import annotations

from typing import Any

from router_service.core.shared.domain import IntentDefinition
from router_service.core.slots.grounding import (
    apply_history_slot_values,
    normalize_slot_memory,
    normalize_structured_slot_memory,
)
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphAction,
    GraphNodeState,
    GraphSessionState,
    GraphStatus,
    ProactiveRecommendationItem,
    ProactiveRecommendationPayload,
    SlotBindingSource,
    SlotBindingState,
)


class SlotResolutionService:
    """Centralizes slot defaulting, history reuse, and binding-source reconstruction."""

    def apply_history_prefill_policy(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        *,
        source_message: str,
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
    ) -> None:
        """Inject reusable history slots into graph nodes before execution starts.

        This runs at compile time, not inside the agent. The goal is to improve
        slot fill accuracy in the router layer while still preserving provenance
        through `history_slot_keys` and `slot_bindings`.
        """
        history_nodes: list[GraphNodeState] = []
        history_texts = [*recent_messages, *long_term_memory]
        history_slot_values = self.history_slot_values(session, long_term_memory=long_term_memory)

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
            self.rebuild_node_slot_bindings(
                node,
                preferred_sources={
                    slot_key: SlotBindingSource.HISTORY
                    for slot_key in history_slot_keys
                },
                source_text=node.source_fragment or source_message,
            )
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

    def history_slot_values(
        self,
        session: GraphSessionState,
        *,
        long_term_memory: list[str],
    ) -> dict[str, Any]:
        """Collect reusable slot values from recent task results and long-term memory."""
        values: dict[str, Any] = dict(session.shared_slot_memory)

        for task in reversed(session.tasks):
            if not task.slot_memory:
                continue
            for key, value in task.slot_memory.items():
                if key in values or value is None:
                    continue
                values[key] = value

        for digest in reversed(session.business_memory_digests):
            for key, value in digest.slot_memory.items():
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

    def structured_slot_bindings(
        self,
        *,
        slot_memory: dict[str, Any],
        source: SlotBindingSource,
        source_text: str | None,
        confidence: float,
    ) -> list[SlotBindingState]:
        """Build binding metadata for already-structured slot payloads."""
        return [
            SlotBindingState(
                slot_key=slot_key,
                value=value,
                source=source,
                source_text=source_text,
                confidence=confidence,
            )
            for slot_key, value in slot_memory.items()
        ]

    def rebuild_node_slot_bindings(
        self,
        node: GraphNodeState,
        *,
        preferred_sources: dict[str, SlotBindingSource] | None = None,
        source_text: str | None = None,
        source_text_slot_keys: set[str] | None = None,
    ) -> None:
        """Rebuild node bindings from `slot_memory` after merge/normalization steps."""
        existing_by_key = {binding.slot_key: binding for binding in node.slot_bindings}
        rebuilt: list[SlotBindingState] = []
        for slot_key, value in node.slot_memory.items():
            existing = existing_by_key.get(slot_key)
            preferred_source = preferred_sources[slot_key] if preferred_sources and slot_key in preferred_sources else None
            inferred_source_text = existing.source_text if existing is not None and existing.source_text else None
            if inferred_source_text is None and source_text is not None:
                if source_text_slot_keys is None or slot_key in source_text_slot_keys:
                    inferred_source_text = source_text
            rebuilt.append(
                SlotBindingState(
                    slot_key=slot_key,
                    value=value,
                    source=(
                        preferred_source
                        if preferred_source is not None
                        else existing.source if existing is not None else SlotBindingSource.USER_MESSAGE
                    ),
                    source_text=inferred_source_text,
                    confidence=existing.confidence if existing is not None else node.confidence,
                    is_modified=existing.is_modified if existing is not None else False,
                )
            )
        node.slot_bindings = rebuilt

    def apply_proactive_slot_defaults(
        self,
        graph: ExecutionGraphState,
        *,
        selected_items: list[ProactiveRecommendationItem],
        proactive_recommendation: ProactiveRecommendationPayload | None,
        intents_by_code: dict[str, IntentDefinition],
    ) -> None:
        """Merge proactive recommendation defaults into graph nodes before dispatch."""
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
            original_slot_keys = set(node.slot_memory)
            selected_item: ProactiveRecommendationItem | None = None
            candidates = items_by_intent.get(node.intent_code)
            if candidates:
                selected_item = candidates.pop(0)
            elif fallback_items_by_intent.get(node.intent_code):
                selected_item = fallback_items_by_intent[node.intent_code][0]

            merged_slot_memory: dict[str, Any] = {}
            shared_slot_keys: set[str] = set()
            if shared_slot_memory:
                shared_defaults = {
                    key: value
                    for key, value in shared_slot_memory.items()
                    if key in allowed_slot_keys
                }
                merged_slot_memory.update(shared_defaults)
                shared_slot_keys.update(shared_defaults)
            selected_slot_keys: set[str] = set()
            if selected_item is not None and selected_item.slot_memory:
                selected_defaults = {
                    key: value
                    for key, value in selected_item.slot_memory.items()
                    if key in allowed_slot_keys
                }
                merged_slot_memory.update(selected_defaults)
                selected_slot_keys.update(selected_defaults)
            merged_slot_memory.update(node.slot_memory)
            node.slot_memory = normalize_structured_slot_memory(
                slot_memory=merged_slot_memory,
                slot_schema=intent.slot_schema,
            )
            recommendation_keys = {
                slot_key
                for slot_key in node.slot_memory
                if slot_key not in original_slot_keys
                and (slot_key in shared_slot_keys or slot_key in selected_slot_keys)
            }
            self.rebuild_node_slot_bindings(
                node,
                preferred_sources={
                    slot_key: SlotBindingSource.RECOMMENDATION
                    for slot_key in recommendation_keys
                },
                source_text=(
                    selected_item.title
                    if selected_item is not None
                    else proactive_recommendation.intro_text if proactive_recommendation is not None else node.source_fragment
                ),
                source_text_slot_keys=recommendation_keys,
            )
            if selected_item is not None:
                if not node.title:
                    node.title = selected_item.title
                if not node.source_fragment:
                    node.source_fragment = selected_item.title
