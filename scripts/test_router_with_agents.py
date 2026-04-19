#!/usr/bin/env python3
"""Interactive and non-interactive router test with downstream agent execution."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4


BASE_URL = "http://127.0.0.1:8000"
CUST_ID = "cust_demo"


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Send one JSON request and return the decoded JSON response body."""
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8").strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body}") from exc


def _stream_request(
    url: str,
    payload: dict[str, Any],
    timeout: float = 60.0,
) -> None:
    """Send request and print SSE events as they arrive."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            event_name = ""
            data_lines: list[str] = []
            while True:
                line = response.readline()
                if not line:
                    break
                text = line.decode("utf-8").strip()
                if text == "":
                    if event_name and data_lines:
                        try:
                            data = json.loads("\n".join(data_lines))
                            _print_event(event_name, data)
                        except json.JSONDecodeError:
                            pass
                    event_name = ""
                    data_lines = []
                    continue
                if text.startswith("event:"):
                    event_name = text.split(":", 1)[1].strip()
                elif text.startswith("data:"):
                    data_lines.append(text.split(":", 1)[1].strip())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body}") from exc


def _print_event(event_name: str, data: dict[str, Any]) -> None:
    """Print a formatted SSE event."""
    # Skip verbose delta events
    if event_name == "recognition.delta":
        return

    status = data.get("status", "")
    message = data.get("message", "")
    intent_code = data.get("intent_code", "")
    ishandover = data.get("ishandover")

    # Key events with detailed output
    if event_name in ("recognition.completed", "node.completed", "graph.completed", "session.idle"):
        print(f"\n[{event_name}]")
        if intent_code:
            print(f"  intent: {intent_code}")
        if status:
            print(f"  status: {status}")
        if message:
            print(f"  message: {message}")
        if ishandover is not None:
            print(f"  ishandover: {ishandover}")
        # Print payload for node.completed
        if event_name == "node.completed":
            payload = data.get("payload", {})
            if payload:
                print(f"  payload: {json.dumps(payload, ensure_ascii=False)[:200]}")
    elif event_name == "node.running":
        print(f"\n[{event_name}] intent={intent_code}")
    elif event_name == "session.waiting_user_input":
        print(f"\n[{event_name}]")
        print(f"  message: {message}")


class RouterTestClient:
    """Client for testing router with agent execution."""

    def __init__(
        self,
        base_url: str,
        cust_id: str,
        timeout: float = 60.0,
        *,
        auto_confirm: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cust_id = cust_id
        self.timeout = timeout
        self.auto_confirm = auto_confirm
        self.session_id: str | None = None

    def create_session(self) -> str:
        """Create a new router session."""
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions",
            payload={"cust_id": self.cust_id},
            timeout=self.timeout,
        )
        self.session_id = payload["session_id"]
        return self.session_id

    def send_message(
        self,
        content: str,
        *,
        stream: bool = True,
        intent_code: str | None = None,
    ) -> dict[str, Any]:
        """Send a message and optionally stream events."""
        if not self.session_id:
            raise RuntimeError("Session not created. Call create_session() first.")

        payload: dict[str, Any] = {"content": content, "cust_id": self.cust_id}

        # If auto_confirm is enabled and we have intent_code, use guidedSelection
        if self.auto_confirm and intent_code:
            payload["guidedSelection"] = {
                "selectedIntents": [
                    {
                        "intentCode": intent_code,
                        "sourceFragment": content,
                        "slotMemory": {},
                    }
                ]
            }

        if stream:
            _stream_request(
                url=f"{self.base_url}/api/router/v2/sessions/{self.session_id}/messages/stream",
                payload=payload,
                timeout=self.timeout,
            )
            # Return snapshot after streaming
            result = _request_json(
                method="GET",
                url=f"{self.base_url}/api/router/v2/sessions/{self.session_id}",
                timeout=self.timeout,
            )
            return result
        else:
            result = _request_json(
                method="POST",
                url=f"{self.base_url}/api/router/v2/sessions/{self.session_id}/messages",
                payload=payload,
                timeout=self.timeout,
            )
            return result.get("snapshot", {})

    def confirm_and_execute(self, confirm_token: str) -> dict[str, Any]:
        """Confirm pending graph and execute."""
        if not self.session_id:
            raise RuntimeError("Session not created.")
        result = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions/{self.session_id}/actions",
            payload={
                "action_code": "confirm_graph",
                "confirm_token": confirm_token,
            },
            timeout=self.timeout,
        )
        return result.get("snapshot", {})

    def get_snapshot(self) -> dict[str, Any]:
        """Get current session snapshot."""
        if not self.session_id:
            raise RuntimeError("Session not created.")
        return _request_json(
            method="GET",
            url=f"{self.base_url}/api/router/v2/sessions/{self.session_id}",
            timeout=self.timeout,
        )


def run_interactive(client: RouterTestClient) -> int:
    """Run interactive REPL for testing."""
    print("=" * 60)
    print("Router Interactive Test")
    print("=" * 60)
    print(f"Session: {client.session_id}")
    print(f"Cust ID: {client.cust_id}")
    print("Type your message and press Enter. Type 'quit' or 'exit' to stop.")
    print("Commands: 'status' - show current session state")
    print("=" * 60)

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            return 0

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye!")
            return 0
        if user_input.lower() == "status":
            snapshot = client.get_snapshot()
            print(json.dumps(snapshot, ensure_ascii=False, indent=2)[:500])
            continue

        try:
            # First, send message to get pending graph
            snapshot = client.send_message(user_input, stream=False)

            # Check if we have a pending graph that needs confirmation
            pending_graph = snapshot.get("pending_graph")
            if pending_graph and pending_graph.get("status") == "waiting_confirmation":
                confirm_token = pending_graph.get("confirm_token")
                nodes = pending_graph.get("nodes", [])
                if nodes:
                    print(f"\n[Pending Graph] Intent: {nodes[0].get('intent_code')}")
                    print(f"  Title: {nodes[0].get('title')}")
                    print(f"  Confirm token: {confirm_token}")

                # Auto-confirm and execute
                print("\n[Auto-confirming and executing...]")
                client.confirm_and_execute(confirm_token)

                # Stream the execution events
                # The execution happens via the confirm action, so we need to check the result
                snapshot = client.get_snapshot()
                current_graph = snapshot.get("current_graph")
                if current_graph:
                    nodes = current_graph.get("nodes", [])
                    if nodes:
                        node = nodes[0]
                        print(f"\n[node.completed]")
                        print(f"  status: {node.get('status')}")
                        print(f"  message: {node.get('message')}")
                        print(f"  slot_memory: {node.get('slot_memory')}")
            else:
                # Check current_graph for multi-turn slot filling
                current_graph = snapshot.get("current_graph")
                if current_graph:
                    nodes = current_graph.get("nodes", [])
                    if nodes:
                        node = nodes[0]
                        print(f"\n[node status]")
                        print(f"  status: {node.get('status')}")
                        print(f"  message: {node.get('message')}")
                        print(f"  slot_memory: {node.get('slot_memory')}")

                # Check messages for assistant response
                messages = snapshot.get("messages", [])
                if messages:
                    last_msg = messages[-1]
                    if last_msg.get("role") == "assistant":
                        print(f"\n[assistant] {last_msg.get('content')}")

        except Exception as exc:
            print(f"Error: {exc}")

    return 0


def run_non_interactive(
    client: RouterTestClient,
    messages: list[str],
    *,
    stream: bool = True,
) -> int:
    """Run non-interactive test with predefined messages."""
    print("=" * 60)
    print("Router Non-Interactive Test")
    print("=" * 60)
    print(f"Session: {client.session_id}")
    print(f"Messages: {len(messages)}")
    print("=" * 60)

    for i, message in enumerate(messages, 1):
        print(f"\n[Message {i}] {message}")
        try:
            # Send message (non-streaming to get snapshot)
            snapshot = client.send_message(message, stream=False)

            # Check if we have a pending graph that needs confirmation (first message)
            pending_graph = snapshot.get("pending_graph")
            current_graph = snapshot.get("current_graph")

            if pending_graph and pending_graph.get("status") == "waiting_confirmation":
                confirm_token = pending_graph.get("confirm_token")
                nodes = pending_graph.get("nodes", [])
                if nodes:
                    print(f"\n[Pending Graph] Intent: {nodes[0].get('intent_code')}")
                    print(f"  Title: {nodes[0].get('title')}")

                # Auto-confirm and execute
                print("\n[Auto-confirming and executing...]")
                snapshot = client.confirm_and_execute(confirm_token)
                current_graph = snapshot.get("current_graph")

            # Print current graph state (for multi-turn slot filling)
            if current_graph:
                nodes = current_graph.get("nodes", [])
                if nodes:
                    node = nodes[0]
                    status = node.get("status")
                    print(f"\n[node status]")
                    print(f"  status: {status}")
                    print(f"  message: {node.get('message')}")
                    print(f"  slot_memory: {node.get('slot_memory')}")

                    if status == "completed":
                        # Node completed, show the result
                        pass
                    elif status == "waiting_user_input":
                        # Waiting for more slots
                        print(f"  waiting for: {node.get('message')}")

            # Check messages for assistant response
            messages_list = snapshot.get("messages", [])
            if messages_list:
                last_msg = messages_list[-1]
                if last_msg.get("role") == "assistant":
                    print(f"\n[assistant] {last_msg.get('content')}")

        except Exception as exc:
            print(f"Error: {exc}")
            return 1

    print("\n" + "=" * 60)
    print("Test completed.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test router with downstream agent execution.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode - type messages and see responses
  python scripts/test_router_with_agents.py -i

  # Single message with all info (executes immediately)
  python scripts/test_router_with_agents.py -m "帮我转账500块给张三"

  # Multi-turn conversation (slot filling)
  python scripts/test_router_with_agents.py -m "帮我转账" -m "给张三" -m "500块"

  # Specify custom base URL
  python scripts/test_router_with_agents.py -i --base-url http://localhost:8000

Flow:
  1. First message triggers intent recognition
  2. If pending_graph needs confirmation, auto-confirms and executes
  3. If node is waiting_user_input, subsequent messages fill slots
  4. When all slots filled, agent executes and returns result
""",
    )
    parser.add_argument(
        "--base-url",
        default=BASE_URL,
        help=f"Router base URL (default: {BASE_URL})",
    )
    parser.add_argument(
        "--cust-id",
        default=CUST_ID,
        help=f"Customer ID (default: {CUST_ID})",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Run in interactive mode (REPL)",
    )
    parser.add_argument(
        "-m",
        "--message",
        action="append",
        dest="messages",
        help="Message to send (can be specified multiple times)",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable SSE streaming (use synchronous API)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Request timeout in seconds (default: 60)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    client = RouterTestClient(
        base_url=args.base_url,
        cust_id=args.cust_id,
        timeout=args.timeout,
    )

    session_id = client.create_session()

    if args.interactive:
        return run_interactive(client)

    if args.messages:
        return run_non_interactive(
            client,
            args.messages,
            stream=not args.no_stream,
        )

    # Default: show help if no mode specified
    print("Error: Specify --interactive or --message")
    print("Use --help for usage information.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
