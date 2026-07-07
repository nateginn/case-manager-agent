"""
Shared utilities — staging helper used by BillingAgent, ReferralAgent, and
ChatAgent so all three write consistent JSON entries to
memory/staged_chat_messages.json, plus atomic-JSON-write and bounded-retry
helpers shared across agents and tools.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

_STAGED_MESSAGES_PATH = Path(__file__).parent / "memory" / "staged_chat_messages.json"

# ---------------------------------------------------------------------------
# Atomic JSON persistence
# ---------------------------------------------------------------------------


def atomic_write_json(path: Path, data: Any) -> None:
    """
    Write *data* as JSON to *path* via write-temp-then-rename so a crash
    mid-write can never leave a truncated/corrupt file behind.

    The temp file lives in the same directory as *path* (same volume), which
    makes ``os.replace`` atomic on both Windows and POSIX. Raises on failure —
    callers decide whether to swallow.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Date-of-birth normalization
# ---------------------------------------------------------------------------

_DOB_FORMATS = (
    "%Y-%m-%d",       # 2007-09-18
    "%m/%d/%Y",       # 09/18/2007, 9/18/2007
    "%m/%d/%y",       # 9/18/07
    "%m-%d-%Y",       # 09-18-2007
    "%m-%d-%y",       # 09-18-07
    "%m.%d.%Y",       # 9.18.2007
    "%m.%d.%y",       # 9.18.07
    "%B %d, %Y",      # September 18, 2007
    "%B %d %Y",       # September 18 2007
    "%b %d, %Y",      # Sep 18, 2007
    "%b %d %Y",       # Sep 18 2007
    "%d %B %Y",       # 18 September 2007
)


def normalize_dob(raw: str) -> str:
    """
    Normalize a date-of-birth string from any common email/EMR format to
    ``MM/DD/YYYY`` so two independently-sourced DOBs can be compared with
    ``==``.  Returns "" when *raw* is empty or unparseable — callers treat
    that as "no DOB available", never as a match.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    for fmt in _DOB_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# Bounded retry for transient API errors
# ---------------------------------------------------------------------------

_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}

# httpx exception class names — matched by name so utils.py never needs a
# hard httpx import (it arrives transitively via the ollama package).
_TRANSIENT_EXC_NAMES = {
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "TimeoutException",
}


def is_transient_error(exc: Exception) -> bool:
    """Return True for errors worth retrying (rate limits, 5xx, network blips)."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            return int(status) in _TRANSIENT_HTTP_STATUSES
        except (TypeError, ValueError):
            pass
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


def retry_call(
    fn: Callable[[], Any],
    *,
    retries: int = 2,
    base_delay: float = 1.0,
    retry_on: Callable[[Exception], bool] = is_transient_error,
    label: str = "",
) -> Any:
    """
    Call *fn* with up to *retries* additional attempts on transient errors,
    using exponential backoff (base_delay * 2**attempt).

    PHI note: logs only the label, attempt number, and exception type —
    never arguments or payloads.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= retries or not retry_on(exc):
                raise
            delay = base_delay * (2 ** attempt)
            attempt += 1
            logger.warning(
                "retry_call[{}]: {} on attempt {}/{} — retrying in {:.1f}s",
                label or "unnamed", type(exc).__name__, attempt, retries + 1, delay,
            )
            time.sleep(delay)


def stage_chat_message(
    message: str,
    message_type: str,
    email_id: str = "",
    status: str = "pending",
) -> None:
    """
    Append a staged Chat message entry to memory/staged_chat_messages.json.

    Each entry written has the following schema::

        {
          "id":        "<uuid>",
          "type":      "<message_type>",
          "message":   "<text>",
          "email_id":  "<gmail message id>",
          "staged_at": "<ISO 8601 UTC>",
          "sent":      false,
          "status":    "pending" | "needs_routing" | ...
        }

    The file is created if it does not exist. We read-modify-write to
    preserve any previously staged messages; write errors are logged but
    do not raise so the primary pipeline always completes.

    Args:
        message:      Text of the Chat notification. Never logged — may contain PHI.
        message_type: One of "billing_team_notification",
                      "receptionist_referral_notification", "internal_followup", etc.
        email_id:     Gmail message ID for traceability.
        status:       Initial status; defaults to "pending". Pass "needs_routing"
                      for messages that need a human to choose the destination space.
    """
    _STAGED_MESSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if _STAGED_MESSAGES_PATH.exists():
        try:
            data = json.loads(_STAGED_MESSAGES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
            else:
                logger.warning(
                    "staged_chat_messages.json had unexpected root type; resetting"
                )
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Could not read staged chat messages file: {}", exc)

    entry = {
        "id": str(uuid.uuid4()),
        "type": message_type,
        "message": message,
        "email_id": email_id,
        "staged_at": datetime.now(timezone.utc).isoformat(),
        "sent": False,
        "status": status,
    }
    existing.append(entry)

    try:
        # One bounded retry mitigates transient write failures (e.g. AV scan
        # holding the file); full durability is out of scope for a JSON queue.
        retry_call(
            lambda: atomic_write_json(_STAGED_MESSAGES_PATH, existing),
            retries=1,
            retry_on=lambda exc: isinstance(exc, OSError),
            label="stage_chat_message.write",
        )
        logger.debug(
            "Staged chat message id={} type={} status={}",
            entry["id"],
            message_type,
            status,
        )
    except OSError as exc:
        logger.error("Failed to write staged chat messages file: {}", exc)
