#!/usr/bin/env python3
"""Run standard multi-turn intent recognition and slot filling verification cases.

This script is designed for direct execution without command-line arguments.
It reads a JSON case file, then for each user turn:

1. optionally calls the analyze API to inspect recognition and per-turn slot extraction
2. calls the execute API to advance the real router session
3. validates prompts, graph status, intent code, and merged slot memory

The goal is to support repeatable regression checks for:

- intent recognition correctness
- required-slot follow-up prompts
- multi-turn slot completion
- final execution readiness
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
ANALYZE_BEFORE_EXECUTE = os.getenv("INTENT_ROUTER_ANALYZE_BEFORE_EXECUTE", "1") == "1"
DEFAULT_ANALYSIS_MODE = os.getenv("INTENT_ROUTER_ANALYSIS_MODE", "full")
CASE_IDS = [item.strip() for item in os.getenv("INTENT_ROUTER_CASE_IDS", "").split(",") if item.strip()]
CASE_LIMIT = max(1, int(os.getenv("INTENT_ROUTER_CASE_LIMIT", "1")))


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send one JSON request and return the decoded JSON response."""
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
    """Minimal direct client for the router session APIs."""

    def __init__(self, *, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def create_session(self, *, cust_id: str) -> str:
        """Create a router session and return its id."""
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions",
            payload={"cust_id": cust_id},
        )
        return str(payload["session_id"])

    def analyze_message(
        self,
        *,
        session_id: str,
        cust_id: str,
        content: str,
        analysis_mode: str,
    ) -> dict[str, Any]:
        """Call the analyze-only endpoint for one user turn."""
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions/{session_id}/messages/analyze",
            payload={
                "cust_id": cust_id,
                "content": content,
                "analysisMode": analysis_mode,
            },
        )
        return payload["analysis"]

    def post_message(self, *, session_id: str, cust_id: str, content: str) -> dict[str, Any]:
        """Submit one real user turn and return the router snapshot."""
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions/{session_id}/messages",
            payload={"cust_id": cust_id, "content": content},
        )
        return payload["snapshot"]


def _load_cases(path: Path) -> list[dict[str, Any]]:
    """Load the multiturn standard test cases from a JSON file."""
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
    """Filter the loaded cases so one standard run only exercises a small batch."""
    selected = cases
    if CASE_IDS:
        selected = [
            case
            for case in cases
            if str(case.get("case_id", "")).strip() in set(CASE_IDS)
        ]
    if not selected:
        raise ValueError("no cases matched INTENT_ROUTER_CASE_IDS")
    return selected[:CASE_LIMIT]


def _last_assistant_message(snapshot: dict[str, Any]) -> str:
    """Return the most recent assistant message content from one snapshot."""
    messages = snapshot.get("messages") or []
    for item in reversed(messages):
        if item.get("role") == "assistant":
            return str(item.get("content", ""))
    return ""


def _analysis_primary_codes(analysis: dict[str, Any]) -> list[str]:
    """Return primary intent codes from one analyze response."""
    recognition = analysis.get("recognition") or {}
    primary = recognition.get("primary") or []
    return [str(item.get("intent_code", "")) for item in primary if item.get("intent_code")]


def _analysis_candidate_codes(analysis: dict[str, Any]) -> list[str]:
    """Return candidate intent codes from one analyze response."""
    recognition = analysis.get("recognition") or {}
    candidates = recognition.get("candidates") or []
    return [str(item.get("intent_code", "")) for item in candidates if item.get("intent_code")]


def _analysis_diagnostic_codes(analysis: dict[str, Any]) -> list[str]:
    """Return diagnostic codes from one analyze response."""
    diagnostics = analysis.get("diagnostics") or []
    return [str(item.get("code", "")) for item in diagnostics if item.get("code")]


def _analysis_slot_node(
    analysis: dict[str, Any],
    *,
    intent_code: str | None = None,
) -> dict[str, Any] | None:
    """Return the target slot node from one analyze response."""
    nodes = analysis.get("slot_nodes") or []
    if intent_code:
        for node in nodes:
            if str(node.get("intent_code", "")) == intent_code:
                return node
    return nodes[0] if nodes else None


def _graph_nodes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return graph nodes from one snapshot."""
    graph = snapshot.get("current_graph") or {}
    nodes = graph.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]


def _graph_status(snapshot: dict[str, Any]) -> str | None:
    """Return current graph status from one snapshot."""
    graph = snapshot.get("current_graph") or {}
    status = graph.get("status")
    return str(status) if status is not None else None


def _graph_node(
    snapshot: dict[str, Any],
    *,
    intent_code: str | None = None,
) -> dict[str, Any] | None:
    """Return the target graph node from one snapshot."""
    nodes = _graph_nodes(snapshot)
    if intent_code:
        for node in nodes:
            if str(node.get("intent_code", "")) == intent_code:
                return node
    return nodes[0] if nodes else None


def _assert_equal(*, actual: Any, expected: Any, context: str) -> None:
    """Raise when one actual value differs from the expected value."""
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def _assert_contains_all(*, actual: list[str], expected: list[str], context: str) -> None:
    """Raise when the expected list items are not all present."""
    missing = [item for item in expected if item not in actual]
    if missing:
        raise AssertionError(f"{context}: missing {missing}, actual={actual}")


def _assert_text_contains(*, text: str, expected: list[str], context: str) -> None:
    """Raise when the expected fragments are not all present in text."""
    missing = [item for item in expected if item not in text]
    if missing:
        raise AssertionError(f"{context}: text missing {missing}, actual={text!r}")


def _assert_text_excludes(*, text: str, forbidden: list[str], context: str) -> None:
    """Raise when any forbidden fragment appears in text."""
    present = [item for item in forbidden if item in text]
    if present:
        raise AssertionError(f"{context}: text unexpectedly contains {present}, actual={text!r}")


def _assert_mapping_contains(*, actual: dict[str, Any], expected: dict[str, Any], context: str) -> None:
    """Raise when the actual mapping does not contain the expected key/value subset."""
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if str(actual_value) != str(expected_value):
            raise AssertionError(
                f"{context}: key={key!r} expected {expected_value!r}, got {actual_value!r}"
            )


def _assert_keys_absent(*, actual: dict[str, Any], keys: list[str], context: str) -> None:
    """Raise when any of the specified keys is present in the actual mapping."""
    present = [key for key in keys if key in actual]
    if present:
        raise AssertionError(f"{context}: keys should be absent {present}, actual={actual}")


def _format_failure(
    *,
    case: dict[str, Any],
    turn_index: int | None,
    session_id: str,
    reason: str,
    analyze: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> str:
    """Build one detailed failure payload for easier troubleshooting."""
    return json.dumps(
        {
            "case_id": case.get("case_id"),
            "turn_index": turn_index,
            "session_id": session_id,
            "reason": reason,
            "analyze": analyze,
            "snapshot": snapshot,
        },
        ensure_ascii=False,
        indent=2,
    )


def _verify_analyze(
    *,
    case: dict[str, Any],
    turn: dict[str, Any],
    turn_index: int,
    session_id: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Validate one analyze-only response against the turn expectations."""
    context_prefix = f"{case.get('case_id')} turn {turn_index} analyze"
    try:
        expected_primary_contains = turn.get("expect_analyze_primary_contains") or []
        if expected_primary_contains:
            _assert_contains_all(
                actual=_analysis_primary_codes(analysis),
                expected=[str(item) for item in expected_primary_contains],
                context=f"{context_prefix} primary",
            )

        expected_candidates_contains = turn.get("expect_analyze_candidates_contains") or []
        if expected_candidates_contains:
            _assert_contains_all(
                actual=_analysis_candidate_codes(analysis),
                expected=[str(item) for item in expected_candidates_contains],
                context=f"{context_prefix} candidates",
            )

        expected_diagnostics_contains = turn.get("expect_analyze_diagnostics_contains") or []
        if expected_diagnostics_contains:
            _assert_contains_all(
                actual=_analysis_diagnostic_codes(analysis),
                expected=[str(item) for item in expected_diagnostics_contains],
                context=f"{context_prefix} diagnostics",
            )

        expected_slot_intent = turn.get("expect_analyze_slot_intent")
        expected_slot_memory = turn.get("expect_analyze_slot_memory") or {}
        expected_slot_absent = turn.get("expect_analyze_slot_absent") or []
        if expected_slot_intent or expected_slot_memory or expected_slot_absent:
            node = _analysis_slot_node(
                analysis,
                intent_code=str(expected_slot_intent) if expected_slot_intent else None,
            )
            if node is None:
                raise AssertionError(f"{context_prefix} slot_node: no matching slot node")
            if expected_slot_intent:
                _assert_equal(
                    actual=str(node.get("intent_code", "")),
                    expected=str(expected_slot_intent),
                    context=f"{context_prefix} slot_intent",
                )
            slot_memory = node.get("slot_memory") or {}
            if expected_slot_memory:
                _assert_mapping_contains(
                    actual=slot_memory,
                    expected={str(key): value for key, value in expected_slot_memory.items()},
                    context=f"{context_prefix} slot_memory",
                )
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
                analyze=analysis,
            )
        ) from exc

    return {
        "primary": _analysis_primary_codes(analysis),
        "candidates": _analysis_candidate_codes(analysis),
        "diagnostics": _analysis_diagnostic_codes(analysis),
        "slot_node_count": len(analysis.get("slot_nodes") or []),
    }


def _verify_execute(
    *,
    case: dict[str, Any],
    turn: dict[str, Any],
    turn_index: int,
    session_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Validate one execute response against the turn expectations."""
    context_prefix = f"{case.get('case_id')} turn {turn_index} execute"
    assistant_message = _last_assistant_message(snapshot)
    expected_node_intent = turn.get("expect_execute_node_intent")
    node = _graph_node(snapshot, intent_code=str(expected_node_intent) if expected_node_intent else None)
    try:
        expected_graph_status = turn.get("expect_execute_graph_status")
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
                context=f"{context_prefix} node_intent",
            )

        expected_node_status = turn.get("expect_execute_node_status")
        if expected_node_status is not None:
            _assert_equal(
                actual=str(node.get("status", "")),
                expected=str(expected_node_status),
                context=f"{context_prefix} node_status",
            )

        expected_prompt_contains = turn.get("expect_execute_prompt_contains") or []
        if expected_prompt_contains:
            _assert_text_contains(
                text=assistant_message,
                expected=[str(item) for item in expected_prompt_contains],
                context=f"{context_prefix} prompt_contains",
            )

        expected_prompt_excludes = turn.get("expect_execute_prompt_excludes") or []
        if expected_prompt_excludes:
            _assert_text_excludes(
                text=assistant_message,
                forbidden=[str(item) for item in expected_prompt_excludes],
                context=f"{context_prefix} prompt_excludes",
            )

        slot_memory = node.get("slot_memory") or {}
        expected_slot_memory = turn.get("expect_execute_slot_memory") or {}
        if expected_slot_memory:
            _assert_mapping_contains(
                actual=slot_memory,
                expected={str(key): value for key, value in expected_slot_memory.items()},
                context=f"{context_prefix} slot_memory",
            )

        expected_slot_absent = turn.get("expect_execute_slot_absent") or []
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
        "graph_status": _graph_status(snapshot),
        "node_intent": node.get("intent_code"),
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
    """Validate optional case-level final expectations after all turns finish."""
    node = _graph_node(
        final_snapshot,
        intent_code=str(case.get("expect_final_intent")) if case.get("expect_final_intent") else None,
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
            raise AssertionError(f"{context_prefix} node: no matching final graph node")

        expected_final_intent = case.get("expect_final_intent")
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
                context=f"{context_prefix} status",
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
    """Run one full multi-turn case and return a structured summary."""
    turns = case.get("turns") or []
    if not turns:
        raise AssertionError(f"case {case.get('case_id')} has no turns")

    cust_id = str(case.get("cust_id") or CUST_ID)
    session_id = client.create_session(cust_id=cust_id)
    final_snapshot: dict[str, Any] | None = None
    turn_results: list[dict[str, Any]] = []

    for turn_index, turn in enumerate(turns, start=1):
        content = str(turn.get("content") or "").strip()
        if not content:
            raise AssertionError(f"case {case.get('case_id')} turn {turn_index} has empty content")

        analyze_summary: dict[str, Any] | None = None
        if ANALYZE_BEFORE_EXECUTE and not bool(turn.get("skip_analyze")):
            analysis = client.analyze_message(
                session_id=session_id,
                cust_id=cust_id,
                content=content,
                analysis_mode=str(turn.get("analysis_mode") or DEFAULT_ANALYSIS_MODE),
            )
            analyze_summary = _verify_analyze(
                case=case,
                turn=turn,
                turn_index=turn_index,
                session_id=session_id,
                analysis=analysis,
            )

        final_snapshot = client.post_message(session_id=session_id, cust_id=cust_id, content=content)
        execute_summary = _verify_execute(
            case=case,
            turn=turn,
            turn_index=turn_index,
            session_id=session_id,
            snapshot=final_snapshot,
        )
        turn_results.append(
            {
                "turn_index": turn_index,
                "content": content,
                "analyze": analyze_summary,
                "execute": execute_summary,
            }
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
    """Run the full standard multi-turn suite and print one JSON summary."""
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
                "analyze_before_execute": ANALYZE_BEFORE_EXECUTE,
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
