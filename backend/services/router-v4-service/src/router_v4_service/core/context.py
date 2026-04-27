from __future__ import annotations

from pathlib import Path
from typing import Any

from router_v4_service.core.recognizer import IntentCandidate
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
        candidates: list[IntentCandidate],
        selected_scene: SceneSpec | None,
        transcripts: list[TranscriptRecord],
        scene_index_scenes: list[SceneSpec] | None = None,
    ) -> dict[str, Any]:
        recent_records = transcripts[-self.policy.recent_turn_limit :]
        references = self._references(candidates=candidates, selected_scene=selected_scene)
        scene_index = scene_index_scenes or ([selected_scene] if selected_scene is not None else [candidate.scene for candidate in candidates])
        blocks: list[dict[str, Any]] = []
        self._add_block(blocks, "router_boundary", 240, required=True, priority=0)
        self._add_block(blocks, "routing_state", 180 + len(state.summary), required=True, priority=0)
        self._add_block(blocks, "scene_index", 220 + len(scene_index) * 120, required=True, priority=1)
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
            if selected_scene.skill:
                self._add_block(blocks, "skill_card", 160 + len(str(selected_scene.skill)), priority=2)
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
            "load_trace": self._load_trace(
                state=state,
                candidates=candidates,
                selected_scene=selected_scene,
                recent_records=recent_records,
                included_blocks={block["name"] for block in included},
                dropped_blocks={block["name"] for block in dropped},
                references=references,
                scene_index_scenes=scene_index,
            ),
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
                        "router_boundary",
                        "routing_state",
                        "scene_index",
                        "scene_candidates" if candidates else "",
                        "routing_spec" if selected_scene is not None else "",
                        "routing_slot_spec" if selected_scene is not None else "",
                        "skill_card" if selected_scene is not None and selected_scene.skill else "",
                        "dispatch_contract" if selected_scene is not None else "",
                        "retrieved_references" if references else "",
                    )
                    if block
                ],
                "budget_applied": bool(dropped),
            },
        }

    def _references(
        self,
        *,
        candidates: list[IntentCandidate],
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

    def _load_trace(
        self,
        *,
        state: RoutingSessionState,
        candidates: list[IntentCandidate],
        selected_scene: SceneSpec | None,
        recent_records: list[TranscriptRecord],
        included_blocks: set[str],
        dropped_blocks: set[str],
        references: list[str],
        scene_index_scenes: list[SceneSpec],
    ) -> list[dict[str, Any]]:
        trace: list[dict[str, Any]] = []
        self._trace(
            trace,
            block="router_boundary",
            stage="turn_start",
            source_type="runtime_policy",
            summary="加载三层边界：助手负责展示和最终话术，Router 负责识别/派发/追踪，Agent 负责业务执行。",
            content={
                "assistant": "用户入口、展示、主动推送、最终话术",
                "router": "spec-driven recognize / plan / dispatch / track / handover",
                "agent": "业务提槽、确认、风控、限额、业务 API、结构化结果",
            },
            included_blocks=included_blocks,
            dropped_blocks=dropped_blocks,
        )
        self._trace(
            trace,
            block="routing_state",
            stage="turn_start",
            source_type="session_store",
            summary="读取 Router 路由态；这里只保存路由态，不保存业务执行态。",
            content={
                "active_scene_id": state.active_scene_id,
                "pending_scene_id": state.pending_scene_id,
                "target_agent": state.target_agent,
                "agent_task_id": state.agent_task_id,
                "routing_slots": dict(state.routing_slots),
                "turn_count": state.turn_count,
            },
            included_blocks=included_blocks,
            dropped_blocks=dropped_blocks,
        )
        self._trace(
            trace,
            block="scene_index",
            stage="before_recognition",
            source_type="scene_specs",
            summary="加载场景索引字段给 recognizer；不是加载执行 Agent 的业务流程。",
            files=self._scene_files(scene_index_scenes),
            content=[
                {
                    "scene_id": scene.scene_id,
                    "name": scene.name,
                    "target_agent": scene.target_agent,
                    "skill_id": scene.skill.get("skill_id"),
                    "description": scene.description,
                    "triggers": {
                        "examples": list(scene.triggers.examples),
                        "negative_examples": list(scene.triggers.negative_examples),
                    },
                    "spec_hash": scene.spec_hash,
                }
                for scene in scene_index_scenes
            ],
            included_blocks=included_blocks,
            dropped_blocks=dropped_blocks,
        )
        if candidates:
            self._trace(
                trace,
                block="scene_candidates",
                stage="after_recognition",
                source_type="llm_recognizer_output",
                summary="recognizer 返回候选场景、置信度、理由和 routing slot hints。",
                content=[
                    {
                        "scene_id": candidate.scene.scene_id,
                        "score": candidate.score,
                        "reasons": list(candidate.reasons),
                        "routing_slots": dict(candidate.routing_slots),
                    }
                    for candidate in candidates
                ],
                included_blocks=included_blocks,
                dropped_blocks=dropped_blocks,
            )
        if selected_scene is not None:
            self._trace(
                trace,
                block="routing_spec",
                stage="after_scene_selected",
                source_type="scene_routing_json",
                summary=f"选中 {selected_scene.scene_id} 后加载该场景 routing spec。",
                files=self._scene_files([selected_scene]),
                content={
                    "scene_id": selected_scene.scene_id,
                    "version": selected_scene.version,
                    "name": selected_scene.name,
                    "description": selected_scene.description,
                    "target_agent": selected_scene.target_agent,
                    "spec_hash": selected_scene.spec_hash,
                },
                included_blocks=included_blocks,
                dropped_blocks=dropped_blocks,
            )
            self._trace(
                trace,
                block="routing_slot_spec",
                stage="after_scene_selected",
                source_type="scene_routing_json",
                summary="加载允许传给 Agent 的 routing slot hints；Router runtime 只按这些名称投影。",
                files=self._scene_files([selected_scene]),
                json_path="$.routing_slots",
                content=[
                    {
                        "name": slot.name,
                        "source": slot.source,
                        "handoff": slot.handoff,
                        "required_for_dispatch": slot.required_for_dispatch,
                        "extraction": dict(slot.extraction),
                    }
                    for slot in selected_scene.routing_slots
                ],
                included_blocks=included_blocks,
                dropped_blocks=dropped_blocks,
            )
            skill_doc = self._skill_doc(selected_scene)
            self._trace(
                trace,
                block="skill_card",
                stage="after_scene_selected",
                source_type="scene_skill_metadata",
                summary="加载场景绑定的 Skill 信息。Router 只用于识别和派发上下文，真正执行由 Agent 完成。",
                files=[skill_doc] if skill_doc else [],
                content={
                    "skill": dict(selected_scene.skill),
                    "markdown_excerpt": skill_doc.get("excerpt") if skill_doc else None,
                },
                included_blocks=included_blocks,
                dropped_blocks=dropped_blocks,
            )
            self._trace(
                trace,
                block="dispatch_contract",
                stage="before_dispatch",
                source_type="scene_routing_json",
                summary="加载派发契约，构造 Agent task payload。",
                files=self._scene_files([selected_scene]),
                json_path="$.dispatch_contract",
                content={
                    "task_type": selected_scene.dispatch_contract.task_type,
                    "handoff_fields": list(selected_scene.dispatch_contract.handoff_fields),
                    "target_agent": selected_scene.target_agent,
                },
                included_blocks=included_blocks,
                dropped_blocks=dropped_blocks,
            )
        if recent_records:
            self._trace(
                trace,
                block="recent_transcript",
                stage="after_state",
                source_type="transcript_store",
                summary=f"加载最近 {len(recent_records)} 条 Router transcript，用于多轮续接和审计。",
                content=[
                    {
                        "turn_id": record.turn_id,
                        "event_type": record.event_type,
                        "payload_keys": sorted(record.payload.keys()),
                    }
                    for record in recent_records
                ],
                included_blocks=included_blocks,
                dropped_blocks=dropped_blocks,
            )
        reference_docs = self._reference_docs(selected_scene=selected_scene, candidates=candidates, references=references)
        if reference_docs:
            self._trace(
                trace,
                block="retrieved_references",
                stage="after_scene_selected",
                source_type="reference_markdown",
                summary="按场景 references 加载必要 markdown 片段，用于解释 routing spec 和 Skill 触发。",
                files=reference_docs,
                content=[
                    {
                        "path": item["path"],
                        "exists": item["exists"],
                        "excerpt": item.get("excerpt"),
                    }
                    for item in reference_docs
                ],
                included_blocks=included_blocks,
                dropped_blocks=dropped_blocks,
            )
        return trace

    def _trace(
        self,
        trace: list[dict[str, Any]],
        *,
        block: str,
        stage: str,
        source_type: str,
        summary: str,
        included_blocks: set[str],
        dropped_blocks: set[str],
        content: Any | None = None,
        files: list[dict[str, Any]] | None = None,
        json_path: str | None = None,
    ) -> None:
        trace.append(
            {
                "seq": len(trace) + 1,
                "block": block,
                "stage": stage,
                "source_type": source_type,
                "included": block in included_blocks,
                "dropped": block in dropped_blocks,
                "summary": summary,
                "files": files or [],
                "json_path": json_path,
                "content": content,
            }
        )

    def _scene_files(self, scenes: list[SceneSpec]) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for scene in scenes:
            if not scene.source_path:
                continue
            files.append(
                {
                    "path": scene.source_path,
                    "kind": "routing_spec",
                    "exists": Path(scene.source_path).exists(),
                    "scene_id": scene.scene_id,
                    "hash": scene.spec_hash,
                }
            )
        return files

    def _skill_doc(self, scene: SceneSpec) -> dict[str, Any] | None:
        raw_path = scene.skill.get("path") if isinstance(scene.skill, dict) else None
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        path = self._resolve_related_path(scene=scene, relative_path=raw_path)
        return self._markdown_file(path=path, kind="skill_markdown", logical_path=raw_path)

    def _reference_docs(
        self,
        *,
        selected_scene: SceneSpec | None,
        candidates: list[IntentCandidate],
        references: list[str],
    ) -> list[dict[str, Any]]:
        scenes = [selected_scene] if selected_scene is not None else [candidate.scene for candidate in candidates]
        docs: list[dict[str, Any]] = []
        for ref in references:
            for scene in scenes:
                if scene is None:
                    continue
                path = self._resolve_related_path(scene=scene, relative_path=ref)
                docs.append(self._markdown_file(path=path, kind="reference_markdown", logical_path=ref))
                break
        return docs

    def _resolve_related_path(self, *, scene: SceneSpec, relative_path: str) -> Path:
        if not scene.source_path:
            return Path(relative_path)
        spec_root = Path(scene.source_path).parent.parent
        return (spec_root / relative_path).resolve()

    def _markdown_file(self, *, path: Path, kind: str, logical_path: str) -> dict[str, Any]:
        exists = path.exists()
        text = path.read_text(encoding="utf-8") if exists else ""
        return {
            "path": str(path),
            "logical_path": logical_path,
            "kind": kind,
            "exists": exists,
            "chars": len(text),
            "excerpt": self._excerpt(text),
        }

    def _excerpt(self, text: str, *, limit: int = 520) -> str:
        normalized = "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit].rstrip() + "..."

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
