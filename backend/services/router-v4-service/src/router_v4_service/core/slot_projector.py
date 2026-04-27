from __future__ import annotations

from typing import Any

from router_v4_service.core.models import SceneSpec


class RoutingSlotProjector:
    """Projects recognizer-provided slots onto the selected scene spec."""

    def normalize(self, scene: SceneSpec, slots: dict[str, Any]) -> dict[str, Any]:
        allowed = {slot.name for slot in scene.routing_slots}
        return {
            key: value
            for key, value in slots.items()
            if key in allowed and value not in (None, "")
        }

    def missing_required_for_dispatch(self, scene: SceneSpec, slots: dict[str, Any]) -> list[str]:
        return [
            slot.name
            for slot in scene.routing_slots
            if slot.required_for_dispatch and slots.get(slot.name) in (None, "")
        ]

    def handoff_slots(self, scene: SceneSpec, slots: dict[str, Any]) -> dict[str, Any]:
        allowed = {slot.name for slot in scene.routing_slots if slot.handoff}
        return {key: value for key, value in slots.items() if key in allowed}
