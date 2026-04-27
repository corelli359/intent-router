from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTER_V4_SRC = REPO_ROOT / "backend" / "services" / "router-v4-service" / "src"
if str(ROUTER_V4_SRC) not in sys.path:
    sys.path.insert(0, str(ROUTER_V4_SRC))

from router_v4_service.api.app import create_app  # noqa: E402
from router_v4_service.core.config import RouterV4LLMSettings, RouterV4Settings, load_env_file  # noqa: E402
from router_v4_service.core.context import ContextBuilder  # noqa: E402
from router_v4_service.core.models import ContextPolicy, RouterTurnStatus, RouterV4Input  # noqa: E402
from router_v4_service.core.recognizer import IntentCandidate, LLMIntentRecognizer  # noqa: E402
from router_v4_service.core.runtime import RouterV4Runtime  # noqa: E402
from router_v4_service.core.spec_registry import SpecRegistry  # noqa: E402
from router_v4_service.core.stores import FileRoutingSessionStore, FileTranscriptStore  # noqa: E402


class FakeIntentRecognizer:
    def __init__(self, decisions: dict[str, object] | None = None) -> None:
        self.decisions = decisions or {}

    def recognize(
        self,
        message: str,
        intents: list[object],
        *,
        limit: int = 3,
        push_context: dict[str, object] | None = None,
    ) -> list[IntentCandidate]:
        raw = self.decisions.get(message, self.decisions.get("*", []))
        items = raw if isinstance(raw, list) else [raw]
        intent_by_id = {getattr(intent, "intent_id"): intent for intent in intents}
        candidates: list[IntentCandidate] = []
        for item in items:
            if item in (None, "", []):
                continue
            if isinstance(item, str):
                intent_id = item
                hints: dict[str, object] = {}
                score = 90
                reasons = ("fake_llm",)
            elif isinstance(item, dict):
                intent_id = str(item.get("intent_id") or item.get("intent_code") or item.get("scene_id") or "")
                hints = dict(item.get("hints") or {})
                score = int(item.get("score") or 90)
                reasons = tuple(str(value) for value in item.get("reasons", ("fake_llm",)))
            else:
                continue
            intent = intent_by_id.get(intent_id)
            if intent is None:
                continue
            candidates.append(
                IntentCandidate(
                    intent=intent,
                    score=score,
                    reasons=reasons,
                    routing_hints=hints,
                )
            )
        return candidates[:limit]


def _runtime(decisions: dict[str, object] | None = None, **kwargs: object) -> RouterV4Runtime:
    return RouterV4Runtime(recognizer=FakeIntentRecognizer(decisions), **kwargs)


def _write_intent_catalog(
    spec_root: Path,
    *,
    intent_id: str = "transfer",
    scene_id: str = "transfer",
    target_agent: str = "transfer-agent",
    handoff_fields: list[str] | None = None,
) -> None:
    (spec_root / "agents").mkdir(parents=True, exist_ok=True)
    (spec_root / "skills").mkdir(parents=True, exist_ok=True)
    fields = handoff_fields or ["raw_message"]
    fields_literal = ", ".join(f'"{item}"' for item in fields)
    (spec_root / "intent.md").write_text(
        f"""
+++
[[intents]]
intent_id = "{intent_id}"
scene_id = "{scene_id}"
version = "0.1.0"
name = "{intent_id}"
description = "{intent_id} intent"
target_agent = "{target_agent}"
references = []
skill = {{ skill_id = "{intent_id}", version = "0.1.0", owner = "{target_agent}", path = "skills/{intent_id}.skill.md", description = "{intent_id} skill" }}
dispatch_contract = {{ task_type = "{scene_id}", handoff_fields = [{fields_literal}] }}
+++

# Intent Catalog

## {intent_id}

用户表达办理该业务时命中。
""",
        encoding="utf-8",
    )


def test_spec_registry_loads_default_intent_catalog_and_agents() -> None:
    registry = SpecRegistry()

    intents = registry.intent_index()
    transfer = registry.intent("transfer")
    agent = registry.agent("transfer-agent")

    assert [intent.intent_id for intent in intents] == ["balance_query", "fund_query", "transfer"]
    assert [agent.agent_id for agent in registry.agent_index()] == [
        "balance-agent",
        "fallback-agent",
        "fund-agent",
        "transfer-agent",
    ]
    assert transfer.target_agent == "transfer-agent"
    assert transfer.scene_id == "transfer"
    assert transfer.skill["path"] == "skills/transfer.skill.md"
    assert transfer.dispatch_contract.handoff_fields == ("raw_message", "user_profile_ref", "page_context_ref")
    assert agent.accepted_scene_ids == ("transfer",)
    assert registry.agent("fallback-agent").accepted_scene_ids == ("fallback",)


def test_runtime_dispatches_transfer_scene_without_router_business_slots() -> None:
    runtime = _runtime({"给张三转5000块": "transfer"})

    output = runtime.handle_turn(
        RouterV4Input(
            session_id="sess-transfer",
            message="给张三转5000块",
            user_profile={"user_id": "U001"},
            page_context={"current_page": "home"},
        )
    )

    assert output.status == RouterTurnStatus.DISPATCHED
    assert output.scene_id == "transfer"
    assert output.target_agent == "transfer-agent"
    assert output.agent_task_id
    assert output.response == "task_dispatched"
    assert output.routing_hints == {}
    assert any(event["type"] == "agent_dispatched" for event in output.events)
    assert "skill_reference" in output.prompt_report["included_blocks"]
    blocks = [item["block"] for item in output.prompt_report["load_trace"]]
    assert "intent_catalog" in blocks
    assert "recognized_intents" in blocks
    assert "skill_reference" in blocks
    assert "dispatch_contract" in blocks
    assert "skill_card" not in blocks
    intent_trace = next(item for item in output.prompt_report["load_trace"] if item["block"] == "intent_catalog")
    assert len(intent_trace["files"]) == 1
    assert intent_trace["files"][0]["path"].endswith("intent.md")
    dispatch_event = next(event for event in output.events if event["type"] == "agent_dispatched")
    assert dispatch_event["task_payload"]["intent_id"] == "transfer"
    assert dispatch_event["task_payload"]["scene_id"] == "transfer"
    assert dispatch_event["task_payload"]["routing_hints"] == {}
    assert dispatch_event["task_payload"]["skill_ref"]["skill_id"] == "transfer"
    assert "recipient" not in dispatch_event["task_payload"]
    assert "amount" not in dispatch_event["task_payload"]


def test_llm_intent_recognizer_calls_openai_compatible_endpoint() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "selected_intent_id": "transfer",
                                    "selected_intent_ids": [],
                                    "confidence": 0.93,
                                    "reason": "用户明确表达转账",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    registry = SpecRegistry()
    recognizer = LLMIntentRecognizer(
        RouterV4LLMSettings(
            api_base_url="https://llm.example/v1",
            api_key="test-key",
            model="test-model",
            temperature=0,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    candidates = recognizer.recognize("我要转账", registry.intent_index())

    assert candidates[0].intent.intent_id == "transfer"
    assert candidates[0].score == 93
    assert "llm" in candidates[0].reasons
    assert candidates[0].routing_hints == {}
    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["payload"]["model"] == "test-model"
    assert "intent.md 意图目录" in captured["payload"]["messages"][0]["content"]
    assert "markdown_spec" in captured["payload"]["messages"][1]["content"]
    assert "intents" in captured["payload"]["messages"][1]["content"]
    assert "给张三转5000块" in captured["payload"]["messages"][1]["content"]


def test_runtime_llm_backend_surfaces_missing_llm_config() -> None:
    runtime = RouterV4Runtime(settings=RouterV4Settings(recognizer_backend="llm"))

    output = runtime.handle_turn(RouterV4Input(session_id="sess-llm-missing", message="我要转账"))

    assert output.status == RouterTurnStatus.FAILED
    assert output.events[0]["type"] == "llm_recognition_failed"


def test_load_env_file_ignores_comments_without_shell_execution(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        """
# Router comment with spaces
ROUTER_V4_RECOGNIZER_BACKEND=llm
BROKEN LINE WITHOUT EQUALS
ROUTER_V4_LLM_MODEL=test-model
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("ROUTER_V4_RECOGNIZER_BACKEND", raising=False)
    monkeypatch.delenv("ROUTER_V4_LLM_MODEL", raising=False)

    try:
        load_env_file(env_file)

        assert RouterV4Settings.from_env().recognizer_backend == "llm"
        assert RouterV4Settings.from_env().llm.model == "test-model"
    finally:
        os.environ.pop("ROUTER_V4_RECOGNIZER_BACKEND", None)
        os.environ.pop("ROUTER_V4_LLM_MODEL", None)


def test_runtime_forwards_followup_to_existing_agent_task() -> None:
    runtime = _runtime({"给张三转5000块": "transfer"})
    first = runtime.handle_turn(
        RouterV4Input(session_id="sess-follow", message="给张三转5000块")
    )

    second = runtime.handle_turn(
        RouterV4Input(session_id="sess-follow", message="确认")
    )

    assert second.status == RouterTurnStatus.FORWARDED
    assert second.scene_id == "transfer"
    assert second.target_agent == "transfer-agent"
    assert second.agent_task_id == first.agent_task_id
    assert second.response == "message_forwarded_to_active_agent"


def test_runtime_returns_clarification_when_scene_is_unknown() -> None:
    runtime = _runtime({"今天天气怎么样": []})

    output = runtime.handle_turn(
        RouterV4Input(session_id="sess-unknown", message="今天天气怎么样")
    )

    assert output.status == RouterTurnStatus.CLARIFICATION_REQUIRED
    assert output.scene_id is None
    assert output.target_agent is None


def test_runtime_does_not_dispatch_when_agent_missing(tmp_path: Path) -> None:
    spec_root = tmp_path / "specs"
    (spec_root / "agents").mkdir(parents=True)
    _write_intent_catalog(spec_root, target_agent="missing-agent")
    (spec_root / "agents" / "agent-registry.md").write_text(
        """
+++
agents = []
+++

# Agent Registry
""",
        encoding="utf-8",
    )
    runtime = _runtime({"我要转账": "transfer"}, registry=SpecRegistry(spec_root))

    output = runtime.handle_turn(
        RouterV4Input(session_id="sess-missing-agent", message="我要转账")
    )

    assert output.status == RouterTurnStatus.FAILED
    assert output.target_agent == "missing-agent"
    assert "unknown agent" in output.response


def test_runtime_dispatches_without_router_business_fields_even_if_contract_names_them(tmp_path: Path) -> None:
    spec_root = tmp_path / "specs"
    (spec_root / "agents").mkdir(parents=True)
    _write_intent_catalog(spec_root, handoff_fields=["raw_message", "recipient", "amount"])
    (spec_root / "agents" / "agent-registry.md").write_text(
        """
+++
[[agents]]
agent_id = "transfer-agent"
endpoint = "mock://transfer-agent"
accepted_scene_ids = ["transfer"]
task_schema = "transfer.task.v1"
event_schema = "transfer.event.v1"
+++

# Agent Registry
""",
        encoding="utf-8",
    )
    runtime = _runtime(
        {
            "我要转账": "transfer",
        },
        registry=SpecRegistry(spec_root),
    )

    first = runtime.handle_turn(RouterV4Input(session_id="sess-pending", message="我要转账"))
    second = runtime.handle_turn(RouterV4Input(session_id="sess-pending", message="给张三转500元"))

    assert first.status == RouterTurnStatus.DISPATCHED
    assert first.action_required is None
    assert first.routing_hints == {}
    dispatch_event = next(event for event in first.events if event["type"] == "agent_dispatched")
    assert dispatch_event["task_payload"]["routing_hints"] == {}
    assert "recipient" not in dispatch_event["task_payload"]
    assert "amount" not in dispatch_event["task_payload"]
    assert second.status == RouterTurnStatus.FORWARDED
    assert second.agent_task_id == first.agent_task_id


def test_runtime_can_persist_session_state_between_instances(tmp_path: Path) -> None:
    state_dir = tmp_path / "router-state"
    recognizer = FakeIntentRecognizer(
        {"给张三转5000块": "transfer"}
    )
    first_runtime = RouterV4Runtime(
        recognizer=recognizer,
        session_store=FileRoutingSessionStore(state_dir),
        transcript_store=FileTranscriptStore(state_dir),
    )
    first = first_runtime.handle_turn(RouterV4Input(session_id="sess-persist", message="给张三转5000块"))

    second_runtime = RouterV4Runtime(
        recognizer=recognizer,
        session_store=FileRoutingSessionStore(state_dir),
        transcript_store=FileTranscriptStore(state_dir),
    )
    second = second_runtime.handle_turn(RouterV4Input(session_id="sess-persist", message="确认"))

    assert first.status == RouterTurnStatus.DISPATCHED
    assert second.status == RouterTurnStatus.FORWARDED
    assert second.agent_task_id == first.agent_task_id
    assert second.prompt_report["lifecycle"]["state_reused"] is True
    assert len(second_runtime.session_snapshot("sess-persist")["transcript"]) >= 3


def test_context_report_applies_budget_and_keeps_core_blocks() -> None:
    runtime = _runtime(
        {"给张三转5000块": "transfer"},
        context_builder=ContextBuilder(
            ContextPolicy(max_chars=700, recent_turn_limit=2, retrieved_reference_limit=1)
        ),
    )

    output = runtime.handle_turn(RouterV4Input(session_id="sess-context", message="给张三转5000块"))

    assert output.status == RouterTurnStatus.DISPATCHED
    assert output.prompt_report["max_chars"] == 700
    assert output.prompt_report["dropped_blocks"]
    assert output.prompt_report["included_blocks"][:3] == ["router_boundary", "routing_state", "intent_catalog"]


def test_push_context_generic_acceptance_dispatches_first_recommended_scene() -> None:
    runtime = _runtime({"就按这个办": {"scene_id": "fund_query", "reasons": ("llm_push_selected",)}})

    output = runtime.handle_turn(
        RouterV4Input(
            session_id="sess-push-generic",
            message="就按这个办",
            source="assistant_push",
            push_context={
                "push_id": "push-001",
                "intents": [
                    {"scene_id": "fund_query", "rank": 1},
                    {"scene_id": "balance_query", "rank": 2},
                ],
            },
        )
    )

    assert output.status == RouterTurnStatus.DISPATCHED
    assert output.scene_id == "fund_query"
    assert output.target_agent == "fund-agent"
    assert output.action_required is None
    assert output.events[0]["reasons"] == ["llm_push_selected"]
    assert output.tasks[0]["push_context"]["push_id"] == "push-001"


def test_push_context_rejection_returns_no_action_without_dispatch() -> None:
    runtime = _runtime({"不用了": []})

    output = runtime.handle_turn(
        RouterV4Input(
            session_id="sess-push-reject",
            message="不用了",
            source="assistant_push",
            push_context={"intents": [{"scene_id": "fund_query", "rank": 1}]},
        )
    )

    assert output.status == RouterTurnStatus.NO_ACTION
    assert output.tasks == ()
    snapshot = runtime.session_snapshot("sess-push-reject")
    assert snapshot["session"]["dispatch_status"] == "no_action"


def test_push_context_multi_intent_returns_split_task_plan() -> None:
    runtime = _runtime(
        {
            "都看一下": [
                {"scene_id": "balance_query", "score": 95, "reasons": ("llm_multi_intent",)},
                {"scene_id": "fund_query", "score": 94, "reasons": ("llm_multi_intent",)},
            ]
        }
    )

    output = runtime.handle_turn(
        RouterV4Input(
            session_id="sess-push-plan",
            message="都看一下",
            source="assistant_push",
            push_context={
                "intents": [
                    {"scene_id": "balance_query", "rank": 1},
                    {"scene_id": "fund_query", "rank": 2},
                ]
            },
        )
    )

    assert output.status == RouterTurnStatus.PLANNED
    assert output.graph_id
    assert output.stream_mode == "split_by_task"
    assert [task["scene_id"] for task in output.tasks] == ["balance_query", "fund_query"]
    assert all(task["stream_url"].startswith("/api/router/v4/streams/") for task in output.tasks)
    graph = runtime.graph_snapshot("sess-push-plan", output.graph_id)
    assert graph["found"] is True
    assert graph["graph"]["status"] == "running"


def test_agent_completed_output_is_preserved_for_assistant_generation() -> None:
    runtime = _runtime({"查一下余额": "balance_query"})
    dispatched = runtime.handle_turn(RouterV4Input(session_id="sess-agent-output", message="查一下余额"))

    updated = runtime.handle_agent_output(
        session_id="sess-agent-output",
        task_id=dispatched.task_id or "",
        agent_payload={
            "status": "completed",
            "output": {"data": [{"type": "balance", "currency": "CNY", "amount": "1000.00"}]},
        },
    )

    assert updated.status == RouterTurnStatus.TASK_UPDATED
    assert updated.response == "agent_output_recorded"
    assert updated.agent_output == {"data": [{"type": "balance", "currency": "CNY", "amount": "1000.00"}]}
    snapshot = runtime.session_snapshot("sess-agent-output")
    assert snapshot["session"]["assistant_result_status"] == "ready_for_assistant"
    assert snapshot["session"]["agent_outputs"][dispatched.task_id or ""]["data"][0]["amount"] == "1000.00"


def test_agent_handover_protocol_dispatches_fallback_agent_once() -> None:
    runtime = _runtime({"基金": "fund_query"})
    dispatched = runtime.handle_turn(RouterV4Input(session_id="sess-handover", message="基金"))

    updated = runtime.handle_agent_output(
        session_id="sess-handover",
        task_id=dispatched.task_id or "",
        agent_payload={"ishandover": True, "output": {"data": []}},
    )

    assert updated.status == RouterTurnStatus.TASK_UPDATED
    assert updated.response == "fallback_dispatched"
    assert updated.target_agent == "fallback-agent"
    assert [event["type"] for event in updated.events] == [
        "task.handover_requested",
        "task.fallback_dispatched",
    ]
    snapshot = runtime.session_snapshot("sess-handover")
    original = snapshot["tasks"][dispatched.task_id or ""]
    fallback = snapshot["tasks"][updated.task_id or ""]
    assert original["status"] == "handover_requested"
    assert original["fallback_task_id"] == updated.task_id
    assert fallback["status"] == "fallback_dispatched"
    assert fallback["original_task_id"] == dispatched.task_id


def test_agent_camel_case_handover_is_not_accepted() -> None:
    runtime = _runtime({"基金": "fund_query"})
    dispatched = runtime.handle_turn(RouterV4Input(session_id="sess-camel-handover", message="基金"))

    updated = runtime.handle_agent_output(
        session_id="sess-camel-handover",
        task_id=dispatched.task_id or "",
        agent_payload={"isHandover": True, "output": {"data": []}},
    )

    assert updated.status == RouterTurnStatus.TASK_UPDATED
    assert updated.response == "agent_output_abnormal"
    assert updated.target_agent == "fund-agent"
    snapshot = runtime.session_snapshot("sess-camel-handover")
    assert len(snapshot["tasks"]) == 1
    assert snapshot["tasks"][dispatched.task_id or ""]["status"] == "failed"


def test_fallback_handover_does_not_loop() -> None:
    runtime = _runtime({"基金": "fund_query"})
    dispatched = runtime.handle_turn(RouterV4Input(session_id="sess-no-loop", message="基金"))
    fallback = runtime.handle_agent_output(
        session_id="sess-no-loop",
        task_id=dispatched.task_id or "",
        agent_payload={"ishandover": True, "output": {"data": []}},
    )

    exhausted = runtime.handle_agent_output(
        session_id="sess-no-loop",
        task_id=fallback.task_id or "",
        agent_payload={"ishandover": True, "output": {"data": []}},
    )

    assert exhausted.response == "handover_exhausted"
    snapshot = runtime.session_snapshot("sess-no-loop")
    assert len(snapshot["tasks"]) == 2
    assert snapshot["tasks"][fallback.task_id or ""]["status"] == "handover_exhausted"


def test_api_message_endpoint_dispatches_scene() -> None:
    app = create_app(runtime=_runtime({"查一下余额": "balance_query"}))

    with TestClient(app) as client:
        response = client.post(
            "/api/router/v4/message",
            json={
                "session_id": "sess-api",
                "message": "查一下余额",
                "user_profile": {"user_id": "U001"},
                "page_context": {"current_page": "home"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "dispatched"
    assert payload["scene_id"] == "balance_query"
    assert payload["target_agent"] == "balance-agent"


def test_api_allows_router_v4_observer_frontend_origin() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.options(
            "/api/router/v4/message",
            headers={
                "origin": "http://localhost:3010",
                "access-control-request-method": "POST",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3010"


def test_api_session_snapshot_exposes_router_owned_state() -> None:
    app = create_app(
        runtime=_runtime({"给张三转5000块": "transfer"})
    )

    with TestClient(app) as client:
        client.post(
            "/api/router/v4/message",
            json={"session_id": "sess-snapshot", "message": "给张三转5000块"},
        )
        response = client.get("/api/router/v4/sessions/sess-snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["active_scene_id"] == "transfer"
    assert payload["session"]["dispatch_status"] == "dispatched"
    assert payload["session"]["routing_hints"] == {}
    assert payload["transcript"]


def test_api_agent_output_records_structured_result_and_task_snapshot() -> None:
    app = create_app(runtime=_runtime({"查一下余额": "balance_query"}))

    with TestClient(app) as client:
        first = client.post(
            "/api/router/v4/message",
            json={"session_id": "sess-api-agent-output", "message": "查一下余额"},
        ).json()
        updated = client.post(
            "/api/router/v4/agent-output",
            json={
                "session_id": "sess-api-agent-output",
                "task_id": first["task_id"],
                "status": "completed",
                "output": {"data": [{"type": "balance", "amount": "1000.00"}]},
            },
        )
        snapshot = client.get(
            f"/api/router/v4/sessions/sess-api-agent-output/tasks/{first['task_id']}"
        )

    assert updated.status_code == 200
    assert updated.json()["agent_output"]["data"][0]["amount"] == "1000.00"
    assert snapshot.status_code == 200
    assert snapshot.json()["task"]["status"] == "completed"
