"""
Visit-inquiry handler — Claire's autonomous EMR lookup pipeline.

Many casemanager.art@gmail.com emails are third-party case managers asking
whether a patient attended an appointment and what future visits are
scheduled.  This module:

  1. Classifies an email as a visit/attendance/treatment-status inquiry
     (local Ollama only) and extracts the patient name(s) — care
     coordinators like MedHub and ProvePartners often ask about several
     patients in one email.
  2. Looks each patient up in Prompt EMR via PromptEmrBrowserTool (one
     browser session) and pulls their past + future visits.
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
from utils import normalize_dob, retry_call

_ollama_client = ollama.Client(timeout=60)

# Addresses that belong to us — never included in reply-all recipients.
_OWN_ADDRESSES = {
    "casemanager.art@gmail.com",
    "cm.assistant.art@gmail.com",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_CLASSIFY_PROMPT = """You screen emails for a chiropractic/PT clinic.

Decide if this email is a VISIT INQUIRY: a request from an outside case manager,
attorney, insurer, or care coordinator asking about a specific patient's
appointments at OUR clinic. Visit inquiries include:
  - whether the patient ATTENDED an appointment
  - whether the patient is SCHEDULED, and the date/time of upcoming visits
  - treatment status updates (date of last visit, next scheduled appointment,
    number of visits completed to date)
  - care coordination / attendance confirmation forms asking us to confirm
    scheduling or attendance for one or more patients

NOT a visit inquiry: billing or payment questions, records/document requests
without an attendance or scheduling question, authorization paperwork,
marketing, internal mail.

The email may ask about MORE THAN ONE patient. List EVERY patient being asked
about (the patients, not the sender and not people in signatures).

If the email states a patient's date of birth (DOB), include it in
patient_dobs at the SAME position as that patient's name, copied exactly as
written in the email. Use "" when no DOB is given for that patient.

Reply with ONLY a JSON object, no other text:
{{"is_visit_inquiry": true/false, "patient_names": ["First Last", ...],
  "patient_dobs": ["...", ...]}}

Use "First Last" format. Empty lists if no clear patient name.

From: {sender}
Subject: {subject}
Body:
{body}
"""

# Never look up more than this many patients from a single email.
_MAX_PATIENTS = 4


def _parse_patient_names(parsed: dict) -> list[str]:
    """
    Extract a clean, deduped patient-name list from the model's JSON.
    Accepts both the current ``patient_names`` list and the legacy
    ``patient_name`` string shape.
    """
    raw = parsed.get("patient_names", None)
    if raw is None:
        raw = [parsed.get("patient_name", "")]
    if isinstance(raw, str):
        raw = [raw]

    names: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        name = str(item or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names[:_MAX_PATIENTS]


def _parse_patient_dobs(parsed: dict, count: int) -> list[str]:
    """
    Extract per-patient DOBs aligned with the parsed name list: a list of
    exactly *count* entries, each ``MM/DD/YYYY`` or "" when the email gave
    no (parseable) DOB for that patient.  Unparseable model output degrades
    to "" — a missing DOB just skips the secondary confirmation.
    """
    raw = parsed.get("patient_dobs", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []
    dobs = [normalize_dob(str(item or "")) for item in raw]
    dobs = dobs[:count]
    dobs += [""] * (count - len(dobs))
    return dobs


def classify(email: dict) -> dict:
    """
    Classify an email as a visit inquiry and extract the patient name(s).
    Local Ollama only.  Fails closed: on any error returns not-an-inquiry
    so the email falls through to Claire's normal notification flow.

    Returns:
        {"is_visit_inquiry": bool, "patient_names": list[str],
         "patient_dobs": list[str]}  # aligned with patient_names, "" = unknown
    """
    sender = email.get("sender", "")
    subject = email.get("subject", "")
    body = (email.get("body_text", "") or "")[:3000]

    try:
        model = settings.OLLAMA_LIGHT_MODEL or settings.OLLAMA_MODEL
        resp = retry_call(lambda: _ollama_client.generate(
            model=model,
            prompt=_CLASSIFY_PROMPT.format(sender=sender, subject=subject, body=body),
            options={"num_predict": 200},
            think=False,
        ), retries=1, label="ollama.visit_inquiry")
        raw = getattr(resp, "response", None) or (resp.get("response", "") if isinstance(resp, dict) else "")

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {"is_visit_inquiry": False, "patient_names": [], "patient_dobs": []}
        parsed = json.loads(match.group())
        names = _parse_patient_names(parsed)
        return {
            "is_visit_inquiry": bool(parsed.get("is_visit_inquiry", False)),
            "patient_names": names,
            "patient_dobs": _parse_patient_dobs(parsed, len(names)),
        }
    except Exception as exc:
        logger.warning("visit_inquiry.classify error (fail-closed): {}", exc)
        return {"is_visit_inquiry": False, "patient_names": [], "patient_dobs": []}


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


def _visit_date_key(v: dict):
    """Sortable date for a visit; falls back to epoch on unparseable dates."""
    from datetime import datetime
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(v.get("date", ""), fmt)
        except ValueError:
            continue
    return datetime(1970, 1, 1)


def _completed_count(past: list[dict]) -> int:
    """
    Number of completed past visits.  Uses the stage field when the EMR
    provides one; if no past visit carries a stage, every past visit counts.
    """
    if any(v.get("stage") for v in past):
        return sum(1 for v in past if "complete" in (v.get("stage") or "").lower())
    return len(past)


def _patient_section(patient_name: str, visits: dict | None) -> list[str]:
    """
    Lines for one patient: a summary block (last visit / next appointment /
    completed count — the fields care coordinators ask for by name), then the
    full past and upcoming visit lists.  visits=None means the patient was
    not found in the EMR.
    """
    lines = [f"Regarding your inquiry about {patient_name}:", ""]
    if visits is None:
        lines.append("  We have no record of this patient at our clinic.")
        return lines

    past = visits.get("past_visits", [])
    future = visits.get("future_visits", [])

    last_line = (
        _format_visit_line(max(past, key=_visit_date_key), include_stage=True).removeprefix("  • ")
        if past else "No past visits on record."
    )
    next_line = (
        _format_visit_line(min(future, key=_visit_date_key)).removeprefix("  • ")
        if future else "No upcoming appointments are currently scheduled."
    )
    lines += [
        f"  Date of last visit: {last_line}",
        f"  Next scheduled appointment: {next_line}",
        f"  Visits completed to date: {_completed_count(past)}",
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
    return lines


def build_reply_body(patients: list[tuple[str, dict | None]]) -> str:
    """
    Deterministic reply template — no LLM, so no hallucinated visit data.

    *patients* is a list of (patient_name, visits-dict-or-None) pairs;
    None marks a patient we could not find in the EMR.
    """
    lines = ["Hello,", ""]
    for i, (name, visits) in enumerate(patients):
        if i:
            lines.append("")
        lines.extend(_patient_section(name, visits))

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
    if not verdict["is_visit_inquiry"] or not verdict["patient_names"]:
        return None

    names = verdict["patient_names"]
    dobs = verdict.get("patient_dobs") or [""] * len(names)
    display_name = ", ".join(names)
    logger.info("visit_inquiry: detected inquiry for {} patient(s), running EMR lookup", len(names))  # HIPAA: no names logged

    # Import here so Claire still runs if playwright isn't installed.
    try:
        from tools.prompt_emr_browser_tool import PromptEmrBrowserTool
    except Exception as exc:
        logger.error("visit_inquiry: PromptEmrBrowserTool unavailable: {}", exc)
        return {"status": "emr_error", "patient_name": display_name}

    # One browser session for all lookups; (resolved_name, visits|None) pairs.
    patients: list[tuple[str, dict | None]] = []
    try:
        with PromptEmrBrowserTool(headless=True) as emr:
            for name, dob in zip(names, dobs):
                visits = emr.get_patient_visits(name, dob=dob)
                resolved = visits["patient"]["name"] if visits else name
                patients.append((resolved, visits))
    except Exception as exc:
        logger.error("visit_inquiry: EMR lookup failed: {}", exc)
        return {"status": "emr_error", "patient_name": display_name}

    found = [(n, v) for n, v in patients if v is not None]
    if not found:
        return {"status": "patient_not_found", "patient_name": display_name}

    to, cc = _reply_all_recipients(email)
    if not to:
        logger.warning("visit_inquiry: could not determine reply address")
        return {"status": "emr_error", "patient_name": display_name}

    subject = email.get("subject", "") or "(no subject)"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    body = build_reply_body(patients)

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
        return {"status": "emr_error", "patient_name": display_name}

    logger.info("visit_inquiry: reply-all draft created draft_id={}", draft_id)
    return {
        "status": "drafted",
        "patient_name": ", ".join(n for n, _ in patients),
        "draft_id": draft_id,
        "future_count": sum(len(v["future_visits"]) for _, v in found),
        "past_count": sum(len(v["past_visits"]) for _, v in found),
    }
