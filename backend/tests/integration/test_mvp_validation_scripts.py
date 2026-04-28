from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = ROOT / "scripts"


def _run(cmd: list[str], env: dict[str, str]) -> None:
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "Command failed.\n"
            f"cmd={' '.join(cmd)}\n"
            f"stdout=\n{proc.stdout}\n"
            f"stderr=\n{proc.stderr}"
        )


def _integration_enabled() -> bool:
    return os.getenv("RUN_INTEGRATION") == "1"


def test_router_assistant_contract_script() -> None:
    if not _integration_enabled():
        pytest.skip("Set RUN_INTEGRATION=1 to run integration script checks.")

    base_url = os.getenv("INTENT_ROUTER_BASE_URL", "http://127.0.0.1:8000")
    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "verify_router_assistant_contract.py"),
        "--base-url",
        base_url,
        "--strict-demo",
    ]
    _run(cmd, os.environ.copy())


def test_router_v1_regression_script() -> None:
    if not _integration_enabled():
        pytest.skip("Set RUN_INTEGRATION=1 to run integration script checks.")

    base_url = os.getenv("INTENT_ROUTER_BASE_URL", "http://127.0.0.1:8000")
    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "run_router_v1_regression_suite.py"),
        "--base-url",
        base_url,
    ]
    _run(cmd, os.environ.copy())
