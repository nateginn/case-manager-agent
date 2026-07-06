"""
Monitoring agent — watches for unanswered work emails and alerts the case manager.

No emails are marked as read, labelled, or modified in any way.
No drafts are created. Alerts go directly to Google Chat.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from config import settings
from tools.gmail_tool import GmailTool
from tools.google_chat_tool import GoogleChatTool

_ALERTS_PATH = Path(__file__).parent.parent / "memory" / "sent_alerts.json"

# Patterns that indicate an automated / newsletter sender.
_NOREPLY_RE = re.compile(
    r"(no[_\-]?reply|donotreply|noreply|newsletter|notifications?@|updates?@|"
    r"mailer@|mailchimp|unsubscribe|list-unsubscribe|bounce@|auto-reply)",
    re.IGNORECASE,
)


class MonitoringAgent:
    """
    Read-only email monitor.  Sends Google Chat alerts for unanswered work
    emails.  Never labels, drafts, or modifies any email.
    """

    def __init__(self) -> None:
        self.gmail = GmailTool()
        self._threshold_hours = settings.UNANSWERED_THRESHOLD_HOURS
        self._case_manager_email = (
            settings.CASE_MANAGER_EMAIL or settings.GMAIL_USER_EMAIL
        ).lower()
        self._alert_space = settings.ALERT_SPACE_ID

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan_for_unanswered(self) -> list[dict]:
        """
        Scan unread inbox for work emails with no reply that are older than
        UNANSWERED_THRESHOLD_HOURS.  Send a Google Chat alert for each new
        finding and return the list of alerts dispatched this pass.

        No labels or modifications are applied to any email.
        """
        if not self._alert_space:
            logger.warning(
                "MonitoringAgent: ALERT_SPACE_ID not configured — skipping scan"
            )
            return []

        emails = self.gmail.fetch_unread_emails(max_results=50)
        logger.info("MonitoringAgent.scan: {} unread emails fetched", len(emails))

        alerts_sent: list[dict] = []
        now_utc = datetime.now(timezone.utc)

        for email in emails:
            try:
                if self._is_promotion(email):
                    logger.debug(
                        "Skipping promotion/automated sender: {}",
                        email.get("sender", "")[:50],
                    )
                    continue

                age_hours = self._age_hours(email.get("date", ""), now_utc)
                if age_hours < self._threshold_hours:
                    continue

                thread_msgs = self.gmail.fetch_thread_messages(
                    email.get("thread_id", "")
                )
                if self._has_reply(thread_msgs):
                    continue

                if self._already_alerted(email["id"], now_utc):
                    logger.debug(
                        "Already alerted within window for message_id={}",
                        email["id"],
                    )
                    continue

                alert_text = self._build_alert(email, age_hours, len(thread_msgs))
                chat = GoogleChatTool(self._alert_space)
                sent = chat.send_message(alert_text)
                if sent:
                    self._record_alert(email, now_utc)
                    alerts_sent.append(
                        {"email_id": email["id"], "subject": email.get("subject", "")}
                    )
                    logger.info(
                        "MonitoringAgent: alert sent for message_id={}", email["id"]
                    )

            except Exception as exc:
                logger.error(
                    "MonitoringAgent: error processing message_id={}: {}",
                    email.get("id", "?"),
                    exc,
                )

        logger.info(
            "MonitoringAgent.scan complete: {} alerts sent this pass", len(alerts_sent)
        )
        return alerts_sent

    def learn_from_threads(self, max_threads: int = 100) -> dict:
        """
        Read-only learn pass.  Fetches recent inbox threads, analyses each
        one for reply patterns, and writes a report to memory/learn_report.json.

        No labels, no drafts, no Chat messages are created.
        Returns a summary dict suitable for returning from an API endpoint.
        """
        threads = self.gmail.fetch_recent_threads(max_results=max_threads)
        logger.info("LearnPass: {} thread stubs to analyse", len(threads))

        report: list[dict] = []
        for stub in threads:
            thread_id = stub.get("id", "")
            try:
                messages = self.gmail.fetch_thread_messages(thread_id)
                if not messages:
                    continue
                messages_sorted = sorted(
                    messages, key=lambda m: m["internal_date_ms"]
                )
                first = messages_sorted[0]
                latest = messages_sorted[-1]
                has_reply = self._has_reply(messages)
                report.append(
                    {
                        "thread_id": thread_id,
                        "message_count": len(messages),
                        "first_sender": first.get("sender", ""),
                        "first_date": first.get("date", ""),
                        "latest_date": latest.get("date", ""),
                        "has_outbound_reply": has_reply,
                        "snippet": first.get("snippet", ""),
                    }
                )
            except Exception as exc:
                logger.warning(
                    "LearnPass: failed on thread_id={}: {}", thread_id, exc
                )

        learn_path = Path(__file__).parent.parent / "memory" / "learn_report.json"
        learn_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("LearnPass: report written ({} threads)", len(report))

        unanswered = sum(1 for r in report if not r["has_outbound_reply"])
        return {
            "threads_analysed": len(report),
            "with_reply": len(report) - unanswered,
            "without_reply": unanswered,
            "report_path": str(learn_path),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_promotion(self, email: dict) -> bool:
        sender = email.get("sender", "")
        return bool(_NOREPLY_RE.search(sender))

    def _age_hours(self, date_str: str, now: datetime) -> float:
        if not date_str:
            return 0.0
        try:
            from email.utils import parsedate_to_datetime

            sent_dt = parsedate_to_datetime(date_str)
            if sent_dt.tzinfo is None:
                sent_dt = sent_dt.replace(tzinfo=timezone.utc)
            return (now - sent_dt).total_seconds() / 3600
        except Exception:
            return 0.0

    def _has_reply(self, thread_messages: list[dict]) -> bool:
        """Return True if any message in the thread was sent by the case manager."""
        for msg in thread_messages:
            # Gmail marks outbound messages with the SENT system label.
            if "SENT" in msg.get("label_ids", []):
                return True
            # Fallback: sender field contains the case manager email address.
            sender = msg.get("sender", "").lower()
            if self._case_manager_email and self._case_manager_email in sender:
                return True
        return False

    def _already_alerted(self, email_id: str, now: datetime) -> bool:
        window = timedelta(hours=settings.ALERT_DEDUPE_WINDOW_HOURS)
        cutoff = now - window
        for entry in self._load_alerts():
            if entry.get("email_id") != email_id:
                continue
            try:
                alerted_at = datetime.fromisoformat(entry["alerted_at"])
                if alerted_at.tzinfo is None:
                    alerted_at = alerted_at.replace(tzinfo=timezone.utc)
                if alerted_at > cutoff:
                    return True
            except Exception:
                pass
        return False

    def _build_alert(
        self, email: dict, age_hours: float, thread_len: int
    ) -> str:
        sender = email.get("sender", "unknown sender")
        subject = email.get("subject", "(no subject)")
        if age_hours < 48:
            age_str = f"{age_hours:.0f} hours"
        else:
            age_str = f"{age_hours / 24:.0f} days"
        reply_note = (
            "no reply sent"
            if thread_len <= 1
            else f"{thread_len} messages, no outbound reply found"
        )
        return (
            f"⚠️ Unanswered email — {age_str} ago\n"
            f"From: {sender}\n"
            f"Subject: {subject}\n"
            f"Thread: {reply_note}"
        )

    def _record_alert(self, email: dict, now: datetime) -> None:
        alerts = self._load_alerts()
        alerts.append(
            {
                "email_id": email["id"],
                "subject": email.get("subject", ""),
                "alerted_at": now.isoformat(),
            }
        )
        _ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ALERTS_PATH.write_text(
            json.dumps(alerts, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _load_alerts(self) -> list[dict]:
        if not _ALERTS_PATH.exists():
            return []
        try:
            data = json.loads(_ALERTS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
