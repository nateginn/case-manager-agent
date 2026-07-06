"""
Visit-inquiry handler — Claire's autonomous EMR lookup pipeline.

Many casemanager.art@gmail.com emails are third-party case managers asking
whether a patient attended an appointment and what future visits are
scheduled.  This module:

  1. Classifies an email as a visit/attendance inquiry (local Ollama only)
     and extracts the patient name.
  2. Looks the patient up in Prompt EMR via PromptEmrBrowserTool and pulls
     their past + future visits.
  3. Creates a REPLY-ALL Gmail *draft* (never sends) so the case manager
     reviews and approves before anything leaves the clinic.

HIPAA posture:
  - Classification and name extraction run on local Ollama only.
  - Visit data comes from the clinic's own EMR session and lands in a Gmail
    draft — the human approves the send, and is responsible for confirming
    the requester is legitimate before releasing PHI.
  - Nothing from the email body or the EMR is written to logs.
"""

from __future__ import annotations

import json
import re

import ollama
from loguru import logger

from config import settings
from utils import retry_call

_ollama_client = ollama.Client(timeout=60)

# Addresses that belong to us — never included in reply-all recipients.
_OWN_ADDRESSES = {
    "casemanager.art@gmail.com",
    "cm.assistant.art@gmail.com",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_CLASSIFY_PROMPT = """You screen emails for a chiropractic/PT clinic.

Decide if this email is a VISIT INQUIRY: a request from an outside case manager,
attorney, insurer, or provider asking whether a specific patient ATTENDED
appointments and/or what FUTURE appointments are scheduled.

NOT a visit inquiry: billing questions, referrals, records requests without an
attendance question, scheduling requests, marketing, internal mail.

Reply with ONLY a JSON object, no other text:
{{"is_visit_inquiry": true/false, "patient_name": "First Last" or ""}}

The patient_name is the PATIENT being asked about (not the sender).
Use "First Last" format. Empty string if no clear patient name.

From: {sender}
Subject: {subject}
Body:
{body}
"""


def classify(email: dict) -> dict:
    """
    Classify an email as a visit inquiry and extract the patient name.
    Local Ollama only.  Fails closed: on any error returns not-an-inquiry
    so the email falls through to Claire's normal notification flow.

    Returns:
        {"is_visit_inquiry": bool, "patient_name": str}
    """
    sender = email.get("sender", "")
    subject = email.get("subject", "")
    body = (email.get("body_text", "") or "")[:2000]

    try:
        model = settings.OLLAMA_LIGHT_MODEL or settings.OLLAMA_MODEL
        resp = retry_call(lambda: _ollama_client.generate(
            model=model,
            prompt=_CLASSIFY_PROMPT.format(sender=sender, subject=subject, body=body),
            options={"num_predict": 100},
            think=False,
        ), retries=1, label="ollama.visit_inquiry")
        raw = getattr(resp, "response", None) or (resp.get("response", "") if isinstance(resp, dict) else "")

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {"is_visit_inquiry": False, "patient_name": ""}
        parsed = json.loads(match.group())
        return {
            "is_visit_inquiry": bool(parsed.get("is_visit_inquiry", False)),
            "patient_name": str(parsed.get("patient_name", "") or "").strip(),
        }
    except Exception as exc:
        logger.warning("visit_inquiry.classify error (fail-closed): {}", exc)
        return {"is_visit_inquiry": False, "patient_name": ""}


def _reply_all_recipients(email: dict) -> tuple[str, str]:
    """
    Compute (to, cc) for a reply-all, excluding our own addresses.

    to: the original sender (Reply-To header wins over From).
    cc: everyone else on the original To/Cc lines.
    """
    own = {a.lower() for a in _OWN_ADDRESSES}
    if settings.GMAIL_USER_EMAIL:
        own.add(settings.GMAIL_USER_EMAIL.lower())

    sender_field = email.get("reply_to", "") or email.get("sender", "")
    sender_addrs = [a for a in _EMAIL_RE.findall(sender_field) if a.lower() not in own]
    to = sender_addrs[0] if sender_addrs else ""

    cc_addrs: list[str] = []
    for field in (email.get("to", ""), email.get("cc", "")):
        for addr in _EMAIL_RE.findall(field):
            low = addr.lower()
            if low in own or low == to.lower() or low in (a.lower() for a in cc_addrs):
                continue
            cc_addrs.append(addr)

    return to, ", ".join(cc_addrs)


def _format_visit_line(v: dict, include_stage: bool = False) -> str:
    """
    Format one visit as e.g. "  • 7/30/26 (Thu, 8:05am) — AUTO PT Follow up".
    Location is omitted; the trailing case name in parentheses is stripped
    from the visit type. Stage is included only for past visits, where it
    answers the attendance question ("Completed", "No Show", ...).
    """
    parts = [v.get("date", "")]
    if v.get("day_time"):
        parts.append(f"({v['day_time']})")
    visit_type = re.sub(r"\s*\([^)]*\)\s*$", "", v.get("visit_type", "")).strip()
    if visit_type:
        parts.append(f"— {visit_type}")
    if include_stage and v.get("stage"):
        parts.append(f"— {v['stage']}")
    return "  • " + " ".join(parts)


def build_reply_body(patient_name: str, visits: dict) -> str:
    """Deterministic reply template — no LLM, so no hallucinated visit data."""
    past = visits.get("past_visits", [])
    future = visits.get("future_visits", [])

    lines = [
        "Hello,",
        "",
        f"Regarding your inquiry about {patient_name}:",
        "",
        "Past visits:",
    ]
    if past:
        lines.extend(_format_visit_line(v, include_stage=True) for v in past)
    else:
        lines.append("  • No past visits on record.")

    lines += ["", "Upcoming appointments:"]
    if future:
        lines.extend(_format_visit_line(v) for v in future)
    else:
        lines.append("  • No upcoming appointments are currently scheduled.")

    lines += [
        "",
        "Please let us know if you need any additional information.",
        "",
        "Best regards,",
    ]
    # No signature block here: the real signature (logo, locations, office
    # hours, confidentiality statement) lives in the Gmail account's send-as
    # settings and is appended to drafts separately — never recreated in code.
    return "\n".join(lines)


def try_handle(email: dict, gmail) -> dict | None:
    """
    Full pipeline for one email.  Returns None if the email is not a visit
    inquiry (caller proceeds with normal notification), otherwise:

        {"status": "drafted", "patient_name": ..., "draft_id": ...,
         "future_count": int, "past_count": int}
        {"status": "patient_not_found", "patient_name": ...}
        {"status": "emr_error", "patient_name": ...}

    All failure statuses mean: fall back to normal notification, but record
    the attempt so we don't retry the EMR on every cycle.
    """
    verdict = classify(email)
    if not verdict["is_visit_inquiry"] or not verdict["patient_name"]:
        return None

    patient_name = verdict["patient_name"]
    logger.info("visit_inquiry: detected inquiry, running EMR lookup")  # HIPAA: no name logged

    # Import here so Claire still runs if playwright isn't installed.
    try:
        from tools.prompt_emr_browser_tool import PromptEmrBrowserTool
    except Exception as exc:
        logger.error("visit_inquiry: PromptEmrBrowserTool unavailable: {}", exc)
        return {"status": "emr_error", "patient_name": patient_name}

    try:
        with PromptEmrBrowserTool(headless=True) as emr:
            visits = emr.get_patient_visits(patient_name)
    except Exception as exc:
        logger.error("visit_inquiry: EMR lookup failed: {}", exc)
        return {"status": "emr_error", "patient_name": patient_name}

    if visits is None:
        return {"status": "patient_not_found", "patient_name": patient_name}

    to, cc = _reply_all_recipients(email)
    if not to:
        logger.warning("visit_inquiry: could not determine reply address")
        return {"status": "emr_error", "patient_name": patient_name}

    subject = email.get("subject", "") or "(no subject)"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    body = build_reply_body(visits["patient"]["name"], visits)

    try:
        draft_id = gmail.create_draft(
            to=to,
            subject=subject,
            body=body,
            thread_id=email.get("thread_id") or None,
            cc=cc,
            in_reply_to=email.get("message_id_header", ""),
            signature_html=gmail.get_signature(),
        )
    except Exception as exc:
        logger.error("visit_inquiry: draft creation failed: {}", exc)
        return {"status": "emr_error", "patient_name": patient_name}

    logger.info("visit_inquiry: reply-all draft created draft_id={}", draft_id)
    return {
        "status": "drafted",
        "patient_name": visits["patient"]["name"],
        "draft_id": draft_id,
        "future_count": len(visits["future_visits"]),
        "past_count": len(visits["past_visits"]),
    }
