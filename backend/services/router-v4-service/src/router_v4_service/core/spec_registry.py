from __future__ import annotations

import hashlib
from pathlib import Path
import tomllib
from typing import Any

from router_v4_service.core.models import (
    AgentDefinition,
    DispatchContract,
    IntentSpec,
)


DEFAULT_SPEC_ROOT = Path(__file__).resolve().parents[1] / "default_specs"


class SpecRegistryError(RuntimeError):
    """Raised when routing specs or agent registry entries are invalid."""


class SpecRegistry:
    """Loads the single intent catalog and execution-agent registry entries."""

    def __init__(self, spec_root: str | Path | None = None) -> None:
        self.spec_root = Path(spec_root).expanduser().resolve() if spec_root else DEFAULT_SPEC_ROOT.resolve()
        self._intents: dict[str, IntentSpec] | None = None
        self._agents: dict[str, AgentDefinition] | None = None

    def intent_index(self) -> list[IntentSpec]:
        return sorted(self._load_intents().values(), key=lambda item: item.intent_id)

    def intent(self, intent_id: str) -> IntentSpec:
        intents = self._load_intents()
        try:
            return intents[intent_id]
        except KeyError as exc:
            raise SpecRegistryError(f"unknown intent: {intent_id}") from exc

    def agent(self, agent_id: str) -> AgentDefinition:
        agents = self._load_agents()
        try:
            return agents[agent_id]
        except KeyError as exc:
            raise SpecRegistryError(f"unknown agent: {agent_id}") from exc

    def agent_index(self) -> list[AgentDefinition]:
        return sorted(self._load_agents().values(), key=lambda item: item.agent_id)

    def _load_intents(self) -> dict[str, IntentSpec]:
        if self._intents is not None:
            return self._intents
        path = self.spec_root / "intent.md"
        raw = path.read_text(encoding="utf-8")
        frontmatter, markdown = _load_markdown_spec(raw, path)
        raw_intents = frontmatter.get("intents")
        if not isinstance(raw_intents, list):
            raise SpecRegistryError("intent catalog markdown frontmatter must contain an intents array")
        intents: dict[str, IntentSpec] = {}
        catalog_hash = _hash_text(raw)
        for item in raw_intents:
            if not isinstance(item, dict):
                raise SpecRegistryError("intent catalog entries must be objects")
            intent = _parse_intent_spec(item, catalog_markdown=markdown, spec_hash=catalog_hash, source=path)
            if intent.intent_id in intents:
                raise SpecRegistryError(f"duplicate intent_id: {intent.intent_id}")
            intents[intent.intent_id] = intent
        self._intents = intents
        return intents

    def _load_agents(self) -> dict[str, AgentDefinition]:
        if self._agents is not None:
            return self._agents
        path = self.spec_root / "agents" / "agent-registry.md"
        payload, _markdown = _load_markdown_spec(path.read_text(encoding="utf-8"), path)
        raw_agents = payload.get("agents")
        if not isinstance(raw_agents, list):
            raise SpecRegistryError("agent registry markdown frontmatter must contain an agents array")
        agents: dict[str, AgentDefinition] = {}
        for item in raw_agents:
            if not isinstance(item, dict):
                raise SpecRegistryError("agent registry entries must be objects")
            agent = AgentDefinition(
                agent_id=_required_str(item, "agent_id"),
                endpoint=_required_str(item, "endpoint"),
                accepted_scene_ids=tuple(str(value) for value in item.get("accepted_scene_ids", [])),
                task_schema=str(item.get("task_schema") or ""),
                event_schema=str(item.get("event_schema") or ""),
                supports_stream=bool(item.get("supports_stream", False)),
            )
            if agent.agent_id in agents:
                raise SpecRegistryError(f"duplicate agent_id: {agent.agent_id}")
            agents[agent.agent_id] = agent
        self._agents = agents
        return agents


def _load_markdown_spec(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    text = raw.lstrip()
    if not text.startswith("+++\n"):
        raise SpecRegistryError(f"markdown spec must start with TOML frontmatter: {path}")
    end = text.find("\n+++", 4)
    if end == -1:
        raise SpecRegistryError(f"markdown spec frontmatter is not closed: {path}")
    frontmatter_raw = text[4:end].strip()
    body_start = end + len("\n+++")
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    try:
        payload = tomllib.loads(frontmatter_raw)
    except tomllib.TOMLDecodeError as exc:
        raise SpecRegistryError(f"invalid markdown frontmatter: {path}") from exc
    return payload, text[body_start:].strip()


def _parse_intent_spec(payload: dict[str, Any], *, catalog_markdown: str, spec_hash: str, source: Path) -> IntentSpec:
    dispatch = payload.get("dispatch_contract")
    if not isinstance(dispatch, dict):
        raise SpecRegistryError(f"intent dispatch_contract must be an object: {source}")
    skill = payload.get("skill")
    if not isinstance(skill, dict):
        raise SpecRegistryError(f"intent skill reference must be an object: {source}")
    intent_id = _required_str(payload, "intent_id")
    return IntentSpec(
        intent_id=intent_id,
        scene_id=str(payload.get("scene_id") or intent_id),
        name=_required_str(payload, "name"),
        version=str(payload.get("version") or "0.0.0"),
        description=str(payload.get("description") or ""),
        target_agent=_required_str(payload, "target_agent"),
        skill=dict(skill),
        dispatch_contract=DispatchContract(
            task_type=_required_str(dispatch, "task_type"),
            handoff_fields=tuple(str(value) for value in dispatch.get("handoff_fields", [])),
        ),
        references=tuple(str(value) for value in payload.get("references", [])),
        spec_hash=spec_hash,
        spec_markdown=_intent_section(catalog_markdown, intent_id),
        source_path=str(source),
    )


def _intent_section(markdown: str, intent_id: str) -> str:
    lines = markdown.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == f"## {intent_id}":
            start = index
            break
    if start is None:
        return markdown.strip()
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SpecRegistryError(f"{key} is required")
    return value.strip()


def _hash_text(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
