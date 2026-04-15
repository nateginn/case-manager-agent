"""
Google Chat tool — post messages to a Google Chat space via the
Google Chat REST API v1 using shared OAuth 2.0 credentials.

PHI policy: message text is never logged. Only space IDs and
HTTP status codes are written to logs.
"""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

from config import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/chat.messages.create",
]

TOKEN_PATH = Path("token.json")


def _get_credentials() -> Credentials:
    """
    Load or refresh OAuth credentials from token.json.
    Shares the same token file as GmailTool so only one
    browser consent flow is needed.
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

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return creds


class GoogleChatTool:
    """Post text messages to a Google Chat space via the REST API."""

    def __init__(self, space_id: str) -> None:
        if not space_id:
            raise ValueError("space_id must be set.")
        self.space_id = space_id
        creds = _get_credentials()
        self._service = build("chat", "v1", credentials=creds)

    def send_message(self, text: str) -> bool:
        """
        Post a plain-text message to self.space_id.
        Returns True on success.
        PHI note: message text is never logged.
        """
        try:
            self._service.spaces().messages().create(
                parent=f"spaces/{self.space_id}",
                body={"text": text},
            ).execute()
            logger.info("Google Chat message posted to space_id={}", self.space_id)
            return True
        except HttpError as exc:
            logger.error(
                "Failed to post Chat message to space_id={}: status={}",
                self.space_id,
                exc.status_code,
            )
            return False
