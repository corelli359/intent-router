#!/usr/bin/env python3
"""Run a direct ladder load test against the live router HTTP API."""

from __future__ import annotations

import argparse
import asyncio
import json
from statistics import mean
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


async def _sample_process_stats(pid: int, interval_seconds: float, stop_event: asyncio.Event) -> dict[str, float | None]:
    peak_cpu_percent: float | None = None
    peak_rss_mb: float | None = None
    while not stop_event.is_set():
        process = await asyncio.create_subprocess_exec(
            "ps",
            "-p",
            str(pid),
            "-o",
            "%cpu=",
            "-o",
            "rss=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        raw = stdout.decode("utf-8").strip().split()
        if len(raw) >= 2:
            cpu_percent = float(raw[0])
            rss_mb = float(raw[1]) / 1024
            peak_cpu_percent = cpu_percent if peak_cpu_percent is None else max(peak_cpu_percent, cpu_percent)
            peak_rss_mb = rss_mb if peak_rss_mb is None else max(peak_rss_mb, rss_mb)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue
    return {
        "peak_cpu_percent": peak_cpu_percent,
        "peak_rss_mb": peak_rss_mb,
    }


async def _create_session(
    client: httpx.AsyncClient,
    *,
    session_id: str | None = None,
) -> tuple[bool, str | None, str | None]:
    payload: dict[str, str] = {}
    if session_id is not None:
        payload["session_id"] = session_id
    response = await client.post("/api/router/v2/sessions", json=payload)
    if response.status_code != 201:
        return False, None, f"create_session:{response.status_code}"
    created_session_id = response.json().get("session_id")
    if not isinstance(created_session_id, str) or not created_session_id:
        return False, None, "create_session:missing_session_id"
    return True, created_session_id, None


async def _post_message(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    content: str,
    execution_mode: str,
) -> tuple[bool, str | None]:
    message_response = await client.post(
        f"/api/router/v2/sessions/{session_id}/messages",
        json={
            "content": content,
            "executionMode": execution_mode,
        },
    )
    if message_response.status_code != 200:
        return False, f"message:{message_response.status_code}"
    snapshot = message_response.json().get("snapshot") or {}
    current_graph = snapshot.get("current_graph")
    if not isinstance(current_graph, dict):
        return False, "missing_current_graph"
    if current_graph.get("status") != "ready_for_dispatch":
        return False, f"graph_status:{current_graph.get('status')}"
    return True, None


def _new_perf_session_id() -> str:
    return f"perf_{uuid4().hex[:16]}"


async def _exercise_create_then_message(
    client: httpx.AsyncClient,
    *,
    content: str,
    execution_mode: str,
) -> tuple[bool, float, str | None]:
    started = perf_counter()
    try:
        created, session_id, error_key = await _create_session(client)
        if not created or session_id is None:
            return False, (perf_counter() - started) * 1000, error_key
        ok, error_key = await _post_message(
            client,
            session_id=session_id,
            content=content,
            execution_mode=execution_mode,
        )
        return ok, (perf_counter() - started) * 1000, error_key
    except Exception as exc:
        return False, (perf_counter() - started) * 1000, type(exc).__name__


async def _exercise_create_only(client: httpx.AsyncClient) -> tuple[bool, float, str | None]:
    started = perf_counter()
    try:
        created, _, error_key = await _create_session(client)
        return created, (perf_counter() - started) * 1000, error_key
    except Exception as exc:
        return False, (perf_counter() - started) * 1000, type(exc).__name__


async def _exercise_message_auto_create(
    client: httpx.AsyncClient,
    *,
    content: str,
    execution_mode: str,
) -> tuple[bool, float, str | None]:
    started = perf_counter()
    try:
        ok, error_key = await _post_message(
            client,
            session_id=_new_perf_session_id(),
            content=content,
            execution_mode=execution_mode,
        )
        return ok, (perf_counter() - started) * 1000, error_key
    except Exception as exc:
        return False, (perf_counter() - started) * 1000, type(exc).__name__


async def _run_step(
    *,
    client: httpx.AsyncClient,
    concurrency: int,
    duration_seconds: float,
    content: str,
    execution_mode: str,
    flow_mode: str,
) -> dict[str, Any]:
    deadline = perf_counter() + duration_seconds
    latencies_ms: list[float] = []
    errors: dict[str, int] = {}
    success_count = 0
    failure_count = 0
    worker_sessions: list[str] | None = None

    if flow_mode == "message_existing_session":
        worker_sessions = []
        for _ in range(concurrency):
            requested_session_id = _new_perf_session_id()
            created, created_session_id, error_key = await _create_session(
                client,
                session_id=requested_session_id,
            )
            if not created or created_session_id is None:
                return {
                    "concurrency": concurrency,
                    "duration_seconds": duration_seconds,
                    "elapsed_seconds": 0.0,
                    "total_requests": 0,
                    "success_count": 0,
                    "failure_count": 1,
                    "success_rate": 0.0,
                    "rps": 0.0,
                    "avg_ms": None,
                    "p50_ms": None,
                    "p95_ms": None,
                    "p99_ms": None,
                    "max_ms": None,
                    "errors": {error_key or "session_setup_failed": 1},
                }
            worker_sessions.append(created_session_id)

    async def worker(worker_index: int) -> None:
        nonlocal success_count, failure_count
        while perf_counter() < deadline:
            if flow_mode == "create_only":
                ok, latency_ms, error_key = await _exercise_create_only(client)
            elif flow_mode == "message_auto_create":
                ok, latency_ms, error_key = await _exercise_message_auto_create(
                    client,
                    content=content,
                    execution_mode=execution_mode,
                )
            elif flow_mode == "message_existing_session":
                assert worker_sessions is not None
                started = perf_counter()
                try:
                    ok, error_key = await _post_message(
                        client,
                        session_id=worker_sessions[worker_index],
                        content=content,
                        execution_mode=execution_mode,
                    )
                    latency_ms = (perf_counter() - started) * 1000
                except Exception as exc:
                    ok = False
                    latency_ms = (perf_counter() - started) * 1000
                    error_key = type(exc).__name__
            else:
                ok, latency_ms, error_key = await _exercise_create_then_message(
                    client,
                    content=content,
                    execution_mode=execution_mode,
                )
            latencies_ms.append(latency_ms)
            if ok:
                success_count += 1
            else:
                failure_count += 1
                errors[error_key or "unknown"] = errors.get(error_key or "unknown", 0) + 1

    started = perf_counter()
    await asyncio.gather(*(worker(worker_index) for worker_index in range(concurrency)))
    elapsed = perf_counter() - started
    total = success_count + failure_count
    return {
        "concurrency": concurrency,
        "duration_seconds": duration_seconds,
        "elapsed_seconds": round(elapsed, 3),
        "total_requests": total,
        "success_count": success_count,
        "failure_count": failure_count,
        "success_rate": round(success_count / total, 4) if total else 0.0,
        "rps": round(total / elapsed, 2) if elapsed > 0 else 0.0,
        "avg_ms": round(mean(latencies_ms), 2) if latencies_ms else None,
        "p50_ms": round(_percentile(latencies_ms, 50), 2) if latencies_ms else None,
        "p95_ms": round(_percentile(latencies_ms, 95), 2) if latencies_ms else None,
        "p99_ms": round(_percentile(latencies_ms, 99), 2) if latencies_ms else None,
        "max_ms": round(max(latencies_ms), 2) if latencies_ms else None,
        "errors": errors,
    }


async def _main_async(args: argparse.Namespace) -> int:
    timeout = httpx.Timeout(args.timeout_seconds)
    stop_event = asyncio.Event()
    monitor_task: asyncio.Task[dict[str, float | None]] | None = None
    if args.target_pid is not None:
        monitor_task = asyncio.create_task(
            _sample_process_stats(args.target_pid, args.monitor_interval_seconds, stop_event)
        )

    limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=timeout,
        limits=limits,
        trust_env=False,
    ) as client:
        steps: list[dict[str, Any]] = []
        for concurrency in args.concurrency_steps:
            step = await _run_step(
                client=client,
                concurrency=concurrency,
                duration_seconds=args.duration_seconds,
                content=args.content,
                execution_mode=args.execution_mode,
                flow_mode=args.flow_mode,
            )
            steps.append(step)
            if step["failure_count"] > 0:
                break

    process_stats = {"peak_cpu_percent": None, "peak_rss_mb": None}
    if monitor_task is not None:
        stop_event.set()
        process_stats = await monitor_task

    supported = next(
        (
            step["concurrency"]
            for step in reversed(steps)
            if step["failure_count"] == 0 and step["success_count"] > 0
        ),
        0,
    )
    payload = {
        "base_url": args.base_url,
        "flow_mode": args.flow_mode,
        "execution_mode": args.execution_mode,
        "content": args.content,
        "duration_seconds": args.duration_seconds,
        "timeout_seconds": args.timeout_seconds,
        "steps": steps,
        "supported_concurrency": supported,
        "process_stats": process_stats,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run direct router ladder perf test.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8013", help="Router base URL.")
    parser.add_argument(
        "--concurrency-steps",
        default="1,5,10,20,30,50,80,120",
        help="Comma-separated concurrency ladder.",
    )
    parser.add_argument("--duration-seconds", type=float, default=5.0, help="Duration per step.")
    parser.add_argument("--timeout-seconds", type=float, default=30.0, help="HTTP timeout per flow.")
    parser.add_argument(
        "--flow-mode",
        default="create_then_message",
        choices=("create_then_message", "create_only", "message_auto_create", "message_existing_session"),
        help="Which router request flow to measure.",
    )
    parser.add_argument("--execution-mode", default="router_only", help="Message executionMode.")
    parser.add_argument("--content", default="转5000元给朋友", help="Test message content.")
    parser.add_argument("--target-pid", type=int, default=None, help="Optional router PID for ps sampling.")
    parser.add_argument("--monitor-interval-seconds", type=float, default=0.5, help="Process stat sample interval.")
    args = parser.parse_args()
    args.concurrency_steps = [int(item) for item in str(args.concurrency_steps).split(",") if item.strip()]
    return args


def main() -> int:
    args = parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
