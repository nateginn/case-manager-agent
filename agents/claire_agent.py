"""
Claire — two-way email monitoring assistant.

Watches casemanager.art@gmail.com for new emails, summarises each thread
with context via Ollama, and sends a notification card to the Claire alert
space appearing as cm.assistant.art@gmail.com.  The case manager replies
*within the specific card thread* in Chat; Claire executes the command.

Commands (reply to the specific card thread):
  delete              — move email to Gmail Trash (requires confirm)
  confirm             — confirm a pending delete
  cancel / keep       — cancel a pending delete
  forward greeley     — forward summary to Greeley receptionist space
  forward denver      — forward summary to Denver space
  got it / thanks     — acknowledge only, no email action
  waiting             — silence this thread until new email arrives

Space-level commands (type anywhere in the space):
  new day / clean start — delete ALL Chat messages + wipe state for a fresh start
  reset / clear all     — dismiss all pending notifications (keeps Chat history)
  trash all             — confirm a pending junk batch deletion

Auto-sync: each cycle checks pending/waiting entries against Gmail and
auto-resolves any that are no longer in the inbox (handled manually).

Nudge: pending entries (not "waiting") get one gentle reminder after
CLAIRE_NUDGE_DAYS days if still unresolved.

PHI policy: email body content is passed to local Ollama only — never logged.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ollama
from loguru import logger

from config import settings
from tools.gmail_tool import GmailTool
from tools.google_chat_tool import GoogleChatTool
from utils import atomic_write_json, retry_call
from . import pav_request, sender_lists, visit_inquiry

_STATE_PATH = Path(__file__).parent.parent / "memory" / "claire_state.json"

_ASSISTANT_EMAIL = "cm.assistant.art@gmail.com"

_NOREPLY_RE = re.compile(
    r"(no[_\-]?reply|donotreply|noreply|newsletter|notifications?@|updates?@|"
    r"mailer@|mailchimp|unsubscribe|list-unsubscribe|bounce@|auto-reply)",
    re.IGNORECASE,
)

# Known trusted sender domains — always work, never junk.
_TRUSTED_SENDER_RE = re.compile(
    r"@(marrick\.com|movedocs\.com|healthsps\.com|medrisknet\.com|"
    r"medhub\.health|provepartners\.com|zocdoc\.com|marrickbilling\.com)",
    re.IGNORECASE,
)

# Subject keywords that strongly indicate clinic work email.
_WORK_SUBJECT_RE = re.compile(
    r"(\bPAV\b|\bEOB\b|authoriz|scheduling|appoint|billing|credentiat|"
    r"records?|treatment|therapy|patient|script|\breferral\b|\bDOB\b|\bDOI\b|"
    r"\bvisits?\b|attend|\bDOS\b|"
    r"urgent|marrick|\bART\b|chiro|shockwave|insurance|"
    r"\d{1,2}[/\.]\d{1,2}[/\.]\d{2,4})",  # date pattern like 04.16.1980 or 8/10/1988
    re.IGNORECASE,
)

# Prefixes of messages Claire sends — used to filter self-messages after restart.
_CLAIRE_MSG_PREFIXES = (
    "📬 Showing", "📬 Sent ", "📭 No more", "✅ Cleared", "✅ Sent ",
    "⏰ ", "📋 Reminder", "↩️ ", "🗑️ ", "Got it —", "Forwarded to",
    "Done —", "Cancelled —", "Skipped —", "⚠️ Confirm",
    "Waiting for confirmation", "I didn't understand",
)

_ollama_client = ollama.Client(timeout=30)


class ClaireAgent:
    """Two-way email notification assistant."""

    def __init__(self) -> None:
        self.gmail = GmailTool()

        token_path = Path(settings.CLAIRE_ASSISTANT_TOKEN_PATH)
        self._chat = GoogleChatTool(settings.CLAIRE_ALERT_SPACE_ID, token_path=token_path)

        self._state_lock = threading.Lock()
        self._processed_chat_names: set[str] = set()
        self._sent_names: list[str] = []
        self._last_chat_poll: datetime = datetime.now(timezone.utc) - timedelta(minutes=5)

        # Seed sent-message tracking from persisted state so a restart doesn't
        # forget which Chat messages are Claire's own.
        persisted = self._load_state().get("sent_message_names", [])
        self._sent_names = list(persisted)
        self._processed_chat_names.update(persisted)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_cycle(self) -> dict:
        """Run one full Claire cycle."""
        summary: dict = {"synced": 0, "expired": 0, "nudged": 0, "replies_handled": 0, "new_notifications": 0, "errors": 0}
        try:
            summary["synced"] = self._sync_pending_state()
            summary["expired"] = self._expire_stale_entries()
            summary["nudged"] = self._send_nudges()
            summary["replies_handled"] = self._poll_chat_replies()
            summary["new_notifications"] = self._scan_new_emails()
        except Exception as exc:
            logger.error("ClaireAgent.run_cycle error: {}", exc)
            summary["errors"] += 1
        return summary

    # ------------------------------------------------------------------
    # Cycle steps
    # ------------------------------------------------------------------

    def _sync_pending_state(self) -> int:
        """Auto-resolve pending/waiting entries whose email is no longer in the inbox."""
        state = self._load_state()
        resolved = 0

        for entry in list(state["emails"].values()):
            if entry.get("status") not in ("pending", "waiting"):
                continue
            email_id = entry.get("email_id", "")
            if not email_id:
                continue
            try:
                msg = self.gmail._service.users().messages().get(
                    userId=settings.GMAIL_USER_EMAIL,
                    id=email_id,
                    format="minimal",
                ).execute()
                label_ids = msg.get("labelIds", [])
                if "INBOX" not in label_ids:
                    entry["status"] = "resolved"
                    entry["action_taken"] = "handled_in_gmail"
                    entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    resolved += 1
                    logger.info("Claire: auto-resolved email_id={} (no longer in INBOX)", email_id)
            except Exception as exc:
                logger.debug("Claire._sync_pending_state fetch failed email_id={}: {}", email_id, exc)

        if resolved:
            self._save_state(state)

        logger.debug("Claire._sync_pending_state: {} entries auto-resolved", resolved)
        return resolved

    def _scan_new_emails(self) -> int:
        """Fetch inbox emails, deduplicate by thread, notify Chat once per thread."""
        emails = self.gmail.fetch_inbox_emails(max_results=50)
        logger.info("Claire._scan_new_emails: {} inbox emails", len(emails))

        state = self._load_state()

        # Deduplicate: one entry per thread_id, newest message first (API order).
        seen_threads: set[str] = set()
        deduped: list[dict] = []
        for email in emails:
            tid = email.get("thread_id") or email.get("id", "")
            if tid in seen_threads:
                continue
            seen_threads.add(tid)
            deduped.append(email)

        logger.info("Claire._scan_new_emails: {} unique threads", len(deduped))

        # Threads already waiting in the queue must not be re-scanned on every cycle.
        # Only drain the queue when the user explicitly says "next 10".
        queued_thread_ids: set[str] = {q.get("thread_id", "") for q in state.get("notification_queue", [])}

        # Threads previously classified as junk stay junk — don't re-ask GLM every cycle.
        seen_junk_thread_ids: set[str] = set(state.get("seen_junk_threads", []))

        # Load learned sender lists once per scan, not once per email.
        learned_lists = sender_lists.load_lists()

        junk_batch: list[dict] = []
        to_notify: list[dict] = []
        auto_drafted = 0

        for email in deduped:
            email_id = email.get("id", "")
            thread_id = email.get("thread_id", "") or email_id
            if not email_id:
                continue

            # Check for existing state entry for this thread.
            existing = state["emails"].get(thread_id)
            if existing:
                existing_status = existing.get("status", "")
                if existing_status in ("pending", "waiting"):
                    if existing.get("email_id") == email_id:
                        logger.debug("Claire: thread unchanged, skipping thread_id={}", thread_id)
                        continue
                    logger.info("Claire: new activity in thread_id={}, re-notifying", thread_id)
                elif existing_status in ("resolved", "expired"):
                    pass  # Treat as fresh — fall through.

            # Already waiting in the queue — don't re-queue until user requests next batch.
            if thread_id in queued_thread_ids:
                logger.debug("Claire: thread already queued, skipping thread_id={}", thread_id)
                continue

            # Already classified as junk this session — skip without re-asking GLM.
            if thread_id in seen_junk_thread_ids:
                logger.debug("Claire: thread already seen as junk, skipping thread_id={}", thread_id)
                continue

            if self._is_automated(email):
                logger.debug("Claire: skipping automated sender {}", email.get("sender", "")[:50])
                continue
            if self._is_junk(email, learned_lists):
                logger.debug("Claire: queuing junk email_id={}", email_id)
                junk_batch.append(email)
                seen_junk_thread_ids.add(thread_id)
                continue

            # Marrick PAV auto-forward: deterministic detection + forward
            # draft to the billing team. One attempt per thread.
            if (
                settings.CLAIRE_PAV_FORWARD_ENABLED
                and thread_id not in state.get("pav_requests", {})
            ):
                result = pav_request.try_handle(email, self.gmail)
                if result is not None:
                    state.setdefault("pav_requests", {})[thread_id] = {
                        "status": result["status"],
                        "email_id": email_id,
                        "handled_at": datetime.now(timezone.utc).isoformat(),
                    }
                    if result["status"] == "drafted":
                        sender_short = (email.get("sender", "") or "")[:60]
                        att = result["attachment_count"]
                        self._send_tracked(
                            f"📄 *Marrick PAV request auto-handled*\n"
                            f"From: {sender_short}\n"
                            f"Subject: {email.get('subject', '(no subject)')[:80]}\n"
                            f"➡️ A forward *draft* to "
                            f"{settings.CLAIRE_BILLING_FORWARD_NAME} "
                            f"({att} attachment{'s' if att != 1 else ''}) is waiting "
                            f"in Gmail Drafts — review and send."
                        )
                        # Track as pending so sync/expiry still applies to the thread.
                        state["emails"][thread_id] = {
                            "email_id": email_id,
                            "thread_id": thread_id,
                            "status": "pending",
                            "subject": email.get("subject", "(no subject)"),
                            "sender": email.get("sender", ""),
                            "notified_at": datetime.now(timezone.utc).isoformat(),
                            "expires_at": (
                                datetime.now(timezone.utc)
                                + timedelta(hours=settings.CLAIRE_REPLY_TIMEOUT_HOURS)
                            ).isoformat(),
                            "pav_request": True,
                        }
                        auto_drafted += 1
                        continue
                    # draft_error — fall through to the normal notification
                    # with a note about what was attempted.
                    email["_pav_note"] = (
                        "(I detected a Marrick PAV request but couldn't create "
                        "the forward draft — please forward it to billing manually.)"
                    )

            # Visit-inquiry auto-draft: classify + EMR lookup + reply-all draft.
            # One attempt per thread — success or failure is recorded so we
            # never re-run the EMR for the same thread on every cycle.
            if (
                settings.CLAIRE_EMR_LOOKUP_ENABLED
                and thread_id not in state.get("visit_inquiries", {})
            ):
                result = visit_inquiry.try_handle(email, self.gmail)
                if result is not None:
                    state.setdefault("visit_inquiries", {})[thread_id] = {
                        "status": result["status"],
                        "email_id": email_id,
                        "handled_at": datetime.now(timezone.utc).isoformat(),
                    }
                    if result["status"] == "drafted":
                        sender_short = (email.get("sender", "") or "")[:60]
                        self._send_tracked(
                            f"📋 *Visit inquiry auto-handled*\n"
                            f"From: {sender_short}\n"
                            f"Patient: {result['patient_name']}\n"
                            f"Found {result['past_count']} past / {result['future_count']} upcoming visits.\n"
                            f"➡️ A reply-all *draft* is waiting in Gmail Drafts — "
                            f"please verify the requester is legitimate, then review and send."
                        )
                        # Track as pending so sync/expiry still applies to the thread.
                        state["emails"][thread_id] = {
                            "email_id": email_id,
                            "thread_id": thread_id,
                            "status": "pending",
                            "subject": email.get("subject", "(no subject)"),
                            "sender": email.get("sender", ""),
                            "notified_at": datetime.now(timezone.utc).isoformat(),
                            "expires_at": (
                                datetime.now(timezone.utc)
                                + timedelta(hours=settings.CLAIRE_REPLY_TIMEOUT_HOURS)
                            ).isoformat(),
                            "visit_inquiry": True,
                        }
                        auto_drafted += 1
                        continue
                    # patient_not_found / emr_error — fall through to the normal
                    # notification so the case manager handles it manually, with
                    # a note about what was attempted.
                    email["_visit_inquiry_note"] = (
                        f"(I detected a visit inquiry for patient "
                        f"'{result['patient_name']}' but "
                        + (
                            "couldn't find them in Prompt EMR."
                            if result["status"] == "patient_not_found"
                            else "the EMR lookup failed — my login session may need a refresh."
                        )
                        + ")"
                    )

            to_notify.append(email)

        # Send first batch immediately; queue the rest.
        batch_size = settings.CLAIRE_NOTIFICATION_BATCH_SIZE
        send_now = to_notify[:batch_size]
        queue_rest = to_notify[batch_size:]

        # Also include any already-queued items that haven't been sent yet.
        existing_queue = state.get("notification_queue", [])

        notified = self._send_notification_batch(send_now, state)

        if queue_rest:
            # Serialize to queue: store minimal fields needed to rebuild notification.
            new_queue_entries = [
                {
                    "email_id": e.get("id", ""),
                    "thread_id": e.get("thread_id", "") or e.get("id", ""),
                    "sender": e.get("sender", ""),
                    "subject": e.get("subject", "(no subject)"),
                    "body_text": (e.get("body_text", "") or "")[:800],
                }
                for e in queue_rest
            ]
            state["notification_queue"] = existing_queue + new_queue_entries
            remaining = len(state["notification_queue"])
            self._send_tracked(
                f"📬 Showing {notified} of {notified + remaining} emails. "
                f"Say *next 10* to see {min(batch_size, remaining)} more."
            )
        else:
            state["notification_queue"] = existing_queue

        # Persist the updated junk-seen set so threads stay suppressed across cycles.
        state["seen_junk_threads"] = list(seen_junk_thread_ids)

        # Send one batched junk notification if any junk was found.
        if junk_batch:
            self._notify_junk_batch(junk_batch, state)

        if notified or junk_batch or queue_rest or seen_junk_thread_ids or auto_drafted:
            self._save_state(state)

        logger.info("Claire._scan_new_emails: {} new notifications, {} queued, {} junk, {} auto-drafted",
                    notified, len(queue_rest), len(junk_batch), auto_drafted)
        return notified + auto_drafted

    def _send_notification_batch(self, emails: list[dict], state: dict) -> int:
        """Send Chat notifications for a list of emails; update state in place. Returns count sent."""
        notified = 0
        for email in emails:
            email_id = email.get("email_id") or email.get("id", "")
            thread_id = email.get("thread_id", "") or email_id
            # Reconstruct email dict if coming from queue (has email_id key instead of id).
            if "id" not in email:
                email = dict(email)
                email["id"] = email_id

            thread_history = self.gmail.fetch_thread(thread_id, email_id)
            msg_result = self._notify_chat(email, thread_history)
            if msg_result is None:
                logger.warning("Claire: Chat notification failed for email_id={}", email_id)
                continue

            now = datetime.now(timezone.utc)
            state["emails"][thread_id] = {
                "email_id": email_id,
                "thread_id": thread_id,
                "subject": email.get("subject", "(no subject)"),
                "sender": email.get("sender", ""),
                "notified_at": now.isoformat(),
                "chat_message_name": msg_result["name"],
                "chat_thread_name": msg_result["thread_name"],
                "status": "pending",
                "pending_action": None,
                "reply_text": None,
                "reply_received_at": None,
                "resolved_at": None,
                "expires_at": (now + timedelta(hours=settings.CLAIRE_REPLY_TIMEOUT_HOURS)).isoformat(),
                "action_taken": None,
                "nudged_at": None,
            }
            notified += 1
        return notified

    def _poll_chat_replies(self) -> int:
        """Check the Claire Chat space for new replies from the case manager."""
        messages = self._chat.list_messages(after_time=self._last_chat_poll)
        self._last_chat_poll = datetime.now(timezone.utc)

        state = self._load_state()
        handled = 0
        state_dirty = False

        # Build thread_name → state entry lookup.
        thread_to_entry: dict[str, dict] = {}
        for entry in state["emails"].values():
            tn = entry.get("chat_thread_name", "")
            if tn:
                thread_to_entry[tn] = entry

        # Build thread_name → junk batch lookup.
        thread_to_junk: dict[str, dict] = {}
        for batch in state.get("junk_batches", {}).values():
            tn = batch.get("chat_thread_name", "")
            if tn:
                thread_to_junk[tn] = batch

        for msg in messages:
            name = msg.get("name", "")
            if name in self._processed_chat_names:
                continue

            sender_email = msg.get("sender_email", "").lower()
            sender_type = msg.get("sender_type", "HUMAN")
            text = msg.get("text", "").strip()
            thread_name = msg.get("thread_name", "")

            logger.debug(
                "Claire._poll_chat_replies: msg={} sender={!r} thread={} text={!r}",
                name, sender_email, thread_name, text[:60],
            )

            # Skip own messages. Primary signal is the persisted sent-message
            # name set (survives restarts); the prefix check is a last-resort
            # fallback used only when the API gives us no sender identity, so
            # a user message that happens to quote a card is never swallowed.
            is_own = (
                sender_email == _ASSISTANT_EMAIL.lower()
                or sender_type == "BOT"
                or (not sender_email and text.startswith(_CLAIRE_MSG_PREFIXES))
            )
            if is_own:
                self._processed_chat_names.add(name)
                continue

            if not text:
                self._processed_chat_names.add(name)
                continue

            lower_text = text.lower().strip()

            # --- Space-level: next batch ---
            if any(kw in lower_text for kw in ("next 10", "next batch", "more", "next")):
                queue = state.get("notification_queue", [])
                if not queue:
                    self._send_tracked("📭 No more emails in the queue — you're all caught up.")
                else:
                    batch_size = settings.CLAIRE_NOTIFICATION_BATCH_SIZE
                    batch = queue[:batch_size]
                    state["notification_queue"] = queue[batch_size:]
                    sent = self._send_notification_batch(batch, state)
                    self._save_state(state)
                    remaining = len(state["notification_queue"])
                    if remaining:
                        self._send_tracked(
                            f"📬 Sent {sent} more. {remaining} still in queue — say *next 10* to continue."
                        )
                    else:
                        self._send_tracked(f"✅ Sent {sent} more — queue is now empty.")
                self._processed_chat_names.add(name)
                handled += 1
                state_dirty = True
                continue

            # --- Space-level: new day (delete all messages + wipe state) ---
            if any(kw in lower_text for kw in ("new day", "clean start", "fresh start", "start fresh")):
                deleted = self._chat.delete_all_messages()
                self._processed_chat_names.clear()
                self._sent_names.clear()
                self._save_state(self._fresh_state())
                self._send_tracked(
                    f"🌅 Fresh start — {deleted} message{'s' if deleted != 1 else ''} cleared. "
                    "Claire is watching your inbox."
                )
                handled += 1
                state_dirty = False
                continue

            # --- Space-level reset command (no thread match needed) ---
            if any(kw in lower_text for kw in ("reset", "clear all", "start over")):
                count = 0
                for entry in state["emails"].values():
                    if entry.get("status") == "pending":
                        entry["status"] = "resolved"
                        entry["action_taken"] = "manual_reset"
                        entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
                        count += 1
                self._send_tracked(
                    f"✅ Cleared — {count} pending notification{'s' if count != 1 else ''} dismissed."
                )
                self._processed_chat_names.add(name)
                handled += 1
                state_dirty = True
                continue

            # --- Match to a specific thread ---
            matched_entry = thread_to_entry.get(thread_name)
            matched_junk = thread_to_junk.get(thread_name)

            if matched_entry is None and matched_junk is None:
                # Unthreaded message — send reminder if there are pending items.
                pending_count = sum(1 for e in state["emails"].values() if e.get("status") == "pending")
                if pending_count > 0:
                    self._send_tracked(
                        "↩️ To respond to a specific email, please tap *Reply* on the notification card.\n"
                        "Or say *reset* to clear all pending notifications."
                    )
                self._processed_chat_names.add(name)
                continue

            # --- Handle junk batch reply ---
            if matched_junk is not None:
                self._handle_junk_reply(lower_text, matched_junk, state)
                self._processed_chat_names.add(name)
                handled += 1
                state_dirty = True
                continue

            # --- Handle work email reply ---
            entry = matched_entry

            # Delete confirmation flow.
            if entry.get("pending_action") == "delete":
                subject = entry.get("subject", "(no subject)")
                if any(kw in lower_text for kw in ("confirm", "yes")):
                    ok = self.gmail.delete_message(entry["email_id"])
                    if ok:
                        entry["status"] = "resolved"
                        entry["action_taken"] = "deleted"
                        entry["pending_action"] = None
                        entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
                        self._reply_tracked(
                            f"Done — email moved to trash.\nSubject: {subject}",
                            thread_name,
                        )
                    else:
                        self._reply_tracked(
                            f"Could not trash that email — please delete manually.\nSubject: {subject}",
                            thread_name,
                        )
                elif any(kw in lower_text for kw in ("cancel", "no", "keep")):
                    entry["pending_action"] = None
                    self._reply_tracked(
                        f"Cancelled — email kept in inbox.\nSubject: {subject}",
                        thread_name,
                    )
                else:
                    self._reply_tracked(
                        f"Waiting for confirmation.\nSubject: {subject}\n"
                        f"Reply *confirm* to trash or *cancel* to keep it.",
                        thread_name,
                    )
                self._processed_chat_names.add(name)
                handled += 1
                state_dirty = True
                continue

            # Normal command flow.
            cmd = self._parse_command(text)
            action = cmd.get("action", "unknown")

            if action == "delete":
                entry["pending_action"] = "delete"
                entry["reply_text"] = text
                entry["reply_received_at"] = datetime.now(timezone.utc).isoformat()
                self._reply_tracked(
                    f"⚠️ Confirm: move this email to trash?\n"
                    f"From: {entry.get('sender', '')}\n"
                    f"Subject: {entry.get('subject', '(no subject)')}\n"
                    f"Reply *confirm* to proceed or *cancel* to keep it.",
                    thread_name,
                )
            elif action == "waiting":
                entry["status"] = "waiting"
                entry["reply_text"] = text
                entry["reply_received_at"] = datetime.now(timezone.utc).isoformat()
                self._reply_tracked(
                    f"Got it — I'll stay quiet on this thread until something new arrives.\n"
                    f"Subject: {entry.get('subject', '(no subject)')}",
                    thread_name,
                )
            elif action != "unknown":
                self._execute_command(cmd, entry, thread_name)
                entry["status"] = "resolved"
                entry["reply_text"] = text
                entry["reply_received_at"] = datetime.now(timezone.utc).isoformat()
                entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
                entry["action_taken"] = action
            else:
                self._reply_tracked(
                    "I didn't understand that. Reply with:\n"
                    "  delete | forward greeley | forward denver | got it | waiting",
                    thread_name,
                )

            self._processed_chat_names.add(name)
            handled += 1
            state_dirty = True

        if state_dirty:
            self._save_state(state)

        return handled

    def _expire_stale_entries(self) -> int:
        """Mark pending entries that have passed their expiry time."""
        state = self._load_state()
        now = datetime.now(timezone.utc)
        expired_subjects = []

        for entry in state["emails"].values():
            if entry.get("status") not in ("pending",):
                continue
            try:
                expires_at = datetime.fromisoformat(entry["expires_at"])
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if now >= expires_at:
                entry["status"] = "expired"
                entry["action_taken"] = "expired"
                entry["resolved_at"] = now.isoformat()
                expired_subjects.append(entry.get("subject", "(no subject)"))

        if expired_subjects:
            self._save_state(state)
            subjects_text = "\n".join(f"  • {s}" for s in expired_subjects)
            self._send_tracked(f"⏰ No reply received — marked expired:\n{subjects_text}")
            logger.info("Claire: {} entries expired", len(expired_subjects))

        return len(expired_subjects)

    def _send_nudges(self) -> int:
        """Send one gentle reminder per pending entry older than CLAIRE_NUDGE_DAYS."""
        state = self._load_state()
        now = datetime.now(timezone.utc)
        nudge_threshold = timedelta(days=settings.CLAIRE_NUDGE_DAYS)
        nudged = 0

        for entry in state["emails"].values():
            if entry.get("status") != "pending":
                continue
            if entry.get("nudged_at"):
                continue
            try:
                notified_at = datetime.fromisoformat(entry["notified_at"])
                if notified_at.tzinfo is None:
                    notified_at = notified_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if now - notified_at < nudge_threshold:
                continue

            subject = entry.get("subject", "(no subject)")
            sender = entry.get("sender", "unknown")
            thread_name = entry.get("chat_thread_name", "")
            reminder = f"📋 Reminder: \"{subject}\" from {sender} — still needs a response."
            self._chat.reply_in_thread(reminder, thread_name)
            entry["nudged_at"] = now.isoformat()
            nudged += 1

        if nudged:
            self._save_state(state)

        logger.debug("Claire._send_nudges: {} nudges sent", nudged)
        return nudged

    # ------------------------------------------------------------------
    # Junk batch handling
    # ------------------------------------------------------------------

    def _notify_junk_batch(self, junk_emails: list[dict], state: dict) -> None:
        """Send a single batched junk notification card and store in state."""
        lines = [f"🗑️ {len(junk_emails)} likely junk email{'s' if len(junk_emails) != 1 else ''} found:"]
        for e in junk_emails:
            sender = e.get("sender", "unknown")
            subject = e.get("subject", "(no subject)")
            lines.append(f"  • {subject} — {sender}")
        lines.append('Reply "trash all" to move them to Trash, or "skip" to leave them.')
        text = "\n".join(lines)

        result = self._chat.send_message_full(text)
        if not result:
            logger.warning("Claire: failed to send junk batch notification")
            return
        self._record_sent(result.get("name"))

        batch_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        if "junk_batches" not in state:
            state["junk_batches"] = {}
        state["junk_batches"][batch_id] = {
            "batch_id": batch_id,
            "items": [
                {
                    "email_id": e.get("id", ""),
                    "thread_id": e.get("thread_id", "") or e.get("id", ""),
                    "sender": e.get("sender", ""),
                    "subject": e.get("subject", ""),
                }
                for e in junk_emails
            ],
            "notified_at": now.isoformat(),
            "chat_message_name": result["name"],
            "chat_thread_name": result["thread_name"],
            "status": "pending",
            "expires_at": (now + timedelta(hours=settings.CLAIRE_REPLY_TIMEOUT_HOURS)).isoformat(),
        }
        logger.info("Claire: junk batch notification sent ({} items)", len(junk_emails))

    def _handle_junk_reply(self, lower_text: str, batch: dict, state: dict) -> None:
        """Process a reply to a junk batch card."""
        thread_name = batch.get("chat_thread_name", "")
        batch_senders = [item.get("sender", "") for item in batch.get("items", [])]
        if any(kw in lower_text for kw in ("trash all", "yes", "confirm")):
            trashed = 0
            for item in batch.get("items", []):
                if self.gmail.delete_message(item["email_id"]):
                    trashed += 1
            batch["status"] = "resolved"
            learned = 0
            try:
                learned = sender_lists.add_to_blocklist(batch_senders)
            except Exception as exc:
                logger.warning("Claire: blocklist learning failed: {}", exc)
            learned_note = "\nLearned — future emails from these senders go straight to junk." if learned else ""
            self._reply_tracked(
                f"Done — {trashed} email{'s' if trashed != 1 else ''} moved to trash.{learned_note}",
                thread_name,
            )
        elif any(kw in lower_text for kw in ("skip", "no", "keep")):
            batch["status"] = "resolved"
            # Un-junk these threads so they re-enter the work queue on the next cycle.
            skipped_thread_ids = {item.get("thread_id", "") or item.get("email_id", "") for item in batch.get("items", [])}
            seen = set(state.get("seen_junk_threads", []))
            seen -= skipped_thread_ids
            state["seen_junk_threads"] = list(seen)
            learned = 0
            try:
                learned = sender_lists.add_to_allowlist(batch_senders)
            except Exception as exc:
                logger.warning("Claire: allowlist learning failed: {}", exc)
            learned_note = "\nLearned — these senders will be treated as work from now on." if learned else ""
            self._reply_tracked(
                f"Skipped — emails left in inbox. They'll appear as work notifications next cycle.{learned_note}",
                thread_name,
            )
        else:
            self._reply_tracked(
                'Reply "trash all" to delete or "skip" to leave them.',
                thread_name,
            )

    # ------------------------------------------------------------------
    # Notification building
    # ------------------------------------------------------------------

    def _notify_chat(self, email: dict, thread_history: list[dict]) -> dict | None:
        """Build and send the Chat notification. Returns {name, thread_name} or None."""
        text = self._build_notification(email, thread_history)
        result = self._chat.send_message_full(text)
        if result:
            self._record_sent(result.get("name"))
        return result

    def _build_notification(self, email: dict, thread_history: list[dict]) -> str:
        """Compose the Claire notification card with full-thread Ollama summary."""
        sender = email.get("sender", "unknown sender")
        subject = email.get("subject", "(no subject)")
        current_body = email.get("body_text", "") or email.get("body_html", "")
        total_count = len(thread_history) + 1  # prior + current

        # Build full thread text for Ollama (prior messages + current).
        thread_parts = []
        for m in thread_history:
            subj = m.get("subject", "")
            subj_line = f"Subject: {subj}\n" if subj else ""
            thread_parts.append(
                f"From: {m.get('sender','')}\nDate: {m.get('date','')}\n{subj_line}{m.get('body','')[:1200]}"
            )
        thread_parts.append(f"From: {sender}\nSubject: {subject}\n[Most recent message]\n{current_body[:2000]}")
        full_thread_text = "\n---\n".join(thread_parts)

        # Ollama summary of the full thread.
        thread_summary = f"{total_count} message{'s' if total_count != 1 else ''} in thread."
        try:
            model = settings.OLLAMA_LIGHT_MODEL or settings.OLLAMA_MODEL
            resp = retry_call(lambda: _ollama_client.generate(
                model=model,
                prompt=(
                    "Summarize this email thread for a chiropractic/PT clinic case manager. "
                    "Output ONLY the summary — no notes, no headers, no meta-commentary, no disclaimers. "
                    "Write 2-3 plain sentences maximum. "
                    "First sentence: what the most recent message is asking or saying. "
                    "Second sentence (if thread has prior messages): one sentence of prior context. "
                    "Do not mention patient names. Do not explain these instructions.\n\n"
                    f"{full_thread_text}"
                ),
                options={"num_predict": 250},
                think=False,
            ), retries=1, label="ollama.summary")
            raw = getattr(resp, "response", None) or (resp.get("response", "") if isinstance(resp, dict) else "")
            summary = raw.strip()
            # Strip leaked reasoning blocks the model sometimes appends.
            for marker in ("**Note:", "Note:", "(Note:", "**Summary:", "**Prior context:", "**Most recent"):
                if marker in summary:
                    summary = summary[:summary.index(marker)].strip()
            if summary:
                thread_summary = summary
        except Exception as exc:
            logger.warning("Claire: Ollama summary failed (showing count): {}", exc)

        # Ollama suggested action.
        suggested_line = ""
        try:
            model = settings.OLLAMA_LIGHT_MODEL or settings.OLLAMA_MODEL
            resp = retry_call(lambda: _ollama_client.generate(
                model=model,
                prompt=(
                    "Given this email thread about a chiropractic/PT clinic, what is the most appropriate "
                    "response? Reply with exactly one of: forward_greeley, forward_denver, acknowledge, needs_attention.\n\n"
                    f"Subject: {subject}\nSummary: {thread_summary}"
                ),
                options={"num_predict": 10},
                think=False,
            ), retries=1, label="ollama.suggest")
            raw2 = getattr(resp, "response", None) or (resp.get("response", "") if isinstance(resp, dict) else "")
            answer = raw2.strip().lower()
            label_map = {
                "forward_greeley": "forward to Greeley",
                "forward_denver": "forward to Denver",
                "acknowledge": "acknowledge (got it)",
                "needs_attention": "needs your attention",
            }
            for key, label in label_map.items():
                if key.replace("_", "") in answer.replace("_", ""):
                    suggested_line = f"\n💡 Suggested: {label}"
                    break
        except Exception as exc:
            logger.warning("Claire: Ollama suggested action failed: {}", exc)

        thread_note = f"({total_count} message{'s' if total_count != 1 else ''} in thread)" if total_count > 1 else ""

        # Note from a visit-inquiry attempt that couldn't complete (patient not
        # found / EMR error) — tells the case manager what was already tried.
        inquiry_note = email.get("_visit_inquiry_note", "") or email.get("_pav_note", "")
        inquiry_line = f"\n⚕️ {inquiry_note}" if inquiry_note else ""

        return (
            f"📬 {sender}\n"
            f"Subject: {subject}"
            + (f" {thread_note}" if thread_note else "") + "\n\n"
            f"{thread_summary}"
            f"{suggested_line}"
            f"{inquiry_line}\n\n"
            f"→ Reply: delete | forward greeley | forward denver | got it | waiting"
        )

    # ------------------------------------------------------------------
    # Junk / automated detection
    # ------------------------------------------------------------------

    def _is_automated(self, email: dict) -> bool:
        return bool(_NOREPLY_RE.search(email.get("sender", "")))

    def _is_junk(self, email: dict, learned: dict | None = None) -> bool:
        """
        Classify email as junk. Learned sender lists first (user decisions),
        then rules (fast + reliable), Ollama only for ambiguous.
        """
        sender = email.get("sender", "")
        subject = email.get("subject", "")
        if not sender and not subject:
            return False

        # Learned decisions from past "trash all" / "skip" replies win over
        # the static rules — the case manager has already told us the answer.
        verdict = sender_lists.check(sender, learned if learned is not None else sender_lists.load_lists())
        if verdict == "allow":
            return False
        if verdict == "block":
            logger.debug("Claire._is_junk: learned blocklist match")
            return True

        # Trusted sender domains are always work — no LLM call needed.
        if _TRUSTED_SENDER_RE.search(sender):
            return False

        # Work-like subject keywords strongly indicate clinic email.
        if _WORK_SUBJECT_RE.search(subject):
            return False

        # Ambiguous — ask Ollama only if rules don't decide.
        # Default to work (fail-open): in a clinic inbox almost everything is relevant.
        # Only mark junk if the model is clearly confident it's unsolicited marketing/spam.
        try:
            model = settings.OLLAMA_LIGHT_MODEL or settings.OLLAMA_MODEL
            resp = retry_call(lambda: _ollama_client.generate(
                model=model,
                prompt=(
                    "You classify emails for a chiropractic/PT clinic case manager.\n"
                    "WORK: billing, authorizations, insurance, scheduling, treatment, records, "
                    "legal funding, credentialing, case management, PAV forms, EOBs, scripts, "
                    "anything that could relate to patients or clinic operations.\n"
                    "If the subject mentions a person's name, a visit, an appointment, or "
                    "attendance, it is WORK — patients, attorneys, and case managers often "
                    "write from personal gmail/outlook addresses.\n"
                    "JUNK: unsolicited commercial marketing, spam, mass newsletters, "
                    "promotions from non-healthcare companies.\n"
                    "When in doubt, reply 'work'. Only reply 'junk' when you are confident "
                    "this is unsolicited marketing unrelated to the clinic.\n"
                    "Reply with exactly one word: 'work' or 'junk'.\n"
                    f"From: {sender}\nSubject: {subject}"
                ),
                options={"num_predict": 5},
                think=False,
            ), retries=1, label="ollama.junk")
            raw = getattr(resp, "response", None) or (resp.get("response", "") if isinstance(resp, dict) else "")
            answer = raw.strip().lower()
            return "junk" in answer and "work" not in answer
        except Exception as exc:
            logger.warning("Claire._is_junk Ollama error (fail-open): {}", exc)
            return False

    # ------------------------------------------------------------------
    # Command parsing and execution
    # ------------------------------------------------------------------

    def _parse_command(self, text: str) -> dict:
        """Parse a plain-language command. Returns {action: str, target: str | None}."""
        lower = text.lower()

        if "delete" in lower or "trash" in lower:
            return {"action": "delete", "target": None}
        if "greeley" in lower:
            return {"action": "forward", "target": "greeley"}
        if "denver" in lower:
            return {"action": "forward", "target": "denver"}
        if any(kw in lower for kw in ("waiting", "monitoring", "watching", "hold")):
            return {"action": "waiting", "target": None}
        if any(kw in lower for kw in ("got it", "thanks", "thank you", "handle", "i'll", "will do", "ok", "okay", "acknowledge")):
            return {"action": "acknowledge", "target": None}

        # Ollama fallback — delete intentionally excluded from fallback.
        try:
            model = settings.OLLAMA_LIGHT_MODEL or settings.OLLAMA_MODEL
            resp = retry_call(lambda: _ollama_client.generate(
                model=model,
                prompt=(
                    "What action does this reply request? Reply with exactly one of: "
                    "forward_greeley, forward_denver, acknowledge, waiting, unknown.\n"
                    f"Reply text: '{text}'"
                ),
                options={"num_predict": 10},
                think=False,
            ), retries=1, label="ollama.command")
            raw = getattr(resp, "response", None) or (resp.get("response", "") if isinstance(resp, dict) else "")
            answer = raw.strip().lower()
            if "greeley" in answer:
                return {"action": "forward", "target": "greeley"}
            if "denver" in answer:
                return {"action": "forward", "target": "denver"}
            if "waiting" in answer:
                return {"action": "waiting", "target": None}
            if "acknowledge" in answer:
                return {"action": "acknowledge", "target": None}
        except Exception as exc:
            logger.warning("Claire._parse_command Ollama fallback error: {}", exc)

        return {"action": "unknown", "target": None}

    def _execute_command(self, cmd: dict, entry: dict, thread_name: str) -> None:
        """Execute the parsed command and reply in the notification thread."""
        action = cmd.get("action", "unknown")
        target = cmd.get("target")
        subject = entry.get("subject", "(no subject)")
        sender = entry.get("sender", "")

        if action == "forward":
            space_id = self._resolve_forward_space(target)
            if space_id:
                fwd_tool = GoogleChatTool(space_id)
                fwd_tool.send_message(
                    f"📨 New email for your attention\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}"
                )
                self._reply_tracked(
                    f"Forwarded to {target.title()} receptionist.\nSubject: {subject}",
                    thread_name,
                )
            else:
                self._reply_tracked(
                    f"No Chat space configured for '{target}'. "
                    "Set CLAIRE_GREELEY_SPACE_ID or CLAIRE_DENVER_SPACE_ID in .env.",
                    thread_name,
                )

        elif action == "acknowledge":
            self._chat.reply_in_thread(f"Got it — marked as reviewed.\nSubject: {subject}", thread_name)

        else:
            self._reply_tracked(
                "I didn't understand that. Reply with:\n"
                "  delete | forward greeley | forward denver | got it | waiting",
                thread_name,
            )

    def _resolve_forward_space(self, target: str | None) -> str:
        if target == "greeley":
            return settings.CLAIRE_GREELEY_SPACE_ID or settings.GOOGLE_CHAT_SPACE_GREELEY
        if target == "denver":
            return settings.CLAIRE_DENVER_SPACE_ID or settings.GOOGLE_CHAT_SPACE_DENVER
        return ""

    def _record_sent(self, name: str | None) -> None:
        """Track a Chat message Claire sent so replies polling never re-processes it."""
        if not name:
            return
        self._processed_chat_names.add(name)
        self._sent_names.append(name)

    def _send_tracked(self, text: str) -> dict | None:
        """Send a status/system message and track its name so it won't be re-processed."""
        result = self._chat.send_message_full(text)
        if result:
            self._record_sent(result.get("name"))
        return result

    def _reply_tracked(self, text: str, thread_name: str) -> str | None:
        """Reply in a thread and track the sent message name."""
        name = self._chat.reply_in_thread(text, thread_name)
        self._record_sent(name)
        return name

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _fresh_state() -> dict:
        return {
            "emails": {},
            "junk_batches": {},
            "notification_queue": [],
            "seen_junk_threads": [],
            "sent_message_names": [],
        }

    def _load_state(self) -> dict:
        with self._state_lock:
            if not _STATE_PATH.exists():
                return self._fresh_state()
            try:
                data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
                if "junk_batches" not in data:
                    data["junk_batches"] = {}
                if "seen_junk_threads" not in data:
                    data["seen_junk_threads"] = []
                if "sent_message_names" not in data:
                    data["sent_message_names"] = []
                return data
            except json.JSONDecodeError:
                logger.warning("Claire: claire_state.json corrupt — starting fresh")
                return self._fresh_state()

    def _save_state(self, state: dict) -> None:
        with self._state_lock:
            self._sent_names = self._sent_names[-500:]
            state["sent_message_names"] = self._sent_names
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            try:
                atomic_write_json(_STATE_PATH, state)
            except OSError as exc:
                logger.warning("Failed to save Claire state: {}", exc)
