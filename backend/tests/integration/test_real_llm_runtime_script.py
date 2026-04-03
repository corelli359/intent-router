from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "verify_real_llm_runtime.py"


def test_real_llm_runtime_script() -> None:
    if os.getenv("RUN_REAL_LLM_TEST") != "1":
        pytest.skip("Set RUN_REAL_LLM_TEST=1 to run the real LLM runtime smoke test.")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "Real LLM runtime smoke test failed.\n"
            f"stdout=\n{proc.stdout}\n"
            f"stderr=\n{proc.stderr}"
        )
