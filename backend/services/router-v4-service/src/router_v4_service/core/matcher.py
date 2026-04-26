from __future__ import annotations

from dataclasses import dataclass

from router_v4_service.core.models import SceneSpec


@dataclass(frozen=True, slots=True)
class SceneCandidate:
    scene: SceneSpec
    score: int
    reasons: tuple[str, ...]


class SceneMatcher:
    """Metadata-driven scene matcher.

    Matching terms are declared by scene routing specs. Runtime code only
    implements generic scoring.
    """

    def shortlist(self, message: str, scenes: list[SceneSpec], *, limit: int = 3) -> list[SceneCandidate]:
        candidates: list[SceneCandidate] = []
        for scene in scenes:
            score, reasons = self._score_scene(message, scene)
            if score > 0:
                candidates.append(SceneCandidate(scene=scene, score=score, reasons=tuple(reasons)))
        candidates.sort(key=lambda item: (-item.score, item.scene.scene_id))
        return candidates[:limit]

    def _score_scene(self, message: str, scene: SceneSpec) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        for keyword in scene.triggers.keywords:
            if keyword and keyword in message:
                score += 3
                reasons.append(f"keyword:{keyword}")
        for example in scene.triggers.examples:
            if example and example in message:
                score += 4
                reasons.append(f"example:{example}")
        for keyword in scene.triggers.negative_keywords:
            if keyword and keyword in message:
                score -= 4
                reasons.append(f"negative_keyword:{keyword}")
        for example in scene.triggers.negative_examples:
            if example and example in message:
                score -= 5
                reasons.append(f"negative_example:{example}")
        return max(score, 0), reasons
