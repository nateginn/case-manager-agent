"""
Google Chat tool — post and read messages in a Google Chat space via the
Google Chat REST API v1 using OAuth 2.0 credentials.

Supports two credential contexts:
  - token.json          (casemanager.art@gmail.com) — default, send-only to team spaces
  - assistant_token.json (cm.assistant.art@gmail.com) — Claire send + read in alert space

PHI policy: message text is never logged. Only space IDs and
HTTP status codes are written to logs.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import httplib2
import google_auth_httplib2
import requests as _requests_lib
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

from config import settings
from utils import retry_call

# Full scopes used by the casemanager account (Gmail + Chat send).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/chat.messages.create",
]

# Scopes for the assistant account — Chat read+write only, no Gmail access.
CHAT_ONLY_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
]

TOKEN_PATH = Path("token.json")


def _get_credentials(
    token_path: Path = TOKEN_PATH,
    scopes: list[str] | None = None,
) -> Credentials:
    """
    Load or refresh OAuth credentials from *token_path*.

    Args:
        token_path: Path to the token JSON file. Defaults to token.json
                    (the casemanager account).
        scopes:     OAuth scopes to request. Defaults to SCOPES (Gmail + Chat).
                    Pass CHAT_ONLY_SCOPES for the assistant account.
    """
    if scopes is None:
        scopes = SCOPES

    creds_path = Path(settings.GOOGLE_CREDENTIALS_PATH)
    if not creds_path.exists():
        raise FileNotFoundError(
            f"Google credentials file not found: {creds_path}. "
            "Set GOOGLE_CREDENTIALS_PATH in .env."
        )

    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            _session = _requests_lib.Session()
            _ca_bundle = Path(__file__).parent.parent / "windows_cacerts.pem"
            _session.verify = str(_ca_bundle) if _ca_bundle.exists() else True
            creds.refresh(Request(session=_session))
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), scopes)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _build_service(api_name: str, api_version: str, creds: Credentials):
    """Build a Google API service using httplib2 with the Windows CA bundle when available."""
    ca_bundle = Path(__file__).parent.parent / "windows_cacerts.pem"
    if ca_bundle.exists():
        http = google_auth_httplib2.AuthorizedHttp(
            creds, http=httplib2.Http(ca_certs=str(ca_bundle))
        )
        return build(api_name, api_version, http=http)
    return build(api_name, api_version, credentials=creds)


class GoogleChatTool:
    """Post and read messages in a Google Chat space via the REST API."""

    def __init__(self, space_id: str, token_path: Path | None = None) -> None:
        """
        Args:
            space_id:   Google Chat space ID (the alphanumeric segment from the space URL).
            token_path: Optional path to an alternate OAuth token file. Defaults to
                        token.json (casemanager account). Pass Path("assistant_token.json")
                        for the Jarvis assistant account.
        """
        if not space_id:
            raise ValueError("space_id must be set.")
        self.space_id = space_id

        resolved_token = token_path or TOKEN_PATH
        scopes = CHAT_ONLY_SCOPES if token_path is not None else SCOPES
        creds = _get_credentials(token_path=resolved_token, scopes=scopes)
        self._service = _build_service("chat", "v1", creds)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_message(self, text: str) -> bool:
        """
        Post a plain-text message to self.space_id.
        Returns True on success.
        PHI note: message text is never logged.
        """
        return self.send_message_ext(text) is not None

    def send_message_ext(self, text: str) -> str | None:
        """
        Post a plain-text message to self.space_id.
        Returns the Chat message resource name (e.g. 'spaces/XXX/messages/YYY')
        on success, or None on failure.
        PHI note: message text is never logged.
        """
        result = self.send_message_full(text)
        return result["name"] if result else None

    def send_message_full(self, text: str) -> dict | None:
        """
        Post a plain-text message to self.space_id.
        Returns {"name": message_name, "thread_name": thread_name} on success,
        or None on failure. Use this when you need both the message and thread
        resource names for threaded reply matching.
        PHI note: message text is never logged.
        """
        try:
            result = retry_call(
                lambda: self._service.spaces().messages().create(
                    parent=f"spaces/{self.space_id}",
                    body={"text": text},
                ).execute(),
                retries=2, label="chat.send",
            )
            logger.info("Google Chat message posted to space_id={}", self.space_id)
            return {
                "name": result.get("name", ""),
                "thread_name": result.get("thread", {}).get("name", ""),
            }
        except HttpError as exc:
            logger.error(
                "Failed to post Chat message to space_id={}: status={}",
                self.space_id,
                exc.status_code,
            )
            return None

    def reply_in_thread(self, text: str, thread_name: str) -> str | None:
        """
        Post a reply within an existing Chat thread.
        Args:
            text:        Message text to send.
            thread_name: Thread resource name (e.g. 'spaces/XXX/threads/YYY').
        Returns the new message resource name on success, or None on failure.
        Falls back to a plain space message if the thread no longer exists.
        PHI note: message text is never logged.
        """
        if not thread_name:
            return self.send_message_ext(text)
        try:
            result = retry_call(
                lambda: self._service.spaces().messages().create(
                    parent=f"spaces/{self.space_id}",
                    body={"text": text, "thread": {"name": thread_name}},
                    messageReplyOption="REPLY_MESSAGE_OR_FAIL",
                ).execute(),
                retries=2, label="chat.reply",
            )
            logger.info("Google Chat thread reply posted space_id={}", self.space_id)
            return result.get("name")
        except HttpError as exc:
            logger.warning(
                "reply_in_thread failed (thread_name={}), falling back to space message: status={}",
                thread_name,
                exc.status_code,
            )
            return self.send_message_ext(text)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def list_messages(self, after_time: datetime | None = None) -> list[dict]:
        """
        Return messages posted in self.space_id after *after_time*.

        Uses spaces.messages.list with a createTime filter. Returns an empty
        list on any API error (fail-open — missing a reply is safer than
        crashing the polling loop).

        Args:
            after_time: UTC datetime; only messages newer than this are returned.
                        If None, returns the 25 most recent messages.

        Returns:
            List of dicts with keys:
              name (str), sender_email (str), sender_display_name (str),
              text (str), create_time (str ISO-8601)

        PHI note: message text is never logged.
        """
        try:
            kwargs: dict = {
                "parent": f"spaces/{self.space_id}",
                "pageSize": 50,
            }
            if after_time:
                ts = after_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                kwargs["filter"] = f'createTime > "{ts}"'

            result = retry_call(
                lambda: self._service.spaces().messages().list(**kwargs).execute(),
                retries=2, label="chat.list",
            )
            raw = result.get("messages", [])

            messages = []
            for m in raw:
                sender = m.get("sender", {})
                messages.append({
                    "name": m.get("name", ""),
                    "thread_name": m.get("thread", {}).get("name", ""),
                    "sender_email": sender.get("email", ""),
                    "sender_display_name": sender.get("displayName", ""),
                    "sender_type": sender.get("type", "HUMAN"),
                    "text": m.get("text", ""),
                    "create_time": m.get("createTime", ""),
                })
            logger.info(
                "list_messages space_id={} count={}", self.space_id, len(messages)
            )
            return messages
        except HttpError as exc:
            logger.error(
                "list_messages failed space_id={}: status={}",
                self.space_id,
                exc.status_code,
            )
            return []

    def delete_all_messages(self) -> int:
        """Delete every message in self.space_id. Returns count deleted."""
        deleted = 0
        page_token = None
        while True:
            kwargs: dict = {"parent": f"spaces/{self.space_id}", "pageSize": 100}
            if page_token:
                kwargs["pageToken"] = page_token
            try:
                result = self._service.spaces().messages().list(**kwargs).execute()
            except HttpError as exc:
                logger.error("delete_all_messages list failed space_id={}: {}", self.space_id, exc.status_code)
                break
            for m in result.get("messages", []):
                try:
                    self._service.spaces().messages().delete(name=m["name"]).execute()
                    deleted += 1
                    time.sleep(0.15)
                except HttpError:
                    pass  # skip messages posted by other accounts
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        logger.info("delete_all_messages: {} deleted from space_id={}", deleted, self.space_id)
        return deleted
