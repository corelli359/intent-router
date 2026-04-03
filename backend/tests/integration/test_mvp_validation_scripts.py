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


def test_admin_intent_validation_script() -> None:
    if not _integration_enabled():
        pytest.skip("Set RUN_INTEGRATION=1 to run integration script checks.")

    base_url = os.getenv("INTENT_ROUTER_BASE_URL", "http://127.0.0.1:8000")
    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "verify_admin_intents.py"),
        "--base-url",
        base_url,
    ]
    _run(cmd, os.environ.copy())


def test_router_lifecycle_validation_script() -> None:
    if not _integration_enabled():
        pytest.skip("Set RUN_INTEGRATION=1 to run integration script checks.")
    if os.getenv("RUN_ROUTER_SSE_TEST") != "1":
        pytest.skip("Set RUN_ROUTER_SSE_TEST=1 to run SSE lifecycle validation.")

    base_url = os.getenv("INTENT_ROUTER_BASE_URL", "http://127.0.0.1:8000")
    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "verify_router_lifecycle.py"),
        "--base-url",
        base_url,
    ]
    _run(cmd, os.environ.copy())
