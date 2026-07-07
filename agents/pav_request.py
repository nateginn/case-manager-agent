"""
Marrick PAV handler — auto-draft forwards to the billing team.

When a patient completes care, Marrick sends a Patient Account Verification
("PAV") request.  These are always handled by the billing team, so Claire:

  1. Detects the PAV request deterministically (no LLM): sender is
     @marrick.com and the subject or body mentions a PAV.
  2. Creates a forward *draft* (never sends) to the billing team member
     (settings.CLAIRE_BILLING_FORWARD_TO) carrying the original message text
     and its attachments, asking them to reply-all in the same email string
     once the PAV is returned to the case manager.
  3. Notifies the Chat space that the draft is waiting for approval.

HIPAA posture: detection is pure regex — nothing leaves the machine.  The
forward goes to the clinic's own biller and sits in Gmail Drafts until the
case manager approves the send.  Nothing from the email body is logged.
"""

from __future__ import annotations

import re

from loguru import logger

from config import settings

_MARRICK_SENDER_RE = re.compile(r"@marrick\.com\b", re.IGNORECASE)

# "PAV" as a standalone word, or the spelled-out phrase.
_PAV_RE = re.compile(r"\bPAV\b|patient\s+account\s+verification", re.IGNORECASE)


def is_pav_request(email: dict) -> bool:
    """Deterministic detector: Marrick sender + PAV mention in subject or body."""
    if not _MARRICK_SENDER_RE.search(email.get("sender", "") or ""):
        return False
    subject = email.get("subject", "") or ""
    body = (email.get("body_text", "") or "")[:2000]
    return bool(_PAV_RE.search(subject) or _PAV_RE.search(body))


def build_forward_body(email: dict) -> str:
    """Deterministic forward note + quoted original — no LLM content."""
    name = settings.CLAIRE_BILLING_FORWARD_NAME or "there"
    lines = [
        f"Hi {name},",
        "",
        "Will you please reply-all in this email string when you have "
        "returned the PAV to the case manager?",
        "",
        "Thank you!",
        "",
        "---------- Forwarded message ---------",
        f"From: {email.get('sender', '')}",
        f"Date: {email.get('date', '')}",
        f"Subject: {email.get('subject', '')}",
        f"To: {email.get('to', '')}",
        "",
        email.get("body_text", "") or "",
    ]
    return "\n".join(lines)


def try_handle(email: dict, gmail) -> dict | None:
    """
    Full pipeline for one email.  Returns None if the email is not a Marrick
    PAV request (caller proceeds with normal notification), otherwise:

        {"status": "drafted", "draft_id": ..., "attachment_count": int}
        {"status": "draft_error"}

    draft_error means: fall back to normal notification, but record the
    attempt so we don't retry on every cycle.
    """
    if not is_pav_request(email):
        return None

    logger.info("pav_request: detected Marrick PAV request")  # HIPAA: no PHI logged

    to = settings.CLAIRE_BILLING_FORWARD_TO
    if not to:
        logger.warning("pav_request: CLAIRE_BILLING_FORWARD_TO not set")
        return {"status": "draft_error"}

    subject = email.get("subject", "") or "(no subject)"
    if not subject.lower().startswith(("fwd:", "fw:")):
        subject = f"Fwd: {subject}"

    try:
        attachments = gmail.fetch_message_attachments(email)
    except Exception as exc:
        logger.error("pav_request: attachment fetch failed: {}", exc)
        return {"status": "draft_error"}

    try:
        draft_id = gmail.create_draft(
            to=to,
            subject=subject,
            body=build_forward_body(email),
            thread_id=email.get("thread_id") or None,
            signature_html=gmail.get_signature(),
            attachments=attachments,
        )
    except Exception as exc:
        logger.error("pav_request: draft creation failed: {}", exc)
        return {"status": "draft_error"}

    logger.info("pav_request: forward draft created draft_id={}", draft_id)
    return {
        "status": "drafted",
        "draft_id": draft_id,
        "attachment_count": len(attachments),
    }
