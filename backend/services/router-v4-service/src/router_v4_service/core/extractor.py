from __future__ import annotations

from typing import Any

from router_v4_service.core.models import RoutingSlotSpec, SceneSpec


TRIM_CHARS = " \t\r\n,，。.!！?？;；:："


class RoutingSlotExtractor:
    """Generic slot hint extraction selected by scene-provided slot specs."""

    def extract(self, message: str, scene: SceneSpec) -> dict[str, Any]:
        slots: dict[str, Any] = {}
        for slot in scene.routing_slots:
            if slot.source != "user_utterance":
                continue
            value = self._extract_slot(message, slot)
            if value not in (None, ""):
                slots[slot.name] = value
        return slots

    def missing_required_for_dispatch(self, scene: SceneSpec, slots: dict[str, Any]) -> list[str]:
        return [
            slot.name
            for slot in scene.routing_slots
            if slot.required_for_dispatch and slots.get(slot.name) in (None, "")
        ]

    def handoff_slots(self, scene: SceneSpec, slots: dict[str, Any]) -> dict[str, Any]:
        allowed = {slot.name for slot in scene.routing_slots if slot.handoff}
        return {key: value for key, value in slots.items() if key in allowed}

    def _extract_slot(self, message: str, slot: RoutingSlotSpec) -> Any:
        extractor = dict(slot.extractor)
        extractor_type = str(extractor.get("type") or "text")
        if extractor_type == "number":
            return self._extract_number(message)
        if extractor_type == "after_terms":
            return self._extract_after_terms(message, extractor)
        return None

    def _extract_number(self, message: str) -> int | float | None:
        numbers: list[str] = []
        current = ""
        dot_used = False
        for char in message:
            if char.isdigit():
                current += char
                continue
            if char == "." and current and not dot_used:
                current += char
                dot_used = True
                continue
            if current:
                numbers.append(current.rstrip("."))
                current = ""
                dot_used = False
        if current:
            numbers.append(current.rstrip("."))
        for raw in numbers:
            if not raw:
                continue
            try:
                return float(raw) if "." in raw else int(raw)
            except ValueError:
                continue
        return None

    def _extract_after_terms(self, message: str, extractor: dict[str, Any]) -> str | None:
        terms = [str(value) for value in extractor.get("terms", []) if str(value)]
        stop_terms = [str(value) for value in extractor.get("stop_terms", []) if str(value)]
        max_chars = int(extractor.get("max_chars", 24))
        for term in terms:
            start = message.find(term)
            if start < 0:
                continue
            candidate = message[start + len(term) :]
            for stop in stop_terms:
                stop_index = candidate.find(stop)
                if stop_index >= 0:
                    candidate = candidate[:stop_index]
            candidate = candidate[:max_chars].strip(TRIM_CHARS)
            if candidate:
                return candidate
        return None
