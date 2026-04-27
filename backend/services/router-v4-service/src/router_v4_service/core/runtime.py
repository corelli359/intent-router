from __future__ import annotations

import uuid
from pathlib import Path
import tomllib
from typing import Any

from router_v4_service.core.agent_client import AgentDispatchClient, AgentDispatchError
from router_v4_service.core.config import RouterV4Settings
from router_v4_service.core.context import ContextBuilder
from router_v4_service.core.models import (
    GraphStatus,
    RouterGraphState,
    RouterTaskState,
    RouterTurnStatus,
    RouterV4Input,
    RouterV4Output,
    RoutingSessionState,
    IntentSpec,
    TaskStatus,
)
from router_v4_service.core.recognizer import (
    IntentCandidate,
    IntentRecognizer,
    IntentRecognizerError,
    LLMIntentRecognizer,
)
from router_v4_service.core.spec_registry import SpecRegistry, SpecRegistryError
from router_v4_service.core.skill_runtime import LLMSkillExecutor, SkillDecision, SkillExecutor
from router_v4_service.core.stores import (
    FileRoutingSessionStore,
    FileTranscriptStore,
    InMemoryRoutingSessionStore,
    InMemoryTranscriptStore,
    RoutingSessionStore,
    TranscriptRecord,
    TranscriptStore,
)
from router_v4_service.core.tool_runtime import LocalSkillToolExecutor, ToolExecutor


class RouterV4Runtime:
    """Spec-driven Intent/Skill ReAct runtime.

    The runtime starts ReAct from the intent catalog, then progressively loads
    the selected Skill. Business behavior is declared by Skill markdown and
    emitted as structured LLM/tool decisions; Router code only validates and
    tracks the generic lifecycle.
    """

    def __init__(
        self,
        *,
        registry: SpecRegistry | None = None,
        recognizer: IntentRecognizer | None = None,
        agent_client: AgentDispatchClient | None = None,
        session_store: RoutingSessionStore | None = None,
        transcript_store: TranscriptStore | None = None,
        context_builder: ContextBuilder | None = None,
        settings: RouterV4Settings | None = None,
        skill_executor: SkillExecutor | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        self.settings = settings or RouterV4Settings.from_env()
        self.registry = registry or SpecRegistry(self.settings.spec_root)
        self.recognizer = recognizer or LLMIntentRecognizer(self.settings.llm)
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
        self.skill_executor = skill_executor or (LLMSkillExecutor(self.settings.llm) if self.settings.llm.ready else None)
        self.tool_executor = tool_executor or LocalSkillToolExecutor()

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

        if state.agent_task_id and state.target_agent and state.dispatch_status in {"dispatched", "waiting_agent", "running"}:
            if self.skill_executor is not None:
                return self._continue_active_skill(state=state, request=request, turn_id=turn_id)
            return self._forward_to_active_agent(state=state, request=request, turn_id=turn_id)

        push_intent_ids = self._push_intent_ids(request)
        intents = self._candidate_intents(push_intent_ids)
        try:
            candidates = self.recognizer.recognize(
                request.message,
                intents,
                push_context=dict(request.push_context),
            )
        except IntentRecognizerError as exc:
            prompt_report = self._build_prompt_report(
                state=state,
                candidates=[],
                selected_intent=None,
                intent_index_intents=intents,
            )
            self._append(
                state,
                turn_id,
                "intent_react_failed",
                {"error": str(exc), "recognizer_backend": self.settings.recognizer_backend},
            )
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.FAILED,
                response=f"Intent ReAct 执行失败：{exc}",
                events=({"type": "intent_react_failed", "error": str(exc)},),
                prompt_report=prompt_report,
            )
        if not candidates:
            if self._is_push_request(request):
                return self._return_no_action(state=state, request=request, turn_id=turn_id)
            prompt_report = self._build_prompt_report(
                state=state,
                candidates=[],
                selected_intent=None,
                intent_index_intents=intents,
            )
            self._append(state, turn_id, "intent_unrecognized", {"message": request.message})
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.CLARIFICATION_REQUIRED,
                response="我还没有识别出具体业务场景，请换个说法或补充要办理的业务。",
                events=({"type": "intent_unrecognized"},),
                prompt_report=prompt_report,
            )

        if len(candidates) > 1:
            return self._plan_candidate_tasks(
                state=state,
                request=request,
                candidates=candidates,
                turn_id=turn_id,
                intent_index_intents=intents,
            )

        selected = candidates[0]
        intent = selected.intent
        prompt_report = self._build_prompt_report(
            state=state,
            candidates=candidates,
            selected_intent=intent,
            intent_index_intents=intents,
        )
        try:
            agent = self.registry.agent(intent.target_agent)
        except SpecRegistryError as exc:
            self._append(state, turn_id, "agent_missing", {"target_agent": intent.target_agent})
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.FAILED,
                response=str(exc),
                scene_id=intent.scene_id,
                target_agent=intent.target_agent,
                events=({"type": "agent_missing", "target_agent": intent.target_agent},),
                prompt_report=prompt_report,
            )

        routing_hints: dict[str, Any] = {}
        task_payload = self._build_agent_task_payload(
            request=request,
            state=state,
            intent=intent,
            routing_hints=routing_hints,
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
                scene_id=intent.scene_id,
                target_agent=agent.agent_id,
                routing_hints=routing_hints,
                events=({"type": "agent_dispatch_failed"},),
                prompt_report=prompt_report,
            )

        task_id = dispatch.agent_task_id
        task = self._make_task(
            task_id=task_id,
            intent=intent,
            target_agent=agent.agent_id,
            agent_task_id=dispatch.agent_task_id,
            status=TaskStatus.DISPATCHED,
            request=request,
            routing_hints=routing_hints,
            business_context=dict(task_payload.get("business_context") or {}),
        )
        state.tasks[task_id] = task
        state.bind_dispatch(
            task_id=task_id,
            intent_id=intent.intent_id,
            scene_id=intent.scene_id,
            target_agent=agent.agent_id,
            agent_task_id=dispatch.agent_task_id,
            routing_hints=routing_hints,
            summary=f"{intent.name}已派发给 {agent.agent_id}",
        )
        state.assistant_result_status = self._assistant_status_for_task(task.status)
        self._append(
            state,
            turn_id,
            "agent_dispatched",
            {
                "scene_id": intent.scene_id,
                "intent_id": intent.intent_id,
                "target_agent": agent.agent_id,
                "agent_task_id": dispatch.agent_task_id,
                "task_id": task_id,
                "routing_hints": routing_hints,
                "task_payload": self._task_payload_preview(task_payload),
            },
        )
        base_events = (
            {
                "type": "intent_selected",
                "intent_id": intent.intent_id,
                "scene_id": intent.scene_id,
                "score": selected.score,
                "reasons": list(selected.reasons),
            },
            {
                "type": "agent_dispatched",
                "target_agent": agent.agent_id,
                "agent_task_id": dispatch.agent_task_id,
                "task_id": task_id,
                "scene_id": intent.scene_id,
                "task_payload": self._task_payload_preview(task_payload),
            },
        )
        if self.skill_executor is not None:
            return self._run_skill_react_turn(
                state=state,
                request=request,
                task=task,
                intent=intent,
                turn_id=turn_id,
                status=RouterTurnStatus.DISPATCHED,
                base_events=base_events,
                prompt_report=prompt_report,
            )
        self.session_store.save(state)
        return RouterV4Output(
            session_id=request.session_id,
            status=RouterTurnStatus.DISPATCHED,
            response="task_dispatched",
            scene_id=intent.scene_id,
            target_agent=agent.agent_id,
            agent_task_id=dispatch.agent_task_id,
            task_id=task_id,
            routing_hints=routing_hints,
            tasks=(task.to_dict(),),
            assistant_result_status=state.assistant_result_status,
            events=base_events,
            prompt_report=prompt_report,
        )

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
        task_id = state.active_task_ids[0] if state.active_task_ids else state.agent_task_id
        task = state.tasks.get(task_id)
        if task is not None:
            state.assistant_result_status = "waiting_agent"
        self.session_store.save(state)
        self._append(
            state,
            turn_id,
            "agent_message_forwarded",
            {"target_agent": state.target_agent, "agent_task_id": state.agent_task_id},
        )
        prompt_report = self._build_prompt_report(state=state, candidates=[], selected_intent=None)
        return RouterV4Output(
            session_id=request.session_id,
            status=RouterTurnStatus.FORWARDED,
            response="message_forwarded_to_active_agent",
            scene_id=state.active_scene_id,
            target_agent=state.target_agent,
            agent_task_id=state.agent_task_id,
            task_id=task_id,
            routing_hints=dict(state.routing_hints),
            tasks=(task.to_dict(),) if task is not None else (),
            assistant_result_status=state.assistant_result_status,
            events=(
                {
                    "type": "agent_message_forwarded",
                    "target_agent": state.target_agent,
                    "agent_task_id": state.agent_task_id,
                },
            ),
            prompt_report=prompt_report,
        )

    def _continue_active_skill(
        self,
        *,
        state: RoutingSessionState,
        request: RouterV4Input,
        turn_id: str,
    ) -> RouterV4Output:
        task_id = state.active_task_ids[0] if state.active_task_ids else state.agent_task_id
        task = state.tasks.get(str(task_id or ""))
        if task is None:
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.FAILED,
                response="active_task_not_found",
                events=({"type": "task.not_found", "task_id": task_id},),
            )
        try:
            intent = self.registry.intent(task.intent_id)
        except SpecRegistryError as exc:
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.FAILED,
                response=str(exc),
                task_id=task.task_id,
                events=({"type": "intent_missing", "intent_id": task.intent_id},),
            )
        self._append(
            state,
            turn_id,
            "skill_message_forwarded",
            {"target_agent": state.target_agent, "task_id": task.task_id},
        )
        prompt_report = self._build_prompt_report(state=state, candidates=[], selected_intent=intent)
        return self._run_skill_react_turn(
            state=state,
            request=request,
            task=task,
            intent=intent,
            turn_id=turn_id,
            status=RouterTurnStatus.FORWARDED,
            base_events=(
                {
                    "type": "skill_message_forwarded",
                    "target_agent": state.target_agent,
                    "task_id": task.task_id,
                },
            ),
            prompt_report=prompt_report,
        )

    def _run_skill_react_turn(
        self,
        *,
        state: RoutingSessionState,
        request: RouterV4Input,
        task: RouterTaskState,
        intent: IntentSpec,
        turn_id: str,
        status: RouterTurnStatus,
        base_events: tuple[dict[str, Any], ...],
        prompt_report: dict[str, Any],
    ) -> RouterV4Output:
        if self.skill_executor is None:
            raise RuntimeError("skill_executor is not configured")
        try:
            skill_markdown, skill_path, skill_metadata = self._load_skill_markdown(intent)
            decision = self.skill_executor.decide(
                user_message=request.message,
                skill_markdown=skill_markdown,
                task_payload=self._skill_task_payload(task=task, intent=intent, current_message=request.message),
                task_memory=dict(task.skill_memory),
            )
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.abnormal_agent_output = {"error": str(exc)}
            state.dispatch_status = task.status.value
            state.assistant_result_status = "agent_output_abnormal"
            self._append(state, turn_id, "skill_react_failed", {"task_id": task.task_id, "error": str(exc)})
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.FAILED,
                response=f"Skill ReAct 执行失败：{exc}",
                scene_id=task.scene_id,
                target_agent=task.target_agent,
                agent_task_id=task.agent_task_id,
                task_id=task.task_id,
                tasks=(task.to_dict(),),
                assistant_result_status=state.assistant_result_status,
                events=(*base_events, {"type": "skill_react_failed", "task_id": task.task_id}),
                prompt_report=prompt_report,
            )

        skill_loaded_event = {
            "type": "skill_loaded",
            "task_id": task.task_id,
            "skill_id": intent.skill.get("skill_id"),
            "path": skill_path,
            "chars": len(skill_markdown),
            "metadata": dict(skill_metadata),
        }
        decision_event = {
            "type": "skill_react_decision",
            "task_id": task.task_id,
            "decision": decision.to_dict(),
        }
        self._append(state, turn_id, "skill_loaded", skill_loaded_event)
        self._append(state, turn_id, "skill_react_decision", decision_event)

        if not decision.task_supported or decision.action == "handover":
            return self._handle_handover_output(
                state=state,
                task=task,
                agent_payload={"ishandover": True, "output": {"data": []}, "reason": decision.reason},
            )

        self._apply_skill_slots(task=task, slots_patch=decision.slots_patch)
        action = self._normalized_skill_action(
            task=task,
            decision=decision,
            required_slots=_required_slots_from_skill_metadata(skill_metadata),
        )
        if action == "cancel":
            task.status = TaskStatus.CANCELLED
            task.agent_output = {"data": [], "reason": decision.reason or "user_cancelled"}
            state.agent_outputs[task.task_id] = task.agent_output
            state.dispatch_status = task.status.value
            state.assistant_result_status = self._assistant_status_for_task(task.status)
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.TASK_UPDATED,
                response=decision.assistant_message or "已取消本次任务。",
                scene_id=task.scene_id,
                target_agent=task.target_agent,
                agent_task_id=task.agent_task_id,
                task_id=task.task_id,
                tasks=(task.to_dict(),),
                agent_output=task.agent_output,
                assistant_result_status=state.assistant_result_status,
                events=(*base_events, skill_loaded_event, decision_event, {"type": "task.cancelled", "task_id": task.task_id}),
                prompt_report=prompt_report,
            )

        if action == "submit":
            task.status = TaskStatus.COMPLETED
            task.skill_memory["skill_step"] = "completed"
            task.agent_output = self._execute_skill_submit(task=task, decision=decision)
            state.agent_outputs[task.task_id] = task.agent_output
            self._remember_business_output(state=state, task=task)
            state.dispatch_status = task.status.value
            state.assistant_result_status = self._assistant_status_for_task(task.status)
            self._refresh_graph_statuses(state)
            self.session_store.save(state)
            return RouterV4Output(
                session_id=request.session_id,
                status=RouterTurnStatus.TASK_UPDATED,
                response=decision.assistant_message or "任务已完成。",
                scene_id=task.scene_id,
                target_agent=task.target_agent,
                agent_task_id=task.agent_task_id,
                task_id=task.task_id,
                tasks=(task.to_dict(),),
                agent_output=task.agent_output,
                assistant_result_status=state.assistant_result_status,
                events=(*base_events, skill_loaded_event, decision_event, {"type": "task.completed", "task_id": task.task_id}),
                prompt_report=prompt_report,
            )

        task.status = TaskStatus.RUNNING
        task.skill_memory["skill_step"] = "waiting_confirmation" if action == "ask_confirmation" else "collecting_fields"
        state.dispatch_status = task.status.value
        state.assistant_result_status = self._assistant_status_for_task(task.status)
        self.session_store.save(state)
        return RouterV4Output(
            session_id=request.session_id,
            status=status,
            response=decision.assistant_message or "请继续补充信息。",
            scene_id=task.scene_id,
            target_agent=task.target_agent,
            agent_task_id=task.agent_task_id,
            task_id=task.task_id,
            tasks=(task.to_dict(),),
            assistant_result_status=state.assistant_result_status,
            events=(*base_events, skill_loaded_event, decision_event, {"type": "task.running", "task_id": task.task_id}),
            prompt_report=prompt_report,
        )

    def _build_agent_task_payload(
        self,
        *,
        request: RouterV4Input,
        state: RoutingSessionState,
        intent: IntentSpec,
        routing_hints: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "router_session_id": request.session_id,
            "intent_id": intent.intent_id,
            "scene_id": intent.scene_id,
            "task_type": intent.dispatch_contract.task_type,
            "raw_message": request.message,
            "source": request.source,
            "push_context": dict(request.push_context),
            "routing_hints": dict(routing_hints),
            "skill_ref": dict(intent.skill),
            "business_context": self._business_context_for_task(state=state, intent=intent),
            "context_refs": {
                "user_profile_present": bool(request.user_profile),
                "page_context_present": bool(request.page_context),
                "business_memory_present": bool(state.business_memory),
            },
            "intent_catalog_hash": intent.spec_hash,
        }
        for field in intent.dispatch_contract.handoff_fields:
            if field == "raw_message":
                payload[field] = request.message
            elif field == "user_profile_ref":
                payload[field] = "request.user_profile"
            elif field == "page_context_ref":
                payload[field] = "request.page_context"
        if state.agent_task_id:
            payload["previous_agent_task_id"] = state.agent_task_id
        return payload

    def _skill_task_payload(self, *, task: RouterTaskState, intent: IntentSpec, current_message: str) -> dict[str, Any]:
        payload = task.to_dict()
        payload.update(
            {
                "router_session_id": task.task_id.split(":", 1)[0] if ":" in task.task_id else "",
                "current_message": current_message,
                "skill_ref": dict(intent.skill),
                "business_context": dict(task.business_context),
            }
        )
        return payload

    def _load_skill_markdown(self, intent: IntentSpec) -> tuple[str, str, dict[str, Any]]:
        skill_path = str(intent.skill.get("path") or "")
        if not skill_path:
            raise RuntimeError(f"intent {intent.intent_id} does not declare skill.path")
        source_path = Path(intent.source_path)
        base_dir = source_path.parent if intent.source_path else Path.cwd()
        path = (base_dir / skill_path).resolve()
        content = path.read_text(encoding="utf-8")
        return content, str(path), _skill_markdown_metadata(content)

    def _apply_skill_slots(self, *, task: RouterTaskState, slots_patch: dict[str, Any]) -> None:
        if not isinstance(task.skill_memory, dict):
            task.skill_memory = {}
        slots = dict(task.skill_memory.get("slots") or {})
        for key, value in slots_patch.items():
            normalized = _optional_text(value)
            if normalized is None:
                continue
            slots[str(key)] = normalized
            task.skill_memory[str(key)] = normalized
        task.skill_memory["slots"] = slots

    def _normalized_skill_action(
        self,
        *,
        task: RouterTaskState,
        decision: SkillDecision,
        required_slots: tuple[str, ...],
    ) -> str:
        action = decision.action.strip()
        if action == "tool_call":
            action = "submit"
        required_complete = _required_slots_complete(task=task, required_slots=required_slots)
        if action == "ask_missing" and required_complete is True:
            return "ask_confirmation"
        if action in {"ask_confirmation", "submit"} and required_complete is False:
            return "ask_missing"
        if action == "ask_missing" and decision.required_slots_complete is True:
            return "ask_confirmation"
        if action in {"ask_confirmation", "submit"} and decision.required_slots_complete is False:
            return "ask_missing"
        if (
            action == "ask_confirmation"
            and decision.confirmation_observed is True
            and task.skill_memory.get("skill_step") == "waiting_confirmation"
        ):
            return "submit"
        if action == "submit" and decision.confirmation_observed is False:
            return "ask_confirmation"
        if action == "submit" and task.skill_memory.get("skill_step") != "waiting_confirmation":
            return "ask_confirmation"
        if action not in {"ask_missing", "ask_confirmation", "submit", "cancel", "handover"}:
            return "ask_missing"
        return action

    def _execute_skill_submit(self, *, task: RouterTaskState, decision: SkillDecision) -> dict[str, Any]:
        tool_call = dict(decision.tool_call)
        if not tool_call:
            tool_call = {
                "name": f"{task.scene_id}.submit",
                "arguments": dict(task.skill_memory.get("slots") or {}),
            }
        result = self.tool_executor.execute(
            tool_call,
            task_context={
                "task_id": task.task_id,
                "intent_id": task.intent_id,
                "scene_id": task.scene_id,
                "target_agent": task.target_agent,
                "skill_memory": dict(task.skill_memory),
                "business_context": dict(task.business_context),
            },
        )
        output = result.to_agent_output(agent_id=task.target_agent, skill_id=task.intent_id)
        output["decision"] = decision.to_dict()
        return output

    def _task_payload_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "router_session_id": payload.get("router_session_id"),
            "intent_id": payload.get("intent_id"),
            "scene_id": payload.get("scene_id"),
            "task_type": payload.get("task_type"),
            "raw_message": payload.get("raw_message"),
            "routing_hints": dict(payload.get("routing_hints") or {}),
            "skill_ref": dict(payload.get("skill_ref") or {}),
            "business_context": dict(payload.get("business_context") or {}),
            "context_refs": dict(payload.get("context_refs") or {}),
            "intent_catalog_hash": payload.get("intent_catalog_hash"),
        }

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
                "routing_hints": dict(state.routing_hints),
                "turn_count": state.turn_count,
                "summary": state.summary,
                "active_graph_id": state.active_graph_id,
                "active_task_ids": list(state.active_task_ids),
                "source": state.source,
                "push_context": dict(state.push_context),
                "raw_messages": list(state.raw_messages),
                "selected_intent_ids": list(state.selected_intent_ids),
                "selected_scene_ids": list(state.selected_scene_ids),
                "target_agents": list(state.target_agents),
                "agent_task_ids": list(state.agent_task_ids),
                "handover_records": list(state.handover_records),
                "agent_outputs": dict(state.agent_outputs),
                "business_memory": dict(state.business_memory),
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
                tasks=(task.to_dict(),),
                agent_output=dict(agent_payload),
                assistant_result_status=state.assistant_result_status,
                events=({"type": "task.agent_output_abnormal", "task_id": task.task_id},),
            )

        status = str(agent_payload.get("status") or TaskStatus.COMPLETED.value)
        task.status = self._task_status_from_agent(status)
        output = agent_payload.get("output")
        task.agent_output = dict(output) if isinstance(output, dict) else {"raw": output}
        state.agent_outputs[task.task_id] = task.agent_output
        self._remember_business_output(state=state, task=task)
        state.assistant_result_status = self._assistant_status_for_task(task.status)
        self._refresh_graph_statuses(state)
        if state.agent_task_id == task.agent_task_id:
            state.dispatch_status = task.status.value
        self._append(
            state,
            self._new_turn_id(),
            "task.agent_output_recorded",
            {
                "task_id": task.task_id,
                "status": task.status.value,
                "agent_output": task.agent_output,
                "assistant_result_status": state.assistant_result_status,
            },
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
            tasks=(task.to_dict(),),
            agent_output=task.agent_output,
            action_required=_optional_action_required(task.agent_output),
            assistant_result_status=state.assistant_result_status,
            events=(
                {
                    "type": f"task.{task.status.value}",
                    "task_id": task.task_id,
                    "assistant_result_status": state.assistant_result_status,
                },
            ),
        )

    def _business_context_for_task(self, *, state: RoutingSessionState, intent: IntentSpec) -> dict[str, Any]:
        if not state.business_memory:
            return {}
        recent_items = [
            dict(item)
            for item in state.business_memory.get("recent_completed_tasks", [])
            if isinstance(item, dict)
        ][-5:]
        context: dict[str, Any] = {}
        if recent_items:
            context["recent_completed_tasks"] = recent_items
        last_by_scene = state.business_memory.get("last_completed_by_scene")
        if isinstance(last_by_scene, dict):
            context["last_completed_by_scene"] = {
                str(scene_id): dict(record)
                for scene_id, record in last_by_scene.items()
                if isinstance(record, dict)
            }
            same_scene = last_by_scene.get(intent.scene_id)
            if isinstance(same_scene, dict):
                context["last_completed_for_same_scene"] = dict(same_scene)
        return context

    def _remember_business_output(self, *, state: RoutingSessionState, task: RouterTaskState) -> None:
        if task.status != TaskStatus.COMPLETED or not isinstance(task.agent_output, dict):
            return
        data = task.agent_output.get("data")
        if not isinstance(data, list):
            return
        records: list[dict[str, Any]] = [
            dict(item)
            for item in state.business_memory.get("recent_completed_tasks", [])
            if isinstance(item, dict)
        ]
        for item in data:
            if not isinstance(item, dict):
                continue
            compact_data = _compact_business_data(item)
            record = {
                "task_id": task.task_id,
                "intent_id": task.intent_id,
                "scene_id": task.scene_id,
                "target_agent": task.target_agent,
                "type": item.get("type"),
                "status": item.get("status"),
                "data": compact_data,
            }
            records.append(record)
            last_by_scene = dict(state.business_memory.get("last_completed_by_scene") or {})
            last_by_scene[task.scene_id] = record
            state.business_memory["last_completed_by_scene"] = last_by_scene
        if records:
            state.business_memory["recent_completed_tasks"] = records[-10:]

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
                tasks=(task.to_dict(),),
                agent_output=dict(agent_payload),
                assistant_result_status=state.assistant_result_status,
                events=({"type": "task.handover_exhausted", "task_id": task.task_id},),
            )

        task.status = TaskStatus.HANDOVER_REQUESTED
        task.handover_used = True
        state.assistant_result_status = "handover_requested"
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
            "routing_hints": dict(task.routing_hints),
            "push_context": dict(task.push_context),
            "intent_id": "fallback",
            "scene_id": "fallback",
            "task_type": "fallback",
            "intent_catalog_hash": task.intent_catalog_hash,
        }
        try:
            fallback_agent = self.registry.agent(self.settings.fallback_agent_id)
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
            intent_id="fallback",
            scene_id="fallback",
            target_agent=fallback_agent.agent_id,
            agent_task_id=dispatch.agent_task_id,
            status=TaskStatus.FALLBACK_DISPATCHED,
            raw_message=task.raw_message,
            routing_hints=dict(task.routing_hints),
            intent_catalog_hash=task.intent_catalog_hash,
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
        state.assistant_result_status = "waiting_fallback_agent"
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
            assistant_result_status=state.assistant_result_status,
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
        state.summary = "LLM/spec 未选择助手推送意图，Router 不派发任务"
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
            prompt_report=self._build_prompt_report(state=state, candidates=[], selected_intent=None),
        )

    def _plan_candidate_tasks(
        self,
        *,
        state: RoutingSessionState,
        request: RouterV4Input,
        candidates: list[IntentCandidate],
        turn_id: str,
        intent_index_intents: list[Any] | None = None,
    ) -> RouterV4Output:
        graph_id = "graph_" + uuid.uuid4().hex[:12]
        tasks: list[RouterTaskState] = []
        events: list[dict[str, Any]] = [{"type": "plan.created", "graph_id": graph_id}]
        for candidate in candidates:
            intent = candidate.intent
            try:
                agent = self.registry.agent(intent.target_agent)
            except SpecRegistryError as exc:
                events.append({"type": "task.plan_failed", "intent_id": intent.intent_id, "error": str(exc)})
                continue
            routing_hints: dict[str, Any] = {}
            payload = self._build_agent_task_payload(
                request=request,
                state=state,
                intent=intent,
                routing_hints=routing_hints,
            )
            try:
                dispatch = self.agent_client.dispatch(agent=agent, task_payload=payload)
            except AgentDispatchError as exc:
                events.append({"type": "task.dispatch_failed", "intent_id": intent.intent_id, "error": str(exc)})
                continue
            task = self._make_task(
                task_id=dispatch.agent_task_id,
                intent=intent,
                target_agent=agent.agent_id,
                agent_task_id=dispatch.agent_task_id,
                status=TaskStatus.DISPATCHED,
                request=request,
                routing_hints=routing_hints,
                business_context=dict(payload.get("business_context") or {}),
            )
            state.tasks[task.task_id] = task
            tasks.append(task)
            state.selected_intent_ids = self._append_unique(state.selected_intent_ids, intent.intent_id)
            state.selected_scene_ids = self._append_unique(state.selected_scene_ids, intent.scene_id)
            state.target_agents = self._append_unique(state.target_agents, agent.agent_id)
            state.agent_task_ids = self._append_unique(state.agent_task_ids, dispatch.agent_task_id)
            events.append(
                {
                    "type": "task.dispatched",
                    "task_id": task.task_id,
                    "intent_id": intent.intent_id,
                    "scene_id": intent.scene_id,
                    "target_agent": agent.agent_id,
                    "score": candidate.score,
                    "reasons": list(candidate.reasons),
                    "task_payload": self._task_payload_preview(payload),
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
        state.assistant_result_status = "waiting_agent"
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
            assistant_result_status=state.assistant_result_status,
            events=tuple(events),
            prompt_report=self._build_prompt_report(
                state=state,
                candidates=candidates,
                selected_intent=None,
                intent_index_intents=intent_index_intents,
            ),
        )

    def _candidate_intents(self, push_intent_ids: list[str]) -> list[Any]:
        if not push_intent_ids:
            return self.registry.intent_index()
        intents: list[Any] = []
        for intent_id in push_intent_ids:
            try:
                intents.append(self.registry.intent(intent_id))
            except SpecRegistryError:
                continue
        return intents or self.registry.intent_index()

    def _push_intent_ids(self, request: RouterV4Input) -> list[str]:
        raw_items = request.push_context.get("intents") if isinstance(request.push_context, dict) else None
        if not isinstance(raw_items, list):
            return []
        ranked: list[tuple[int, str]] = []
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            intent_id = item.get("intent_id") or item.get("intent_code") or item.get("scene_id")
            if not isinstance(intent_id, str) or not intent_id.strip():
                continue
            rank = item.get("rank")
            ranked.append((rank if isinstance(rank, int) else index + 1, intent_id.strip()))
        return [intent_id for _, intent_id in sorted(ranked, key=lambda value: value[0])]

    def _is_push_request(self, request: RouterV4Input) -> bool:
        return request.source == "assistant_push" or bool(request.push_context)

    def _make_task(
        self,
        *,
        task_id: str,
        intent: IntentSpec,
        target_agent: str,
        agent_task_id: str,
        status: TaskStatus,
        request: RouterV4Input,
        routing_hints: dict[str, Any],
        business_context: dict[str, Any] | None = None,
    ) -> RouterTaskState:
        return RouterTaskState(
            task_id=task_id,
            intent_id=intent.intent_id,
            scene_id=intent.scene_id,
            target_agent=target_agent,
            agent_task_id=agent_task_id,
            status=status,
            raw_message=request.message,
            routing_hints=dict(routing_hints),
            intent_catalog_hash=intent.spec_hash,
            stream_url=self._stream_url(task_id),
            resume_token=self._resume_token(task_id),
            source=request.source,
            push_context=dict(request.push_context),
            business_context=dict(business_context or {}),
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
        if "isHandover" in payload:
            return data == []
        if "ishandover" not in payload:
            return False
        return (payload.get("ishandover") is True) != (data == [])

    def _task_status_from_agent(self, status: str) -> TaskStatus:
        try:
            return TaskStatus(status)
        except ValueError:
            return TaskStatus.COMPLETED

    def _assistant_status_for_task(self, status: TaskStatus) -> str:
        if status == TaskStatus.COMPLETED:
            return "ready_for_assistant"
        if status == TaskStatus.RUNNING:
            return "waiting_user"
        if status in {TaskStatus.DISPATCHED, TaskStatus.FALLBACK_DISPATCHED}:
            return "waiting_agent"
        if status == TaskStatus.HANDOVER_REQUESTED:
            return "handover_requested"
        if status == TaskStatus.HANDOVER_EXHAUSTED:
            return "handover_exhausted"
        return "agent_output_abnormal"

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

    def _build_prompt_report(
        self,
        *,
        state: RoutingSessionState,
        candidates: list[IntentCandidate],
        selected_intent: IntentSpec | None,
        intent_index_intents: list[Any] | None = None,
    ) -> dict[str, Any]:
        return self.context_builder.build_report(
            state=state,
            candidates=candidates,
            selected_intent=selected_intent,
            transcripts=self.transcript_store.list_for_session(state.session_id),
            intent_index_intents=intent_index_intents,
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


def _optional_action_required(output: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(output, dict):
        return None
    action_required = output.get("action_required")
    if isinstance(action_required, dict):
        return dict(action_required)
    return None


def _skill_markdown_metadata(content: str) -> dict[str, Any]:
    text = content.lstrip()
    if not text.startswith("+++\n"):
        return {}
    end = text.find("\n+++", 4)
    if end == -1:
        return {}
    raw = text[4:end].strip()
    if not raw:
        return {}
    try:
        payload = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return {}
    return dict(payload)


def _required_slots_from_skill_metadata(metadata: dict[str, Any]) -> tuple[str, ...]:
    raw = metadata.get("required_slots")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _required_slots_complete(*, task: RouterTaskState, required_slots: tuple[str, ...]) -> bool | None:
    if not required_slots:
        return None
    slots = task.skill_memory.get("slots")
    slot_values = dict(slots) if isinstance(slots, dict) else {}
    for slot in required_slots:
        if _optional_text(task.skill_memory.get(slot) or slot_values.get(slot)) is None:
            return False
    return True


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _compact_business_data(item: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = (
        "type",
        "status",
        "recipient",
        "amount",
        "currency",
        "audit_id",
        "account_scope",
        "product_id",
        "product_name",
    )
    return {key: item.get(key) for key in allowed_keys if key in item}
