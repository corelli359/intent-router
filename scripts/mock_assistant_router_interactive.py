#!/usr/bin/env python3
"""Interactive mock assistant shell that talks to router directly.

This script plays the role of assistant-service and sends the assistant ->
router payload directly to:

    POST /api/v1/message

It keeps one conversation state in memory so you can do multi-turn testing by
reusing the same session id.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

try:
    from mock_assistant_router_stream import (
        DEFAULT_BASE_URL,
        DEFAULT_CURRENT_DISPLAY,
        DEFAULT_CUST_ID,
        DEFAULT_TIMEOUT_SECONDS,
        default_session_id,
        run_one_turn,
    )
except ImportError:
    from scripts.mock_assistant_router_stream import (
        DEFAULT_BASE_URL,
        DEFAULT_CURRENT_DISPLAY,
        DEFAULT_CUST_ID,
        DEFAULT_TIMEOUT_SECONDS,
        default_session_id,
        run_one_turn,
    )


@dataclass
class InteractiveState:
    base_url: str = DEFAULT_BASE_URL
    session_id: str = default_session_id()
    current_display: str = DEFAULT_CURRENT_DISPLAY
    cust_id: str = DEFAULT_CUST_ID
    execution_mode: str = "execute"
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    stream: bool = True
    slots_data: dict[str, Any] | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive assistant-like router test shell.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Router base URL. Default: {DEFAULT_BASE_URL}")
    parser.add_argument("--session-id", default=default_session_id(), help="Conversation session id.")
    parser.add_argument(
        "--current-display",
        default=DEFAULT_CURRENT_DISPLAY,
        help=f"Assistant currentDisplay. Default: {DEFAULT_CURRENT_DISPLAY}",
    )
    parser.add_argument("--cust-id", default=DEFAULT_CUST_ID, help=f"custId. Default: {DEFAULT_CUST_ID}")
    parser.add_argument(
        "--execution-mode",
        default="execute",
        choices=("execute", "router_only"),
        help="Assistant executionMode.",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout in seconds.")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="Use non-stream JSON mode.")
    parser.set_defaults(stream=True)
    return parser.parse_args()


def print_help() -> None:
    print("可用命令：")
    print("  /help                    查看帮助")
    print("  /show                    查看当前会话配置")
    print("  /quit                    退出")
    print("  /session <id>            切换 session_id")
    print("  /display <name>          切换 currentDisplay")
    print("  /mode execute|router_only  切换 executionMode")
    print("  /stream on|off           切换 stream")
    print("  /cust <id>               切换 custId")
    print("  /slots <json>            设置 slots_data")
    print("  /clear-slots             清空 slots_data")
    print("直接输入其它文本：按当前配置发一轮消息")


def print_state(state: InteractiveState) -> None:
    print("=== current state ===")
    print(f"base_url:        {state.base_url}")
    print(f"session_id:      {state.session_id}")
    print(f"current_display: {state.current_display}")
    print(f"cust_id:         {state.cust_id}")
    print(f"execution_mode:  {state.execution_mode}")
    print(f"stream:          {state.stream}")
    print(f"timeout_seconds: {state.timeout_seconds}")
    print(f"slots_data:      {json.dumps(state.slots_data, ensure_ascii=False) if state.slots_data else '{}'}")
    print()


def handle_command(state: InteractiveState, raw_text: str) -> bool:
    parts = raw_text.strip().split(maxsplit=1)
    command = parts[0]
    argument = parts[1] if len(parts) > 1 else ""

    if command in {"/quit", "/exit"}:
        raise EOFError
    if command == "/help":
        print_help()
        return True
    if command == "/show":
        print_state(state)
        return True
    if command == "/session":
        state.session_id = argument.strip() or default_session_id()
        print(f"session_id -> {state.session_id}")
        return True
    if command == "/display":
        if not argument.strip():
            print("display 不能为空")
            return True
        state.current_display = argument.strip()
        print(f"current_display -> {state.current_display}")
        return True
    if command == "/mode":
        if argument.strip() not in {"execute", "router_only"}:
            print("mode 只能是 execute 或 router_only")
            return True
        state.execution_mode = argument.strip()
        print(f"execution_mode -> {state.execution_mode}")
        return True
    if command == "/stream":
        lowered = argument.strip().lower()
        if lowered not in {"on", "off"}:
            print("stream 只能是 on 或 off")
            return True
        state.stream = lowered == "on"
        print(f"stream -> {state.stream}")
        return True
    if command == "/cust":
        if not argument.strip():
            print("custId 不能为空")
            return True
        state.cust_id = argument.strip()
        print(f"cust_id -> {state.cust_id}")
        return True
    if command == "/slots":
        if not argument.strip():
            print("请提供 JSON，例如 /slots {\"amount\":\"200\"}")
            return True
        try:
            parsed = json.loads(argument)
        except json.JSONDecodeError as exc:
            print(f"slots_data JSON 解析失败: {exc}")
            return True
        if not isinstance(parsed, dict):
            print("slots_data 必须是 JSON object")
            return True
        state.slots_data = parsed
        print(f"slots_data -> {json.dumps(state.slots_data, ensure_ascii=False)}")
        return True
    if command == "/clear-slots":
        state.slots_data = None
        print("slots_data 已清空")
        return True
    return False


def interactive_loop(state: InteractiveState) -> int:
    print("进入交互模式。输入 /help 查看命令。")
    print_state(state)
    while True:
        try:
            raw_text = input(f"[{state.session_id} | {state.current_display}]> ").strip()
        except KeyboardInterrupt:
            print("\n已中断")
            return 130
        except EOFError:
            print("\n已退出")
            return 0

        if not raw_text:
            continue

        try:
            if raw_text.startswith("/") and handle_command(state, raw_text):
                continue
        except EOFError:
            print("已退出")
            return 0

        try:
            run_one_turn(
                session_id=state.session_id,
                txt=raw_text,
                current_display=state.current_display,
                base_url=state.base_url,
                cust_id=state.cust_id,
                execution_mode=state.execution_mode,
                stream=state.stream,
                timeout_seconds=state.timeout_seconds,
                slots_data=state.slots_data,
                print_request=False,
                print_response=True,
            )
        except Exception as exc:
            print(f"请求失败: {exc}")
        print()


def main() -> int:
    args = parse_args()
    state = InteractiveState(
        base_url=args.base_url,
        session_id=args.session_id,
        current_display=args.current_display,
        cust_id=args.cust_id,
        execution_mode=args.execution_mode,
        timeout_seconds=args.timeout,
        stream=args.stream,
    )
    return interactive_loop(state)


if __name__ == "__main__":
    raise SystemExit(main())
