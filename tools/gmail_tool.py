"""
Gmail tool — interact with the configured Gmail account via the Google Gmail
API v1.  Uses OAuth 2.0 with locally-stored credentials; no data is sent to
third-party services beyond Google's own APIs.

PHI policy: only message IDs, subjects, and sender addresses are logged.
Body content and attachment bytes are never written to logs.
"""

from __future__ import annotations

import base64
from email import message_from_bytes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

from config import settings

# gmail.modify is a superset of readonly; keeping readonly explicit for clarity.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
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
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            logger.debug("Loaded existing token from {}", TOKEN_PATH)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Token expired — refreshing silently")
                creds.refresh(Request())
            else:
                logger.info("No valid token found — starting OAuth2 browser flow")
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                creds = flow.run_local_server(port=0)

            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            logger.info("Token saved to {}", TOKEN_PATH)

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

    # ------------------------------------------------------------------
    # 3. Fetch attachment bytes
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

    # ------------------------------------------------------------------
    # 4. Create draft
    # ------------------------------------------------------------------

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
    ) -> str:
        """
        Create a Gmail draft.  Never sends the message.

        Args:
            to:        Recipient email address.
            subject:   Email subject line.
            body:      Plain-text body content.
            thread_id: Optional Gmail thread ID to attach the draft to an
                       existing conversation.

        Returns:
            The draft ID string (e.g. ``"r123456789"``).

        PHI note: only the draft ID is logged.
        """
        logger.info("Creating draft subject_len={}", len(subject))  # HIPAA: no PHI logged

        mime = MIMEMultipart("alternative")
        mime["To"] = to
        mime["From"] = settings.GMAIL_USER_EMAIL
        mime["Subject"] = subject
        mime.attach(MIMEText(body, "plain", "utf-8"))

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

        self._service.users().messages().modify(
            userId=settings.GMAIL_USER_EMAIL,
            id=message_id,
            body={"addLabelIds": [label_id], "removeLabelIds": []},
        ).execute()

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
        result = (
            self._service.users()
            .messages()
            .list(
                userId=settings.GMAIL_USER_EMAIL,
                q=query,
                maxResults=max_results,
            )
            .execute()
        )
        return result.get("messages", [])

    def _fetch_full_message(self, message_id: str) -> dict:
        """
        Fetch a single message in ``full`` format and unpack it into a
        normalised dict.  Handles both simple (non-multipart) and multipart
        MIME structures.
        """
        msg = (
            self._service.users()
            .messages()
            .get(
                userId=settings.GMAIL_USER_EMAIL,
                id=message_id,
                format="full",
            )
            .execute()
        )

        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("subject", "(no subject)")
        sender = headers.get("from", "")
        date = headers.get("date", "")

        logger.debug("Processing message_id={}", message_id)  # HIPAA: no PHI logged

        body_text, body_html, has_attachments, attachment_filenames = (
            self._extract_parts(msg["payload"])
        )

        return {
            "id": message_id,
            "thread_id": msg.get("threadId", ""),
            "subject": subject,
            "sender": sender,
            "date": date,
            "body_text": body_text,
            "body_html": body_html,
            "has_attachments": has_attachments,
            "attachment_filenames": attachment_filenames,
        }

    def _extract_parts(
        self, payload: dict
    ) -> tuple[str, str, bool, list[str]]:
        """
        Recursively walk a Gmail payload dict and collect:
          - plain-text body
          - HTML body
          - whether any attachments are present
          - list of attachment filenames (not the bytes — those are lazy-fetched)

        Returns (body_text, body_html, has_attachments, attachment_filenames).
        """
        body_text_parts: list[str] = []
        body_html_parts: list[str] = []
        attachment_filenames: list[str] = []

        self._walk_parts(payload, body_text_parts, body_html_parts, attachment_filenames)

        body_text = "\n".join(body_text_parts)
        body_html = "\n".join(body_html_parts)
        has_attachments = bool(attachment_filenames)

        return body_text, body_html, has_attachments, attachment_filenames

    def _walk_parts(
        self,
        part: dict,
        text_acc: list[str],
        html_acc: list[str],
        attachments_acc: list[str],
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

        # Attachment — record filename; bytes are fetched on demand
        if filename:
            attachments_acc.append(filename)
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
