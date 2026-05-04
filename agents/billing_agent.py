"""
Billing agent — processes emails classified as billing or insurance inquiries.

Workflow per email:
  1. Classify the billing email subtype via Ollama (one of five subtypes)
  2. Generate a professional draft reply tailored to the subtype (Ollama)
  3. Stage a Google Chat notification for billing staff (local JSON file)
  4. Create a Gmail draft via GmailTool and return the draft ID

PHI policy: patient names and claim details are used to construct messages
but are never written to logs.  Only message IDs, draft IDs, subtypes, and
boolean flags are logged.
"""

from __future__ import annotations

from typing import Literal

import ollama
from loguru import logger

_ollama_client = ollama.Client(timeout=180)

from config import settings
from tools.gmail_tool import GmailTool
from memory.email_store import EmailStore
from utils import stage_chat_message
from training.ingest_history import phi_scrub

# Shared clinic system prompt for every Ollama call in this agent.
_CLINIC_SYSTEM_PROMPT = (
    "You are an assistant for a chiropractic and physical therapy clinic. "
    "All responses must be professional, HIPAA-aware, and concise."
)

# Valid billing email subtypes.
BillingSubtype = Literal[
    "eligibility_question",
    "claim_status",
    "authorization_request",
    "payment_inquiry",
    "other",
]

_VALID_SUBTYPES: frozenset[str] = frozenset({
    "eligibility_question",
    "claim_status",
    "authorization_request",
    "payment_inquiry",
    "other",
})

_SUBTYPE_CLASSIFICATION_PROMPT = """\
You are an email triage assistant for a medical billing office.
Classify the billing-related email below into exactly one of these five subtypes:

  eligibility_question    — questions about patient insurance eligibility,
                            coverage verification, or benefits
  claim_status            — inquiries about claim submission status, claim
                            processing, denials, or EOBs
  authorization_request   — prior authorization requests or pre-cert inquiries
                            requiring a timely response
  payment_inquiry         — questions about invoices, payments received or
                            outstanding balances, remittance advice
  other                   — billing-related but does not fit the above four,
                            or genuinely ambiguous

Reply with exactly one word from the list above.
Do not include punctuation, explanation, or any other text."""


class BillingAgent:
    """
    Handles the end-to-end billing inquiry workflow for a single email that
    has already been classified as a billing email by the OrchestratorAgent.
    """

    def __init__(self) -> None:
        self.gmail = GmailTool()
        self.store = EmailStore()

    # ------------------------------------------------------------------
    # Primary entry point called by OrchestratorAgent
    # ------------------------------------------------------------------

    def run(self, payload: dict) -> dict:
        """
        Thin wrapper used by OrchestratorAgent.  Accepts ``{"email": <dict>}``
        and returns ``{"status": ..., "draft_id": ...}``.
        """
        email = payload.get("email")
        if not email:
            logger.warning("BillingAgent.run called with no email in payload")
            return {"status": "no_email"}

        try:
            draft_id = self.process(email)
            return {"status": "draft" if settings.DRAFT_MODE else "processed", "draft_id": draft_id}
        except Exception as exc:
            email_id = email.get("id", "")
            logger.error(
                "BillingAgent failed on email id={}: {}",
                email_id,
                exc,
            )
            if email_id:
                try:
                    self.gmail.apply_label(email_id, "agent-timed-out")
                except Exception:
                    pass
            return {"status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # 1. Main processing pipeline
    # ------------------------------------------------------------------

    def process(self, email: dict) -> str:
        """
        Run the full billing inquiry pipeline for *email*.

        Steps:
          a. Classify the billing email into one of five subtypes.
          b. Persist the email body to the vector store.
          c. Generate a draft reply appropriate for the subtype.
          d. Stage a Google Chat notification for billing staff.
          e. Create the Gmail draft and return its ID.

        Args:
            email: Full email dict from ``GmailTool.fetch_unread_emails``.

        Returns:
            Gmail draft ID string.
        """
        message_id: str = email.get("id", "")
        subject: str = email.get("subject", "(no subject)")
        logger.info("BillingAgent.process start message_id={}", message_id)  # HIPAA: no PHI logged

        # --- a. Classify billing subtype ---
        subtype = self._classify_subtype(email)
        logger.info(
            "BillingAgent subtype={} message_id={}",
            subtype,
            message_id,
        )

        # --- b. Persist to vector store ---
        body_text = email.get("body_text") or email.get("body_html") or ""
        self.store.save({
            "raw": body_text,
            "structured": {"subtype": subtype},
            "type": "billing",
            "email_id": message_id,
        })

        # --- c. Generate draft reply ---
        reply_body = self.draft_reply(email, subtype)

        # --- d. Stage billing team notification ---
        self.draft_chat_to_billing(email, subtype)

        # --- e. Create Gmail draft ---
        sender: str = email.get("sender", "")
        reply_to = self._extract_email_address(sender)
        reply_subject = self._reply_subject(subject)
        thread_id: str | None = email.get("thread_id") or None

        draft_id = self.gmail.create_draft(
            to=reply_to,
            subject=reply_subject,
            body=reply_body,
            thread_id=thread_id,
        )
        logger.info(
            "BillingAgent.process complete message_id={} draft_id={}",
            message_id,
            draft_id,
        )

        try:
            self.store.save_summary(
                email_id=message_id,
                summary=(
                    f"Billing {subtype.replace('_', ' ')} processed. "
                    "Draft reply created for sender."
                ),
                classification="billing",
            )
        except Exception as exc:
            logger.warning("save_summary failed message_id={}: {}", message_id, exc)

        return draft_id

    # ------------------------------------------------------------------
    # 2. Billing subtype classification
    # ------------------------------------------------------------------

    def _classify_subtype(self, email: dict) -> BillingSubtype:
        """
        Use the local LLM to classify the billing email into one of five
        subtypes.  Only the subject and first 500 characters of the body are
        sent to the model.  Temperature is 0.0 for deterministic output.

        PHI note: subject is logged; body snippet is not.

        Returns:
            One of the five subtype strings; falls back to ``"other"`` if the
            model returns an unrecognised value.
        """
        subject: str = email.get("subject", "(no subject)")
        body_snippet: str = (email.get("body_text") or email.get("body_html") or "")[:500]
        user_message = f"Subject: {subject}\n\nBody (first 500 chars):\n{body_snippet}"

        response = _ollama_client.chat(
            model=settings.OLLAMA_MODEL,
            options={"temperature": 0.0},
            messages=[
                {"role": "system", "content": _SUBTYPE_CLASSIFICATION_PROMPT},
                {"role": "user", "content": user_message},
            ],
        )

        raw: str = response["message"]["content"].strip().lower()
        subtype = raw.rstrip(".,;:!?").split()[0] if raw else "other"

        if subtype not in _VALID_SUBTYPES:
            logger.warning(
                "LLM returned unexpected billing subtype {!r} for message_id={}; defaulting to 'other'",
                subtype,
                email.get("id"),
            )
            subtype = "other"

        return subtype  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 3. Draft reply to sender
    # ------------------------------------------------------------------

    def draft_reply(self, email: dict, subtype: str) -> str:
        """
        Use the local LLM to write a professional acknowledgment reply
        appropriate for the billing email subtype.

        Per-subtype tone:
          - ``eligibility_question``   — acknowledge receipt; office will verify
                                         and follow up within 1 business day.
          - ``claim_status``           — acknowledge; billing team will investigate
                                         and respond.
          - ``authorization_request``  — acknowledge urgency; office will respond
                                         within the required timeframe.
          - ``payment_inquiry``        — acknowledge; direct to billing department.
          - ``other``                  — professional acknowledgment; flag for
                                         human review.

        PHI note: email body is never logged.  Only draft length is logged.

        Args:
            email:   Full email dict (subject and sender used for context).
            subtype: Billing subtype string from ``_classify_subtype``.

        Returns:
            Plain-text email body for the draft.
        """
        subject: str = email.get("subject", "")
        sender: str = email.get("sender", "")
        thread_history: list[dict] = email.get("thread_history", [])

        history_block = ""
        if thread_history:
            lines = ["--- Prior Conversation History ---"]
            for msg in thread_history:
                lines.append(f"[{msg.get('date', '')}] From: {msg.get('sender', '')}")
                lines.append(phi_scrub(msg.get("body", "")))
                lines.append("")
            lines.append("--- End History ---")
            history_block = "\n".join(lines) + "\n\n"

        instructions: dict[str, str] = {
            "eligibility_question": (
                "Acknowledge receipt of their eligibility or insurance coverage question. "
                "State that our office will verify the patient's insurance benefits and "
                "follow up with a response within 1 business day."
            ),
            "claim_status": (
                "Acknowledge receipt of their claim status inquiry. "
                "State that our billing team is investigating the claim and will "
                "respond with a status update as soon as possible."
            ),
            "authorization_request": (
                "Acknowledge receipt of their prior authorization or pre-certification request. "
                "Emphasize that our team understands the urgency and will respond within "
                "the required timeframe. State we will contact them if additional clinical "
                "documentation is needed."
            ),
            "payment_inquiry": (
                "Acknowledge receipt of their payment or billing inquiry. "
                "Direct them to contact our billing department directly for detailed "
                "account information. Include a placeholder '[Billing Dept Phone/Email]' "
                "for the contact details."
            ),
            "other": (
                "Acknowledge receipt of their billing-related inquiry. "
                "State that it has been forwarded to the appropriate team member for review "
                "and they will receive a response within 2 business days."
            ),
        }

        instruction = instructions.get(subtype, instructions["other"])

        user_prompt = f"""\
{history_block}Current Email:
Write a professional acknowledgment email replying to a billing or insurance inquiry.

Context:
- Original email subject: {subject}
- From: {sender}
- Inquiry type: {subtype.replace("_", " ")}

Instructions:
- {instruction}
- Keep the tone professional, warm, and brief (2–4 short paragraphs maximum)
- Do NOT include a subject line
- Do NOT include a salutation line — start directly with the opening paragraph
- End with a closing and the clinic name placeholder "[Clinic Name]"
- Return the email body text only"""

        response = _ollama_client.chat(
            model=settings.OLLAMA_MODEL,
            options={"temperature": 0.3},
            messages=[
                {"role": "system", "content": _CLINIC_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        body: str = response["message"]["content"].strip()
        logger.debug(
            "draft_reply generated message_id={} subtype={} body_len={}",
            email.get("id"),
            subtype,
            len(body),
        )
        return body

    # ------------------------------------------------------------------
    # 4. Stage billing team notification
    # ------------------------------------------------------------------

    def draft_chat_to_billing(self, email: dict, subtype: str) -> str:
        """
        Generate a Google Chat notification for billing staff and write it
        to the local staging file at ``memory/staged_chat_messages.json``.

        The message is NOT sent — it remains staged until a separate process
        dispatches it via GoogleChatTool.

        Format::

            💰 Billing inquiry received — [subtype] from [sender].
            Subject: [subject]. Needs attention.

        PHI note: sender address and subject (non-PHI email headers) are
        included.  Body content is never staged or logged.

        Args:
            email:   Full email dict (sender and subject used only).
            subtype: Billing subtype string from ``_classify_subtype``.

        Returns:
            The formatted message string.
        """
        sender: str = email.get("sender", "Unknown sender")
        subject: str = email.get("subject", "(no subject)")
        message_id: str = email.get("id", "")
        display_subtype = subtype.replace("_", " ")

        message = (
            f"\U0001f4b0 Billing inquiry received \u2014 {display_subtype} from {sender}. "
            f"Subject: {subject}. "
            f"Needs attention."
        )

        stage_chat_message(
            message=message,
            message_type="billing_team_notification",
            email_id=message_id,
        )
        logger.info(
            "Billing team notification staged (email_id={} subtype={})",
            message_id,
            subtype,
        )
        return message

    # ------------------------------------------------------------------
    # Private helpers — string utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_email_address(sender: str) -> str:
        """
        Return the bare email address from a ``"Display Name <addr>"`` string.
        Falls back to the whole string if no angle-bracket pair is found.
        """
        if "<" in sender and ">" in sender:
            return sender.split("<", 1)[1].rstrip(">").strip()
        return sender.strip()

    @staticmethod
    def _reply_subject(original_subject: str) -> str:
        """Prepend 'Re: ' unless the subject already starts with it."""
        stripped = original_subject.strip()
        if stripped.lower().startswith("re:"):
            return stripped
        return f"Re: {stripped}"
