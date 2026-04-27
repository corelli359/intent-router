from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from router_v4_service.core.models import (
    AgentDefinition,
    DispatchContract,
    RoutingSlotSpec,
    SceneSpec,
    TriggerSpec,
)


DEFAULT_SPEC_ROOT = Path(__file__).resolve().parents[1] / "default_specs"


class SpecRegistryError(RuntimeError):
    """Raised when routing specs or agent registry entries are invalid."""


class SpecRegistry:
    """Loads scene routing specs and execution-agent registry entries."""

    def __init__(self, spec_root: str | Path | None = None) -> None:
        self.spec_root = Path(spec_root).expanduser().resolve() if spec_root else DEFAULT_SPEC_ROOT.resolve()
        self._scenes: dict[str, SceneSpec] | None = None
        self._agents: dict[str, AgentDefinition] | None = None

    def scene_index(self) -> list[SceneSpec]:
        return sorted(self._load_scenes().values(), key=lambda item: item.scene_id)

    def scene(self, scene_id: str) -> SceneSpec:
        scenes = self._load_scenes()
        try:
            return scenes[scene_id]
        except KeyError as exc:
            raise SpecRegistryError(f"unknown scene: {scene_id}") from exc

    def agent(self, agent_id: str) -> AgentDefinition:
        agents = self._load_agents()
        try:
            return agents[agent_id]
        except KeyError as exc:
            raise SpecRegistryError(f"unknown agent: {agent_id}") from exc

    def agent_index(self) -> list[AgentDefinition]:
        return sorted(self._load_agents().values(), key=lambda item: item.agent_id)

    def _load_scenes(self) -> dict[str, SceneSpec]:
        if self._scenes is not None:
            return self._scenes
        scenes_dir = self.spec_root / "scenes"
        scenes: dict[str, SceneSpec] = {}
        for path in sorted(scenes_dir.glob("*.routing.json")):
            raw = path.read_text(encoding="utf-8")
            payload = _load_json_object(raw, path)
            scene = _parse_scene_spec(payload, spec_hash=_hash_text(raw), source=path)
            if scene.scene_id in scenes:
                raise SpecRegistryError(f"duplicate scene_id: {scene.scene_id}")
            scenes[scene.scene_id] = scene
        self._scenes = scenes
        return scenes

    def _load_agents(self) -> dict[str, AgentDefinition]:
        if self._agents is not None:
            return self._agents
        path = self.spec_root / "agents" / "agent-registry.json"
        payload = _load_json_object(path.read_text(encoding="utf-8"), path)
        raw_agents = payload.get("agents")
        if not isinstance(raw_agents, list):
            raise SpecRegistryError("agent registry must contain an agents array")
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


def _load_json_object(raw: str, path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecRegistryError(f"invalid JSON spec: {path}") from exc
    if not isinstance(payload, dict):
        raise SpecRegistryError(f"JSON spec must be an object: {path}")
    return payload


def _parse_scene_spec(payload: dict[str, Any], *, spec_hash: str, source: Path) -> SceneSpec:
    triggers = payload.get("triggers")
    if not isinstance(triggers, dict):
        raise SpecRegistryError(f"scene triggers must be an object: {source}")
    dispatch = payload.get("dispatch_contract")
    if not isinstance(dispatch, dict):
        raise SpecRegistryError(f"dispatch_contract must be an object: {source}")
    routing_slots_payload = payload.get("routing_slots", [])
    if not isinstance(routing_slots_payload, list):
        raise SpecRegistryError(f"routing_slots must be an array: {source}")
    return SceneSpec(
        scene_id=_required_str(payload, "scene_id"),
        name=_required_str(payload, "name"),
        version=str(payload.get("version") or "0.0.0"),
        description=str(payload.get("description") or ""),
        target_agent=_required_str(payload, "target_agent"),
        skill=dict(payload.get("skill") or {}),
        triggers=TriggerSpec(
            examples=tuple(str(value) for value in triggers.get("examples", [])),
            negative_examples=tuple(str(value) for value in triggers.get("negative_examples", [])),
            keywords=tuple(str(value) for value in triggers.get("keywords", [])),
            negative_keywords=tuple(str(value) for value in triggers.get("negative_keywords", [])),
        ),
        routing_slots=tuple(_parse_routing_slot(value, source) for value in routing_slots_payload),
        dispatch_contract=DispatchContract(
            task_type=_required_str(dispatch, "task_type"),
            handoff_fields=tuple(str(value) for value in dispatch.get("handoff_fields", [])),
        ),
        references=tuple(str(value) for value in payload.get("references", [])),
        spec_hash=spec_hash,
        source_path=str(source),
    )


def _parse_routing_slot(payload: object, source: Path) -> RoutingSlotSpec:
    if not isinstance(payload, dict):
        raise SpecRegistryError(f"routing slot must be an object: {source}")
    return RoutingSlotSpec(
        name=_required_str(payload, "name"),
        source=str(payload.get("source") or "user_utterance"),
        required_for_dispatch=bool(payload.get("required_for_dispatch", False)),
        handoff=bool(payload.get("handoff", True)),
        extraction=dict(payload.get("extraction") or {}),
    )


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SpecRegistryError(f"{key} is required")
    return value.strip()


def _hash_text(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
