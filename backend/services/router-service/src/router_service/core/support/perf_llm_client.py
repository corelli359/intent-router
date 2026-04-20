from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_PERF_TRANSFER_MESSAGE = "给小明转500元"
_PERF_TRANSFER_SLOTS = {
    "payee_name": "小明",
    "amount": "500",
}


def _extract_transfer_slots(message: str) -> dict[str, str]:
    normalized = "".join(message.split())
    if normalized != _PERF_TRANSFER_MESSAGE:
        return {}
    return dict(_PERF_TRANSFER_SLOTS)


def _json_text_mentions_key(raw_json: object, key: str) -> bool:
    """Return whether one compact JSON variable mentions a key without reparsing it."""
    return key in str(raw_json or "")


@dataclass(slots=True)
class FastPerfLLMClient:
    """In-process perf stub that removes all outbound LLM HTTP I/O."""

    default_model: str = "fake-router-llm"

    async def run_json(
        self,
        *,
        prompt: Any,
        variables: dict[str, Any],
        model: str | None = None,
        on_delta: Any | None = None,
    ) -> Any:
        del prompt, model, on_delta
        message = str(variables.get("message") or "")
        slots = _extract_transfer_slots(message)
        if "intent_json" in variables and "existing_slot_memory_json" in variables:
            intent_json = variables.get("intent_json")
            existing_slot_memory_json = variables.get("existing_slot_memory_json")
            payload_slots: list[dict[str, Any]] = []
            if (
                "payee_name" in slots
                and _json_text_mentions_key(intent_json, "payee_name")
                and not _json_text_mentions_key(existing_slot_memory_json, "payee_name")
            ):
                payload_slots.append(
                    {
                        "slot_key": "payee_name",
                        "value": slots["payee_name"],
                        "source": "user_message",
                        "source_text": message,
                        "confidence": 0.99,
                    }
                )
            if (
                "amount" in slots
                and _json_text_mentions_key(intent_json, "amount")
                and not _json_text_mentions_key(existing_slot_memory_json, "amount")
            ):
                payload_slots.append(
                    {
                        "slot_key": "amount",
                        "value": slots["amount"],
                        "source": "user_message",
                        "source_text": message,
                        "confidence": 0.99,
                    }
                )
            return {"slots": payload_slots, "ambiguousSlotKeys": []}

        if "intents_json" in variables:
            if not slots:
                return {"matches": []}
            return {
                "matches": [
                    {
                        "intent_code": "AG_TRANS",
                        "confidence": 0.99,
                        "reason": "perf stub matched transfer intent",
                    }
                ]
            }

        if "matched_intents_json" in variables or "recognition_hint_json" in variables:
            if not slots:
                return {
                    "summary": "",
                    "needs_confirmation": False,
                    "primary_intents": [],
                    "candidate_intents": [],
                    "nodes": [],
                    "edges": [],
                }
            return {
                "summary": f"识别到事项：给{slots['payee_name']}转账 {slots['amount']} 元",
                "needs_confirmation": False,
                "primary_intents": [
                    {
                        "intent_code": "AG_TRANS",
                        "confidence": 0.99,
                        "reason": "perf stub matched transfer intent",
                    }
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "AG_TRANS",
                        "title": f"给{slots['payee_name']}转账 {slots['amount']} 元",
                        "confidence": 0.99,
                        "source_fragment": message,
                        "slot_memory": slots,
                        "slot_bindings": [
                            {
                                "slot_key": "payee_name",
                                "value": slots["payee_name"],
                                "source": "user_message",
                                "source_text": message,
                                "confidence": 0.99,
                            },
                            {
                                "slot_key": "amount",
                                "value": slots["amount"],
                                "source": "user_message",
                                "source_text": message,
                                "confidence": 0.99,
                            },
                        ],
                    }
                ],
                "edges": [],
            }

        return {
            "action": "resume_current",
            "reason": "perf stub keeps current flow",
            "target_intent_code": None,
        }

    async def aclose(self) -> None:
        return None
