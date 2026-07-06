"""
Orchestrator agent — classifies incoming emails and routes them to the
appropriate specialist agent.

PHI policy: only email IDs, subjects, and classification results are logged.
Body content is never written to logs.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import ollama
from loguru import logger

# Shared Ollama client with a 180-second request timeout so qwen3:32b thinking
# loops cannot stall the agent pass indefinitely.
_ollama_client = ollama.Client(timeout=180)
from pydantic import BaseModel, Field

from config import settings
from tools.gmail_tool import GmailTool
from utils import atomic_write_json, retry_call
from .referral_agent import ReferralAgent
from .billing_agent import BillingAgent
from .chat_agent import ChatAgent

# ---------------------------------------------------------------------------
# Bounded re-attempt tracking for failed/timed-out emails
# ---------------------------------------------------------------------------

_RETRY_STATE_PATH = Path(__file__).parent.parent / "memory" / "timeout_retries.json"

# After this many failed processing attempts the email is parked
# (agent-processed) so the poll loop stops returning it.
MAX_TIMEOUT_RETRIES = 3
# Minimum wait between re-attempts so a down Ollama isn't hammered every poll.
RETRY_COOLDOWN_MINUTES = 15


def _load_retry_state() -> dict:
    if not _RETRY_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(_RETRY_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read timeout_retries.json: {}", exc)
        return {}


def _save_retry_state(state: dict) -> None:
    try:
        _RETRY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_RETRY_STATE_PATH, state)
    except OSError as exc:
        logger.warning("Could not write timeout_retries.json: {}", exc)

# Valid classification categories.
Category = Literal["referral", "billing", "internal", "unknown"]

_VALID_CATEGORIES: frozenset[str] = frozenset({"referral", "billing", "internal", "unknown"})

_CLASSIFICATION_SYSTEM_PROMPT = """\
You are an email triage assistant for a medical case management office.
Classify the email below into exactly one of these four categories:

  referral  — patient referral requests, specialist referrals, referral faxes,
               prior-authorization requests, or requests to schedule a specialist
  billing   — insurance claims, EOBs, payment notices, claim denials, invoices,
               remittance advice, or billing disputes
  internal  — messages from staff, scheduling notes, office communications,
               or any message that does not fit the above two categories
  unknown   — spam, automated marketing, or emails where the category is
               genuinely ambiguous

Reply with exactly one word: referral, billing, internal, or unknown.
Do not include punctuation, explanation, or any other text."""


class ProcessingResult(BaseModel):
    """Structured outcome returned by ``OrchestratorAgent.process_email``."""

    email_id: str
    classification: Category
    agent_status: str = ""
    draft_id: str | None = None
    chat_message_staged: bool = False
    notes: str = ""


class OrchestratorAgent:
    """
    Receives a raw email dict (as returned by ``GmailTool.fetch_unread_emails``)
    and routes it to the correct specialist agent.

    All LLM calls use Ollama running locally; no email content is sent to
    external services.
    """

    def __init__(self) -> None:
        self.gmail = GmailTool()
        self.referral_agent = ReferralAgent()
        self.billing_agent = BillingAgent()
        self.chat_agent = ChatAgent()
        logger.info(
            "OrchestratorAgent ready (model={}, draft_mode={})",
            settings.OLLAMA_MODEL,
            settings.DRAFT_MODE,
        )

    # ------------------------------------------------------------------
    # 1. Email classification
    # ------------------------------------------------------------------

    def classify_email(self, email: dict) -> Category:
        """
        Use the local LLM to classify *email* into one of four categories:
        ``"referral"``, ``"billing"``, ``"internal"``, or ``"unknown"``.

        Only the subject and first 500 characters of the body are sent to the
        model — attachments are never included.  Temperature is set to 0.0 for
        fully deterministic output.

        PHI note: subject is logged; body snippet is not.

        Args:
            email: A dict from ``GmailTool.fetch_unread_emails``.

        Returns:
            One of the four category strings.
        """
        subject: str = email.get("subject", "(no subject)")
        body_snippet: str = (email.get("body_text") or email.get("body_html") or "")[:500]

        user_message = f"Subject: {subject}\n\nBody (first 500 chars):\n{body_snippet}"

        logger.debug("Classifying email id={}", email.get("id"))  # HIPAA: no PHI logged

        # Bounded retry on transient failures. Worst case with the 180s client
        # timeout is ~9 minutes across 3 attempts — acceptable for a background loop.
        response = retry_call(
            lambda: _ollama_client.chat(
                model=settings.OLLAMA_MODEL,
                options={"temperature": 0.0},
                messages=[
                    {"role": "system", "content": _CLASSIFICATION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
            ),
            retries=2, base_delay=2.0, label="ollama.classify",
        )

        raw: str = response["message"]["content"].strip().lower()

        # Strip punctuation the model may have added despite instructions
        category = raw.rstrip(".,;:!?").split()[0] if raw else "unknown"

        if category not in _VALID_CATEGORIES:
            logger.warning(
                "LLM returned unexpected category {!r} for email id={}; defaulting to 'unknown'",
                category,
                email.get("id"),
            )
            category = "unknown"

        logger.info(
            "Classified email id={} -> {}",  # HIPAA: no PHI logged
            email.get("id"),
            category,
        )
        return category  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 2. Main processing entry point
    # ------------------------------------------------------------------

    def process_email(self, email: dict) -> ProcessingResult:
        """
        Classify *email* and route it to the correct specialist agent.

        Routing logic:
          - ``"referral"``  → ReferralAgent
          - ``"billing"``   → BillingAgent
          - ``"internal"``  → ChatAgent
          - ``"unknown"``   → Gmail draft flagging the email for human review

        The email is marked ``agent-processed`` in Gmail after every successful
        dispatch regardless of category.

        Args:
            email: A dict from ``GmailTool.fetch_unread_emails``.

        Returns:
            A :class:`ProcessingResult` describing what happened.
        """
        email_id: str = email.get("id", "")
        subject: str = email.get("subject", "(no subject)")

        # Double-check: skip if this message was labelled between the list
        # query and now.  Guards against two concurrent /agent/run calls
        # both fetching the same inbox snapshot before either applies the
        # agent-processed label.
        if self.gmail.is_processed(email_id):
            logger.info(
                "Skipping email id={} — already labelled agent-processed (concurrent pass guard)",
                email_id,
            )
            return ProcessingResult(
                email_id=email_id,
                classification="unknown",
                agent_status="skipped",
                notes="Skipped — already processed by a concurrent agent pass",
            )

        try:
            classification = self.classify_email(email)
        except Exception as exc:
            logger.error(
                "Ollama error classifying email id={}: {}",
                email_id,
                exc,
            )
            try:
                self.gmail.apply_label(email_id, "agent-timed-out")
            except Exception:
                pass
            attempts = self._record_failed_attempt(email_id)
            if attempts >= MAX_TIMEOUT_RETRIES:
                # Park the email so the poll query stops returning it; the
                # agent-timed-out label stays on it for manual review.
                logger.warning(
                    "Giving up on email id={} after {} failed attempts", email_id, attempts
                )
                try:
                    self.gmail.mark_as_processed(email_id)
                except Exception:
                    pass
            return ProcessingResult(
                email_id=email_id,
                classification="unknown",
                agent_status="timed_out",
                notes=(
                    f"Ollama timeout or error during classification: {type(exc).__name__} "
                    f"(attempt {attempts}/{MAX_TIMEOUT_RETRIES})"
                ),
            )

        # Fetch prior thread messages and attach to the email dict so specialist
        # agents can inject conversation history into their draft prompts.
        thread_id = email.get("thread_id", "")
        email["thread_history"] = (
            self.gmail.fetch_thread(thread_id, email_id) if thread_id else []
        )

        draft_id: str | None = None
        chat_message_staged = False
        agent_status = ""
        notes = ""

        try:
            if classification == "referral":
                result = self.referral_agent.run({"email": email})
                agent_status = result.get("status", "")
                notes = f"Referral agent: {agent_status}"

            elif classification == "billing":
                result = self.billing_agent.run({"email": email})
                agent_status = result.get("status", "")
                notes = f"Billing agent: {agent_status}"

            elif classification == "internal":
                body_text = email.get("body_text") or email.get("body_html") or ""
                result = self.chat_agent.run({
                    "text": body_text,
                    "history": [],
                })
                agent_status = result.get("status", "")
                chat_message_staged = agent_status == "ok"
                notes = "Internal email processed by ChatAgent"

            else:  # unknown
                draft_id = self._create_review_draft(email_id, subject)
                agent_status = "draft_created" if draft_id else "draft_failed"
                notes = f"Unclassified — review draft {'created' if draft_id else 'failed'}"

        except Exception as exc:
            logger.exception(
                "Agent raised an exception processing email id={} classification={}",
                email_id,
                classification,
            )
            try:
                self.gmail.apply_label(email_id, "agent-timed-out")
            except Exception:
                pass
            attempts = self._record_failed_attempt(email_id)
            if attempts < MAX_TIMEOUT_RETRIES:
                # Leave the email unmarked so the next poll (after the cooldown)
                # retries it instead of parking it forever.
                return ProcessingResult(
                    email_id=email_id,
                    classification=classification,
                    agent_status="error_will_retry",
                    notes=(
                        f"Exception during processing: {type(exc).__name__} "
                        f"(attempt {attempts}/{MAX_TIMEOUT_RETRIES} — will retry)"
                    ),
                )
            agent_status = "error"
            notes = f"Exception during processing after {attempts} attempts: {type(exc).__name__}: {exc}"

        # Success (or retries exhausted) — clear any retry bookkeeping.
        self._clear_failed_attempts(email_id, success=agent_status != "error")

        # Mark the email so run_loop skips it on the next poll
        try:
            self.gmail.mark_as_processed(email_id)
        except Exception as exc:
            logger.error("Failed to mark email id={} as processed: {}", email_id, exc)

        result_obj = ProcessingResult(
            email_id=email_id,
            classification=classification,
            agent_status=agent_status,
            draft_id=draft_id,
            chat_message_staged=chat_message_staged,
            notes=notes,
        )
        logger.info(
            "Finished processing email id={} classification={} status={}",
            email_id,
            classification,
            agent_status,
        )
        return result_obj

    # ------------------------------------------------------------------
    # 3. Polling loop
    # ------------------------------------------------------------------

    def run_loop(self, interval_seconds: int = 60) -> None:
        """
        Poll Gmail for unread, unprocessed emails every *interval_seconds*
        and call :meth:`process_email` on each one.

        The loop runs indefinitely (use Ctrl-C or a process signal to stop).
        Emails already labeled ``agent-processed`` are excluded from each
        fetch via the Gmail query so they are never re-processed even across
        restarts.

        Args:
            interval_seconds: Seconds to sleep between polling rounds.
        """
        logger.info(
            "Orchestrator loop started (interval={}s, draft_mode={})",
            interval_seconds,
            settings.DRAFT_MODE,
        )

        while True:
            try:
                self._poll_once()
            except KeyboardInterrupt:
                logger.info("Orchestrator loop stopped by user")
                break
            except Exception as exc:
                # Log but never crash the loop on transient errors
                logger.error("Unhandled error in poll cycle: {}", exc)

            logger.debug("Sleeping {}s until next poll", interval_seconds)
            time.sleep(interval_seconds)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_failed_attempt(self, email_id: str) -> int:
        """Increment and persist the failure count for *email_id*; return it."""
        if not email_id:
            return 0
        state = _load_retry_state()
        entry = state.get(email_id, {"attempts": 0})
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        state[email_id] = entry
        _save_retry_state(state)
        return entry["attempts"]

    def _clear_failed_attempts(self, email_id: str, success: bool = True) -> None:
        """Drop retry bookkeeping for *email_id*; on success also remove the label."""
        if not email_id:
            return
        state = _load_retry_state()
        if email_id not in state:
            return
        del state[email_id]
        _save_retry_state(state)
        if success:
            try:
                self.gmail.remove_label(email_id, "agent-timed-out")
            except Exception as exc:
                logger.debug("Could not remove agent-timed-out label id={}: {}", email_id, exc)

    def _in_retry_cooldown(self, email_id: str, retry_state: dict) -> bool:
        """True if *email_id* failed recently and should be skipped this poll."""
        entry = retry_state.get(email_id)
        if not entry:
            return False
        try:
            last = datetime.fromisoformat(entry.get("last_attempt_at", ""))
        except ValueError:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last < timedelta(minutes=RETRY_COOLDOWN_MINUTES)

    def _poll_once(self) -> None:
        """Fetch one page of unprocessed unread emails and dispatch each."""
        # Use the gmail tool's private helpers directly so we can inject the
        # -label:agent-processed filter.  fetch_unread_emails uses a fixed
        # query; reaching for the underlying helpers here is intentional —
        # the orchestrator owns the polling loop and must control the query.
        stubs = self.gmail._list_messages(  # noqa: SLF001
            query="is:unread in:inbox -label:agent-processed",
            max_results=50,
        )

        if not stubs:
            logger.debug("No unprocessed unread emails found")
            return

        logger.info("Poll found {} unprocessed email(s)", len(stubs))

        retry_state = _load_retry_state()

        for stub in stubs:
            message_id: str = stub["id"]
            if self._in_retry_cooldown(message_id, retry_state):
                logger.debug("Skipping email id={} — in retry cooldown", message_id)
                continue
            try:
                email = self.gmail._fetch_full_message(message_id)  # noqa: SLF001
            except Exception as exc:
                logger.error("Failed to fetch email id={}: {}", message_id, exc)
                continue

            result = self.process_email(email)
            logger.info(
                "Poll result: id={} classification={} status={} notes={!r}",
                result.email_id,
                result.classification,
                result.agent_status,
                result.notes,
            )

    def _create_review_draft(self, original_message_id: str, original_subject: str) -> str | None:
        """
        Create a Gmail draft flagging an unclassified email for human review.

        The draft is threaded to the original email so the reviewer has
        full context.  Returns the draft ID on success, or ``None`` if
        draft creation fails.

        PHI note: only the original subject (already a non-PHI header) is
        logged.  Body content is never included in the draft subject line.
        """
        review_subject = f"\u26a0\ufe0f Unclassified email needs review: {original_subject}"
        body = (
            "This email could not be automatically classified by the case manager agent.\n\n"
            "Please review the original message and take appropriate action.\n\n"
            f"Original message ID: {original_message_id}\n"
            "Categories considered: referral, billing, internal, unknown"
        )

        logger.info(
            "Creating review draft for unclassified email id={} subject={!r}",
            original_message_id,
            original_subject,
        )

        try:
            thread_id = self._get_thread_id(original_message_id)
            draft_id = self.gmail.create_draft(
                to=settings.GMAIL_USER_EMAIL,
                subject=review_subject,
                body=body,
                thread_id=thread_id,
            )
            return draft_id
        except Exception as exc:
            logger.error(
                "Failed to create review draft for email id={}: {}",
                original_message_id,
                exc,
            )
            return None

    def _get_thread_id(self, message_id: str) -> str | None:
        """Return the threadId for *message_id*, or ``None`` on failure."""
        try:
            email = self.gmail._fetch_full_message(message_id)  # noqa: SLF001
            return email.get("thread_id") or None
        except Exception:
            return None
