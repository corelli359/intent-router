from __future__ import annotations

from typing import Any

from router_service.core.skill_runtime.models import SkillSpec, SlotDefinition


TRIM_CHARS = " \t\r\n,，。.!！?？;；:："


class SlotExtractor:
    """Generic extractor implementations selected by Skill metadata."""

    def extract(self, message: str, skill: SkillSpec) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for slot in skill.slots:
            value = self.extract_for_slot(message, slot, direct_reply=False)
            if value not in (None, ""):
                values[slot.name] = value
        return values

    def extract_direct_reply(self, message: str, slot: SlotDefinition) -> Any:
        return self.extract_for_slot(message, slot, direct_reply=True)

    def extract_for_slot(self, message: str, slot: SlotDefinition, *, direct_reply: bool) -> Any:
        extractor = dict(slot.extractor)
        extractor_type = str(extractor.get("type") or "text")
        if extractor_type == "number":
            return self._extract_number(message)
        if extractor_type == "after_terms":
            value = self._extract_after_terms(message, extractor)
            if value in (None, "") and direct_reply and extractor.get("accept_direct_reply", True):
                return self._direct_text(message, extractor)
            return value
        if extractor_type == "text" and direct_reply:
            return self._direct_text(message, extractor)
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
        terms = [str(item) for item in extractor.get("terms", []) if str(item)]
        stop_terms = [str(item) for item in extractor.get("stop_terms", []) if str(item)]
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

    def _direct_text(self, message: str, extractor: dict[str, Any]) -> str | None:
        max_chars = int(extractor.get("max_chars", 24))
        candidate = message.strip(TRIM_CHARS)[:max_chars].strip(TRIM_CHARS)
        return candidate or None
