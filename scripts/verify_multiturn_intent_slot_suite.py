#!/usr/bin/env python3
"""Run multi-turn intent + slot verification through the real user-facing dialog APIs.

This script intentionally behaves like an external user/client:

1. create one session
2. send one user turn at a time through `/sessions/{session_id}/messages`
3. validate the returned assistant prompt / graph status / intent / slot memory

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


def _graph_nodes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return graph nodes from the snapshot payload."""
    graph = snapshot.get("current_graph") or {}
    nodes = graph.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]


def _graph_status(snapshot: dict[str, Any]) -> str | None:
    """Return current graph status when present."""
    graph = snapshot.get("current_graph") or {}
    status = graph.get("status")
    return str(status) if status is not None else None


def _graph_node(
    snapshot: dict[str, Any],
    *,
    intent_code: str | None = None,
) -> dict[str, Any] | None:
    """Return the first matching graph node."""
    nodes = _graph_nodes(snapshot)
    if intent_code:
        for node in nodes:
            if str(node.get("intent_code", "")) == intent_code:
                return node
    return nodes[0] if nodes else None


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
            "snapshot": snapshot,
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
    assistant_message = _last_assistant_message(snapshot)
    expected_node_intent = turn.get("expect_intent")
    node = _graph_node(snapshot, intent_code=str(expected_node_intent) if expected_node_intent else None)

    try:
        expected_graph_status = turn.get("expect_graph_status")
        if expected_graph_status is not None:
            _assert_equal(
                actual=_graph_status(snapshot),
                expected=str(expected_graph_status),
                context=f"{context_prefix} graph_status",
            )

        if node is None:
            raise AssertionError(f"{context_prefix} node: no matching graph node")

        if expected_node_intent is not None:
            _assert_equal(
                actual=str(node.get("intent_code", "")),
                expected=str(expected_node_intent),
                context=f"{context_prefix} intent",
            )

        expected_node_status = turn.get("expect_node_status")
        if expected_node_status is not None:
            _assert_equal(
                actual=str(node.get("status", "")),
                expected=str(expected_node_status),
                context=f"{context_prefix} node_status",
            )

        expected_assistant_contains = turn.get("expect_assistant_contains") or []
        if expected_assistant_contains:
            _assert_text_contains(
                text=assistant_message,
                expected=[str(item) for item in expected_assistant_contains],
                context=f"{context_prefix} assistant_contains",
            )

        expected_assistant_excludes = turn.get("expect_assistant_excludes") or []
        if expected_assistant_excludes:
            _assert_text_excludes(
                text=assistant_message,
                forbidden=[str(item) for item in expected_assistant_excludes],
                context=f"{context_prefix} assistant_excludes",
            )

        slot_memory = node.get("slot_memory") or {}
        expected_slot_memory = turn.get("expect_slot_memory") or {}
        if expected_slot_memory:
            _assert_mapping_contains(
                actual=slot_memory,
                expected={str(key): value for key, value in expected_slot_memory.items()},
                context=f"{context_prefix} slot_memory",
            )

        expected_slot_absent = turn.get("expect_slot_absent") or []
        if expected_slot_absent:
            _assert_keys_absent(
                actual=slot_memory,
                keys=[str(item) for item in expected_slot_absent],
                context=f"{context_prefix} slot_absent",
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
        "graph_status": _graph_status(snapshot),
        "intent_code": node.get("intent_code"),
        "node_status": node.get("status"),
        "assistant_message": assistant_message,
        "slot_memory": node.get("slot_memory") or {},
    }


def _verify_final_expectations(
    *,
    case: dict[str, Any],
    session_id: str,
    final_snapshot: dict[str, Any],
) -> None:
    """Validate case-level final expectations after all turns finish."""
    expected_final_intent = case.get("expect_final_intent")
    node = _graph_node(
        final_snapshot,
        intent_code=str(expected_final_intent) if expected_final_intent else None,
    )
    context_prefix = f"{case.get('case_id')} final"

    try:
        expected_final_graph_status = case.get("expect_final_graph_status")
        if expected_final_graph_status is not None:
            _assert_equal(
                actual=_graph_status(final_snapshot),
                expected=str(expected_final_graph_status),
                context=f"{context_prefix} graph_status",
            )

        if node is None:
            raise AssertionError(f"{context_prefix} node: no matching graph node")

        if expected_final_intent is not None:
            _assert_equal(
                actual=str(node.get("intent_code", "")),
                expected=str(expected_final_intent),
                context=f"{context_prefix} intent",
            )

        expected_final_status = case.get("expect_final_status")
        if expected_final_status is not None:
            _assert_equal(
                actual=str(node.get("status", "")),
                expected=str(expected_final_status),
                context=f"{context_prefix} node_status",
            )

        expected_final_slot_memory = case.get("expect_final_slot_memory") or {}
        if expected_final_slot_memory:
            _assert_mapping_contains(
                actual=node.get("slot_memory") or {},
                expected={str(key): value for key, value in expected_final_slot_memory.items()},
                context=f"{context_prefix} slot_memory",
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
        "final_graph_status": _graph_status(final_snapshot),
        "final_assistant_message": _last_assistant_message(final_snapshot),
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
