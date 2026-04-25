from __future__ import annotations

from router_service.core.skill_runtime.models import SkillIndexEntry


class SkillMatcher:
    """Metadata-driven skill matcher.

    This intentionally scores terms declared by Skill metadata. It does not embed
    business-specific keyword rules in runtime code.
    """

    def shortlist(self, message: str, index: list[SkillIndexEntry], *, limit: int = 3) -> list[SkillIndexEntry]:
        normalized = message.strip()
        scored: list[tuple[int, SkillIndexEntry]] = []
        for entry in index:
            if entry.status != "active":
                continue
            score = self._score_entry(normalized, entry)
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1].skill_id))
        return [entry for _score, entry in scored[:limit]]

    def _score_entry(self, message: str, entry: SkillIndexEntry) -> int:
        score = 0
        for keyword in entry.keywords:
            if keyword and keyword in message:
                score += 3
        if entry.name and entry.name in message:
            score += 2
        if entry.skill_id and entry.skill_id in message:
            score += 1
        return score
