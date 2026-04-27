from __future__ import annotations

from pathlib import Path
from typing import Any

from router_v4_service.core.recognizer import IntentCandidate
from router_v4_service.core.models import ContextPolicy, IntentSpec, RoutingSessionState
from router_v4_service.core.stores import TranscriptRecord


class ContextBuilder:
    """Build a bounded report of context blocks used by Router.

    The service exposes the report instead of a provider-specific prompt so the
    loading lifecycle is testable: state is always included, scene details are
    loaded only after recognition/selection, and lower-priority blocks are
    dropped when the budget is exceeded.
    """

    def __init__(self, policy: ContextPolicy | None = None) -> None:
        self.policy = policy or ContextPolicy()

    def build_report(
        self,
        *,
        state: RoutingSessionState,
        candidates: list[IntentCandidate],
        selected_intent: IntentSpec | None,
        transcripts: list[TranscriptRecord],
        intent_index_intents: list[IntentSpec] | None = None,
    ) -> dict[str, Any]:
        recent_records = transcripts[-self.policy.recent_turn_limit :]
        references = self._references(candidates=candidates)
        intent_index = intent_index_intents or [candidate.intent for candidate in candidates]
        blocks: list[dict[str, Any]] = []
        self._add_block(blocks, "router_boundary", 240, required=True, priority=0)
        self._add_block(blocks, "routing_state", 180 + len(state.summary), required=True, priority=0)
        self._add_block(blocks, "intent_catalog", 260 + sum(len(self._excerpt(intent.spec_markdown)) for intent in intent_index), required=True, priority=1)
        if candidates:
            self._add_block(
                blocks,
                "recognized_intents",
                120 + sum(len(candidate.intent.description) for candidate in candidates),
                priority=2,
            )
        if selected_intent is not None:
            self._add_block(blocks, "skill_reference", 120 + len(str(selected_intent.skill)), priority=2)
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
                selected_intent=selected_intent,
                recent_records=recent_records,
                included_blocks={block["name"] for block in included},
                dropped_blocks={block["name"] for block in dropped},
                references=references,
                intent_index_intents=intent_index,
            ),
            "recognized_intent_ids": [candidate.intent.intent_id for candidate in candidates],
            "selected_intent_id": selected_intent.intent_id if selected_intent else None,
            "selected_scene_id": selected_intent.scene_id if selected_intent else None,
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
                        "intent_catalog",
                        "recognized_intents" if candidates else "",
                        "skill_reference" if selected_intent is not None else "",
                        "dispatch_contract" if selected_intent is not None else "",
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
    ) -> list[str]:
        refs: list[str] = []
        for candidate in candidates:
            for ref in candidate.intent.references:
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
        selected_intent: IntentSpec | None,
        recent_records: list[TranscriptRecord],
        included_blocks: set[str],
        dropped_blocks: set[str],
        references: list[str],
        intent_index_intents: list[IntentSpec],
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
                "routing_hints": dict(state.routing_hints),
                "turn_count": state.turn_count,
            },
            included_blocks=included_blocks,
            dropped_blocks=dropped_blocks,
        )
        self._trace(
            trace,
            block="intent_catalog",
            stage="before_recognition",
            source_type="single_intent_catalog",
            summary="识别前只加载一个 intent.md 意图目录；目录内只有意图边界和 skill_ref，不加载 Skill 正文。",
            files=self._intent_files(intent_index_intents),
            content=[
                {
                    "intent_id": intent.intent_id,
                    "name": intent.name,
                    "markdown_excerpt": self._excerpt(intent.spec_markdown),
                    "spec_hash": intent.spec_hash,
                }
                for intent in intent_index_intents
            ],
            included_blocks=included_blocks,
            dropped_blocks=dropped_blocks,
        )
        if candidates:
            self._trace(
                trace,
                block="recognized_intents",
                stage="after_recognition",
                source_type="llm_recognizer_output",
                summary="recognizer 只返回本轮选中的 intent_id、置信度和理由；不返回场景和业务字段。",
                content=[
                    {
                        "intent_id": candidate.intent.intent_id,
                        "score": candidate.score,
                        "reasons": list(candidate.reasons),
                    }
                    for candidate in candidates
                ],
                included_blocks=included_blocks,
                dropped_blocks=dropped_blocks,
            )
        if selected_intent is not None:
            self._trace(
                trace,
                block="skill_reference",
                stage="after_recognition",
                source_type="intent_catalog_frontmatter",
                summary=f"命中 {selected_intent.intent_id} 后，只读取同一 intent.md 中的 skill_ref；Skill 正文由 Agent 加载。",
                files=self._intent_files([selected_intent]),
                spec_path=f"frontmatter.intents[{selected_intent.intent_id}].skill",
                content={
                    "intent_id": selected_intent.intent_id,
                    "scene_id": selected_intent.scene_id,
                    "target_agent": selected_intent.target_agent,
                    "skill_ref": dict(selected_intent.skill),
                },
                included_blocks=included_blocks,
                dropped_blocks=dropped_blocks,
            )
            self._trace(
                trace,
                block="dispatch_contract",
                stage="before_dispatch",
                source_type="intent_catalog_frontmatter",
                summary="从 intent.md 的命中意图条目读取派发契约，构造 Agent task payload。",
                files=self._intent_files([selected_intent]),
                spec_path=f"frontmatter.intents[{selected_intent.intent_id}].dispatch_contract",
                content={
                    "task_type": selected_intent.dispatch_contract.task_type,
                    "handoff_fields": list(selected_intent.dispatch_contract.handoff_fields),
                    "target_agent": selected_intent.target_agent,
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
        reference_docs = self._reference_docs(candidates=candidates, references=references)
        if reference_docs:
            self._trace(
                trace,
                block="retrieved_references",
                stage="before_recognition",
                source_type="intent_reference_markdown",
                summary="按 intent references 加载必要 markdown 片段，用于解释意图边界。",
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
        spec_path: str | None = None,
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
                "spec_path": spec_path,
                "content": content,
            }
        )

    def _intent_files(self, intents: list[IntentSpec]) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for intent in intents:
            if not intent.source_path:
                continue
            if intent.source_path in seen_paths:
                continue
            seen_paths.add(intent.source_path)
            files.append(
                {
                    "path": intent.source_path,
                    "kind": "intent_catalog_markdown",
                    "exists": Path(intent.source_path).exists(),
                    "hash": intent.spec_hash,
                }
            )
        return files

    def _reference_docs(
        self,
        *,
        candidates: list[IntentCandidate],
        references: list[str],
    ) -> list[dict[str, Any]]:
        intents = [candidate.intent for candidate in candidates]
        docs: list[dict[str, Any]] = []
        for ref in references:
            for intent in intents:
                if intent is None:
                    continue
                path = self._resolve_related_path(source_path=intent.source_path, relative_path=ref)
                docs.append(self._markdown_file(path=path, kind="intent_reference_markdown", logical_path=ref))
                break
        return docs

    def _resolve_related_path(self, *, source_path: str, relative_path: str) -> Path:
        if not source_path:
            return Path(relative_path)
        spec_root = Path(source_path).parent.parent
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
