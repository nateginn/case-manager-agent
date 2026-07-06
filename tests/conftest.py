"""
Pytest configuration — adds the project root to sys.path so tests can
import from agents/, tools/, memory/, training/, and config without
installing the package.
"""
import sys
from pathlib import Path

import pytest

# Insert the repo root (parent of tests/) at the front of sys.path.
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def isolate_retry_state(tmp_path, monkeypatch):
    """Keep orchestrator retry bookkeeping out of the real memory/ directory."""
    import agents.orchestrator as orchestrator_module

    monkeypatch.setattr(
        orchestrator_module, "_RETRY_STATE_PATH", tmp_path / "timeout_retries.json"
    )
