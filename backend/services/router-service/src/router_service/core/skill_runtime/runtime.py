from __future__ import annotations

from typing import Any

from router_service.core.skill_runtime.matcher import SkillMatcher
from router_service.core.skill_runtime.models import (
    SkillRuntimeInput,
    SkillRuntimeOutput,
    SkillSessionState,
    SkillSpec,
    SlotDefinition,
    StepDefinition,
    ToolCallLog,
)
from router_service.core.skill_runtime.prompt_builder import SkillPromptBuilder
from router_service.core.skill_runtime.skill_loader import SkillSpecLoader
from router_service.core.skill_runtime.slot_extractor import SlotExtractor
from router_service.core.skill_runtime.tools import ApiTool, CapabilityError


class SkillRuntimeController:
    """Markdown-first v4 Skill runtime.

    The controller keeps execution state in code and treats the LLM-facing
    context as a rebuildable view over that state.
    """

    def __init__(
        self,
        *,
        loader: SkillSpecLoader | None = None,
        matcher: SkillMatcher | None = None,
        extractor: SlotExtractor | None = None,
        api_tool: ApiTool | None = None,
        prompt_builder: SkillPromptBuilder | None = None,
    ) -> None:
        self.loader = loader or SkillSpecLoader()
        self.matcher = matcher or SkillMatcher()
        self.extractor = extractor or SlotExtractor()
        self.api_tool = api_tool or ApiTool()
        self.prompt_builder = prompt_builder or SkillPromptBuilder()
        self.sessions: dict[str, SkillSessionState] = {}

    @classmethod
    def from_spec_root(cls, spec_root: str | None) -> "SkillRuntimeController":
        return cls(loader=SkillSpecLoader(spec_root) if spec_root else SkillSpecLoader())

    def handle(self, request: SkillRuntimeInput) -> SkillRuntimeOutput:
        session = self.sessions.setdefault(
            request.session_id,
            SkillSessionState(session_id=request.session_id),
        )
        session.turn_count += 1
        tool_log: list[ToolCallLog] = []

        self._build_prompt_view(request=request, session=session)

        if session.pending_confirmation is not None:
            return self._handle_confirmation_turn(request=request, session=session, tool_log=tool_log)

        skill = self._resolve_skill_for_turn(request=request, session=session, tool_log=tool_log)
        if skill is None:
            return SkillRuntimeOutput(
                session_id=request.session_id,
                response="我还不能稳定处理这个请求，可以换个说法再试一次。",
                status="unrecognized",
                tool_calls_log=tuple(tool_log),
            )

        extracted = self.extractor.extract(request.message, skill)
        if session.awaiting_slot:
            slot = self._slot_by_name(skill, session.awaiting_slot)
            direct_value = self.extractor.extract_direct_reply(request.message, slot)
            if direct_value not in (None, ""):
                extracted[slot.name] = direct_value
        session.slots.update(extracted)

        missing = self._missing_required_slots(skill, session.slots)
        if missing:
            next_slot = missing[0]
            session.current_skill_id = skill.skill_id
            session.awaiting_slot = next_slot
            return SkillRuntimeOutput(
                session_id=request.session_id,
                response=self._slot_by_name(skill, next_slot).prompt,
                status="waiting_user_input",
                action_required={"type": "input", "slot": next_slot},
                skill=skill.skill_id,
                skill_step=self._first_step_id(skill, "collect_slots"),
                slots=dict(session.slots),
                missing_slots=tuple(missing),
                tool_calls_log=tuple(tool_log),
            )

        session.awaiting_slot = None
        return self._execute_steps(
            request=request,
            session=session,
            skill=skill,
            tool_log=tool_log,
            start_index=0,
        )

    def _resolve_skill_for_turn(
        self,
        *,
        request: SkillRuntimeInput,
        session: SkillSessionState,
        tool_log: list[ToolCallLog],
    ) -> SkillSpec | None:
        if session.current_skill_id:
            return self._read_skill(session.current_skill_id, tool_log)

        index = self.loader.load_skill_index()
        candidates = self.matcher.shortlist(request.message, index)
        tool_log.append(
            ToolCallLog(
                tool="skill_index",
                args={"message": request.message},
                result={"candidates": [item.skill_id for item in candidates]},
            )
        )
        if not candidates:
            return None
        skill = self._read_skill(candidates[0].skill_id, tool_log)
        self._read_references(skill, tool_log)
        session.current_skill_id = skill.skill_id
        return skill

    def _read_skill(self, skill_id: str, tool_log: list[ToolCallLog]) -> SkillSpec:
        skill = self.loader.load_skill(skill_id)
        tool_log.append(
            ToolCallLog(
                tool="skill_read",
                args={"name": skill_id},
                result={"status": skill.index.status, "version": skill.index.version},
            )
        )
        return skill

    def _read_references(self, skill: SkillSpec, tool_log: list[ToolCallLog]) -> None:
        for reference in skill.references:
            doc = self.loader.read_reference(reference)
            tool_log.append(
                ToolCallLog(
                    tool="skill_read",
                    args={"name": reference},
                    result={"path": doc.path},
                )
            )

    def _execute_steps(
        self,
        *,
        request: SkillRuntimeInput,
        session: SkillSessionState,
        skill: SkillSpec,
        tool_log: list[ToolCallLog],
        start_index: int,
    ) -> SkillRuntimeOutput:
        for step_index, step in enumerate(skill.steps[start_index:], start=start_index):
            if step.kind == "collect_slots":
                continue
            if step.kind == "api_call":
                try:
                    result = self._call_api_step(
                        request=request,
                        skill=skill,
                        step=step,
                        slots=session.slots,
                        tool_log=tool_log,
                    )
                except CapabilityError as exc:
                    completed_slots = dict(session.slots)
                    session.reset_current_skill()
                    return SkillRuntimeOutput(
                        session_id=request.session_id,
                        response=str(exc),
                        status="failed",
                        skill=skill.skill_id,
                        skill_step=step.step_id,
                        slots=completed_slots,
                        tool_calls_log=tuple(tool_log),
                    )
                if not result.get("ok", False):
                    response = self._exception_message(skill, step, result)
                    completed_slots = dict(session.slots)
                    session.reset_current_skill()
                    return SkillRuntimeOutput(
                        session_id=request.session_id,
                        response=response,
                        status="failed",
                        skill=skill.skill_id,
                        skill_step=step.step_id,
                        slots=completed_slots,
                        tool_calls_log=tuple(tool_log),
                    )
                continue
            if step.kind == "confirm":
                message = self._format_template(str(step.config.get("message_template") or "请确认是否执行。"), session.slots)
                session.pending_confirmation = {
                    "skill_id": skill.skill_id,
                    "slots": dict(session.slots),
                    "message": message,
                    "next_step_index": step_index + 1,
                    "skill_step": step.step_id,
                }
                return SkillRuntimeOutput(
                    session_id=request.session_id,
                    response=message,
                    status="waiting_confirmation",
                    action_required={"type": "confirm", "data": dict(session.slots)},
                    skill=skill.skill_id,
                    skill_step=step.step_id,
                    slots=dict(session.slots),
                    tool_calls_log=tuple(tool_log),
                )
            if step.kind == "final":
                last_result = tool_log[-1].result if tool_log and tool_log[-1].result else {}
                message = self._format_template(
                    str(step.config.get("message_template") or "已完成。"),
                    {**session.slots, **last_result},
                )
                completed_slots = dict(session.slots)
                session.summary = message
                session.reset_current_skill()
                return SkillRuntimeOutput(
                    session_id=request.session_id,
                    response=message,
                    status="completed",
                    skill=skill.skill_id,
                    skill_step=step.step_id,
                    slots=completed_slots,
                    tool_calls_log=tuple(tool_log),
                )

        completed_slots = dict(session.slots)
        session.reset_current_skill()
        return SkillRuntimeOutput(
            session_id=request.session_id,
            response="已完成。",
            status="completed",
            skill=skill.skill_id,
            slots=completed_slots,
            tool_calls_log=tuple(tool_log),
        )

    def _handle_confirmation_turn(
        self,
        *,
        request: SkillRuntimeInput,
        session: SkillSessionState,
        tool_log: list[ToolCallLog],
    ) -> SkillRuntimeOutput:
        policy = self.loader.load_agent_policy()
        confirmation_policy = dict(policy.get("confirmation", {}))
        confirm_terms = [str(item) for item in confirmation_policy.get("confirm_terms", ["确认"])]
        cancel_terms = [str(item) for item in confirmation_policy.get("cancel_terms", ["取消"])]
        pending = dict(session.pending_confirmation or {})
        skill_id = str(pending.get("skill_id") or "")

        if _contains_any(request.message, cancel_terms):
            session.reset_current_skill()
            return SkillRuntimeOutput(
                session_id=request.session_id,
                response="已取消当前操作。",
                status="cancelled",
                skill=skill_id or None,
                tool_calls_log=tuple(tool_log),
            )

        if not _contains_any(request.message, confirm_terms):
            return SkillRuntimeOutput(
                session_id=request.session_id,
                response=str(pending.get("message") or "请确认是否执行。"),
                status="waiting_confirmation",
                action_required={"type": "confirm", "data": dict(pending.get("slots") or {})},
                skill=skill_id or None,
                skill_step=int(pending.get("skill_step") or 0) or None,
                slots=dict(pending.get("slots") or {}),
                tool_calls_log=tuple(tool_log),
            )

        skill = self._read_skill(skill_id, tool_log)
        session.slots = dict(pending.get("slots") or {})
        session.pending_confirmation = None
        session.awaiting_slot = None
        return self._execute_steps(
            request=request,
            session=session,
            skill=skill,
            tool_log=tool_log,
            start_index=int(pending.get("next_step_index") or 0),
        )

    def _call_api_step(
        self,
        *,
        request: SkillRuntimeInput,
        skill: SkillSpec,
        step: StepDefinition,
        slots: dict[str, Any],
        tool_log: list[ToolCallLog],
    ) -> dict[str, Any]:
        capability = str(step.config.get("capability") or "")
        if capability not in skill.allowed_capabilities:
            raise CapabilityError(f"skill {skill.skill_id} cannot call capability {capability}")
        endpoint = request.business_apis.get(capability)
        body = self._step_body(step, slots)
        result = self.api_tool.call(
            capability=capability,
            endpoint=endpoint,
            body=body,
            user_profile=request.user_profile,
        )
        tool_log.append(
            ToolCallLog(
                tool="api_call",
                args={"capability": capability, "endpoint": endpoint, "body": body},
                result=result,
            )
        )
        return result

    def _step_body(self, step: StepDefinition, slots: dict[str, Any]) -> dict[str, Any]:
        body_slots = step.config.get("body_slots")
        if not isinstance(body_slots, list) or not body_slots:
            return dict(slots)
        return {str(name): slots.get(str(name)) for name in body_slots}

    def _build_prompt_view(self, *, request: SkillRuntimeInput, session: SkillSessionState) -> str:
        return self.prompt_builder.build(
            agent_rules=self.loader.load_agent_document().body,
            user_profile=request.user_profile,
            page_context=request.page_context,
            session=session,
            skill_index=self.loader.load_skill_index(),
        )

    def _missing_required_slots(self, skill: SkillSpec, slots: dict[str, Any]) -> list[str]:
        return [slot.name for slot in skill.slots if slot.required and slots.get(slot.name) in (None, "")]

    def _slot_by_name(self, skill: SkillSpec, name: str) -> SlotDefinition:
        for slot in skill.slots:
            if slot.name == name:
                return slot
        raise KeyError(name)

    def _first_step_id(self, skill: SkillSpec, kind: str) -> int | None:
        for step in skill.steps:
            if step.kind == kind:
                return step.step_id
        return None

    def _exception_message(self, skill: SkillSpec, step: StepDefinition, result: dict[str, Any]) -> str:
        capability = str(step.config.get("capability") or "")
        error_code = str(result.get("error_code") or "api_error")
        for key in (f"{capability}.{error_code}", error_code, capability):
            if key in skill.exception_messages:
                return self._format_template(skill.exception_messages[key], result)
        return str(result.get("message") or "业务接口调用失败。")

    def _format_template(self, template: str, values: dict[str, Any]) -> str:
        try:
            return template.format(**values)
        except KeyError:
            return template


def _contains_any(message: str, terms: list[str]) -> bool:
    return any(term and term in message for term in terms)
