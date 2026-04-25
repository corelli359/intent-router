#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC_ROOT = REPO_ROOT / "docs" / "examples" / "claw_router_demo" / "specs"


@dataclass(slots=True)
class SseEvent:
    event: str
    data: dict[str, Any]

    def render(self) -> str:
        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n"


@dataclass(slots=True)
class MarkdownDocument:
    path: Path
    metadata: dict[str, Any]
    body: str


@dataclass(slots=True)
class SkillIndexEntry:
    skill_code: str
    version: str
    name: str
    status: str
    domain: str
    risk_level: str
    executor: str
    keywords: list[str]
    path: Path


@dataclass(slots=True)
class SkillSpec:
    index: SkillIndexEntry
    candidate_card: str
    required_slots: list[str]
    slot_extractors: dict[str, dict[str, Any]]
    confirmation: dict[str, Any]
    presentation: dict[str, Any]


@dataclass(slots=True)
class Decision:
    action: str
    reason: str
    skill_code: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)
    message: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "skill_code": self.skill_code,
            "slots": self.slots,
            "missing_slots": self.missing_slots,
            "message": self.message,
        }


@dataclass(slots=True)
class SessionState:
    session_id: str
    spec_bundle_version: str | None = None
    waiting_skill_code: str | None = None
    slots_by_skill: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_confirmation: dict[str, Any] | None = None
    turn_count: int = 0


class MarkdownSpecLoader:
    def __init__(self, spec_root: Path) -> None:
        self.spec_root = spec_root
        self._doc_cache: dict[Path, MarkdownDocument] = {}
        self._skill_cache: dict[str, SkillSpec] = {}

    def load_document(self, relative_path: str) -> MarkdownDocument:
        path = self.spec_root / relative_path
        if path not in self._doc_cache:
            raw = path.read_text(encoding="utf-8")
            metadata, body = parse_frontmatter(raw)
            self._doc_cache[path] = MarkdownDocument(path=path, metadata=metadata, body=body)
        return self._doc_cache[path]

    def load_bootstrap(self) -> dict[str, MarkdownDocument]:
        return {
            "bundle": self.load_document("bundle.md"),
            "runtime": self.load_document("runtime.md"),
            "actions": self.load_document("actions.md"),
        }

    def load_policy_machine_spec(self, relative_path: str) -> dict[str, Any]:
        doc = self.load_document(relative_path)
        return extract_machine_spec(doc.body)

    def load_catalog_index(self) -> list[SkillIndexEntry]:
        entries: list[SkillIndexEntry] = []
        for path in sorted((self.spec_root / "skills").glob("*/SKILL.md")):
            raw = path.read_text(encoding="utf-8")
            metadata, _body = parse_frontmatter(raw)
            skill_code = str(metadata["skill_code"])
            entries.append(
                SkillIndexEntry(
                    skill_code=skill_code,
                    version=str(metadata.get("version", "0.0.0")),
                    name=str(metadata.get("name", skill_code)),
                    status=str(metadata.get("status", "inactive")),
                    domain=str(metadata.get("domain", "")),
                    risk_level=str(metadata.get("risk_level", "low")),
                    executor=str(metadata.get("executor", "")),
                    keywords=[str(item) for item in metadata.get("keywords", [])],
                    path=path,
                )
            )
        return entries

    def load_candidate_card(self, entry: SkillIndexEntry) -> str:
        raw = entry.path.read_text(encoding="utf-8")
        _metadata, body = parse_frontmatter(raw)
        return extract_section(body, "Candidate Card").strip()

    def load_skill(self, skill_code: str) -> SkillSpec:
        if skill_code in self._skill_cache:
            return self._skill_cache[skill_code]

        entry = next(
            (candidate for candidate in self.load_catalog_index() if candidate.skill_code == skill_code),
            None,
        )
        if entry is None:
            raise ValueError(f"unknown skill: {skill_code}")

        raw = entry.path.read_text(encoding="utf-8")
        _metadata, body = parse_frontmatter(raw)
        machine = extract_machine_spec(body)
        spec = SkillSpec(
            index=entry,
            candidate_card=extract_section(body, "Candidate Card").strip(),
            required_slots=[str(item) for item in machine.get("required_slots", [])],
            slot_extractors=dict(machine.get("slot_extractors", {})),
            confirmation=dict(machine.get("confirmation", {})),
            presentation=dict(machine.get("presentation", {})),
        )
        self._skill_cache[skill_code] = spec
        return spec

    def load_executor(self, executor_ref: str) -> dict[str, Any]:
        machine = self.load_policy_machine_spec("executors/agent-http.md")
        executors = machine.get("executors", {})
        if executor_ref not in executors:
            raise ValueError(f"unknown executor: {executor_ref}")
        return dict(executors[executor_ref])


class PatternTools:
    """Infrastructure-only text matching used by demo extractors declared in specs."""

    NUMBER_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")
    TRAILING_NOISE = re.compile(r"[\s,，。.!！?？]+$")

    @classmethod
    def extract_slots(cls, message: str, skill: SkillSpec) -> dict[str, Any]:
        slots: dict[str, Any] = {}
        for slot_name, extractor in skill.slot_extractors.items():
            extractor_type = extractor.get("type")
            if extractor_type == "number":
                value = cls._extract_number(message, extractor)
            elif extractor_type == "after_terms":
                value = cls._extract_after_terms(message, extractor)
            else:
                value = None
            if value not in (None, ""):
                slots[slot_name] = value
        return slots

    @classmethod
    def _extract_number(cls, message: str, extractor: dict[str, Any]) -> int | float | None:
        matches = list(cls.NUMBER_PATTERN.finditer(message))
        if not matches:
            return None

        units = [str(unit) for unit in extractor.get("units", [])]
        preferred = None
        for match in matches:
            suffix = message[match.end() : match.end() + 3]
            if any(suffix.startswith(unit) for unit in units):
                preferred = match
                break
        selected = preferred or matches[0]
        raw = selected.group(1)
        value = float(raw) if "." in raw else int(raw)
        return value

    @classmethod
    def _extract_after_terms(cls, message: str, extractor: dict[str, Any]) -> str | None:
        terms = [str(term) for term in extractor.get("terms", [])]
        stop_terms = [str(term) for term in extractor.get("stop_terms", [])]
        max_chars = int(extractor.get("max_chars", 16))

        for term in terms:
            index = message.find(term)
            if index < 0:
                continue
            candidate = message[index + len(term) :]
            for stop in stop_terms:
                stop_index = candidate.find(stop)
                if stop_index >= 0:
                    candidate = candidate[:stop_index]
            candidate = candidate[:max_chars]
            candidate = cls.TRAILING_NOISE.sub("", candidate).strip()
            if candidate:
                return candidate
        return None


class MarkdownDecisionEngine:
    """Offline stand-in for an LLM that emits structured decisions."""

    def decide_for_new_turn(
        self,
        *,
        message: str,
        candidates: list[SkillIndexEntry],
        loaded_skills: dict[str, SkillSpec],
    ) -> Decision:
        if not candidates:
            return Decision(
                action="final",
                reason="no_candidate_skill",
                message="我还不能稳定处理这个请求，可以换个说法再试一次。",
            )

        selected = candidates[0]
        skill = loaded_skills[selected.skill_code]
        slots = PatternTools.extract_slots(message, skill)
        missing = missing_required_slots(skill, slots)
        if missing:
            return Decision(
                action="ask_user",
                reason="missing_required_slots",
                skill_code=skill.index.skill_code,
                slots=slots,
                missing_slots=missing,
                message=missing_slot_prompt(skill, missing),
            )
        return Decision(
            action="execute_skill",
            reason="all_required_slots_present",
            skill_code=skill.index.skill_code,
            slots=slots,
        )

    def decide_for_waiting_turn(self, *, message: str, skill: SkillSpec, existing_slots: dict[str, Any]) -> Decision:
        slots = {**existing_slots, **PatternTools.extract_slots(message, skill)}
        missing = missing_required_slots(skill, slots)
        if missing:
            return Decision(
                action="ask_user",
                reason="still_missing_required_slots",
                skill_code=skill.index.skill_code,
                slots=slots,
                missing_slots=missing,
                message=missing_slot_prompt(skill, missing),
            )
        return Decision(
            action="execute_skill",
            reason="waiting_turn_completed_slots",
            skill_code=skill.index.skill_code,
            slots=slots,
        )


class MockExecutorRegistry:
    def __init__(self, loader: MarkdownSpecLoader) -> None:
        self.loader = loader

    def execute(self, executor_ref: str, slots: dict[str, Any]) -> dict[str, Any]:
        executor = self.loader.load_executor(executor_ref)
        template = str(executor.get("response_template", "已完成。"))
        return {
            "executor": executor_ref,
            "type": executor.get("type", "mock"),
            "message": template.format(**slots),
        }


class ClawRouterHarness:
    def __init__(self, spec_root: Path = DEFAULT_SPEC_ROOT) -> None:
        self.loader = MarkdownSpecLoader(spec_root)
        self.decision_engine = MarkdownDecisionEngine()
        self.executors = MockExecutorRegistry(self.loader)
        self.sessions: dict[str, SessionState] = {}

    def handle_message(self, session_id: str, message: str) -> list[SseEvent]:
        session = self.sessions.setdefault(session_id, SessionState(session_id=session_id))
        session.turn_count += 1

        events: list[SseEvent] = [
            SseEvent("run_started", {"session_id": session_id, "turn": session.turn_count, "input": message})
        ]

        if session.spec_bundle_version is None:
            bootstrap = self.loader.load_bootstrap()
            bundle_version = str(bootstrap["bundle"].metadata["version"])
            session.spec_bundle_version = bundle_version
            events.append(
                SseEvent(
                    "spec_loaded",
                    {
                        "level": "L0",
                        "loaded": list(bootstrap.keys()),
                        "spec_bundle_version": bundle_version,
                    },
                )
            )
        else:
            events.append(
                SseEvent(
                    "spec_resumed",
                    {"level": "L0", "spec_bundle_version": session.spec_bundle_version},
                )
            )

        events.append(
            SseEvent(
                "session_loaded",
                {
                    "level": "L1",
                    "waiting_skill_code": session.waiting_skill_code,
                    "pending_confirmation": bool(session.pending_confirmation),
                },
            )
        )

        if session.pending_confirmation is not None:
            events.extend(self._handle_confirmation_turn(session, message))
            return events

        if session.waiting_skill_code:
            events.extend(self._handle_waiting_turn(session, message))
            return events

        events.extend(self._handle_new_turn(session, message))
        return events

    def _handle_new_turn(self, session: SessionState, message: str) -> list[SseEvent]:
        events: list[SseEvent] = []
        catalog = [entry for entry in self.loader.load_catalog_index() if entry.status == "active"]
        events.append(
            SseEvent(
                "catalog_index_loaded",
                {"level": "L2", "skill_count": len(catalog), "skills": [entry.skill_code for entry in catalog]},
            )
        )

        candidates = recall_candidates(message, catalog)
        events.append(
            SseEvent(
                "skill_shortlisted",
                {
                    "level": "L2",
                    "candidates": [
                        {"skill_code": entry.skill_code, "name": entry.name, "risk_level": entry.risk_level}
                        for entry in candidates
                    ],
                },
            )
        )

        loaded_skills: dict[str, SkillSpec] = {}
        for candidate in candidates[:3]:
            card = self.loader.load_candidate_card(candidate)
            skill = self.loader.load_skill(candidate.skill_code)
            loaded_skills[candidate.skill_code] = skill
            events.append(
                SseEvent(
                    "candidate_card_loaded",
                    {
                        "level": "L3",
                        "skill_code": candidate.skill_code,
                        "card": compact_text(card),
                    },
                )
            )

        decision = self.decision_engine.decide_for_new_turn(
            message=message,
            candidates=candidates,
            loaded_skills=loaded_skills,
        )
        events.append(SseEvent("decision_generated", {"by": "offline_markdown_decision_engine", **decision.to_payload()}))
        events.extend(self._apply_decision(session, decision))
        return events

    def _handle_waiting_turn(self, session: SessionState, message: str) -> list[SseEvent]:
        assert session.waiting_skill_code is not None
        skill = self.loader.load_skill(session.waiting_skill_code)
        existing_slots = session.slots_by_skill.get(skill.index.skill_code, {})
        events = [
            SseEvent(
                "deep_skill_spec_loaded",
                {
                    "level": "L4",
                    "skill_code": skill.index.skill_code,
                    "existing_slots": existing_slots,
                },
            )
        ]
        decision = self.decision_engine.decide_for_waiting_turn(
            message=message,
            skill=skill,
            existing_slots=existing_slots,
        )
        events.append(SseEvent("decision_generated", {"by": "offline_markdown_decision_engine", **decision.to_payload()}))
        events.extend(self._apply_decision(session, decision))
        return events

    def _handle_confirmation_turn(self, session: SessionState, message: str) -> list[SseEvent]:
        turn_policy = self.loader.load_policy_machine_spec("policies/turn-policy.md")
        confirmation = session.pending_confirmation or {}
        skill_code = str(confirmation["skill_code"])
        skill = self.loader.load_skill(skill_code)
        events = [
            SseEvent(
                "turn_policy_loaded",
                {"level": "L4", "policy": "turn-policy", "skill_code": skill_code},
            )
        ]
        if contains_any(message, [str(term) for term in turn_policy.get("cancel_terms", [])]):
            session.pending_confirmation = None
            session.waiting_skill_code = None
            session.slots_by_skill.pop(skill_code, None)
            events.append(SseEvent("cancelled", {"skill_code": skill_code, "message": "已取消当前事项。"}))
            return events

        if contains_any(message, [str(term) for term in turn_policy.get("confirm_terms", [])]):
            slots = dict(confirmation["slots"])
            session.pending_confirmation = None
            events.extend(self._execute_skill(session, skill, slots, confirmed=True))
            return events

        events.append(
            SseEvent(
                "waiting_confirmation",
                {
                    "skill_code": skill_code,
                    "message": confirmation.get("message", "请确认是否执行。"),
                },
            )
        )
        return events

    def _apply_decision(self, session: SessionState, decision: Decision) -> list[SseEvent]:
        events: list[SseEvent] = []
        validation = self._validate_decision(decision)
        events.append(SseEvent("decision_validated", validation))
        if not validation["valid"]:
            events.append(
                SseEvent(
                    "completed",
                    {"status": "rejected", "message": f"决策无效：{validation['reason']}"},
                )
            )
            return events

        if decision.action == "final":
            events.append(SseEvent("completed", {"status": "final", "message": decision.message}))
            return events

        if decision.action == "ask_user":
            assert decision.skill_code is not None
            session.waiting_skill_code = decision.skill_code
            session.slots_by_skill[decision.skill_code] = dict(decision.slots)
            events.append(
                SseEvent(
                    "waiting_user_input",
                    {
                        "skill_code": decision.skill_code,
                        "slots": decision.slots,
                        "missing_slots": decision.missing_slots,
                        "message": decision.message,
                    },
                )
            )
            return events

        if decision.action == "execute_skill":
            assert decision.skill_code is not None
            skill = self.loader.load_skill(decision.skill_code)
            events.extend(self._execute_skill(session, skill, decision.slots, confirmed=False))
            return events

        events.append(SseEvent("completed", {"status": "ignored", "message": "未处理的 action。"}))
        return events

    def _execute_skill(
        self,
        session: SessionState,
        skill: SkillSpec,
        slots: dict[str, Any],
        *,
        confirmed: bool,
    ) -> list[SseEvent]:
        events: list[SseEvent] = []
        guardrails = self.loader.load_policy_machine_spec("policies/guardrails.md")
        if skill.index.risk_level in guardrails.get("sensitive_skill_risk_levels", []) or skill.confirmation:
            events.append(
                SseEvent(
                    "guardrail_policy_loaded",
                    {
                        "level": "L4",
                        "policy": "guardrails",
                        "skill_code": skill.index.skill_code,
                        "risk_level": skill.index.risk_level,
                    },
                )
            )

        if self._requires_confirmation(skill, slots, guardrails) and not confirmed:
            knowledge = self.loader.load_policy_machine_spec("knowledge/payment-risk.md")
            notice = knowledge.get("chunks", {}).get("large_transfer_notice")
            events.append(
                SseEvent(
                    "knowledge_loaded",
                    {
                        "level": "L5",
                        "knowledge": "payment-risk",
                        "chunk": "large_transfer_notice",
                        "notice": notice,
                    },
                )
            )
            message = str(skill.confirmation.get("message_template", "请确认是否执行。")).format(**slots)
            session.pending_confirmation = {
                "skill_code": skill.index.skill_code,
                "slots": dict(slots),
                "message": message,
            }
            session.waiting_skill_code = None
            session.slots_by_skill[skill.index.skill_code] = dict(slots)
            events.append(
                SseEvent(
                    "confirmation_required",
                    {"skill_code": skill.index.skill_code, "slots": slots, "message": message},
                )
            )
            return events

        events.append(
            SseEvent(
                "executor_contract_loaded",
                {"level": "L6", "executor": skill.index.executor, "skill_code": skill.index.skill_code},
            )
        )
        result = self.executors.execute(skill.index.executor, slots)
        session.pending_confirmation = None
        session.waiting_skill_code = None
        session.slots_by_skill.pop(skill.index.skill_code, None)
        events.append(SseEvent("executing", {"skill_code": skill.index.skill_code, "executor": skill.index.executor}))
        events.append(SseEvent("executor_result", result))
        events.append(SseEvent("completed", {"status": "completed", "message": result["message"]}))
        return events

    def _validate_decision(self, decision: Decision) -> dict[str, Any]:
        action_spec = self.loader.load_policy_machine_spec("actions.md").get("actions", {})
        if decision.action not in action_spec:
            return {"valid": False, "reason": f"action_not_allowed:{decision.action}"}

        if decision.action in {"ask_user", "execute_skill"}:
            if not decision.skill_code:
                return {"valid": False, "reason": "missing_skill_code"}
            skill = self.loader.load_skill(decision.skill_code)
            if skill.index.status != "active":
                return {"valid": False, "reason": "skill_not_active"}
            if decision.action == "execute_skill":
                missing = missing_required_slots(skill, decision.slots)
                if missing:
                    return {"valid": False, "reason": "missing_required_slots", "missing_slots": missing}

        return {"valid": True, "reason": "ok"}

    def _requires_confirmation(
        self,
        skill: SkillSpec,
        slots: dict[str, Any],
        guardrails: dict[str, Any],
    ) -> bool:
        skill_threshold = skill.confirmation.get("amount_gt")
        global_threshold = guardrails.get("max_direct_transfer_amount")
        configured = [value for value in [skill_threshold, global_threshold] if value is not None]
        threshold = min(float(value) for value in configured) if configured else None
        amount = slots.get("amount")
        if threshold is not None and isinstance(amount, (int, float)):
            return amount > threshold
        return False


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        return {}, raw
    marker = "\n---\n"
    end = raw.find(marker, 4)
    if end < 0:
        return {}, raw
    frontmatter = raw[4:end]
    body = raw[end + len(marker) :]
    return parse_simple_yaml(frontmatter), body


def parse_simple_yaml(text: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        payload[key.strip()] = parse_scalar(raw_value.strip())
    return payload


def parse_scalar(raw: str) -> Any:
    if raw == "":
        return ""
    if raw in {"true", "false"}:
        return raw == "true"
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(item.strip()) for item in inner.split(",")]
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def extract_section(markdown: str, heading: str) -> str:
    target = f"## {heading}"
    lines = markdown.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == target:
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if line.startswith("## ") and line.strip() != target:
            end = index
            break
    return "\n".join(lines[start:end])


def extract_machine_spec(markdown: str) -> dict[str, Any]:
    section = extract_section(markdown, "Machine Spec")
    match = re.search(r"```json\s*(.*?)\s*```", section, re.DOTALL)
    if not match:
        return {}
    return json.loads(match.group(1))


def compact_text(text: str, limit: int = 220) -> str:
    compact = " ".join(part.strip() for part in text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def contains_any(message: str, terms: list[str]) -> bool:
    return any(term and term in message for term in terms)


def recall_candidates(message: str, catalog: list[SkillIndexEntry]) -> list[SkillIndexEntry]:
    scored: list[tuple[int, SkillIndexEntry]] = []
    for entry in catalog:
        score = sum(1 for keyword in entry.keywords if keyword and keyword in message)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1].skill_code))
    return [entry for _score, entry in scored]


def missing_required_slots(skill: SkillSpec, slots: dict[str, Any]) -> list[str]:
    return [slot for slot in skill.required_slots if slots.get(slot) in (None, "")]


def missing_slot_prompt(skill: SkillSpec, missing: list[str]) -> str:
    prompts = skill.presentation.get("missing_slot_prompts", {})
    return str(prompts.get(missing[0], f"请补充 {missing[0]}。"))


def run_messages(harness: ClawRouterHarness, session_id: str, messages: list[str]) -> int:
    for message in messages:
        print(f"> user: {message}")
        for event in harness.handle_message(session_id, message):
            print(event.render())
    return 0


def self_test() -> None:
    harness = ClawRouterHarness()
    session_id = "self-test"
    first = harness.handle_message(session_id, "帮我给王芳转账")
    assert any(event.event == "waiting_user_input" for event in first), "first turn should ask for amount"
    second = harness.handle_message(session_id, "300")
    assert any(
        event.event == "completed" and event.data.get("status") == "completed" for event in second
    ), "second turn should complete transfer"

    harness = ClawRouterHarness()
    first = harness.handle_message("self-test-confirm", "帮我给王芳转账2000元")
    assert any(event.event == "confirmation_required" for event in first), "large amount should require confirmation"
    second = harness.handle_message("self-test-confirm", "确认")
    assert any(
        event.event == "completed" and event.data.get("status") == "completed" for event in second
    ), "confirmation should execute"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Markdown-first Claw Router demo")
    parser.add_argument("--spec-root", type=Path, default=DEFAULT_SPEC_ROOT)
    parser.add_argument("--session-id", default=f"demo-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--message", action="append", help="Run one or more user messages")
    parser.add_argument("--demo", choices=["transfer", "confirmation", "balance"])
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        self_test()
        print("self-test passed")
        return 0

    harness = ClawRouterHarness(args.spec_root)

    if args.demo == "transfer":
        return run_messages(harness, args.session_id, ["帮我给王芳转账", "300"])
    if args.demo == "confirmation":
        return run_messages(harness, args.session_id, ["帮我给王芳转账2000元", "确认"])
    if args.demo == "balance":
        return run_messages(harness, args.session_id, ["查询一下账户余额"])
    if args.message:
        return run_messages(harness, args.session_id, args.message)
    if args.interactive:
        print(f"session: {args.session_id}")
        while True:
            try:
                message = input("> user: ").strip()
            except EOFError:
                break
            if message in {"exit", "quit"}:
                break
            if not message:
                continue
            for event in harness.handle_message(args.session_id, message):
                print(event.render())
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
