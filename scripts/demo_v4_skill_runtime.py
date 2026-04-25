#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER_SRC = REPO_ROOT / "backend" / "services" / "router-service" / "src"
if str(ROUTER_SRC) not in sys.path:
    sys.path.insert(0, str(ROUTER_SRC))

from router_service.core.skill_runtime.models import SkillRuntimeInput  # noqa: E402
from router_service.core.skill_runtime.runtime import SkillRuntimeController  # noqa: E402


DEFAULT_USER_PROFILE = {
    "user_id": "U001",
    "account_type": "I类",
    "risk_level": "C3",
    "available_balance": 50000,
}
DEFAULT_BUSINESS_APIS = {
    "risk_check": "mock://risk/check",
    "transfer": "mock://transfer",
}


def run_messages(runtime: SkillRuntimeController, session_id: str, messages: list[str]) -> int:
    for message in messages:
        print(f"> user: {message}")
        output = runtime.handle(
            SkillRuntimeInput(
                session_id=session_id,
                message=message,
                user_profile=dict(DEFAULT_USER_PROFILE),
                page_context={"current_page": "首页"},
                business_apis=dict(DEFAULT_BUSINESS_APIS),
            )
        )
        print(json.dumps(output.to_dict(), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the v4 markdown Skill runtime demo")
    parser.add_argument("--session-id", default=f"v4-demo-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--message", action="append", help="Run one or more user messages")
    parser.add_argument("--demo", choices=["transfer", "confirmation", "insufficient-balance"])
    args = parser.parse_args(argv)

    runtime = SkillRuntimeController()
    if args.demo == "transfer":
        return run_messages(runtime, args.session_id, ["帮我给张三转账", "500", "确认"])
    if args.demo == "confirmation":
        return run_messages(runtime, args.session_id, ["给李四转2000块", "确认"])
    if args.demo == "insufficient-balance":
        runtime_input = SkillRuntimeInput(
            session_id=args.session_id,
            message="给王五转60000元",
            user_profile=dict(DEFAULT_USER_PROFILE),
            page_context={"current_page": "首页"},
            business_apis=dict(DEFAULT_BUSINESS_APIS),
        )
        print(json.dumps(runtime.handle(runtime_input).to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.message:
        return run_messages(runtime, args.session_id, args.message)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
