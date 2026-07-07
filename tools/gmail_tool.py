"""
Gmail tool — interact with the configured Gmail account via the Google Gmail
API v1.  Uses OAuth 2.0 with locally-stored credentials; no data is sent to
third-party services beyond Google's own APIs.

PHI policy: only message IDs, subjects, and sender addresses are logged.
Body content and attachment bytes are never written to logs.
"""

from __future__ import annotations

import base64
import html as html_lib
import json as _json
from email import encoders, message_from_bytes
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import httplib2
import google_auth_httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

from config import settings
from utils import retry_call

# gmail.modify is a superset of readonly; keeping readonly explicit for clarity.
# gmail.settings.basic is read-only for our use: fetching the account's
# configured send-as signature so drafts carry the real signature block.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/chat.messages.create",
]

TOKEN_PATH = Path("token.json")
PROCESSED_LABEL_NAME = "agent-processed"


class GmailTool:
    """
    Wrapper around the Gmail API v1.

    Call ``authenticate()`` explicitly (or let ``__init__`` call it) before
    using any other method.  The service object is cached on the instance so
    subsequent calls do not re-authenticate.
    """

    def __init__(self) -> None:
        self._service: Any = None
        self._processed_label_id: str | None = None
        self._signature_cache: str | None = None
        self.authenticate()

    # ------------------------------------------------------------------
    # 1. Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """
        Run the OAuth2 flow using the credentials.json file specified in
        config.  Stores the resulting token in token.json for reuse.

        On first run a browser window will open for user consent.
        On subsequent runs the stored refresh token is used silently.
        Raises ``FileNotFoundError`` if GOOGLE_CREDENTIALS_PATH is unset or
        the file does not exist.
        """
        creds_path = Path(settings.GOOGLE_CREDENTIALS_PATH)
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Google credentials file not found: {creds_path}. "
                "Set GOOGLE_CREDENTIALS_PATH in .env."
            )

        creds: Credentials | None = None

        if TOKEN_PATH.exists():
            # Check the token file's granted scopes BEFORE loading: google-auth
            # replaces the file's scopes with the requested ones, so comparing
            # creds.scopes afterwards would always pass.
            try:
                stored_scopes = set(_json.loads(TOKEN_PATH.read_text(encoding="utf-8")).get("scopes", []))
            except Exception:
                stored_scopes = set()
            if stored_scopes and not set(SCOPES).issubset(stored_scopes):
                logger.info("Token missing newly required scopes — re-consent needed")
            else:
                creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
                logger.debug("Loaded existing token from {}", TOKEN_PATH)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Token expired — refreshing silently")
                import requests as _req_lib
                _session = _req_lib.Session()
                _ca_bundle = Path(__file__).parent.parent / "windows_cacerts.pem"
                _session.verify = str(_ca_bundle) if _ca_bundle.exists() else True
                creds.refresh(Request(session=_session))
            else:
                logger.info("No valid token found — starting OAuth2 browser flow")
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                creds = flow.run_local_server(port=0)

            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            logger.info("Token saved to {}", TOKEN_PATH)

        _ca_bundle = Path(__file__).parent.parent / "windows_cacerts.pem"
        if _ca_bundle.exists():
            _http = google_auth_httplib2.AuthorizedHttp(
                creds, http=httplib2.Http(ca_certs=str(_ca_bundle))
            )
            self._service = build("gmail", "v1", http=_http)
        else:
            self._service = build("gmail", "v1", credentials=creds)
        logger.info(
            "GmailTool authenticated (user={}, scopes={})",
            settings.GMAIL_USER_EMAIL,
            len(SCOPES),
        )

    # ------------------------------------------------------------------
    # 2. Fetch unread emails
    # ------------------------------------------------------------------

    def fetch_unread_emails(self, max_results: int = 20) -> list[dict]:
        """
        Return up to *max_results* unread messages from the inbox.

        Each dict contains:
          id, thread_id, subject, sender, date,
          body_text (str), body_html (str),
          has_attachments (bool), attachment_filenames (list[str])

        PHI note: body_text / body_html are returned to the caller but
        are never written to logs.
        """
        logger.info("Fetching up to {} unread emails", max_results)

        stubs = self._list_messages(
            query="is:unread in:inbox",
            max_results=max_results,
        )
        logger.debug("Found {} unread message stubs", len(stubs))

        emails: list[dict] = []
        for stub in stubs:
            try:
                email_dict = self._fetch_full_message(stub["id"])
                emails.append(email_dict)
            except HttpError as exc:
                logger.error("Failed to fetch message id={}: {}", stub["id"], exc)

        logger.info("Fetched {} unread emails successfully", len(emails))
        return emails

    def fetch_inbox_emails(self, max_results: int = 50) -> list[dict]:
        """
        Return up to *max_results* inbox emails regardless of read status.

        Excludes promotions, social, and updates categories. Fetches messages
        from the last 7 days so Jarvis catches older unanswered emails that
        were read but never replied to.

        PHI note: body_text / body_html are returned to the caller but
        are never written to logs.
        """
        logger.info("Fetching up to {} inbox emails (all, not just unread)", max_results)

        stubs = self._list_messages(
            query="in:inbox -category:promotions -category:social -category:updates newer_than:7d",
            max_results=max_results,
        )
        logger.debug("Found {} inbox message stubs", len(stubs))

        emails: list[dict] = []
        for stub in stubs:
            try:
                email_dict = self._fetch_full_message(stub["id"])
                emails.append(email_dict)
            except HttpError as exc:
                logger.error("Failed to fetch message id={}: {}", stub["id"], exc)

        logger.info("Fetched {} inbox emails successfully", len(emails))
        return emails

    # ------------------------------------------------------------------
    # 3. Fetch thread history
    # ------------------------------------------------------------------

    def fetch_thread(self, thread_id: str, current_message_id: str = "") -> list[dict]:
        """
        Return prior messages in the Gmail thread, excluding the current email.

        Calls users.threads.get with format='full', sorts messages by
        internalDate oldest-first (the API returns them in that order, but we
        sort explicitly to be safe), and extracts sender, date, and plain-text
        body for each message that is not *current_message_id*.

        Returns an empty list if the thread has only one message, if
        *thread_id* is empty, or if the API call fails (graceful degradation).

        PHI note: message bodies are returned to the caller but never logged.

        Args:
            thread_id:          Gmail thread ID to fetch.
            current_message_id: ID of the email currently being processed;
                                excluded from the returned history.

        Returns:
            List of ``{"sender": str, "date": str, "body": str}`` dicts,
            oldest message first.
        """
        if not thread_id:
            return []

        try:
            thread = (
                self._service.users()
                .threads()
                .get(
                    userId=settings.GMAIL_USER_EMAIL,
                    id=thread_id,
                    format="full",
                )
                .execute()
            )
        except Exception as exc:
            logger.warning(
                "fetch_thread failed for thread_id={}: {} — returning empty history",
                thread_id,
                exc,
            )
            return []

        messages: list[dict] = thread.get("messages", [])
        if len(messages) <= 1:
            return []

        # Sort oldest-first by internalDate (Gmail returns oldest-first in
        # practice, but sort explicitly for correctness).
        messages = sorted(messages, key=lambda m: int(m.get("internalDate", 0)))

        history: list[dict] = []
        for msg in messages:
            if msg.get("id") == current_message_id:
                continue

            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            body_text, _, _, _ = self._extract_parts(msg.get("payload", {}))
            history.append({
                "sender": headers.get("from", ""),
                "date": headers.get("date", ""),
                "subject": headers.get("subject", ""),
                "body": body_text,
            })

        logger.debug(
            "fetch_thread thread_id={} prior_messages={}",
            thread_id,
            len(history),
        )
        return history

    # ------------------------------------------------------------------
    # 3b. Fetch all messages in a thread (for monitoring)
    # ------------------------------------------------------------------

    def fetch_thread_messages(self, thread_id: str) -> list[dict]:
        """
        Return all messages in a thread with enough metadata to determine
        whether the case manager has replied.

        Each dict contains:
          id, sender, date, label_ids, snippet, internal_date_ms

        The ``label_ids`` field includes Gmail system labels such as ``SENT``,
        which is the most reliable indicator that a message was sent by the
        account owner (as opposed to received).

        PHI note: only thread_id is logged; body and sender are not.
        """
        if not thread_id:
            return []
        try:
            thread = (
                self._service.users()
                .threads()
                .get(userId=settings.GMAIL_USER_EMAIL, id=thread_id, format="metadata")
                .execute()
            )
        except Exception as exc:
            logger.warning("fetch_thread_messages failed thread_id={}: {}", thread_id, exc)
            return []

        messages = thread.get("messages", [])
        result = []
        for msg in messages:
            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            result.append({
                "id": msg.get("id", ""),
                "sender": headers.get("from", ""),
                "date": headers.get("date", ""),
                "label_ids": msg.get("labelIds", []),
                "snippet": msg.get("snippet", "")[:120],
                "internal_date_ms": int(msg.get("internalDate", 0)),
            })

        logger.debug("fetch_thread_messages thread_id={} messages={}", thread_id, len(result))
        return result

    def fetch_recent_threads(self, max_results: int = 100) -> list[dict]:
        """
        Fetch recent inbox threads regardless of read/unread status, skipping
        promotions and social categories.  Used for the learn pass.

        Returns a list of thread stubs ``{threadId}``.  The caller is
        responsible for fetching full message content via ``fetch_thread_messages``
        or ``_fetch_full_message`` if needed.

        PHI note: no content is logged; only counts.
        """
        logger.info("fetch_recent_threads max_results={}", max_results)
        try:
            result = (
                self._service.users()
                .threads()
                .list(
                    userId=settings.GMAIL_USER_EMAIL,
                    q="in:inbox -category:promotions -category:social -category:updates",
                    maxResults=max_results,
                )
                .execute()
            )
            threads = result.get("threads", [])
            logger.info("fetch_recent_threads found {} threads", len(threads))
            return threads
        except Exception as exc:
            logger.error("fetch_recent_threads failed: {}", exc)
            return []

    # ------------------------------------------------------------------
    # 4. Fetch attachment bytes
    # ------------------------------------------------------------------

    def fetch_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """
        Return the raw bytes of a message attachment.

        Args:
            message_id:    Gmail message ID that contains the attachment.
            attachment_id: The ``body.attachmentId`` from the message part.

        PHI note: only the message ID is logged; attachment content is not.
        """
        logger.info("Fetching attachment message_id={} attachment_id={}", message_id, attachment_id)

        result = (
            self._service.users()
            .messages()
            .attachments()
            .get(
                userId=settings.GMAIL_USER_EMAIL,
                messageId=message_id,
                id=attachment_id,
            )
            .execute()
        )

        data = result.get("data", "")
        raw_bytes = base64.urlsafe_b64decode(data.encode("utf-8"))
        logger.debug(
            "Attachment fetched message_id={} size_bytes={}",
            message_id,
            len(raw_bytes),
        )
        return raw_bytes

    def fetch_message_attachments(self, email: dict) -> list[dict]:
        """
        Download every attachment of an email dict (as returned by the fetch
        methods, which include ``attachment_parts``).

        Returns:
            List of ``{"filename": str, "mime_type": str, "data": bytes}``.
            Parts without an attachment_id (rare inline parts) are skipped.

        PHI note: attachment bytes are never logged.
        """
        out: list[dict] = []
        for part in email.get("attachment_parts", []):
            if not part.get("attachment_id"):
                continue
            data = self.fetch_attachment(email["id"], part["attachment_id"])
            out.append({
                "filename": part["filename"],
                "mime_type": part.get("mime_type") or "application/octet-stream",
                "data": data,
            })
        return out

    # ------------------------------------------------------------------
    # 4. Create draft
    # ------------------------------------------------------------------

    def get_signature(self) -> str:
        """
        Return the account's configured send-as signature (HTML) from Gmail
        settings.  This is the single source of truth for the signature —
        it is never recreated in code.

        Cached after the first call.  Returns "" on any failure so draft
        creation degrades gracefully to signature-less drafts.
        """
        if self._signature_cache is not None:
            return self._signature_cache
        try:
            res = retry_call(
                lambda: self._service.users()
                .settings()
                .sendAs()
                .list(userId=settings.GMAIL_USER_EMAIL)
                .execute(),
                retries=1, label="gmail.sendAs",
            )
            send_as_list = res.get("sendAs", [])
            primary = next(
                (s for s in send_as_list if s.get("isDefault")),
                send_as_list[0] if send_as_list else {},
            )
            self._signature_cache = primary.get("signature", "") or ""
            logger.info("get_signature: fetched signature ({} chars)", len(self._signature_cache))
        except Exception as exc:
            logger.warning("get_signature failed (drafts will be unsigned): {}", exc)
            self._signature_cache = ""
        return self._signature_cache

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        cc: str = "",
        in_reply_to: str = "",
        signature_html: str = "",
        attachments: list[dict] | None = None,
    ) -> str:
        """
        Create a Gmail draft.  Never sends the message.

        Args:
            to:          Recipient email address(es), comma-separated.
            subject:     Email subject line.
            body:        Plain-text body content.
            thread_id:   Optional Gmail thread ID to attach the draft to an
                         existing conversation.
            cc:          Optional CC address(es), comma-separated (for reply-all).
            in_reply_to: Optional Message-ID header of the email being replied
                         to; sets In-Reply-To/References so mail clients thread
                         the reply correctly.
            signature_html: Optional signature HTML (from ``get_signature()``).
                         When present the draft gets an HTML part with the
                         signature appended below the body.
            attachments: Optional list of ``{"filename", "mime_type", "data"}``
                         dicts (as returned by ``fetch_message_attachments``)
                         to attach — used for forward drafts.

        Returns:
            The draft ID string (e.g. ``"r123456789"``).

        PHI note: only the draft ID is logged.
        """
        logger.info("Creating draft subject_len={} attachments={}",
                    len(subject), len(attachments or []))  # HIPAA: no PHI logged

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))

        if signature_html:
            # HTML alternative: body text (escaped, newlines -> <br>) with the
            # account's real Gmail signature below.  Mail clients display the
            # last alternative part, so the HTML version is what recipients see.
            escaped = html_lib.escape(body).replace("\n", "<br>\n")
            html_part = (
                f'<div dir="ltr">{escaped}<br><br>{signature_html}</div>'
            )
            alt.attach(MIMEText(html_part, "html", "utf-8"))

        if attachments:
            mime = MIMEMultipart("mixed")
            mime.attach(alt)
            for att in attachments:
                maintype, _, subtype = (
                    att.get("mime_type") or "application/octet-stream"
                ).partition("/")
                part = MIMEBase(maintype, subtype or "octet-stream")
                part.set_payload(att["data"])
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition", "attachment",
                    filename=att.get("filename") or "attachment",
                )
                mime.attach(part)
        else:
            mime = alt

        mime["To"] = to
        mime["From"] = settings.GMAIL_USER_EMAIL
        mime["Subject"] = subject
        if cc:
            mime["Cc"] = cc
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = in_reply_to

        encoded = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
        message_body: dict[str, Any] = {"raw": encoded}

        if thread_id:
            message_body["threadId"] = thread_id

        draft = (
            self._service.users()
            .drafts()
            .create(
                userId=settings.GMAIL_USER_EMAIL,
                body={"message": message_body},
            )
            .execute()
        )

        draft_id: str = draft["id"]
        logger.info("Draft created draft_id={}", draft_id)  # HIPAA: no PHI logged
        return draft_id

    # ------------------------------------------------------------------
    # 5. Mark as processed
    # ------------------------------------------------------------------

    def mark_as_processed(self, message_id: str) -> None:
        """
        Add the ``agent-processed`` label to *message_id* so it is skipped
        on future ``fetch_unread_emails`` calls.  Creates the label if it
        does not already exist.

        PHI note: only the message ID is logged.
        """
        label_id = self._get_or_create_processed_label()

        retry_call(
            lambda: self._service.users().messages().modify(
                userId=settings.GMAIL_USER_EMAIL,
                id=message_id,
                body={"addLabelIds": [label_id], "removeLabelIds": []},
            ).execute(),
            retries=2, label="gmail.mark_processed",
        )

        logger.info(
            "Marked message_id={} with label={!r} ({})",
            message_id,
            PROCESSED_LABEL_NAME,
            label_id,
        )

    def is_processed(self, message_id: str) -> bool:
        """
        Return True if *message_id* already carries the ``agent-processed`` label.

        Used as a double-check guard before dispatching an email so two
        concurrent agent passes cannot process the same message twice.
        Fails open (returns False) on any API error so a transient label
        lookup failure never silently drops an email.

        PHI note: only the message ID is logged.
        """
        try:
            label_id = self._get_or_create_processed_label()
            msg = (
                self._service.users()
                .messages()
                .get(
                    userId=settings.GMAIL_USER_EMAIL,
                    id=message_id,
                    format="metadata",
                    metadataHeaders=[],
                )
                .execute()
            )
            already = label_id in msg.get("labelIds", [])
            if already:
                logger.info(
                    "is_processed=True for message_id={} (already labelled)",
                    message_id,
                )
            return already
        except Exception as exc:
            logger.warning(
                "is_processed check failed for message_id={}: {} — treating as not processed",
                message_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # 5b. Apply arbitrary label (e.g. "agent-timed-out")
    # ------------------------------------------------------------------

    def apply_label(self, message_id: str, label_name: str) -> None:
        """
        Apply a Gmail label by name to *message_id*, creating the label if it
        does not already exist.

        PHI note: only the message ID is logged.
        """
        label_id = self._get_or_create_label(label_name)
        retry_call(
            lambda: self._service.users().messages().modify(
                userId=settings.GMAIL_USER_EMAIL,
                id=message_id,
                body={"addLabelIds": [label_id], "removeLabelIds": []},
            ).execute(),
            retries=2, label="gmail.apply_label",
        )
        logger.info(
            "Applied label {!r} ({}) to message_id={}",
            label_name,
            label_id,
            message_id,
        )

    def remove_label(self, message_id: str, label_name: str) -> None:
        """
        Remove a Gmail label by name from *message_id*. No-op if the label
        does not exist yet.

        PHI note: only the message ID is logged.
        """
        label_id = self._get_or_create_label(label_name)
        retry_call(
            lambda: self._service.users().messages().modify(
                userId=settings.GMAIL_USER_EMAIL,
                id=message_id,
                body={"addLabelIds": [], "removeLabelIds": [label_id]},
            ).execute(),
            retries=2, label="gmail.remove_label",
        )
        logger.info(
            "Removed label {!r} ({}) from message_id={}",
            label_name,
            label_id,
            message_id,
        )

    # ------------------------------------------------------------------
    # 6. List drafts
    # ------------------------------------------------------------------

    def list_drafts(self, max_results: int = 50) -> list[dict]:
        """
        Return metadata for up to *max_results* drafts in the user's mailbox,
        sorted newest-first.

        Each dict contains:
          draft_id, message_id, thread_id, subject, to, date,
          internal_date (epoch ms, int), snippet (str, ≤ 100 chars)

        Uses ``format="metadata"`` so no body bytes are fetched.

        PHI note: subject, to-address, and snippet are returned to the caller
        but never written to logs.
        """
        logger.info("Listing up to {} Gmail drafts", max_results)

        stubs = (
            self._service.users()
            .drafts()
            .list(userId=settings.GMAIL_USER_EMAIL, maxResults=max_results)
            .execute()
            .get("drafts", [])
        )

        drafts: list[dict] = []
        for stub in stubs:
            draft_id: str = stub["id"]
            try:
                draft = (
                    self._service.users()
                    .drafts()
                    .get(
                        userId=settings.GMAIL_USER_EMAIL,
                        id=draft_id,
                        format="metadata",
                    )
                    .execute()
                )
                message = draft.get("message", {})
                headers = {
                    h["name"].lower(): h["value"]
                    for h in message.get("payload", {}).get("headers", [])
                }
                drafts.append({
                    "draft_id": draft_id,
                    "message_id": message.get("id", ""),
                    "thread_id": message.get("threadId", ""),
                    "subject": headers.get("subject", "(no subject)"),
                    "to": headers.get("to", ""),
                    "date": headers.get("date", ""),
                    "internal_date": int(message.get("internalDate", 0)),
                    "snippet": message.get("snippet", "")[:100],
                })
            except Exception as exc:
                logger.error("Failed to fetch draft metadata id={}: {}", draft_id, exc)

        drafts.sort(key=lambda d: d["internal_date"], reverse=True)
        logger.debug("list_drafts returned {} draft(s)", len(drafts))
        return drafts

    # ------------------------------------------------------------------
    # 7. Delete draft
    # ------------------------------------------------------------------

    def delete_draft(self, draft_id: str) -> bool:
        """
        Permanently delete a draft by its draft ID.

        Args:
            draft_id: The Gmail draft ID (not message ID).

        Returns:
            ``True`` on success, ``False`` if the API call fails.

        PHI note: only the draft ID is logged.
        """
        try:
            self._service.users().drafts().delete(
                userId=settings.GMAIL_USER_EMAIL,
                id=draft_id,
            ).execute()
            logger.info("Draft deleted draft_id={}", draft_id)
            return True
        except HttpError as exc:
            logger.error("Failed to delete draft draft_id={}: {}", draft_id, exc)
            return False

    def delete_message(self, message_id: str, permanent: bool = False) -> bool:
        """
        Move *message_id* to Gmail Trash (default) or permanently delete it.

        Args:
            message_id: Gmail message ID to trash or delete.
            permanent:  If True, permanently deletes (irreversible). Default False
                        moves to Trash so the case manager can recover if needed.

        Returns:
            True on success, False on API error.

        PHI note: only the message ID is logged.
        """
        try:
            if permanent:
                retry_call(
                    lambda: self._service.users().messages().delete(
                        userId=settings.GMAIL_USER_EMAIL,
                        id=message_id,
                    ).execute(),
                    retries=2, label="gmail.delete",
                )
                logger.info("Message permanently deleted message_id={}", message_id)
            else:
                retry_call(
                    lambda: self._service.users().messages().trash(
                        userId=settings.GMAIL_USER_EMAIL,
                        id=message_id,
                    ).execute(),
                    retries=2, label="gmail.trash",
                )
                logger.info("Message trashed message_id={}", message_id)
            return True
        except HttpError as exc:
            logger.error(
                "delete_message failed message_id={}: {}", message_id, exc
            )
            return False

    # ------------------------------------------------------------------
    # 8. Delete local cache / temp files
    # ------------------------------------------------------------------

    def delete_local_cache(self, revoke_token: bool = False) -> dict[str, bool]:
        """
        Remove locally-cached files created during normal operation and
        optionally revoke the stored OAuth token.

        **PDF processing note**: this project uses ``io.BytesIO`` for all PDF
        extraction — no PDF bytes are ever written to disk.  There are therefore
        no temporary PDF files to clean up.

        Files managed:
          ``token.json``   — Cached OAuth 2.0 access/refresh token.  Deleting
                             it forces a fresh browser-based consent flow on the
                             next startup.  Pass ``revoke_token=True`` to also
                             call Google's token-revocation endpoint before
                             deleting, which invalidates the refresh token
                             server-side.

        Args:
            revoke_token: If ``True`` and a valid token exists, revoke it via
                          Google's revocation endpoint before deleting the file.

        Returns:
            Dict mapping each filename to ``True`` (deleted) / ``False``
            (not present or deletion failed).
        """
        results: dict[str, bool] = {}

        # --- token.json ---
        if TOKEN_PATH.exists():
            if revoke_token:
                try:
                    creds = Credentials.from_authorized_user_file(
                        str(TOKEN_PATH), SCOPES
                    )
                    from google.auth.transport.requests import Request as GRequest
                    import requests as _req

                    revoke_url = "https://oauth2.googleapis.com/revoke"
                    token = creds.token or creds.refresh_token
                    if token:
                        _req.post(
                            revoke_url,
                            params={"token": token},
                            headers={"content-type": "application/x-www-form-urlencoded"},
                            timeout=5,
                        )
                        logger.info("OAuth token revoked via Google revocation endpoint")
                except Exception as exc:
                    logger.warning("Token revocation request failed: {}", exc)

            try:
                TOKEN_PATH.unlink()
                results["token.json"] = True
                logger.info("Deleted local OAuth token cache (token.json)")
            except OSError as exc:
                results["token.json"] = False
                logger.error("Failed to delete token.json: {}", exc)
        else:
            results["token.json"] = False
            logger.debug("token.json not present — nothing to delete")

        return results

    # ------------------------------------------------------------------
    # Private helpers — message fetching
    # ------------------------------------------------------------------

    def _list_messages(self, query: str, max_results: int) -> list[dict]:
        """Return a list of ``{id, threadId}`` stubs matching *query*."""
        result = retry_call(
            lambda: self._service.users()
            .messages()
            .list(
                userId=settings.GMAIL_USER_EMAIL,
                q=query,
                maxResults=max_results,
            )
            .execute(),
            retries=2, label="gmail.list",
        )
        return result.get("messages", [])

    def _fetch_full_message(self, message_id: str) -> dict:
        """
        Fetch a single message in ``full`` format and unpack it into a
        normalised dict.  Handles both simple (non-multipart) and multipart
        MIME structures.
        """
        msg = retry_call(
            lambda: self._service.users()
            .messages()
            .get(
                userId=settings.GMAIL_USER_EMAIL,
                id=message_id,
                format="full",
            )
            .execute(),
            retries=2, label="gmail.get",
        )

        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("subject", "(no subject)")
        sender = headers.get("from", "")
        date = headers.get("date", "")

        logger.debug("Processing message_id={}", message_id)  # HIPAA: no PHI logged

        body_text, body_html, has_attachments, attachment_parts = (
            self._extract_parts(msg["payload"])
        )

        return {
            "id": message_id,
            "thread_id": msg.get("threadId", ""),
            "subject": subject,
            "sender": sender,
            "date": date,
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "reply_to": headers.get("reply-to", ""),
            "message_id_header": headers.get("message-id", ""),
            "body_text": body_text,
            "body_html": body_html,
            "has_attachments": has_attachments,
            "attachment_filenames": [p["filename"] for p in attachment_parts],
            "attachment_parts": attachment_parts,
        }

    def _extract_parts(
        self, payload: dict
    ) -> tuple[str, str, bool, list[dict]]:
        """
        Recursively walk a Gmail payload dict and collect:
          - plain-text body
          - HTML body
          - whether any attachments are present
          - attachment parts: {filename, mime_type, attachment_id} dicts
            (not the bytes — those are lazy-fetched via fetch_attachment)

        Returns (body_text, body_html, has_attachments, attachment_parts).
        """
        body_text_parts: list[str] = []
        body_html_parts: list[str] = []
        attachment_parts: list[dict] = []

        self._walk_parts(payload, body_text_parts, body_html_parts, attachment_parts)

        body_text = "\n".join(body_text_parts)
        body_html = "\n".join(body_html_parts)
        has_attachments = bool(attachment_parts)

        return body_text, body_html, has_attachments, attachment_parts

    def _walk_parts(
        self,
        part: dict,
        text_acc: list[str],
        html_acc: list[str],
        attachments_acc: list[dict],
    ) -> None:
        """Depth-first traversal of a MIME part tree."""
        mime_type: str = part.get("mimeType", "")
        filename: str = part.get("filename", "")
        body: dict = part.get("body", {})
        sub_parts: list[dict] = part.get("parts", [])

        # Recurse into multipart containers
        if mime_type.startswith("multipart/"):
            for sub in sub_parts:
                self._walk_parts(sub, text_acc, html_acc, attachments_acc)
            return

        # Attachment — record metadata; bytes are fetched on demand
        if filename:
            attachments_acc.append({
                "filename": filename,
                "mime_type": mime_type or "application/octet-stream",
                "attachment_id": body.get("attachmentId", ""),
            })
            return

        # Inline body part
        data = body.get("data", "")
        if not data:
            return

        decoded = base64.urlsafe_b64decode(data.encode("utf-8")).decode(
            "utf-8", errors="replace"
        )

        if mime_type == "text/plain":
            text_acc.append(decoded)
        elif mime_type == "text/html":
            html_acc.append(decoded)

    # ------------------------------------------------------------------
    # Private helpers — label management
    # ------------------------------------------------------------------

    def _get_or_create_label(self, label_name: str) -> str:
        """
        Return the label ID for *label_name*, creating the label if it does
        not already exist.  Does not cache; used for infrequent labels.
        """
        existing = (
            self._service.users()
            .labels()
            .list(userId=settings.GMAIL_USER_EMAIL)
            .execute()
            .get("labels", [])
        )
        for label in existing:
            if label.get("name") == label_name:
                return label["id"]

        created = (
            self._service.users()
            .labels()
            .create(
                userId=settings.GMAIL_USER_EMAIL,
                body={
                    "name": label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        logger.info("Created Gmail label {!r} id={}", label_name, created["id"])
        return created["id"]

    def _get_or_create_processed_label(self) -> str:
        """
        Return the label ID for ``agent-processed``, creating the label first
        if it does not exist.  Caches the result in ``_processed_label_id``.
        """
        if self._processed_label_id:
            return self._processed_label_id

        # Check if the label already exists
        existing = (
            self._service.users()
            .labels()
            .list(userId=settings.GMAIL_USER_EMAIL)
            .execute()
            .get("labels", [])
        )

        for label in existing:
            if label.get("name") == PROCESSED_LABEL_NAME:
                self._processed_label_id = label["id"]
                logger.debug(
                    "Found existing label {!r} id={}",
                    PROCESSED_LABEL_NAME,
                    self._processed_label_id,
                )
                return self._processed_label_id

        # Create it
        created = (
            self._service.users()
            .labels()
            .create(
                userId=settings.GMAIL_USER_EMAIL,
                body={
                    "name": PROCESSED_LABEL_NAME,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        self._processed_label_id = created["id"]
        logger.info(
            "Created Gmail label {!r} id={}",
            PROCESSED_LABEL_NAME,
            self._processed_label_id,
        )
        return self._processed_label_id
