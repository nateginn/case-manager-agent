"""
Tests for utils.retry_call, utils.is_transient_error, and
utils.atomic_write_json.
"""

from __future__ import annotations

import json

import pytest

import utils
from utils import atomic_write_json, is_transient_error, retry_call


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Backoff sleeps are pointless in tests."""
    monkeypatch.setattr(utils.time, "sleep", lambda _s: None)


class _FakeHttpError(Exception):
    """Mimics googleapiclient HttpError's status_code attribute."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


# ---------------------------------------------------------------------------
# is_transient_error
# ---------------------------------------------------------------------------


def test_transient_http_statuses():
    assert is_transient_error(_FakeHttpError(503))
    assert is_transient_error(_FakeHttpError(429))
    assert not is_transient_error(_FakeHttpError(404))
    assert not is_transient_error(_FakeHttpError(400))


def test_transient_network_errors():
    assert is_transient_error(ConnectionError("boom"))
    assert is_transient_error(TimeoutError("boom"))
    assert not is_transient_error(ValueError("boom"))


# ---------------------------------------------------------------------------
# retry_call
# ---------------------------------------------------------------------------


def test_success_first_try_calls_once():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert retry_call(fn, retries=2) == "ok"
    assert len(calls) == 1


def test_transient_then_success_retries():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise _FakeHttpError(503)
        return "ok"

    assert retry_call(fn, retries=2) == "ok"
    assert len(calls) == 3


def test_non_transient_raises_immediately():
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        retry_call(fn, retries=2)
    assert len(calls) == 1


def test_exhausted_retries_reraises_last_error():
    calls = []

    def fn():
        calls.append(1)
        raise TimeoutError("still down")

    with pytest.raises(TimeoutError):
        retry_call(fn, retries=2)
    assert len(calls) == 3  # 1 initial + 2 retries


# ---------------------------------------------------------------------------
# atomic_write_json
# ---------------------------------------------------------------------------


def test_atomic_write_creates_valid_json(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"a": 1, "b": ["x"]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": ["x"]}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_preserves_old_file_on_serialization_failure(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"good": True})

    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": object()})

    # Old content intact, no temp litter.
    assert json.loads(target.read_text(encoding="utf-8")) == {"good": True}
    assert not list(tmp_path.glob("*.tmp"))
