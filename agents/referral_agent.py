"""
Referral agent — processes emails classified as referrals.

Workflow per email:
  1. Fetch any PDF attachments and extract text (PdfTool)
  2. Run LLM field extraction on the PDF text (PdfTool.extract_referral_fields)
  3. Generate a professional draft reply to the referring provider (Ollama)
  4. Stage a Google Chat notification for the receptionist (local JSON file)
  5. Create a Gmail draft via GmailTool and return the draft ID

PHI policy: patient name, DOB, and clinical data are used to construct
messages but are never written to logs.  Only message IDs, draft IDs, and
boolean flags are logged.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import ollama
from loguru import logger

from config import settings
from tools.gmail_tool import GmailTool
from tools.pdf_tool import PdfTool
from memory.email_store import EmailStore

# Path to the local staging table for outbound Google Chat messages.
_STAGED_MESSAGES_PATH = Path(__file__).parent.parent / "memory" / "staged_chat_messages.json"

# Shared clinic system prompt used for every Ollama call in this agent.
_CLINIC_SYSTEM_PROMPT = (
    "You are an assistant for a chiropractic and physical therapy clinic. "
    "All responses must be professional, HIPAA-aware, and concise."
)


class ReferralAgent:
    """
    Handles the end-to-end referral intake workflow for a single email that
    has already been classified as a referral by the OrchestratorAgent.
    """

    def __init__(self) -> None:
        self.gmail = GmailTool()
        self.pdf = PdfTool()
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
            logger.warning("ReferralAgent.run called with no email in payload")
            return {"status": "no_email"}

        try:
            draft_id = self.process(email)
            return {"status": "draft" if settings.DRAFT_MODE else "processed", "draft_id": draft_id}
        except Exception as exc:
            logger.exception("ReferralAgent failed on email id={}", email.get("id"))
            return {"status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # 1. Main processing pipeline
    # ------------------------------------------------------------------

    def process(self, email: dict) -> str:
        """
        Run the full referral intake pipeline for *email*.

        Steps:
          a. Fetch PDF attachments and extract their text.
          b. Extract structured referral fields from the PDF text (or email
             body if no PDFs are present).
          c. Build a context string combining email body + referral fields.
          d. Generate a draft reply for the referring provider.
          e. Stage a Google Chat notification for the receptionist.
          f. Create the Gmail draft and return its ID.

        Args:
            email: Full email dict from ``GmailTool.fetch_unread_emails``.

        Returns:
            Gmail draft ID string.
        """
        message_id: str = email.get("id", "")
        subject: str = email.get("subject", "(no subject)")
        logger.info("ReferralAgent.process start message_id={}", message_id)  # HIPAA: no PHI logged

        # --- a. Fetch and extract PDF attachments ---
        pdf_texts: list[str] = []
        if email.get("has_attachments"):
            pdf_texts = self._extract_pdf_texts(message_id, email.get("attachment_filenames", []))
            logger.info(
                "Extracted text from {}/{} PDF attachment(s) for message_id={}",
                len(pdf_texts),
                len(email.get("attachment_filenames", [])),
                message_id,
            )

        # --- b. Extract structured referral fields ---
        # Prefer PDF text (more structured) over email body.
        extraction_source = "\n\n---\n\n".join(pdf_texts) if pdf_texts else (
            email.get("body_text") or email.get("body_html") or ""
        )
        referral_fields = self.pdf.extract_referral_fields(extraction_source) if extraction_source else {}
        logger.debug(
            "Referral field extraction complete message_id={} fields_found={}",
            message_id,
            sum(1 for v in referral_fields.values() if v is not None),
        )

        # --- c. Persist to vector store ---
        self.store.save({
            "raw": extraction_source,
            "structured": referral_fields,
            "type": "referral",
            "email_id": message_id,
        })

        # --- d. Generate draft reply ---
        reply_body = self.draft_reply(email, referral_fields)

        # --- e. Stage receptionist notification ---
        self.draft_chat_to_receptionist(referral_fields, message_id)

        # --- f. Create Gmail draft ---
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
            "ReferralAgent.process complete message_id={} draft_id={}",
            message_id,
            draft_id,
        )
        return draft_id

    # ------------------------------------------------------------------
    # 2. Draft reply to referring provider
    # ------------------------------------------------------------------

    def draft_reply(self, email: dict, referral_fields: dict) -> str:
        """
        Use the local LLM to write a professional acknowledgment reply to the
        referring provider.

        The reply:
          - Thanks them for the referral
          - Confirms receipt
          - States the office will contact the patient to schedule
          - Adds an authorization notice if ``authorization_required`` is True

        Tone is appropriate for a chiropractic/PT/massage/acupuncture/shockwave
        clinic.  The model is instructed to return body text only — no subject
        line, no salutation guessing beyond what the fields provide.

        PHI note: referral fields are passed to the LLM (local Ollama only).
        The generated text is returned to the caller; it is never logged.

        Args:
            email:           Full email dict (subject and sender used only).
            referral_fields: Structured fields from ``PdfTool.extract_referral_fields``.

        Returns:
            Plain-text email body for the draft.
        """
        subject = email.get("subject", "")
        sender = email.get("sender", "")

        first_name = referral_fields.get("patient_first_name") or ""
        last_name = referral_fields.get("patient_last_name") or ""
        patient_name = f"{first_name} {last_name}".strip() or "the patient"
        provider = referral_fields.get("referring_provider_name") or "your office"
        treatment = referral_fields.get("treatment_requested") or "the requested services"
        auth_required: bool | None = referral_fields.get("authorization_required")
        insurance = referral_fields.get("insurance_name") or ""

        auth_line = ""
        if auth_required is True:
            auth_line = (
                f"Please note that prior authorization appears to be required"
                f"{f' from {insurance}' if insurance else ''}. "
                "Our team will initiate the authorization process and will follow up "
                "if additional documentation is needed."
            )
        elif auth_required is False:
            auth_line = "Our records indicate no prior authorization is required for this referral."

        user_prompt = f"""\
Write a professional acknowledgment email replying to a referral received from {provider}.

Context:
- Original email subject: {subject}
- From: {sender}
- Patient: {patient_name}
- Treatment requested: {treatment}
- Authorization note: {auth_line if auth_line else "Not specified"}

Requirements:
- Thank the referring provider for the referral
- Confirm we have received the referral
- State that our office will contact the patient directly to schedule an appointment
- If an authorization note is provided above, include it naturally in the reply
- Keep the tone warm, professional, and brief (3–5 short paragraphs maximum)
- Do NOT include a subject line
- Do NOT include a salutation line — start directly with the opening paragraph
- End with a closing and the clinic name placeholder "[Clinic Name]"
- Return the email body text only"""

        response = ollama.chat(
            model=settings.OLLAMA_MODEL,
            options={"temperature": 0.3},
            messages=[
                {"role": "system", "content": _CLINIC_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        body: str = response["message"]["content"].strip()
        logger.debug(
            "draft_reply generated message_id={} body_len={}",
            email.get("id"),
            len(body),
        )
        return body

    # ------------------------------------------------------------------
    # 3. Stage receptionist notification
    # ------------------------------------------------------------------

    def draft_chat_to_receptionist(
        self, referral_fields: dict, email_id: str = ""
    ) -> str:
        """
        Generate a Google Chat notification for the receptionist and write it
        to the local staging file at ``memory/staged_chat_messages.json``.

        The message is NOT sent — it remains staged until a separate process
        (e.g. a send_staged_messages job) dispatches it via GoogleChatTool.

        Format::

            📋 New referral received — [Patient Name], DOB [DOB].
            Provider: [Referring Provider]. Needs scheduling for [treatment].
            Auth needed: Yes / No / Unknown

        PHI note: patient name, DOB, and provider appear in the staged
        message (which stays on-device in a local JSON file) but are never
        logged.

        Args:
            referral_fields: Structured fields from ``PdfTool.extract_referral_fields``.
            email_id:        Original Gmail message ID (stored for traceability).

        Returns:
            The formatted message string.
        """
        first = referral_fields.get("patient_first_name") or ""
        last = referral_fields.get("patient_last_name") or ""
        patient_name = f"{first} {last}".strip() or "Unknown patient"
        dob = referral_fields.get("date_of_birth") or "N/A"
        provider = referral_fields.get("referring_provider_name") or "Unknown provider"
        treatment = referral_fields.get("treatment_requested") or "unspecified treatment"
        auth_required: bool | None = referral_fields.get("authorization_required")

        if auth_required is True:
            auth_str = "Yes"
        elif auth_required is False:
            auth_str = "No"
        else:
            auth_str = "Unknown"

        message = (
            f"\U0001f4cb New referral received \u2014 {patient_name}, DOB {dob}. "
            f"Provider: {provider}. "
            f"Needs scheduling for {treatment}. "
            f"Auth needed: {auth_str}"
        )

        self._stage_chat_message(
            message=message,
            message_type="receptionist_referral_notification",
            email_id=email_id,
        )
        logger.info(
            "Receptionist notification staged (email_id={} auth_needed={})",
            email_id,
            auth_str,
        )
        return message

    # ------------------------------------------------------------------
    # Private helpers — PDF attachment handling
    # ------------------------------------------------------------------

    def _extract_pdf_texts(self, message_id: str, attachment_filenames: list[str]) -> list[str]:
        """
        Fetch each PDF attachment for *message_id* and return a list of
        extracted text strings (one per successfully processed PDF).

        Non-PDF attachments and attachments with no extractable text are
        skipped silently.  Any per-attachment exception is caught and logged
        so one bad attachment never aborts the rest.
        """
        parts = self._get_pdf_attachment_parts(message_id)
        if not parts:
            logger.debug("No PDF attachment parts found for message_id={}", message_id)
            return []

        texts: list[str] = []
        for part in parts:
            filename: str = part["filename"]
            attachment_id: str = part["attachment_id"]
            try:
                raw_bytes = self.gmail.fetch_attachment(message_id, attachment_id)
                text = self.pdf.extract_text(raw_bytes)
                if text.strip():
                    texts.append(text)
                    logger.debug(
                        "PDF text extracted chars={}",  # HIPAA: no PHI logged — filename omitted
                        len(text),
                    )
                else:
                    logger.warning(
                        "PDF attachment has no extractable text (possibly scanned) "
                        "message_id={}",  # HIPAA: no PHI logged — filename omitted
                        message_id,
                    )
            except Exception as exc:
                logger.error(
                    "Failed to extract PDF text message_id={}: {}",  # HIPAA: no PHI logged — filename omitted
                    message_id,
                    exc,
                )
        return texts

    def _get_pdf_attachment_parts(self, message_id: str) -> list[dict]:
        """
        Re-fetch the Gmail message payload and walk it to collect
        ``{filename, attachment_id}`` for every PDF part.

        ``GmailTool._walk_parts`` records filenames but discards
        ``body.attachmentId``, so we need direct access to the raw Gmail
        payload here.  We use ``self.gmail._service`` deliberately — the
        referral agent owns the attachment-fetching pipeline and this is the
        minimal reach required to do it without modifying GmailTool.

        PHI note: only the filename is logged; attachment bytes are not.
        """
        try:
            msg = (
                self.gmail._service  # noqa: SLF001
                .users()
                .messages()
                .get(userId=settings.GMAIL_USER_EMAIL, id=message_id, format="full")
                .execute()
            )
        except Exception as exc:
            logger.error(
                "Could not fetch message payload for attachment walk message_id={}: {}",
                message_id,
                exc,
            )
            return []

        parts: list[dict] = []
        self._walk_for_pdf_parts(msg.get("payload", {}), parts)
        logger.debug(
            "Found {} PDF part(s) in message_id={}", len(parts), message_id
        )
        return parts

    def _walk_for_pdf_parts(self, part: dict, acc: list[dict]) -> None:
        """Depth-first walk of a Gmail MIME payload; appends PDF parts to *acc*."""
        mime_type: str = part.get("mimeType", "")
        filename: str = part.get("filename", "")
        body: dict = part.get("body", {})
        sub_parts: list[dict] = part.get("parts", [])

        if mime_type.startswith("multipart/"):
            for sub in sub_parts:
                self._walk_for_pdf_parts(sub, acc)
            return

        if filename and self._is_pdf_part(mime_type, filename):
            attachment_id: str = body.get("attachmentId", "")
            if attachment_id:
                acc.append({"filename": filename, "attachment_id": attachment_id})
            else:
                logger.warning(
                    "PDF part has no attachmentId (inline data?)"  # HIPAA: no PHI logged — filename omitted
                )

    @staticmethod
    def _is_pdf_part(mime_type: str, filename: str) -> bool:
        """Return True if the MIME part looks like a PDF."""
        pdf_mime_types = {"application/pdf", "application/x-pdf", "application/octet-stream"}
        return mime_type in pdf_mime_types or filename.lower().endswith(".pdf")

    # ------------------------------------------------------------------
    # Private helpers — chat message staging
    # ------------------------------------------------------------------

    def _stage_chat_message(
        self, message: str, message_type: str, email_id: str = ""
    ) -> None:
        """
        Append *message* to the local staging table at
        ``memory/staged_chat_messages.json``.

        The file holds a JSON array of objects::

            {
              "id":           "<uuid>",
              "type":         "<message_type>",
              "message":      "<text>",
              "email_id":     "<gmail message id>",
              "staged_at":    "<ISO 8601 UTC>",
              "sent":         false
            }

        The file is created if it does not exist.  We read-modify-write to
        preserve any previously staged messages; write errors are logged but
        do not raise so the primary pipeline always completes.
        """
        _STAGED_MESSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)

        existing: list[dict] = []
        if _STAGED_MESSAGES_PATH.exists():
            try:
                existing = json.loads(_STAGED_MESSAGES_PATH.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    logger.warning(
                        "staged_chat_messages.json had unexpected root type; resetting"
                    )
                    existing = []
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Could not read staged chat messages file: {}", exc)
                existing = []

        entry = {
            "id": str(uuid.uuid4()),
            "type": message_type,
            "message": message,
            "email_id": email_id,
            "staged_at": datetime.now(timezone.utc).isoformat(),
            "sent": False,
        }
        existing.append(entry)

        try:
            _STAGED_MESSAGES_PATH.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.debug(
                "Staged chat message id={} to {}", entry["id"], _STAGED_MESSAGES_PATH
            )
        except OSError as exc:
            logger.error("Failed to write staged chat messages file: {}", exc)

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
