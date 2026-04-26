from __future__ import annotations

import uuid
from typing import Any

from router_v4_service.core.agent_client import AgentDispatchClient, AgentDispatchError
from router_v4_service.core.config import RouterV4Settings
from router_v4_service.core.context import ContextBuilder
from router_v4_service.core.extractor import RoutingSlotExtractor
from router_v4_service.core.matcher import SceneCandidate, SceneMatcher
from router_v4_service.core.models import (
    RouterTurnStatus,
    RouterV4Input,
    RouterV4Output,
    RoutingSessionState,
    SceneSpec,
)
from router_v4_service.core.spec_registry import SpecRegistry, SpecRegistryError
from router_v4_service.core.stores import (
    FileRoutingSessionStore,
    FileTranscriptStore,
    InMemoryRoutingSessionStore,
    InMemoryTranscriptStore,
    RoutingSessionStore,
    TranscriptRecord,
    TranscriptStore,
)


class RouterV4Runtime:
    """Spec-driven router runtime.

    This runtime owns routing, dispatch and tracking only. It does not execute
    scene business workflows.
    """

    def __init__(
        self,
        *,
        registry: SpecRegistry | None = None,
        matcher: SceneMatcher | None = None,
        extractor: RoutingSlotExtractor | None = None,
        agent_client: AgentDispatchClient | None = None,
        session_store: RoutingSessionStore | None = None,
        transcript_store: TranscriptStore | None = None,
        context_builder: ContextBuilder | None = None,
        settings: RouterV4Settings | None = None,
    ) -> None:
        self.settings = settings or RouterV4Settings.from_env()
        self.registry = registry or SpecRegistry(self.settings.spec_root)
        self.matcher = matcher or SceneMatcher()
        self.extractor = extractor or RoutingSlotExtractor()
        self.agent_client = agent_client or AgentDispatchClient()
        if session_store is not None:
            self.session_store = session_store
        elif self.settings.state_dir is not None:
            self.session_store = FileRoutingSessionStore(self.settings.state_dir)
        else:
            self.session_store = InMemoryRoutingSessionStore()
        if transcript_store is not None:
            self.transcript_store = transcript_store
        elif self.settings.state_dir is not None:
            self.transcript_store = FileTranscriptStore(self.settings.state_dir)
        else:
            self.transcript_store = InMemoryTranscriptStore()
        self.context_builder = context_builder or ContextBuilder(self.settings.context_policy)

    def handle_turn(self, request: RouterV4Input) -> RouterV4Output:
        state = self.session_store.get_or_create(request.session_id)
        state.turn_count += 1
        turn_id = self._new_turn_id()
        self._append(
            state,
            turn_id,
            "user_message",
            {
                "message": request.message,
                "has_user_profile": bool(request.user_profile),
                "has_page_context": bool(request.page_context),
            },
        )

        if state.agent_task_id and state.target_agent and state.dispatch_status in {"dispatched", "waiting_agent"}:
            return self._forward_to_active_agent(state=state, request=request, turn_id=turn_id)

        pending_scene = self._pending_scene(state)
        if pending_scene is not None:
            candidates = [SceneCandidate(scene=pending_scene, score=100, reasons=("pending_scene",))]
        else:
            scenes = self.registry.scene_index()
            candidates = self.matcher.shortlist(request.message, scenes)
        if not candidates:
            prompt_report = self._build_prompt_report(state=state, candidates=[], selected_scene=None)
            self._append(state, turn_id, "scene_unrecognized", {"message": request.message})
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.CLARIFICATION_REQUIRED,
                response="我还没有识别出具体业务场景，请换个说法或补充要办理的业务。",
                events=({"type": "scene_unrecognized"},),
                prompt_report=prompt_report,
            )

        selected = self._select_candidate(candidates)
        if selected is None:
            prompt_report = self._build_prompt_report(state=state, candidates=candidates, selected_scene=None)
            scene_names = "、".join(candidate.scene.name for candidate in candidates[:2])
            self._append(
                state,
                turn_id,
                "scene_ambiguous",
                {"candidates": [candidate.scene.scene_id for candidate in candidates]},
            )
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.CLARIFICATION_REQUIRED,
                response=f"这个请求可能涉及多个场景：{scene_names}。请补充说明要办理哪一类业务。",
                events=({"type": "scene_ambiguous"},),
                prompt_report=prompt_report,
            )

        scene = selected.scene
        prompt_report = self._build_prompt_report(state=state, candidates=candidates, selected_scene=scene)
        try:
            agent = self.registry.agent(scene.target_agent)
        except SpecRegistryError as exc:
            self._append(state, turn_id, "agent_missing", {"target_agent": scene.target_agent})
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.FAILED,
                response=str(exc),
                scene_id=scene.scene_id,
                target_agent=scene.target_agent,
                events=({"type": "agent_missing", "target_agent": scene.target_agent},),
                prompt_report=prompt_report,
            )

        current_slots = self.extractor.extract(request.message, scene)
        routing_slots = (
            {**state.routing_slots, **current_slots}
            if state.pending_scene_id == scene.scene_id
            else current_slots
        )
        missing = self.extractor.missing_required_for_dispatch(scene, routing_slots)
        if missing:
            state.pending_scene_id = scene.scene_id
            state.dispatch_status = "waiting_router_slot"
            state.routing_slots = dict(routing_slots)
            state.summary = f"{scene.name}场景等待 Router 侧路由槽位：{', '.join(missing)}"
            self._append(
                state,
                turn_id,
                "route_clarification_required",
                {"scene_id": scene.scene_id, "missing_slots": missing},
            )
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.CLARIFICATION_REQUIRED,
                response=f"为了判断是否进入{scene.name}场景，请补充：{missing[0]}。",
                scene_id=scene.scene_id,
                routing_slots=routing_slots,
                action_required={"type": "input", "slot": missing[0], "owner": "router"},
                events=({"type": "route_clarification_required", "missing_slots": missing},),
                prompt_report=prompt_report,
            )

        handoff_slots = self.extractor.handoff_slots(scene, routing_slots)
        task_payload = self._build_agent_task_payload(
            request=request,
            state=state,
            scene=scene,
            routing_slots=handoff_slots,
        )
        try:
            dispatch = self.agent_client.dispatch(agent=agent, task_payload=task_payload)
        except AgentDispatchError as exc:
            self._append(state, turn_id, "agent_dispatch_failed", {"error": str(exc)})
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.FAILED,
                response=str(exc),
                scene_id=scene.scene_id,
                target_agent=agent.agent_id,
                routing_slots=handoff_slots,
                events=({"type": "agent_dispatch_failed"},),
                prompt_report=prompt_report,
            )

        state.bind_dispatch(
            scene_id=scene.scene_id,
            target_agent=agent.agent_id,
            agent_task_id=dispatch.agent_task_id,
            routing_slots=handoff_slots,
            summary=f"{scene.name}场景已派发给 {agent.agent_id}",
        )
        self.session_store.save(state)
        self._append(
            state,
            turn_id,
            "agent_dispatched",
            {
                "scene_id": scene.scene_id,
                "target_agent": agent.agent_id,
                "agent_task_id": dispatch.agent_task_id,
                "routing_slots": handoff_slots,
            },
        )
        return RouterV4Output(
            session_id=request.session_id,
            status=RouterTurnStatus.DISPATCHED,
            response=dispatch.message,
            scene_id=scene.scene_id,
            target_agent=agent.agent_id,
            agent_task_id=dispatch.agent_task_id,
            routing_slots=handoff_slots,
            events=(
                {
                    "type": "scene_selected",
                    "scene_id": scene.scene_id,
                    "score": selected.score,
                    "reasons": list(selected.reasons),
                },
                {
                    "type": "agent_dispatched",
                    "target_agent": agent.agent_id,
                    "agent_task_id": dispatch.agent_task_id,
                },
            ),
            prompt_report=prompt_report,
        )

    def _select_candidate(self, candidates: list[SceneCandidate]) -> SceneCandidate | None:
        if len(candidates) == 1:
            return candidates[0]
        if candidates[0].score == candidates[1].score:
            return None
        return candidates[0]

    def _forward_to_active_agent(
        self,
        *,
        state: RoutingSessionState,
        request: RouterV4Input,
        turn_id: str,
    ) -> RouterV4Output:
        assert state.target_agent is not None
        assert state.agent_task_id is not None
        result = self.agent_client.forward_message(
            agent_id=state.target_agent,
            agent_task_id=state.agent_task_id,
            message=request.message,
        )
        state.dispatch_status = "waiting_agent"
        self.session_store.save(state)
        self._append(
            state,
            turn_id,
            "agent_message_forwarded",
            {"target_agent": state.target_agent, "agent_task_id": state.agent_task_id},
        )
        prompt_report = self._build_prompt_report(state=state, candidates=[], selected_scene=None)
        return RouterV4Output(
            session_id=request.session_id,
            status=RouterTurnStatus.FORWARDED,
            response=result.message,
            scene_id=state.active_scene_id,
            target_agent=state.target_agent,
            agent_task_id=state.agent_task_id,
            routing_slots=dict(state.routing_slots),
            events=(
                {
                    "type": "agent_message_forwarded",
                    "target_agent": state.target_agent,
                    "agent_task_id": state.agent_task_id,
                },
            ),
            prompt_report=prompt_report,
        )

    def _build_agent_task_payload(
        self,
        *,
        request: RouterV4Input,
        state: RoutingSessionState,
        scene: SceneSpec,
        routing_slots: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "router_session_id": request.session_id,
            "scene_id": scene.scene_id,
            "task_type": scene.dispatch_contract.task_type,
            "raw_message": request.message,
            "routing_slots": dict(routing_slots),
            "context_refs": {
                "user_profile_present": bool(request.user_profile),
                "page_context_present": bool(request.page_context),
            },
            "scene_spec_hash": scene.spec_hash,
        }
        for field in scene.dispatch_contract.handoff_fields:
            if field in routing_slots:
                payload[field] = routing_slots[field]
            elif field == "raw_message":
                payload[field] = request.message
            elif field == "user_profile_ref":
                payload[field] = "request.user_profile"
            elif field == "page_context_ref":
                payload[field] = "request.page_context"
        if state.agent_task_id:
            payload["previous_agent_task_id"] = state.agent_task_id
        return payload

    def session_snapshot(self, session_id: str) -> dict[str, Any]:
        state = self.session_store.get_or_create(session_id)
        transcripts = self.transcript_store.list_for_session(session_id)
        return {
            "session": {
                "session_id": state.session_id,
                "active_scene_id": state.active_scene_id,
                "pending_scene_id": state.pending_scene_id,
                "target_agent": state.target_agent,
                "agent_task_id": state.agent_task_id,
                "dispatch_status": state.dispatch_status,
                "routing_slots": dict(state.routing_slots),
                "turn_count": state.turn_count,
                "summary": state.summary,
            },
            "transcript": [record.to_dict() for record in transcripts],
        }

    def _pending_scene(self, state: RoutingSessionState) -> SceneSpec | None:
        if state.pending_scene_id is None or state.dispatch_status != "waiting_router_slot":
            return None
        try:
            return self.registry.scene(state.pending_scene_id)
        except SpecRegistryError:
            state.pending_scene_id = None
            state.dispatch_status = None
            state.routing_slots = {}
            self.session_store.save(state)
            return None

    def _build_prompt_report(
        self,
        *,
        state: RoutingSessionState,
        candidates: list[SceneCandidate],
        selected_scene: SceneSpec | None,
    ) -> dict[str, Any]:
        return self.context_builder.build_report(
            state=state,
            candidates=candidates,
            selected_scene=selected_scene,
            transcripts=self.transcript_store.list_for_session(state.session_id),
        )

    def _append(
        self,
        state: RoutingSessionState,
        turn_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.transcript_store.append(
            TranscriptRecord(
                session_id=state.session_id,
                turn_id=turn_id,
                event_type=event_type,
                payload=payload,
            )
        )

    def _new_turn_id(self) -> str:
        return "turn_" + uuid.uuid4().hex[:12]
