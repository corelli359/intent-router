from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Protocol

import httpx

from router_v4_service.core.config import RouterV4LLMSettings
from router_v4_service.core.models import SceneSpec


@dataclass(frozen=True, slots=True)
class IntentCandidate:
    scene: SceneSpec
    score: int
    reasons: tuple[str, ...]
    routing_hints: dict[str, Any] = field(default_factory=dict)


class IntentRecognizer(Protocol):
    def shortlist(
        self,
        message: str,
        scenes: list[SceneSpec],
        *,
        limit: int = 3,
        push_context: dict[str, Any] | None = None,
    ) -> list[IntentCandidate]:
        """Return scenes directly selected by intent recognition."""


class IntentRecognizerError(RuntimeError):
    """Raised when the spec-driven intent recognizer cannot complete."""


class LLMIntentRecognizer:
    """OpenAI-compatible recognizer driven only by scene routing specs."""

    def __init__(
        self,
        settings: RouterV4LLMSettings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.client = client

    def shortlist(
        self,
        message: str,
        scenes: list[SceneSpec],
        *,
        limit: int = 3,
        push_context: dict[str, Any] | None = None,
    ) -> list[IntentCandidate]:
        if not self.settings.ready:
            raise IntentRecognizerError("LLM recognizer is enabled but ROUTER_V4/ROUTER_LLM settings are incomplete")
        payload = self._call_llm(message=message, scenes=scenes, push_context=push_context or {})
        return self._selected_scenes_from_payload(payload=payload, scenes=scenes)[:limit]

    def _call_llm(
        self,
        *,
        message: str,
        scenes: list[SceneSpec],
        push_context: dict[str, Any],
    ) -> dict[str, Any]:
        request = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是银行助手的意图识别器。必须只根据用户表达、助手推送上下文和给定场景规格进行判断。"
                        "场景是否命中、是否多意图，都必须由场景 spec 中的 triggers、skill 和 dispatch_contract 驱动。"
                        "不要提取业务槽位；提槽属于选中场景的执行 Agent/Skill。"
                        "不要使用外部知识补充未知场景；如果没有明确可执行的场景，selected_scene_id 返回 null。"
                        "助手主动推送时，用户表达可能是指代、承接或省略业务名称；你必须结合 push_context 中按 rank 排序的意图清单判断。"
                        "如果用户表达的是接受或继续办理某个推荐，但没有点名业务，选择最高 rank 的推送意图；"
                        "如果用户表达要同时处理多个推荐，selected_scene_ids 返回多个推送意图。"
                        "承接性表达本身就是可执行意图，不能因为用户没复述业务名称就返回 null。"
                        "只输出 JSON，不要输出解释文字。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_message": message,
                            "push_context": push_context,
                            "assistant_push_policy": _assistant_push_policy(push_context),
                            "scenes": [_scene_payload(scene) for scene in scenes],
                            "output_schema": {
                                "selected_scene_id": "string|null",
                                "selected_scene_ids": ["string"],
                                "confidence": "0.0-1.0",
                                "reason": "short Chinese reason",
                            },
                            "output_contract_notes": [
                                "selected_scene_ids 表示本轮需要同时执行的多个意图，不是备选项。",
                                "不要输出 recipient、amount 等业务字段，也不要把业务字段放入其他对象。",
                                "助手主动推送时，只能在 push_context.intents 对应场景中选择；用户没有表达要执行时返回 null。",
                                "assistant_push_policy.generic_acceptance_target 是用户承接当前推荐但未点名业务时应选择的场景。",
                                "当 assistant_push_policy 存在且用户表达承接当前卡片时，必须返回 generic_acceptance_target；不要把它当成未知意图。",
                                "assistant_push_policy.multi_intent_targets 是用户要求同时处理多个推荐时应返回的场景集合。",
                            ],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": self.settings.temperature,
            "stream": False,
        }
        if self.settings.structured_output_method == "json_mode":
            request["response_format"] = {"type": "json_object"}
        client = self.client or httpx.Client(timeout=self.settings.timeout_seconds)
        try:
            try:
                response = client.post(
                    _chat_completions_url(self.settings.api_base_url or ""),
                    headers=_headers(self.settings),
                    json=request,
                )
                if response.is_error:
                    raise IntentRecognizerError(f"LLM recognizer HTTP {response.status_code}: {response.text[:500]}")
                payload = response.json()
            except IntentRecognizerError:
                raise
            except Exception as exc:
                raise IntentRecognizerError(f"LLM recognizer request failed: {exc}") from exc
        finally:
            if self.client is None:
                client.close()
        content = _completion_content(payload)
        parsed = _extract_json_object(content)
        if not isinstance(parsed, dict):
            raise IntentRecognizerError("LLM recognizer response must be a JSON object")
        return parsed

    def _selected_scenes_from_payload(self, *, payload: dict[str, Any], scenes: list[SceneSpec]) -> list[IntentCandidate]:
        scene_by_id = {scene.scene_id: scene for scene in scenes}
        selected_scene_ids = _selected_scene_ids(payload)
        if not selected_scene_ids:
            return []
        confidence = _confidence(payload.get("confidence") or payload.get("score") or 0.8)
        scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
        reasons = _reasons(payload.get("reason") or payload.get("reasons"))
        candidates: list[IntentCandidate] = []
        for scene_id in selected_scene_ids:
            scene = scene_by_id.get(scene_id)
            if scene is None:
                continue
            scene_confidence = _confidence(scores.get(scene_id, confidence))
            candidates.append(
                IntentCandidate(
                    scene=scene,
                    score=max(1, min(100, int(scene_confidence * 100))),
                    reasons=("llm", f"confidence:{scene_confidence:.2f}", *reasons),
                )
            )
        return candidates


def _scene_payload(scene: SceneSpec) -> dict[str, Any]:
    return {
        "scene_id": scene.scene_id,
        "name": scene.name,
        "description": scene.description,
        "target_agent": scene.target_agent,
        "skill": dict(scene.skill),
        "triggers": {
            "keywords": list(scene.triggers.keywords),
            "negative_keywords": list(scene.triggers.negative_keywords),
            "examples": list(scene.triggers.examples),
            "negative_examples": list(scene.triggers.negative_examples),
        },
        "dispatch_contract": {
            "task_type": scene.dispatch_contract.task_type,
            "handoff_fields": list(scene.dispatch_contract.handoff_fields),
        },
        "references": list(scene.references),
    }


def _assistant_push_policy(push_context: dict[str, Any]) -> dict[str, Any]:
    raw_items = push_context.get("intents") if isinstance(push_context, dict) else None
    if not isinstance(raw_items, list):
        return {}
    ranked: list[tuple[int, str]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        scene_id = item.get("scene_id") or item.get("intent_code")
        if not isinstance(scene_id, str) or not scene_id.strip():
            continue
        rank = item.get("rank")
        ranked.append((rank if isinstance(rank, int) else index + 1, scene_id.strip()))
    ranked_scene_ids = [scene_id for _, scene_id in sorted(ranked, key=lambda value: value[0])]
    if not ranked_scene_ids:
        return {}
    return {
        "ranked_scene_ids": ranked_scene_ids,
        "generic_acceptance_target": ranked_scene_ids[0],
        "multi_intent_targets": ranked_scene_ids,
        "no_action_boundary": "Only return null when the user declines, changes topic, or does not relate the expression to the pushed card.",
    }


def _headers(settings: RouterV4LLMSettings) -> dict[str, str]:
    headers = {"content-type": "application/json", **settings.headers}
    if settings.api_key:
        headers.setdefault("Authorization", f"Bearer {settings.api_key}")
    return headers


def _chat_completions_url(base_url: str) -> str:
    stripped = base_url.rstrip("/")
    if stripped.endswith("/chat/completions"):
        return stripped
    return f"{stripped}/chat/completions"


def _completion_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise IntentRecognizerError("LLM recognizer response payload must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise IntentRecognizerError("LLM recognizer response does not contain choices")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message")
    if not isinstance(message, dict):
        raise IntentRecognizerError("LLM recognizer response does not contain message content")
    content = message.get("content")
    if isinstance(content, str):
        return content
    return str(content or "")


def _extract_json_object(raw: str) -> Any:
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise IntentRecognizerError(f"LLM recognizer did not return JSON: {raw[:200]}")
    return json.loads(text[start : end + 1])


def _selected_scene_ids(payload: dict[str, Any]) -> list[str]:
    raw_list = payload.get("selected_scene_ids")
    if isinstance(raw_list, list):
        normalized = [_normalize_scene_id(item) for item in raw_list if _normalize_scene_id(item)]
        if normalized:
            return normalized
    single = _optional_str(
        payload.get("selected_scene_id")
        or payload.get("scene_id")
        or payload.get("intent_code")
        or payload.get("intent")
    )
    if not single or single in {"none", "null", "unknown"}:
        return []
    return [single]


def _normalize_scene_id(value: Any) -> str:
    text = str(value).strip()
    if not text or text in {"none", "null", "unknown"}:
        return ""
    return text


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _confidence(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.8
    if numeric > 1:
        numeric = numeric / 100
    return max(0.0, min(1.0, numeric))


def _reasons(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value[:3] if str(item).strip())
    if value is None:
        return ()
    text = str(value).strip()
    return (text,) if text else ()
