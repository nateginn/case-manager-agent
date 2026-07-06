"""
Learned sender allow/blocklists for Claire's junk detection.

When the case manager replies "trash all" to a junk batch, those senders are
learned as junk (blocklist). When she replies "skip", those senders are
learned as work (allowlist). The lists are consulted in ClaireAgent._is_junk
before the regex/LLM layers, so learned decisions short-circuit both.

The data file memory/claire_learned_senders.json is intentionally
human-editable (indented JSON, sorted lists):

    {
      "allowlist": { "addresses": [...], "domains": [...] },
      "blocklist": { "addresses": [...], "domains": [...] },
      "updated_at": "<ISO 8601 UTC>"
    }

Note: the allowlist deliberately does not override the no-reply/automated
sender filter — an allowlisted newsletter address still gets skipped as
automated. Lists store sender addresses only (email headers, not PHI bodies).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from loguru import logger

from utils import atomic_write_json

DEFAULT_PATH = Path(__file__).parent.parent / "memory" / "claire_learned_senders.json"

# Free-mail providers: never domain-block these — one spammy gmail.com sender
# must not blocklist every gmail.com sender.
_FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "aol.com", "icloud.com", "msn.com", "comcast.net",
    "protonmail.com", "proton.me",
}


def extract_address(sender: str) -> str:
    """Return the bare lowercase address from a From header like 'Name <a@B.com>'."""
    address = parseaddr(sender or "")[1].strip().lower()
    return address if "@" in address else ""


def extract_domain(address: str) -> str:
    """Return the lowercase domain part of a bare address, or ''."""
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].strip().lower()


def _empty_lists() -> dict:
    return {
        "allowlist": {"addresses": [], "domains": []},
        "blocklist": {"addresses": [], "domains": []},
        "updated_at": "",
    }


def load_lists(path: Path = DEFAULT_PATH) -> dict:
    """Load the learned lists; tolerant of a missing or corrupt file."""
    if not path.exists():
        return _empty_lists()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("sender_lists: could not read {} ({}) — using empty lists", path.name, exc)
        return _empty_lists()

    result = _empty_lists()
    if isinstance(data, dict):
        for list_name in ("allowlist", "blocklist"):
            section = data.get(list_name, {})
            if isinstance(section, dict):
                for key in ("addresses", "domains"):
                    values = section.get(key, [])
                    if isinstance(values, list):
                        result[list_name][key] = [str(v).strip().lower() for v in values if v]
        result["updated_at"] = str(data.get("updated_at", ""))
    return result


def save_lists(data: dict, path: Path = DEFAULT_PATH) -> None:
    """Persist the lists (sorted + deduped) atomically."""
    for list_name in ("allowlist", "blocklist"):
        for key in ("addresses", "domains"):
            data[list_name][key] = sorted(set(data[list_name][key]))
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)


def add_to_blocklist(senders: list[str], path: Path = DEFAULT_PATH) -> int:
    """
    Learn senders as junk ("trash all"). Adds the full address always, and the
    domain when it is not a free-mail provider. Removes the same entries from
    the allowlist — the latest decision wins. Returns count of addresses learned.
    """
    lists = load_lists(path)
    learned = 0
    for sender in senders:
        address = extract_address(sender)
        if not address:
            continue
        if address not in lists["blocklist"]["addresses"]:
            lists["blocklist"]["addresses"].append(address)
            learned += 1
        domain = extract_domain(address)
        if domain and domain not in _FREEMAIL and domain not in lists["blocklist"]["domains"]:
            lists["blocklist"]["domains"].append(domain)
        if address in lists["allowlist"]["addresses"]:
            lists["allowlist"]["addresses"].remove(address)
        if domain in lists["allowlist"]["domains"]:
            lists["allowlist"]["domains"].remove(domain)
    if learned:
        save_lists(lists, path)
        logger.info("sender_lists: {} sender(s) added to blocklist", learned)
    return learned


def add_to_allowlist(senders: list[str], path: Path = DEFAULT_PATH) -> int:
    """
    Learn senders as work ("skip"). Adds addresses only — skip is a weaker
    signal than trash, so it never vouches for a whole domain. Removes the
    addresses from the blocklist. Returns count of addresses learned.
    """
    lists = load_lists(path)
    learned = 0
    for sender in senders:
        address = extract_address(sender)
        if not address:
            continue
        if address not in lists["allowlist"]["addresses"]:
            lists["allowlist"]["addresses"].append(address)
            learned += 1
        if address in lists["blocklist"]["addresses"]:
            lists["blocklist"]["addresses"].remove(address)
    if learned:
        save_lists(lists, path)
        logger.info("sender_lists: {} sender(s) added to allowlist", learned)
    return learned


def check(sender: str, lists: dict) -> str | None:
    """
    Return "allow", "block", or None for *sender* against loaded *lists*.

    Precedence: address matches beat domain matches; at equal specificity
    allow beats block (fail-open toward work).
    """
    address = extract_address(sender)
    if not address:
        return None
    if address in lists["allowlist"]["addresses"]:
        return "allow"
    if address in lists["blocklist"]["addresses"]:
        return "block"
    domain = extract_domain(address)
    if not domain:
        return None
    if domain in lists["allowlist"]["domains"]:
        return "allow"
    if domain in lists["blocklist"]["domains"]:
        return "block"
    return None
