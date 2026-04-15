"""
Shared utilities — staging helper used by BillingAgent, ReferralAgent, and
ChatAgent so all three write consistent JSON entries to
memory/staged_chat_messages.json.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_STAGED_MESSAGES_PATH = Path(__file__).parent / "memory" / "staged_chat_messages.json"


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
        _STAGED_MESSAGES_PATH.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug(
            "Staged chat message id={} type={} status={}",
            entry["id"],
            message_type,
            status,
        )
    except OSError as exc:
        logger.error("Failed to write staged chat messages file: {}", exc)
