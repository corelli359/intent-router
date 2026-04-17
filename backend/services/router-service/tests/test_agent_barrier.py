from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from router_service.core.shared.domain import Task
from router_service.core.support.agent_barrier import (
    ROUTER_AGENT_BARRIER_ENABLED_ENV,
    BarrierAgentClient,
    agent_barrier_triggered,
    build_agent_barrier_error,
)
from router_service.settings import ROUTER_ENV_FILE_ENV, Settings


class AgentBarrierTests(unittest.TestCase):
    def test_build_agent_barrier_error_is_clear_and_detectable(self) -> None:
        error = build_agent_barrier_error(
            intent_code="transfer_money",
            agent_url="http://agent.example.internal/run",
        )

        self.assertTrue(agent_barrier_triggered(error))
        self.assertIn(ROUTER_AGENT_BARRIER_ENABLED_ENV, str(error))
        self.assertIn("transfer_money", str(error))
        self.assertIn("agent.example.internal", str(error))

    def test_settings_reads_agent_barrier_flag_from_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                ROUTER_ENV_FILE_ENV: "/tmp/router-service-does-not-exist.env",
                ROUTER_AGENT_BARRIER_ENABLED_ENV: "true",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertTrue(settings.router_agent_barrier_enabled)

    def test_barrier_agent_client_fails_fast_without_network_io(self) -> None:
        client = BarrierAgentClient()
        task = Task(
            session_id="session-demo",
            intent_code="transfer_money",
            agent_url="http://agent.example.internal/run",
            confidence=0.98,
        )

        async def run() -> None:
            with self.assertRaisesRegex(RuntimeError, ROUTER_AGENT_BARRIER_ENABLED_ENV):
                async for _chunk in client.stream(task, "给小明转500元"):
                    raise AssertionError("barrier client should not yield any chunk")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
