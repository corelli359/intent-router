from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_PATHS = [
    REPO_ROOT / "backend",
    REPO_ROOT / "backend" / "services" / "router-service" / "src",
    REPO_ROOT / "backend" / "services" / "fake-llm-service" / "src",
    REPO_ROOT / "backend" / "services" / "agents" / "account-balance-agent" / "src",
    REPO_ROOT / "backend" / "services" / "agents" / "transfer-money-agent" / "src",
    REPO_ROOT / "backend" / "services" / "agents" / "credit-card-repayment-agent" / "src",
    REPO_ROOT / "backend" / "services" / "agents" / "gas-bill-agent" / "src",
    REPO_ROOT / "backend" / "services" / "agents" / "forex-agent" / "src",
    REPO_ROOT / "backend" / "services" / "agents" / "fallback-agent" / "src",
    REPO_ROOT,
]

for path in PYTHON_PATHS:
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
