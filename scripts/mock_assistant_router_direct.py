#!/usr/bin/env python3
"""Send assistant-shaped requests directly to router `/api/v1/message`.

This script does not call assistant-service. It only builds the same payload
assistant would send to router and streams the result back to the terminal.

Examples:

    python scripts/mock_assistant_router_direct.py --txt "我要转账"

    python scripts/mock_assistant_router_direct.py \
      --session-id demo_transfer_001 \
      --txt "给小明转账"

    python scripts/mock_assistant_router_direct.py \
      --session-id demo_transfer_001 \
      --txt "200"
"""

from __future__ import annotations

import argparse
import json

try:
    from scripts.mock_assistant_router_stream import (
        DEFAULT_BASE_URL,
        DEFAULT_CURRENT_DISPLAY,
        DEFAULT_CUST_ID,
        DEFAULT_TIMEOUT_SECONDS,
        default_session_id,
        run_one_turn,
    )
except ImportError:
    from mock_assistant_router_stream import (
        DEFAULT_BASE_URL,
        DEFAULT_CURRENT_DISPLAY,
        DEFAULT_CUST_ID,
        DEFAULT_TIMEOUT_SECONDS,
        default_session_id,
        run_one_turn,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mock assistant-service and call router `/api/v1/message` directly."
    )
    parser.add_argument("--txt", required=True, help="User text sent to router.")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Router base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Assistant session id. Omit to auto-generate one.",
    )
    parser.add_argument(
        "--current-display",
        default=DEFAULT_CURRENT_DISPLAY,
        help=f"Assistant currentDisplay. Default: {DEFAULT_CURRENT_DISPLAY}",
    )
    parser.add_argument(
        "--cust-id",
        default=DEFAULT_CUST_ID,
        help=f"Customer id. Default: {DEFAULT_CUST_ID}",
    )
    parser.add_argument(
        "--execution-mode",
        default="execute",
        choices=("execute", "router_only"),
        help="Router execution mode.",
    )
    parser.add_argument(
        "--agent-session-id",
        default=None,
        help="Optional override for config_variables.agentSessionID.",
    )
    parser.add_argument(
        "--slots-data",
        default=None,
        help="Optional JSON string forwarded as config_variables.slots_data.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Request timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}",
    )
    parser.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        help="Use non-stream JSON mode.",
    )
    parser.add_argument(
        "--quiet-request",
        dest="print_request",
        action="store_false",
        help="Do not print the outgoing request payload.",
    )
    parser.add_argument(
        "--quiet-response",
        dest="print_response",
        action="store_false",
        help="Do not print streamed frames / JSON response.",
    )
    parser.set_defaults(stream=True, print_request=True, print_response=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    slots_data = json.loads(args.slots_data) if args.slots_data else None
    session_id = args.session_id or default_session_id()
    result = run_one_turn(
        session_id=session_id,
        txt=args.txt,
        current_display=args.current_display,
        base_url=args.base_url,
        cust_id=args.cust_id,
        execution_mode=args.execution_mode,
        agent_session_id=args.agent_session_id,
        stream=args.stream,
        timeout_seconds=args.timeout,
        slots_data=slots_data,
        print_request=args.print_request,
        print_response=args.print_response,
    )
    if not args.print_request:
        print(f"session_id={session_id}")
    if not args.print_response:
        if isinstance(result, list):
            print(f"frames={len(result)}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
