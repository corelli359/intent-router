from __future__ import annotations

from router_service.core.skill_runtime.models import SkillRuntimeInput
from router_service.core.skill_runtime.runtime import SkillRuntimeController
from router_service.core.skill_runtime.skill_loader import SkillSpecLoader


def _request(session_id: str, message: str, *, balance: int = 50000) -> SkillRuntimeInput:
    return SkillRuntimeInput(
        session_id=session_id,
        message=message,
        user_profile={"user_id": "U001", "available_balance": balance},
        page_context={"current_page": "首页"},
        business_apis={"risk_check": "mock://risk/check", "transfer": "mock://transfer"},
    )


def test_default_skill_loader_reads_transfer_skill_and_references() -> None:
    loader = SkillSpecLoader()

    index = loader.load_skill_index()
    transfer = loader.load_skill("transfer")
    reference = loader.read_reference("references/risk_rules.md")

    assert [item.skill_id for item in index] == ["transfer"]
    assert transfer.allowed_capabilities == ("risk_check", "transfer")
    assert [slot.name for slot in transfer.slots] == ["recipient", "amount"]
    assert reference.path == "skills/references/risk_rules.md"


def test_transfer_multiturn_collects_slots_confirms_and_executes() -> None:
    runtime = SkillRuntimeController()

    first = runtime.handle(_request("s-transfer", "帮我给张三转账"))
    assert first.status == "waiting_user_input"
    assert first.skill == "transfer"
    assert first.action_required == {"type": "input", "slot": "amount"}
    assert first.slots["recipient"] == "张三"

    second = runtime.handle(_request("s-transfer", "500"))
    assert second.status == "waiting_confirmation"
    assert second.skill_step == 3
    assert second.action_required == {"type": "confirm", "data": {"recipient": "张三", "amount": 500}}
    assert any(item.tool == "api_call" and item.args["capability"] == "risk_check" for item in second.tool_calls_log)

    third = runtime.handle(_request("s-transfer", "确认"))
    assert third.status == "completed"
    assert third.skill_step == 5
    assert "已成功向张三转账500元" in third.response
    assert "TXN-" in third.response
    assert any(item.tool == "api_call" and item.args["capability"] == "transfer" for item in third.tool_calls_log)


def test_transfer_stops_when_risk_check_rejects_balance() -> None:
    runtime = SkillRuntimeController()

    output = runtime.handle(_request("s-risk", "给李四转60000元", balance=1000))

    assert output.status == "failed"
    assert output.skill_step == 2
    assert output.response == "余额不足，当前可用余额为1000元，请调整转账金额。"


def test_runtime_rejects_ungranted_api_capability() -> None:
    runtime = SkillRuntimeController()
    request = SkillRuntimeInput(
        session_id="s-capability",
        message="给李四转200元",
        user_profile={"available_balance": 1000},
        page_context={},
        business_apis={"transfer": "mock://transfer"},
    )

    output = runtime.handle(request)

    assert output.status == "failed"
    assert output.skill_step == 2
    assert output.response == "capability endpoint is not provided: risk_check"
