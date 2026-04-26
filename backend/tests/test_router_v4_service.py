from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTER_V4_SRC = REPO_ROOT / "backend" / "services" / "router-v4-service" / "src"
if str(ROUTER_V4_SRC) not in sys.path:
    sys.path.insert(0, str(ROUTER_V4_SRC))

from router_v4_service.api.app import create_app  # noqa: E402
from router_v4_service.core.context import ContextBuilder  # noqa: E402
from router_v4_service.core.models import ContextPolicy, RouterTurnStatus, RouterV4Input  # noqa: E402
from router_v4_service.core.runtime import RouterV4Runtime  # noqa: E402
from router_v4_service.core.spec_registry import SpecRegistry  # noqa: E402
from router_v4_service.core.stores import FileRoutingSessionStore, FileTranscriptStore  # noqa: E402


def test_spec_registry_loads_default_scenes_and_agents() -> None:
    registry = SpecRegistry()

    scenes = registry.scene_index()
    transfer = registry.scene("transfer")
    agent = registry.agent("transfer-agent")

    assert [scene.scene_id for scene in scenes] == ["balance_query", "fund_query", "transfer"]
    assert transfer.target_agent == "transfer-agent"
    assert [slot.name for slot in transfer.routing_slots] == ["recipient", "amount"]
    assert agent.accepted_scene_ids == ("transfer",)


def test_runtime_dispatches_transfer_scene_with_routing_slots() -> None:
    runtime = RouterV4Runtime()

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
    assert output.routing_slots == {"recipient": "张三", "amount": 5000}
    assert any(event["type"] == "agent_dispatched" for event in output.events)
    assert "routing_spec" in output.prompt_report["included_blocks"]


def test_runtime_forwards_followup_to_existing_agent_task() -> None:
    runtime = RouterV4Runtime()
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
    assert "继续处理" in second.response


def test_runtime_returns_clarification_when_scene_is_unknown() -> None:
    runtime = RouterV4Runtime()

    output = runtime.handle_turn(
        RouterV4Input(session_id="sess-unknown", message="今天天气怎么样")
    )

    assert output.status == RouterTurnStatus.CLARIFICATION_REQUIRED
    assert output.scene_id is None
    assert output.target_agent is None


def test_runtime_does_not_dispatch_when_agent_missing(tmp_path: Path) -> None:
    spec_root = tmp_path / "specs"
    (spec_root / "scenes").mkdir(parents=True)
    (spec_root / "agents").mkdir(parents=True)
    (spec_root / "scenes" / "transfer.routing.json").write_text(
        """
{
  "scene_id": "transfer",
  "version": "0.1.0",
  "name": "转账",
  "description": "转账场景",
  "target_agent": "missing-agent",
  "triggers": {"keywords": ["转账"]},
  "routing_slots": [],
  "dispatch_contract": {"task_type": "transfer", "handoff_fields": ["raw_message"]},
  "references": []
}
""",
        encoding="utf-8",
    )
    (spec_root / "agents" / "agent-registry.json").write_text(
        '{"agents": []}',
        encoding="utf-8",
    )
    runtime = RouterV4Runtime(registry=SpecRegistry(spec_root))

    output = runtime.handle_turn(
        RouterV4Input(session_id="sess-missing-agent", message="我要转账")
    )

    assert output.status == RouterTurnStatus.FAILED
    assert output.target_agent == "missing-agent"
    assert "unknown agent" in output.response


def test_runtime_keeps_pending_scene_and_collects_required_slots(tmp_path: Path) -> None:
    spec_root = tmp_path / "specs"
    (spec_root / "scenes").mkdir(parents=True)
    (spec_root / "agents").mkdir(parents=True)
    (spec_root / "scenes" / "transfer.routing.json").write_text(
        """
{
  "scene_id": "transfer",
  "version": "0.1.0",
  "name": "转账",
  "description": "转账场景",
  "target_agent": "transfer-agent",
  "triggers": {"keywords": ["转账"], "examples": [], "negative_keywords": [], "negative_examples": []},
  "routing_slots": [
    {
      "name": "recipient",
      "source": "user_utterance",
      "required_for_dispatch": true,
      "handoff": true,
      "extractor": {"type": "after_terms", "terms": ["给"], "stop_terms": ["转"], "max_chars": 16}
    },
    {
      "name": "amount",
      "source": "user_utterance",
      "required_for_dispatch": true,
      "handoff": true,
      "extractor": {"type": "number"}
    }
  ],
  "dispatch_contract": {"task_type": "transfer", "handoff_fields": ["raw_message", "recipient", "amount"]},
  "references": []
}
""",
        encoding="utf-8",
    )
    (spec_root / "agents" / "agent-registry.json").write_text(
        """
{
  "agents": [
    {
      "agent_id": "transfer-agent",
      "endpoint": "mock://transfer-agent",
      "accepted_scene_ids": ["transfer"],
      "task_schema": "transfer.task.v1",
      "event_schema": "transfer.event.v1"
    }
  ]
}
""",
        encoding="utf-8",
    )
    runtime = RouterV4Runtime(registry=SpecRegistry(spec_root))

    first = runtime.handle_turn(RouterV4Input(session_id="sess-pending", message="我要转账"))
    second = runtime.handle_turn(RouterV4Input(session_id="sess-pending", message="给张三转500元"))

    assert first.status == RouterTurnStatus.CLARIFICATION_REQUIRED
    assert first.action_required == {"type": "input", "slot": "recipient", "owner": "router"}
    assert second.status == RouterTurnStatus.DISPATCHED
    assert second.routing_slots == {"recipient": "张三", "amount": 500}
    assert second.events[0]["reasons"] == ["pending_scene"]


def test_runtime_can_persist_session_state_between_instances(tmp_path: Path) -> None:
    state_dir = tmp_path / "router-state"
    first_runtime = RouterV4Runtime(
        session_store=FileRoutingSessionStore(state_dir),
        transcript_store=FileTranscriptStore(state_dir),
    )
    first = first_runtime.handle_turn(RouterV4Input(session_id="sess-persist", message="给张三转5000块"))

    second_runtime = RouterV4Runtime(
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
    runtime = RouterV4Runtime(
        context_builder=ContextBuilder(
            ContextPolicy(max_chars=700, recent_turn_limit=2, retrieved_reference_limit=1)
        )
    )

    output = runtime.handle_turn(RouterV4Input(session_id="sess-context", message="给张三转5000块"))

    assert output.status == RouterTurnStatus.DISPATCHED
    assert output.prompt_report["max_chars"] == 700
    assert output.prompt_report["dropped_blocks"]
    assert output.prompt_report["included_blocks"][:3] == ["agent_rules", "routing_state", "scene_index"]


def test_api_message_endpoint_dispatches_scene() -> None:
    app = create_app()

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


def test_api_session_snapshot_exposes_router_owned_state() -> None:
    app = create_app()

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
    assert payload["transcript"]
