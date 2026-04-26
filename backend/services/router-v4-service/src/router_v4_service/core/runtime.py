from __future__ import annotations

import uuid
from typing import Any

from router_v4_service.core.agent_client import AgentDispatchClient, AgentDispatchError
from router_v4_service.core.config import RouterV4Settings
from router_v4_service.core.context import ContextBuilder
from router_v4_service.core.extractor import RoutingSlotExtractor
from router_v4_service.core.matcher import SceneCandidate, SceneMatcher
from router_v4_service.core.models import (
    GraphStatus,
    RouterGraphState,
    RouterTaskState,
    RouterTurnStatus,
    RouterV4Input,
    RouterV4Output,
    RoutingSessionState,
    SceneSpec,
    TaskStatus,
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

FALLBACK_AGENT_ID = "fallback-agent"
PUSH_GENERIC_ACCEPT_TERMS = ("就按这个", "按这个", "就这个", "办理这个", "按推荐", "就按推荐", "这个")
PUSH_MULTI_TERMS = ("都", "一起", "全部", "都办", "都看")
PUSH_REJECT_TERMS = ("不要", "不用", "取消", "算了", "不办", "拒绝")


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
        state.source = request.source or "user"
        state.push_context = dict(request.push_context)
        state.raw_messages.append(request.message)
        turn_id = self._new_turn_id()
        self._append(
            state,
            turn_id,
            "user_message",
            {
                "message": request.message,
                "source": request.source,
                "has_push_context": bool(request.push_context),
                "has_user_profile": bool(request.user_profile),
                "has_page_context": bool(request.page_context),
            },
        )

        if state.agent_task_id and state.target_agent and state.dispatch_status in {"dispatched", "waiting_agent"}:
            return self._forward_to_active_agent(state=state, request=request, turn_id=turn_id)

        if self._is_push_rejection(request):
            return self._return_no_action(state=state, request=request, turn_id=turn_id)

        push_scene_ids = self._push_scene_ids(request)
        if self._is_multi_push_request(request, push_scene_ids):
            return self._plan_push_tasks(state=state, request=request, scene_ids=push_scene_ids, turn_id=turn_id)

        pending_scene = self._pending_scene(state)
        if pending_scene is not None:
            candidates = [SceneCandidate(scene=pending_scene, score=100, reasons=("pending_scene",))]
        else:
            scenes = self._candidate_scenes(push_scene_ids)
            candidates = self.matcher.shortlist(request.message, scenes)
            if not candidates and self._is_push_generic_acceptance(request) and push_scene_ids:
                candidates = [SceneCandidate(scene=self.registry.scene(push_scene_ids[0]), score=90, reasons=("push_default",))]
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

        task_id = dispatch.agent_task_id
        task = self._make_task(
            task_id=task_id,
            scene=scene,
            target_agent=agent.agent_id,
            agent_task_id=dispatch.agent_task_id,
            status=TaskStatus.DISPATCHED,
            request=request,
            routing_slots=handoff_slots,
        )
        state.tasks[task_id] = task
        state.bind_dispatch(
            task_id=task_id,
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
                "task_id": task_id,
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
            task_id=task_id,
            routing_slots=handoff_slots,
            tasks=(task.to_dict(),),
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
                    "task_id": task_id,
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
            task_id=state.active_task_ids[0] if state.active_task_ids else state.agent_task_id,
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
            "source": request.source,
            "push_context": dict(request.push_context),
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
                "active_graph_id": state.active_graph_id,
                "active_task_ids": list(state.active_task_ids),
                "source": state.source,
                "push_context": dict(state.push_context),
                "raw_messages": list(state.raw_messages),
                "selected_scene_ids": list(state.selected_scene_ids),
                "target_agents": list(state.target_agents),
                "agent_task_ids": list(state.agent_task_ids),
                "handover_records": list(state.handover_records),
                "agent_outputs": dict(state.agent_outputs),
                "assistant_result_status": state.assistant_result_status,
            },
            "tasks": {task_id: task.to_dict() for task_id, task in state.tasks.items()},
            "graphs": {graph_id: graph.to_dict() for graph_id, graph in state.graphs.items()},
            "transcript": [record.to_dict() for record in transcripts],
        }

    def task_snapshot(self, session_id: str, task_id: str) -> dict[str, Any]:
        state = self.session_store.get_or_create(session_id)
        task = state.tasks.get(task_id)
        if task is None:
            return {"found": False, "session_id": session_id, "task_id": task_id}
        return {"found": True, "session_id": session_id, "task": task.to_dict()}

    def graph_snapshot(self, session_id: str, graph_id: str) -> dict[str, Any]:
        state = self.session_store.get_or_create(session_id)
        graph = state.graphs.get(graph_id)
        if graph is None:
            return {"found": False, "session_id": session_id, "graph_id": graph_id}
        return {
            "found": True,
            "session_id": session_id,
            "graph": graph.to_dict(),
            "tasks": [
                state.tasks[task_id].to_dict()
                for task_id in graph.task_ids
                if task_id in state.tasks
            ],
        }

    def handle_agent_output(
        self,
        *,
        session_id: str,
        task_id: str,
        agent_payload: dict[str, Any],
    ) -> RouterV4Output:
        state = self.session_store.get_or_create(session_id)
        task = state.tasks.get(task_id)
        if task is None:
            return RouterV4Output(
                session_id=session_id,
                status=RouterTurnStatus.FAILED,
                response="router_task_not_found",
                task_id=task_id,
                events=({"type": "task.not_found", "task_id": task_id},),
            )

        if self._is_handover_payload(agent_payload):
            return self._handle_handover_output(state=state, task=task, agent_payload=agent_payload)
        if self._is_abnormal_handover_like_payload(agent_payload):
            task.status = TaskStatus.FAILED
            task.abnormal_agent_output = dict(agent_payload)
            if state.agent_task_id == task.agent_task_id:
                state.dispatch_status = task.status.value
            state.assistant_result_status = "agent_output_abnormal"
            self._append(
                state,
                self._new_turn_id(),
                "task.agent_output_abnormal",
                {"task_id": task.task_id, "agent_payload": agent_payload},
            )
            self.session_store.save(state)
            return RouterV4Output(
                session_id=session_id,
                status=RouterTurnStatus.TASK_UPDATED,
                response="agent_output_abnormal",
                scene_id=task.scene_id,
                target_agent=task.target_agent,
                agent_task_id=task.agent_task_id,
                task_id=task.task_id,
                agent_output=dict(agent_payload),
                events=({"type": "task.agent_output_abnormal", "task_id": task.task_id},),
            )

        status = str(agent_payload.get("status") or TaskStatus.COMPLETED.value)
        task.status = self._task_status_from_agent(status)
        output = agent_payload.get("output")
        task.agent_output = dict(output) if isinstance(output, dict) else {"raw": output}
        state.agent_outputs[task.task_id] = task.agent_output
        state.assistant_result_status = "ready_for_assistant"
        self._refresh_graph_statuses(state)
        if state.agent_task_id == task.agent_task_id:
            state.dispatch_status = task.status.value
        self._append(
            state,
            self._new_turn_id(),
            "task.agent_output_recorded",
            {"task_id": task.task_id, "status": task.status.value, "agent_output": task.agent_output},
        )
        self.session_store.save(state)
        return RouterV4Output(
            session_id=session_id,
            status=RouterTurnStatus.TASK_UPDATED,
            response="agent_output_recorded",
            scene_id=task.scene_id,
            target_agent=task.target_agent,
            agent_task_id=task.agent_task_id,
            task_id=task.task_id,
            agent_output=task.agent_output,
            events=({"type": f"task.{task.status.value}", "task_id": task.task_id},),
        )

    def _handle_handover_output(
        self,
        *,
        state: RoutingSessionState,
        task: RouterTaskState,
        agent_payload: dict[str, Any],
    ) -> RouterV4Output:
        if task.handover_used or task.original_task_id is not None:
            task.status = TaskStatus.HANDOVER_EXHAUSTED
            task.agent_output = dict(agent_payload)
            if state.agent_task_id == task.agent_task_id:
                state.dispatch_status = task.status.value
            state.assistant_result_status = "handover_exhausted"
            self._append(
                state,
                self._new_turn_id(),
                "task.handover_exhausted",
                {"task_id": task.task_id, "agent_payload": agent_payload},
            )
            self.session_store.save(state)
            return RouterV4Output(
                session_id=state.session_id,
                status=RouterTurnStatus.TASK_UPDATED,
                response="handover_exhausted",
                scene_id=task.scene_id,
                target_agent=task.target_agent,
                agent_task_id=task.agent_task_id,
                task_id=task.task_id,
                agent_output=dict(agent_payload),
                events=({"type": "task.handover_exhausted", "task_id": task.task_id},),
            )

        task.status = TaskStatus.HANDOVER_REQUESTED
        task.handover_used = True
        if state.agent_task_id == task.agent_task_id:
            state.dispatch_status = task.status.value
        handover_record = {
            "original_task_id": task.task_id,
            "original_scene_id": task.scene_id,
            "original_agent": task.target_agent,
            "original_agent_task_id": task.agent_task_id,
            "raw_message": task.raw_message,
            "agent_payload": dict(agent_payload),
        }
        state.handover_records.append(handover_record)
        fallback_payload = {
            "router_session_id": state.session_id,
            "original_task_id": task.task_id,
            "original_scene_id": task.scene_id,
            "original_agent": task.target_agent,
            "raw_message": task.raw_message,
            "handover_reason": dict(agent_payload),
            "routing_slots": dict(task.routing_slots),
            "push_context": dict(task.push_context),
            "scene_id": "fallback",
            "task_type": "fallback",
            "scene_spec_hash": task.scene_spec_hash,
        }
        try:
            fallback_agent = self.registry.agent(FALLBACK_AGENT_ID)
            dispatch = self.agent_client.dispatch(agent=fallback_agent, task_payload=fallback_payload)
        except (SpecRegistryError, AgentDispatchError) as exc:
            task.status = TaskStatus.FAILED
            self._append(
                state,
                self._new_turn_id(),
                "task.fallback_dispatch_failed",
                {"task_id": task.task_id, "error": str(exc)},
            )
            self.session_store.save(state)
            return RouterV4Output(
                session_id=state.session_id,
                status=RouterTurnStatus.FAILED,
                response=str(exc),
                scene_id=task.scene_id,
                target_agent=task.target_agent,
                task_id=task.task_id,
                events=({"type": "task.fallback_dispatch_failed", "task_id": task.task_id},),
            )

        fallback_task_id = dispatch.agent_task_id
        fallback_task = RouterTaskState(
            task_id=fallback_task_id,
            scene_id="fallback",
            target_agent=fallback_agent.agent_id,
            agent_task_id=dispatch.agent_task_id,
            status=TaskStatus.FALLBACK_DISPATCHED,
            raw_message=task.raw_message,
            routing_slots=dict(task.routing_slots),
            scene_spec_hash=task.scene_spec_hash,
            stream_url=self._stream_url(fallback_task_id),
            resume_token=self._resume_token(fallback_task_id),
            source=task.source,
            push_context=dict(task.push_context),
            original_task_id=task.task_id,
        )
        task.fallback_task_id = fallback_task_id
        state.tasks[fallback_task_id] = fallback_task
        state.active_task_ids = [fallback_task_id]
        state.target_agent = fallback_agent.agent_id
        state.agent_task_id = dispatch.agent_task_id
        state.dispatch_status = TaskStatus.FALLBACK_DISPATCHED.value
        state.target_agents = self._append_unique(state.target_agents, fallback_agent.agent_id)
        state.agent_task_ids = self._append_unique(state.agent_task_ids, dispatch.agent_task_id)
        self._append(
            state,
            self._new_turn_id(),
            "task.handover_requested",
            handover_record,
        )
        self._append(
            state,
            self._new_turn_id(),
            "task.fallback_dispatched",
            {
                "original_task_id": task.task_id,
                "fallback_task_id": fallback_task_id,
                "fallback_agent": fallback_agent.agent_id,
            },
        )
        self.session_store.save(state)
        return RouterV4Output(
            session_id=state.session_id,
            status=RouterTurnStatus.TASK_UPDATED,
            response="fallback_dispatched",
            scene_id="fallback",
            target_agent=fallback_agent.agent_id,
            agent_task_id=dispatch.agent_task_id,
            task_id=fallback_task_id,
            tasks=(task.to_dict(), fallback_task.to_dict()),
            events=(
                {"type": "task.handover_requested", "task_id": task.task_id},
                {
                    "type": "task.fallback_dispatched",
                    "task_id": fallback_task_id,
                    "original_task_id": task.task_id,
                    "target_agent": fallback_agent.agent_id,
                },
            ),
        )

    def _return_no_action(
        self,
        *,
        state: RoutingSessionState,
        request: RouterV4Input,
        turn_id: str,
    ) -> RouterV4Output:
        state.dispatch_status = RouterTurnStatus.NO_ACTION.value
        state.summary = "用户拒绝主动推荐，Router 不派发任务"
        self._append(
            state,
            turn_id,
            "push.no_action",
            {"source": request.source, "push_context": dict(request.push_context)},
        )
        self.session_store.save(state)
        return RouterV4Output(
            session_id=request.session_id,
            status=RouterTurnStatus.NO_ACTION,
            response="no_action",
            events=({"type": "push.no_action"},),
            prompt_report=self._build_prompt_report(state=state, candidates=[], selected_scene=None),
        )

    def _plan_push_tasks(
        self,
        *,
        state: RoutingSessionState,
        request: RouterV4Input,
        scene_ids: list[str],
        turn_id: str,
    ) -> RouterV4Output:
        graph_id = "graph_" + uuid.uuid4().hex[:12]
        tasks: list[RouterTaskState] = []
        events: list[dict[str, Any]] = [{"type": "plan.created", "graph_id": graph_id}]
        for scene_id in scene_ids:
            try:
                scene = self.registry.scene(scene_id)
                agent = self.registry.agent(scene.target_agent)
            except SpecRegistryError as exc:
                events.append({"type": "task.plan_failed", "scene_id": scene_id, "error": str(exc)})
                continue
            routing_slots = self.extractor.handoff_slots(scene, self.extractor.extract(request.message, scene))
            payload = self._build_agent_task_payload(
                request=request,
                state=state,
                scene=scene,
                routing_slots=routing_slots,
            )
            try:
                dispatch = self.agent_client.dispatch(agent=agent, task_payload=payload)
            except AgentDispatchError as exc:
                events.append({"type": "task.dispatch_failed", "scene_id": scene_id, "error": str(exc)})
                continue
            task = self._make_task(
                task_id=dispatch.agent_task_id,
                scene=scene,
                target_agent=agent.agent_id,
                agent_task_id=dispatch.agent_task_id,
                status=TaskStatus.DISPATCHED,
                request=request,
                routing_slots=routing_slots,
            )
            state.tasks[task.task_id] = task
            tasks.append(task)
            state.selected_scene_ids = self._append_unique(state.selected_scene_ids, scene.scene_id)
            state.target_agents = self._append_unique(state.target_agents, agent.agent_id)
            state.agent_task_ids = self._append_unique(state.agent_task_ids, dispatch.agent_task_id)
            events.append(
                {
                    "type": "task.dispatched",
                    "task_id": task.task_id,
                    "scene_id": scene.scene_id,
                    "target_agent": agent.agent_id,
                }
            )

        if not tasks:
            state.dispatch_status = RouterTurnStatus.FAILED.value
            self._append(state, turn_id, "plan.failed", {"events": events})
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.FAILED,
                response="plan_failed",
                events=tuple(events),
            )

        graph = RouterGraphState(
            graph_id=graph_id,
            task_ids=[task.task_id for task in tasks],
            status=GraphStatus.RUNNING,
            source=request.source,
            stream_mode="split_by_task",
        )
        state.graphs[graph_id] = graph
        state.active_graph_id = graph_id
        state.active_task_ids = list(graph.task_ids)
        state.active_scene_id = tasks[0].scene_id
        state.target_agent = tasks[0].target_agent
        state.agent_task_id = tasks[0].agent_task_id
        state.dispatch_status = RouterTurnStatus.PLANNED.value
        state.summary = f"已创建多意图计划 {graph_id}"
        self._append(
            state,
            turn_id,
            "plan.created",
            {"graph_id": graph_id, "task_ids": graph.task_ids, "stream_mode": graph.stream_mode},
        )
        self.session_store.save(state)
        return RouterV4Output(
            session_id=request.session_id,
            status=RouterTurnStatus.PLANNED,
            response="planned",
            graph_id=graph_id,
            stream_mode=graph.stream_mode,
            tasks=tuple(task.to_dict() for task in tasks),
            events=tuple(events),
        )

    def _candidate_scenes(self, push_scene_ids: list[str]) -> list[SceneSpec]:
        if not push_scene_ids:
            return self.registry.scene_index()
        scenes: list[SceneSpec] = []
        for scene_id in push_scene_ids:
            try:
                scenes.append(self.registry.scene(scene_id))
            except SpecRegistryError:
                continue
        return scenes or self.registry.scene_index()

    def _push_scene_ids(self, request: RouterV4Input) -> list[str]:
        raw_items = request.push_context.get("intents") if isinstance(request.push_context, dict) else None
        if not isinstance(raw_items, list):
            return []
        ranked: list[tuple[int, str]] = []
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            scene_id = item.get("scene_id") or item.get("intent_code")
            if not isinstance(scene_id, str) or not scene_id.strip():
                continue
            rank = item.get("rank")
            ranked.append((rank if isinstance(rank, int) else index + 1, scene_id.strip()))
        return [scene_id for _, scene_id in sorted(ranked, key=lambda value: value[0])]

    def _is_push_request(self, request: RouterV4Input) -> bool:
        return request.source == "assistant_push" or bool(request.push_context)

    def _is_push_rejection(self, request: RouterV4Input) -> bool:
        return self._is_push_request(request) and any(term in request.message for term in PUSH_REJECT_TERMS)

    def _is_push_generic_acceptance(self, request: RouterV4Input) -> bool:
        return self._is_push_request(request) and any(term in request.message for term in PUSH_GENERIC_ACCEPT_TERMS)

    def _is_multi_push_request(self, request: RouterV4Input, scene_ids: list[str]) -> bool:
        return self._is_push_request(request) and len(scene_ids) > 1 and any(term in request.message for term in PUSH_MULTI_TERMS)

    def _make_task(
        self,
        *,
        task_id: str,
        scene: SceneSpec,
        target_agent: str,
        agent_task_id: str,
        status: TaskStatus,
        request: RouterV4Input,
        routing_slots: dict[str, Any],
    ) -> RouterTaskState:
        return RouterTaskState(
            task_id=task_id,
            scene_id=scene.scene_id,
            target_agent=target_agent,
            agent_task_id=agent_task_id,
            status=status,
            raw_message=request.message,
            routing_slots=dict(routing_slots),
            scene_spec_hash=scene.spec_hash,
            stream_url=self._stream_url(task_id),
            resume_token=self._resume_token(task_id),
            source=request.source,
            push_context=dict(request.push_context),
        )

    def _stream_url(self, task_id: str) -> str:
        return f"/api/router/v4/streams/{task_id}"

    def _resume_token(self, task_id: str) -> str:
        return "rt_" + uuid.uuid5(uuid.NAMESPACE_URL, task_id).hex[:12]

    def _is_handover_payload(self, payload: dict[str, Any]) -> bool:
        output = payload.get("output")
        data = output.get("data") if isinstance(output, dict) else None
        return payload.get("ishandover") is True and data == []

    def _is_abnormal_handover_like_payload(self, payload: dict[str, Any]) -> bool:
        output = payload.get("output")
        data = output.get("data") if isinstance(output, dict) else None
        return (payload.get("ishandover") is True) != (data == [])

    def _task_status_from_agent(self, status: str) -> TaskStatus:
        try:
            return TaskStatus(status)
        except ValueError:
            return TaskStatus.COMPLETED

    def _refresh_graph_statuses(self, state: RoutingSessionState) -> None:
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.HANDOVER_EXHAUSTED}
        for graph in state.graphs.values():
            tasks = [state.tasks[task_id] for task_id in graph.task_ids if task_id in state.tasks]
            if not tasks:
                continue
            if all(task.status == TaskStatus.COMPLETED for task in tasks):
                graph.status = GraphStatus.COMPLETED
            elif any(task.status == TaskStatus.FAILED for task in tasks) and any(task.status == TaskStatus.COMPLETED for task in tasks):
                graph.status = GraphStatus.PARTIALLY_COMPLETED
            elif all(task.status in terminal for task in tasks):
                graph.status = GraphStatus.FAILED
            else:
                graph.status = GraphStatus.RUNNING

    def _append_unique(self, values: list[str], value: str) -> list[str]:
        if value in values:
            return values
        return [*values, value]

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
