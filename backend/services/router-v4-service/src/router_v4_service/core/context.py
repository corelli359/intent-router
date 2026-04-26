from __future__ import annotations

from typing import Any

from router_v4_service.core.matcher import SceneCandidate
from router_v4_service.core.models import ContextPolicy, RoutingSessionState, SceneSpec
from router_v4_service.core.stores import TranscriptRecord


class ContextBuilder:
    """Build a bounded report of context blocks used by Router.

    The service exposes the report instead of a provider-specific prompt so the
    loading lifecycle is testable: state is always included, scene details are
    loaded only after shortlist/selection, and lower-priority blocks are
    dropped when the budget is exceeded.
    """

    def __init__(self, policy: ContextPolicy | None = None) -> None:
        self.policy = policy or ContextPolicy()

    def build_report(
        self,
        *,
        state: RoutingSessionState,
        candidates: list[SceneCandidate],
        selected_scene: SceneSpec | None,
        transcripts: list[TranscriptRecord],
    ) -> dict[str, Any]:
        recent_records = transcripts[-self.policy.recent_turn_limit :]
        references = self._references(candidates=candidates, selected_scene=selected_scene)
        blocks: list[dict[str, Any]] = []
        self._add_block(blocks, "agent_rules", 240, required=True, priority=0)
        self._add_block(blocks, "routing_state", 180 + len(state.summary), required=True, priority=0)
        self._add_block(blocks, "scene_index", 220, required=True, priority=1)
        if candidates:
            self._add_block(
                blocks,
                "scene_candidates",
                120 + sum(len(candidate.scene.description) for candidate in candidates),
                priority=2,
            )
        if selected_scene is not None:
            self._add_block(blocks, "routing_spec", 180 + len(selected_scene.description), priority=2)
            self._add_block(blocks, "routing_slot_spec", 120 + len(selected_scene.routing_slots) * 80, priority=2)
            self._add_block(blocks, "dispatch_contract", 160, priority=2)
        if recent_records:
            self._add_block(
                blocks,
                "recent_transcript",
                80 + sum(len(str(record.payload)) for record in recent_records),
                priority=3,
            )
        if references:
            self._add_block(
                blocks,
                "retrieved_references",
                80 + sum(len(item) for item in references),
                priority=4,
            )

        included, dropped = self._fit_budget(blocks)
        return {
            "included_blocks": [block["name"] for block in included],
            "dropped_blocks": [block["name"] for block in dropped],
            "candidate_scene_ids": [candidate.scene.scene_id for candidate in candidates],
            "selected_scene_id": selected_scene.scene_id if selected_scene else None,
            "active_scene_id": state.active_scene_id,
            "pending_scene_id": state.pending_scene_id,
            "agent_task_id": state.agent_task_id,
            "recent_turns": len(recent_records),
            "retrieved_references": references if any(block["name"] == "retrieved_references" for block in included) else [],
            "estimated_chars": sum(int(block["estimated_chars"]) for block in included),
            "max_chars": self.policy.max_chars,
            "lifecycle": {
                "state_reused": bool(state.active_scene_id or state.pending_scene_id or state.agent_task_id),
                "progressive_loading": [
                    block
                    for block in (
                        "agent_rules",
                        "routing_state",
                        "scene_index",
                        "scene_candidates" if candidates else "",
                        "routing_spec" if selected_scene is not None else "",
                        "dispatch_contract" if selected_scene is not None else "",
                    )
                    if block
                ],
                "budget_applied": bool(dropped),
            },
        }

    def _references(
        self,
        *,
        candidates: list[SceneCandidate],
        selected_scene: SceneSpec | None,
    ) -> list[str]:
        refs: list[str] = []
        scenes = [selected_scene] if selected_scene is not None else [candidate.scene for candidate in candidates]
        for scene in scenes:
            if scene is None:
                continue
            for ref in scene.references:
                if ref not in refs:
                    refs.append(ref)
                if len(refs) >= self.policy.retrieved_reference_limit:
                    return refs
        return refs

    def _fit_budget(self, blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        included: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        used = 0
        for block in sorted(blocks, key=lambda item: (int(item["priority"]), int(item["index"]))):
            estimated_chars = int(block["estimated_chars"])
            if bool(block["required"]) or used + estimated_chars <= self.policy.max_chars:
                included.append(block)
                used += estimated_chars
            else:
                dropped.append(block)
        included.sort(key=lambda item: int(item["index"]))
        dropped.sort(key=lambda item: int(item["index"]))
        return included, dropped

    def _add_block(
        self,
        blocks: list[dict[str, Any]],
        name: str,
        estimated_chars: int,
        *,
        priority: int,
        required: bool = False,
    ) -> None:
        blocks.append(
            {
                "name": name,
                "estimated_chars": estimated_chars,
                "priority": priority,
                "required": required,
                "index": len(blocks),
            }
        )
