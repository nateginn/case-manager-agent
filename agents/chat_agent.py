"""
Chat agent — two responsibilities:

  1. Process internal coordination emails (staff-to-staff): decide via LLM
     whether a Google Chat notification is needed, stage it if so, and
     optionally create a Gmail draft reply.

  2. Manage the staged Google Chat message queue: list pending messages,
     approve-and-send them to the correct webhook, or reject them with a
     reason.

PHI policy: email body content and patient data are never written to logs.
Only message IDs, draft IDs, sender domains, and boolean flags are logged.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import ollama
from loguru import logger

from config import settings
from tools.gmail_tool import GmailTool
from tools.google_chat_tool import GoogleChatTool
from memory.email_store import EmailStore

# Shared staging file — same file used by ReferralAgent and BillingAgent.
_STAGED_MESSAGES_PATH = Path(__file__).parent.parent / "memory" / "staged_chat_messages.json"

# Message type →  SPACEfield mapping.
_TYPE_TO_SPACE: dict[str, str] = {
    "receptionist_referral_notification": "GOOGLE_CHAT_SPACE_GREELEY",
    "billing_team_notification": "GOOGLE_CHAT_SPACE_BILLING",
    "internal_followup": "GOOGLE_CHAT_SPACE_GREELEY",
}

_CLINIC_SYSTEM_PROMPT = (
    "You are an assistant for a chiropractic and physical therapy clinic. "
    "All responses must be professional, HIPAA-aware, and concise."
)

_INTERNAL_ANALYSIS_SYSTEM_PROMPT = """\
You are an assistant that helps coordinate internal staff communications at a
chiropractic and physical therapy clinic.

Analyze the staff email provided and return ONLY a single valid JSON object — \
no markdown, no explanation.

The JSON object must contain exactly these keys:
  needs_chat_notification  (boolean) — true if the email contains an action item,
                                        scheduling change, urgent note, or anything
                                        the broader team should know about promptly
  chat_message             (string or null) — a concise one-line Google Chat message
                                        summarising the key point; null if
                                        needs_chat_notification is false
  needs_reply              (boolean) — true if a brief acknowledgment reply from
                                        the clinic would be appropriate
  reply_context            (string or null) — 1-2 sentences describing what the
                                        reply should say; null if needs_reply is false

Rules:
- Use null (JSON null) for string fields when the corresponding boolean is false.
- Keep chat_message under 120 characters.
- Return only the JSON object, nothing else."""


class ChatAgent:
    """
    Stateless conversational agent with internal-email processing and staged
    message queue management.
    """

    SYSTEM_PROMPT = (
        "You are a helpful medical case management assistant. Answer questions "
        "concisely and accurately. Never speculate about diagnoses or provide "
        "medical advice. Refer clinical decisions to licensed clinicians."
    )

    def __init__(self) -> None:
        self.gmail = GmailTool()
        self.store = EmailStore()
        logger.debug("ChatAgent ready (model={})", settings.OLLAMA_MODEL)

    # ------------------------------------------------------------------
    # 1. Conversational interface (used by FastAPI /chat and Orchestrator)
    # ------------------------------------------------------------------

    def run(self, payload: dict) -> dict:
        """
        Respond to a conversational message.

        Expected payload keys:
          - ``text`` (required): the user's message
          - ``history`` (optional): list of prior ``{"role", "content"}`` dicts
        """
        user_text = payload.get("text", "")
        history: list[dict] = payload.get("history", [])

        if not user_text:
            return {"status": "error", "message": "Empty input"}

        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        response = ollama.chat(
            model=settings.OLLAMA_MODEL,
            messages=messages,
        )
        reply = response["message"]["content"].strip()
        logger.debug("ChatAgent reply length={}", len(reply))

        return {
            "status": "ok",
            "reply": reply,
            "history": messages[1:] + [{"role": "assistant", "content": reply}],
        }

    # ------------------------------------------------------------------
    # 2. Internal email processing
    # ------------------------------------------------------------------

    def process_internal_email(self, email: dict) -> str | None:
        """
        Process an internal coordination email (staff-to-staff).

        Steps:
          a. Verify the sender is from the same domain as GMAIL_USER_EMAIL.
          b. Ask Ollama to decide: does this need a Chat notification and/or
             a reply?  Returns structured JSON analysis.
          c. If a Chat notification is needed, stage it in the JSON queue.
          d. If a reply is appropriate, draft it with Ollama and create a
             Gmail draft.

        PHI note: body content is sent to the local Ollama model only and
        is never logged.  Only sender domain and boolean flags are logged.

        Args:
            email: Full email dict from ``GmailTool.fetch_unread_emails``.

        Returns:
            Gmail draft ID if a reply draft was created, otherwise ``None``.
        """
        message_id: str = email.get("id", "")
        subject: str = email.get("subject", "(no subject)")
        sender: str = email.get("sender", "")
        body_text: str = email.get("body_text") or email.get("body_html") or ""

        sender_email = self._extract_email_address(sender)
        is_internal = self._is_internal_sender(sender_email)
        logger.info(
            "process_internal_email message_id={} is_internal={}",  # HIPAA: no PHI logged
            message_id,
            is_internal,
        )

        # --- b. LLM analysis ---
        analysis = self._analyse_internal_email(subject, body_text)
        needs_notification: bool = analysis.get("needs_chat_notification", False)
        needs_reply: bool = analysis.get("needs_reply", False)
        logger.info(
            "Internal email analysis message_id={} needs_notification={} needs_reply={}",
            message_id,
            needs_notification,
            needs_reply,
        )

        # --- c. Stage Chat notification if needed ---
        if needs_notification:
            chat_message: str = analysis.get("chat_message") or f"Internal note: {subject}"
            self._stage_chat_message(
                message=chat_message,
                message_type="internal_followup",
                email_id=message_id,
                needs_routing=True,
            )

        # --- d. Create Gmail draft reply if needed ---
        draft_id: str | None = None
        if needs_reply:
            reply_context: str = analysis.get("reply_context") or ""
            reply_body = self._draft_internal_reply(email, reply_context)
            reply_to = sender_email
            reply_subject = self._reply_subject(subject)
            thread_id: str | None = email.get("thread_id") or None
            try:
                draft_id = self.gmail.create_draft(
                    to=reply_to,
                    subject=reply_subject,
                    body=reply_body,
                    thread_id=thread_id,
                )
                logger.info(
                    "Internal reply draft created message_id={} draft_id={}",
                    message_id,
                    draft_id,
                )
            except Exception as exc:
                logger.error(
                    "Failed to create internal reply draft message_id={}: {}",
                    message_id,
                    exc,
                )

        return draft_id

    # ------------------------------------------------------------------
    # 3. Staged message queue management
    # ------------------------------------------------------------------

    def get_staged_messages(self) -> list[dict]:
        """
        Read ``memory/staged_chat_messages.json`` and return all messages
        that are pending (not yet sent, approved, or rejected).

        A message is considered pending when its ``"sent"`` field is
        ``false`` and it has no ``"status"`` field set to ``"rejected"``
        or ``"sent"``.

        Returns:
            List of pending staged message dicts, oldest first.
        """
        all_messages = self._read_staged_messages()
        pending = [
            m for m in all_messages
            if not m.get("sent", False) and m.get("status") not in ("rejected", "sent")
        ]
        logger.debug(
            "get_staged_messages: {}/{} messages are pending",
            len(pending),
            len(all_messages),
        )
        return pending

    
    def approve_and_send(self, message_id: str, space_override: str = "") -> None:
        """
        Mark the staged message identified by *message_id* as approved,
        send it to the correct Google Chat space, then mark it as sent.

        Space routing is determined by the message ``"type"`` field unless
        *space_override* is provided (used by the /chat/route endpoint when
        a human manually selects Denver or Greeley):
        - ``receptionist_referral_notification`` → GOOGLE_CHAT_SPACE_GREELEY
        - ``billing_team_notification``           → GOOGLE_CHAT_SPACE_BILLING
        - ``needs_routing``                       → space_override required
        - all others                              → GOOGLE_CHAT_SPACE_GREELEY

        Raises:
            KeyError: If no message with *message_id* is found.
            RuntimeError: If the Chat API send fails.

        Args:
            message_id:     The ``"id"`` UUID of the staged message to send.
            space_override: Optional Chat space ID supplied by the routing
                            endpoint; bypasses automatic type-based routing.
        """
        messages = self._read_staged_messages()
        entry = self._find_entry(messages, message_id)

        # Mark as approved
        entry["status"] = "approved"
        entry["approved_at"] = datetime.now(timezone.utc).isoformat()
        self._write_staged_messages(messages)
        logger.info("Staged message id={} marked approved", message_id)

        # Resolve space ID — prefer explicit override from routing endpoint
        space_id = space_override or self._resolve_space(entry.get("type", ""))
        text: str = entry.get("message", "")
        success = GoogleChatTool(space_id).send_message(text)
        if not success:
            logger.error(
                "Failed to send staged message id={} type={}",
                message_id,
                entry.get("type"),
            )
            raise RuntimeError(
                f"Google Chat send failed for staged message id={message_id}"
            )

        # Mark as sent
        entry["status"] = "sent"
        entry["sent"] = True
        entry["sent_at"] = datetime.now(timezone.utc).isoformat()
        self._write_staged_messages(messages)
        logger.info(
            "Staged message id={} sent successfully type={}",
            message_id,
            entry.get("type"),
        )

    def reject_message(self, message_id: str, reason: str) -> None:
        """
        Mark the staged message identified by *message_id* as rejected
        with *reason*.  Rejected messages are kept in the file for
        audit purposes but are excluded from ``get_staged_messages``.

        Raises:
            KeyError: If no message with *message_id* is found.

        Args:
            message_id: The ``"id"`` UUID of the staged message to reject.
            reason:     Human-readable reason for rejection.
        """
        messages = self._read_staged_messages()
        entry = self._find_entry(messages, message_id)

        entry["status"] = "rejected"
        entry["rejection_reason"] = reason
        entry["rejected_at"] = datetime.now(timezone.utc).isoformat()
        self._write_staged_messages(messages)
        logger.info(
            "Staged message id={} rejected reason={!r}", message_id, reason
        )

    # ------------------------------------------------------------------
    # Private helpers — LLM calls
    # ------------------------------------------------------------------

    def _analyse_internal_email(self, subject: str, body_text: str) -> dict:
        """
        Ask the LLM to analyse the internal email and return a decision dict.
        Falls back to a safe all-False structure on parse failure.

        PHI note: body_text is sent to local Ollama only; never logged.
        """
        user_content = f"Subject: {subject}\n\nBody:\n{body_text[:1000]}"

        response = ollama.chat(
            model=settings.OLLAMA_MODEL,
            options={"temperature": 0.0},
            messages=[
                {"role": "system", "content": _INTERNAL_ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        raw: str = response["message"]["content"].strip()

        # Strategy 1: direct parse
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        # Strategy 2: extract embedded JSON
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

        logger.warning(
            "Failed to parse internal email analysis JSON; defaulting to no-action"
        )
        return {
            "needs_chat_notification": False,
            "chat_message": None,
            "needs_reply": False,
            "reply_context": None,
        }

    def _draft_internal_reply(self, email: dict, reply_context: str) -> str:
        """
        Use Ollama to draft a brief professional reply to an internal email.

        PHI note: body content is sent to local Ollama only; reply length is
        logged but not the reply text.
        """
        subject = email.get("subject", "")
        sender = email.get("sender", "")

        user_prompt = f"""\
Write a brief professional reply to an internal staff email.

Context:
- Original subject: {subject}
- From: {sender}
- Reply guidance: {reply_context if reply_context else "Acknowledge receipt and confirm the team is aware."}

Requirements:
- Keep it to 1–2 short paragraphs
- Do NOT include a subject line
- Do NOT include a salutation — start with the opening sentence
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
            "Internal reply drafted message_id={} body_len={}",
            email.get("id"),
            len(body),
        )
        return body

    # ------------------------------------------------------------------
    # Private helpers — staged message JSON R/M/W
    # ------------------------------------------------------------------

    def _read_staged_messages(self) -> list[dict]:
        """Return the full staged messages list; empty list if file is absent/corrupt."""
        if not _STAGED_MESSAGES_PATH.exists():
            return []
        try:
            data = json.loads(_STAGED_MESSAGES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            logger.warning("staged_chat_messages.json had unexpected root type")
            return []
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Could not read staged chat messages file: {}", exc)
            return []

    def _write_staged_messages(self, messages: list[dict]) -> None:
        """Overwrite the staged messages file with *messages*."""
        _STAGED_MESSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            _STAGED_MESSAGES_PATH.write_text(
                json.dumps(messages, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Failed to write staged chat messages file: {}", exc)

    @staticmethod
    def _find_entry(messages: list[dict], message_id: str) -> dict:
        """
        Return the entry with ``"id" == message_id`` from *messages*.

        Raises:
            KeyError: if no matching entry is found.
        """
        for entry in messages:
            if entry.get("id") == message_id:
                return entry
        raise KeyError(f"No staged message found with id={message_id!r}")

    def _stage_chat_message(
        self, message: str, message_type: str, email_id: str = "", needs_routing: bool = False
    ) -> None:
        import uuid

        messages = self._read_staged_messages()
        entry = {
            "id": str(uuid.uuid4()),
            "type": message_type,
            "message": message,
            "email_id": email_id,
            "staged_at": datetime.now(timezone.utc).isoformat(),
            "sent": False,
            "status": "needs_routing" if needs_routing else "pending",
        }
        messages.append(entry)
        self._write_staged_messages(messages)
        logger.debug("Staged chat message id={} needs_routing={}", entry["id"], needs_routing)
    
    # ------------------------------------------------------------------
    # Private helpers — space routing
    # ------------------------------------------------------------------
      

    @staticmethod
    def _resolve_space(message_type: str) -> str:
        """Map a staged message type to the correct Chat space ID from config."""
        attr = _TYPE_TO_SPACE.get(message_type, "GOOGLE_CHAT_SPACE_GREELEY")
        space_id: str = getattr(settings, attr, "")
        if not space_id:
            logger.warning(
                "Space ID for config field {} is empty (message_type={!r})",
                attr,
                message_type,
            )
        return space_id

    # ------------------------------------------------------------------
    # Private helpers — string utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_email_address(sender: str) -> str:
        """Return bare email address from ``"Display Name <addr>"`` string."""
        if "<" in sender and ">" in sender:
            return sender.split("<", 1)[1].rstrip(">").strip()
        return sender.strip()

    @staticmethod
    def _is_internal_sender(sender_email: str) -> bool:
        """
        Return True if *sender_email* shares a domain with GMAIL_USER_EMAIL.
        Used as a soft heuristic — does not gate processing.
        """
        clinic_domain = settings.GMAIL_USER_EMAIL.split("@")[-1] if "@" in settings.GMAIL_USER_EMAIL else ""
        if not clinic_domain:
            return False
        sender_domain = sender_email.split("@")[-1] if "@" in sender_email else ""
        return sender_domain.lower() == clinic_domain.lower()

    @staticmethod
    def _reply_subject(original_subject: str) -> str:
        """Prepend 'Re: ' unless the subject already starts with it."""
        stripped = original_subject.strip()
        if stripped.lower().startswith("re:"):
            return stripped
        return f"Re: {stripped}"
