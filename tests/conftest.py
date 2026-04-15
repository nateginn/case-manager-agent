"""
Pytest configuration — adds the project root to sys.path so tests can
import from agents/, tools/, memory/, training/, and config without
installing the package.
"""
import sys
from pathlib import Path

# Insert the repo root (parent of tests/) at the front of sys.path.
sys.path.insert(0, str(Path(__file__).parent.parent))
