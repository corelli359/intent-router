#!/usr/bin/env python3
"""Run multi-turn dialog verification through the real user-facing router APIs.

This script intentionally behaves like an external user/client:

1. create one session
2. send one user turn at a time through `/sessions/{session_id}/messages`
3. validate the returned assistant reply / current intent / current slots / dialog stage

It does not call the analyze-only endpoint and does not inspect internal helper APIs.
The purpose is end-to-end validation of the overall multi-turn capability from the
user side. The script uses `executionMode=router_only`, so the router stops after
intent recognition, slot filling, and follow-up prompts without calling downstream agents.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("INTENT_ROUTER_BASE_URL", "http://intent-router.kkrrc-359.top")
HOST_HEADER = os.getenv("INTENT_ROUTER_HOST_HEADER")
CUST_ID = os.getenv("INTENT_ROUTER_CUST_ID", "cust_demo")
TIMEOUT_SECONDS = float(os.getenv("INTENT_ROUTER_TIMEOUT_SECONDS", "90"))
CASES_FILE = Path(
    os.getenv(
        "INTENT_ROUTER_STANDARD_CASES",
        str(ROOT / "docs" / "examples" / "multiturn_intent_slot_cases.json"),
    )
)
CASE_IDS = [item.strip() for item in os.getenv("INTENT_ROUTER_CASE_IDS", "").split(",") if item.strip()]
CASE_LIMIT = max(1, int(os.getenv("INTENT_ROUTER_CASE_LIMIT", "1")))


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send one JSON request and return the decoded response."""
    headers = {"Content-Type": "application/json"}
    if HOST_HEADER:
        headers["Host"] = HOST_HEADER
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body}") from exc


class RouterClient:
    """Minimal client for the real router dialog APIs."""

    def __init__(self, *, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def create_session(self, *, cust_id: str) -> str:
        """Create one router session and return its id."""
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions",
            payload={"cust_id": cust_id},
        )
        return str(payload["session_id"])

    def post_message(self, *, session_id: str, cust_id: str, content: str) -> dict[str, Any]:
        """Send one real user turn and return the router snapshot."""
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions/{session_id}/messages",
            payload={
                "cust_id": cust_id,
                "content": content,
                "executionMode": "router_only",
            },
        )
        return payload["snapshot"]


def _load_cases(path: Path) -> list[dict[str, Any]]:
    """Load the multi-turn suite case definitions."""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        cases = payload
    else:
        cases = payload.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"no cases found in {path}")
    return [case for case in cases if isinstance(case, dict)]


def _select_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter loaded cases down to a small sequential batch."""
    selected = cases
    if CASE_IDS:
        wanted = set(CASE_IDS)
        selected = [case for case in cases if str(case.get("case_id", "")).strip() in wanted]
    if not selected:
        raise ValueError("no cases matched INTENT_ROUTER_CASE_IDS")
    return selected[:CASE_LIMIT]


def _last_assistant_message(snapshot: dict[str, Any]) -> str:
    """Return the latest assistant message from one router snapshot."""
    messages = snapshot.get("messages") or []
    for item in reversed(messages):
        if item.get("role") == "assistant":
            return str(item.get("content", ""))
    return ""


def _dialog_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the active router payload for the current dialog turn."""
    pending = snapshot.get("pending_graph")
    if isinstance(pending, dict):
        return pending
    current = snapshot.get("current_graph")
    if isinstance(current, dict):
        return current
    return {}


def _dialog_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the current dialog items from the active router payload."""
    payload = _dialog_payload(snapshot)
    nodes = payload.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]


def _dialog_stage(snapshot: dict[str, Any]) -> str:
    """Normalize the raw router status into a user-facing dialog stage."""
    payload = _dialog_payload(snapshot)
    raw_status = str(payload.get("status") or "").strip()
    if raw_status in {"waiting_user_input", "waiting_confirmation_node"}:
        return "asking"
    if raw_status == "waiting_confirmation":
        return "confirming"
    if raw_status == "ready_for_dispatch":
        return "ready"
    if raw_status in {"completed", "partially_completed"}:
        return "done"
    return "idle"


def _current_result(
    snapshot: dict[str, Any],
    *,
    intent_code: str | None = None,
) -> dict[str, Any] | None:
    """Return the current intent result item for this dialog."""
    items = _dialog_items(snapshot)
    if intent_code:
        for item in items:
            if str(item.get("intent_code", "")) == intent_code:
                return item
    return items[0] if items else None


def _dialog_state(snapshot: dict[str, Any], *, intent_code: str | None = None) -> dict[str, Any]:
    """Project one raw router snapshot into a simple dialog-oriented state."""
    result = _current_result(snapshot, intent_code=intent_code)
    return {
        "stage": _dialog_stage(snapshot),
        "assistant_reply": _last_assistant_message(snapshot),
        "intent_code": str(result.get("intent_code", "")) if isinstance(result, dict) else "",
        "slots": dict(result.get("slot_memory") or {}) if isinstance(result, dict) else {},
    }


def _assert_equal(*, actual: Any, expected: Any, context: str) -> None:
    """Raise when two values differ."""
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def _assert_text_contains(*, text: str, expected: list[str], context: str) -> None:
    """Raise when text does not contain all expected fragments."""
    missing = [item for item in expected if item not in text]
    if missing:
        raise AssertionError(f"{context}: text missing {missing}, actual={text!r}")


def _assert_text_excludes(*, text: str, forbidden: list[str], context: str) -> None:
    """Raise when text contains forbidden fragments."""
    present = [item for item in forbidden if item in text]
    if present:
        raise AssertionError(f"{context}: text unexpectedly contains {present}, actual={text!r}")


def _assert_mapping_contains(*, actual: dict[str, Any], expected: dict[str, Any], context: str) -> None:
    """Raise when actual mapping does not contain the expected subset."""
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if str(actual_value) != str(expected_value):
            raise AssertionError(
                f"{context}: key={key!r} expected {expected_value!r}, got {actual_value!r}"
            )


def _assert_keys_absent(*, actual: dict[str, Any], keys: list[str], context: str) -> None:
    """Raise when keys should be absent but are present."""
    present = [key for key in keys if key in actual]
    if present:
        raise AssertionError(f"{context}: keys should be absent {present}, actual={actual}")


def _format_failure(
    *,
    case: dict[str, Any],
    turn_index: int | None,
    session_id: str,
    reason: str,
    snapshot: dict[str, Any] | None = None,
) -> str:
    """Build a detailed failure payload for debugging."""
    return json.dumps(
        {
            "case_id": case.get("case_id"),
            "turn_index": turn_index,
            "session_id": session_id,
            "reason": reason,
            "dialog_state": _dialog_state(snapshot or {}),
        },
        ensure_ascii=False,
        indent=2,
    )


def _verify_turn(
    *,
    case: dict[str, Any],
    turn: dict[str, Any],
    turn_index: int,
    session_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Validate one user-side turn response."""
    context_prefix = f"{case.get('case_id')} turn {turn_index}"
    expected_intent = turn.get("expect_intent")
    state = _dialog_state(snapshot, intent_code=str(expected_intent) if expected_intent else None)

    try:
        expected_stage = turn.get("expect_stage")
        if expected_stage is not None:
            _assert_equal(
                actual=state["stage"],
                expected=str(expected_stage),
                context=f"{context_prefix} stage",
            )

        if not state["intent_code"]:
            raise AssertionError(f"{context_prefix} result: no current intent result")

        if expected_intent is not None:
            _assert_equal(
                actual=state["intent_code"],
                expected=str(expected_intent),
                context=f"{context_prefix} intent",
            )

        expected_reply_contains = turn.get("expect_reply_contains") or []
        if expected_reply_contains:
            _assert_text_contains(
                text=state["assistant_reply"],
                expected=[str(item) for item in expected_reply_contains],
                context=f"{context_prefix} reply_contains",
            )

        expected_reply_excludes = turn.get("expect_reply_excludes") or []
        if expected_reply_excludes:
            _assert_text_excludes(
                text=state["assistant_reply"],
                forbidden=[str(item) for item in expected_reply_excludes],
                context=f"{context_prefix} reply_excludes",
            )

        expected_slots = turn.get("expect_slots") or {}
        if expected_slots:
            _assert_mapping_contains(
                actual=state["slots"],
                expected={str(key): value for key, value in expected_slots.items()},
                context=f"{context_prefix} slots",
            )

        expected_absent_slots = turn.get("expect_absent_slots") or []
        if expected_absent_slots:
            _assert_keys_absent(
                actual=state["slots"],
                keys=[str(item) for item in expected_absent_slots],
                context=f"{context_prefix} absent_slots",
            )
    except AssertionError as exc:
        raise AssertionError(
            _format_failure(
                case=case,
                turn_index=turn_index,
                session_id=session_id,
                reason=str(exc),
                snapshot=snapshot,
            )
        ) from exc

    return {
        "turn_index": turn_index,
        "content": str(turn.get("content") or ""),
        "stage": state["stage"],
        "intent_code": state["intent_code"],
        "assistant_reply": state["assistant_reply"],
        "slots": state["slots"],
    }


def _verify_final_expectations(
    *,
    case: dict[str, Any],
    session_id: str,
    final_snapshot: dict[str, Any],
) -> None:
    """Validate case-level final expectations after all turns finish."""
    expected_final_intent = case.get("expect_final_intent")
    state = _dialog_state(final_snapshot, intent_code=str(expected_final_intent) if expected_final_intent else None)
    context_prefix = f"{case.get('case_id')} final"

    try:
        expected_final_stage = case.get("expect_final_stage")
        if expected_final_stage is not None:
            _assert_equal(
                actual=state["stage"],
                expected=str(expected_final_stage),
                context=f"{context_prefix} stage",
            )

        if not state["intent_code"]:
            raise AssertionError(f"{context_prefix} result: no current intent result")

        if expected_final_intent is not None:
            _assert_equal(
                actual=state["intent_code"],
                expected=str(expected_final_intent),
                context=f"{context_prefix} intent",
            )

        expected_final_slots = case.get("expect_final_slots") or {}
        if expected_final_slots:
            _assert_mapping_contains(
                actual=state["slots"],
                expected={str(key): value for key, value in expected_final_slots.items()},
                context=f"{context_prefix} slots",
            )
    except AssertionError as exc:
        raise AssertionError(
            _format_failure(
                case=case,
                turn_index=None,
                session_id=session_id,
                reason=str(exc),
                snapshot=final_snapshot,
            )
        ) from exc


def _run_case(client: RouterClient, case: dict[str, Any]) -> dict[str, Any]:
    """Run one full multi-turn user-side case."""
    turns = case.get("turns") or []
    if not turns:
        raise AssertionError(f"case {case.get('case_id')} has no turns")

    cust_id = str(case.get("cust_id") or CUST_ID)
    session_id = client.create_session(cust_id=cust_id)
    turn_results: list[dict[str, Any]] = []
    final_snapshot: dict[str, Any] | None = None

    for turn_index, turn in enumerate(turns, start=1):
        content = str(turn.get("content") or "").strip()
        if not content:
            raise AssertionError(f"case {case.get('case_id')} turn {turn_index} has empty content")
        final_snapshot = client.post_message(session_id=session_id, cust_id=cust_id, content=content)
        turn_results.append(
            _verify_turn(
                case=case,
                turn=turn,
                turn_index=turn_index,
                session_id=session_id,
                snapshot=final_snapshot,
            )
        )

    if final_snapshot is None:
        raise AssertionError(f"case {case.get('case_id')} produced no final snapshot")
    _verify_final_expectations(case=case, session_id=session_id, final_snapshot=final_snapshot)

    return {
        "case_id": case.get("case_id"),
        "description": case.get("description", ""),
        "session_id": session_id,
        "turn_results": turn_results,
        "final_stage": _dialog_stage(final_snapshot),
        "final_assistant_reply": _last_assistant_message(final_snapshot),
    }


def main() -> int:
    """Run the selected multi-turn user-side cases."""
    client = RouterClient(base_url=BASE_URL)
    cases = _select_cases(_load_cases(CASES_FILE))
    results: list[dict[str, Any]] = []
    failures: list[str] = []

    for case in cases:
        case_id = case.get("case_id")
        try:
            result = _run_case(client, case)
            results.append(result)
            print(f"[OK] {case_id}")
        except Exception as exc:
            failures.append(str(exc))
            print(f"[FAIL] {case_id}")

    print(
        json.dumps(
            {
                "base_url": BASE_URL,
                "cases_file": str(CASES_FILE),
                "case_ids": CASE_IDS,
                "case_limit": CASE_LIMIT,
                "case_count": len(cases),
                "passed_count": len(results),
                "failed_count": len(failures),
                "results": results,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
