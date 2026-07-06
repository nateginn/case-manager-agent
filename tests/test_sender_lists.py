"""
Tests for the learned sender allow/blocklists (agents/sender_lists.py) and
their integration with ClaireAgent._is_junk.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents import sender_lists


@pytest.fixture()
def lists_path(tmp_path):
    return tmp_path / "learned_senders.json"


# ---------------------------------------------------------------------------
# Address / domain extraction
# ---------------------------------------------------------------------------


def test_extract_address_from_display_name():
    assert sender_lists.extract_address("Jane Doe <Jane@Example.COM>") == "jane@example.com"


def test_extract_address_bare():
    assert sender_lists.extract_address("billing@firm.com") == "billing@firm.com"


def test_extract_address_garbage():
    assert sender_lists.extract_address("not an email") == ""
    assert sender_lists.extract_address("") == ""


def test_extract_domain():
    assert sender_lists.extract_domain("a@b.com") == "b.com"
    assert sender_lists.extract_domain("nodomain") == ""


# ---------------------------------------------------------------------------
# Blocklist learning ("trash all")
# ---------------------------------------------------------------------------


def test_blocklist_adds_address_and_corporate_domain(lists_path):
    sender_lists.add_to_blocklist(["Promo <promo@spamco.com>"], path=lists_path)
    lists = sender_lists.load_lists(lists_path)
    assert "promo@spamco.com" in lists["blocklist"]["addresses"]
    assert "spamco.com" in lists["blocklist"]["domains"]


def test_blocklist_never_domain_blocks_freemail(lists_path):
    sender_lists.add_to_blocklist(["spammer@gmail.com"], path=lists_path)
    lists = sender_lists.load_lists(lists_path)
    assert "spammer@gmail.com" in lists["blocklist"]["addresses"]
    assert "gmail.com" not in lists["blocklist"]["domains"]


def test_blocklist_removes_from_allowlist(lists_path):
    sender_lists.add_to_allowlist(["vendor@corp.com"], path=lists_path)
    sender_lists.add_to_blocklist(["vendor@corp.com"], path=lists_path)
    lists = sender_lists.load_lists(lists_path)
    assert "vendor@corp.com" not in lists["allowlist"]["addresses"]
    assert "vendor@corp.com" in lists["blocklist"]["addresses"]


# ---------------------------------------------------------------------------
# Allowlist learning ("skip")
# ---------------------------------------------------------------------------


def test_allowlist_adds_addresses_only(lists_path):
    sender_lists.add_to_allowlist(["Rep <rep@vendor.com>"], path=lists_path)
    lists = sender_lists.load_lists(lists_path)
    assert "rep@vendor.com" in lists["allowlist"]["addresses"]
    assert lists["allowlist"]["domains"] == []  # skip never vouches for a domain


def test_allowlist_removes_from_blocklist_addresses(lists_path):
    sender_lists.add_to_blocklist(["rep@vendor.com"], path=lists_path)
    sender_lists.add_to_allowlist(["rep@vendor.com"], path=lists_path)
    lists = sender_lists.load_lists(lists_path)
    assert "rep@vendor.com" not in lists["blocklist"]["addresses"]
    assert "rep@vendor.com" in lists["allowlist"]["addresses"]


# ---------------------------------------------------------------------------
# check() precedence
# ---------------------------------------------------------------------------


def test_check_allow_address_beats_block_domain(lists_path):
    sender_lists.add_to_blocklist(["promo@spamco.com"], path=lists_path)  # blocks spamco.com domain
    sender_lists.add_to_allowlist(["rep@spamco.com"], path=lists_path)
    lists = sender_lists.load_lists(lists_path)
    assert sender_lists.check("rep@spamco.com", lists) == "allow"
    assert sender_lists.check("other@spamco.com", lists) == "block"


def test_check_unknown_returns_none(lists_path):
    lists = sender_lists.load_lists(lists_path)
    assert sender_lists.check("someone@unrelated.org", lists) is None
    assert sender_lists.check("garbage", lists) is None


# ---------------------------------------------------------------------------
# File format
# ---------------------------------------------------------------------------


def test_file_is_human_editable_json(lists_path):
    sender_lists.add_to_blocklist(["b@corp.com", "a@corp.com"], path=lists_path)
    raw = lists_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert "\n" in raw  # indented, not minified
    assert data["blocklist"]["addresses"] == sorted(data["blocklist"]["addresses"])
    assert data["updated_at"]


def test_load_tolerates_corrupt_file(lists_path):
    lists_path.write_text("{not json", encoding="utf-8")
    lists = sender_lists.load_lists(lists_path)
    assert lists["allowlist"]["addresses"] == []
    assert lists["blocklist"]["addresses"] == []


# ---------------------------------------------------------------------------
# _is_junk integration — learned lists short-circuit rules and the LLM
# ---------------------------------------------------------------------------


def _make_lists(block_addresses=(), allow_addresses=()):
    return {
        "allowlist": {"addresses": list(allow_addresses), "domains": []},
        "blocklist": {"addresses": list(block_addresses), "domains": []},
        "updated_at": "",
    }


def test_is_junk_blocklisted_sender_no_llm_call(monkeypatch):
    from agents import claire_agent

    def _fail(*_a, **_kw):  # any LLM call is a test failure
        raise AssertionError("Ollama should not be called for a learned sender")

    monkeypatch.setattr(claire_agent._ollama_client, "generate", _fail)

    email = {"sender": "promo@spamco.com", "subject": "Buy now!!!"}
    learned = _make_lists(block_addresses=["promo@spamco.com"])
    assert claire_agent.ClaireAgent._is_junk(SimpleNamespace(), email, learned) is True


def test_is_junk_allowlisted_sender_is_work(monkeypatch):
    from agents import claire_agent

    def _fail(*_a, **_kw):
        raise AssertionError("Ollama should not be called for a learned sender")

    monkeypatch.setattr(claire_agent._ollama_client, "generate", _fail)

    email = {"sender": "rep@vendor.com", "subject": "Random subject"}
    learned = _make_lists(allow_addresses=["rep@vendor.com"])
    assert claire_agent.ClaireAgent._is_junk(SimpleNamespace(), email, learned) is False
