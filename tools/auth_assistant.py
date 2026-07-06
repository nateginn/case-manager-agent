"""
One-time OAuth setup for the Jarvis assistant account.

Run once from the case-manager-agent-dev directory:
    python tools/auth_assistant.py

Sign in as cm.assistant.art@gmail.com when the browser opens.
Saves assistant_token.json in the current directory.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow

from config import settings

CHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
]

def main() -> None:
    creds_path = Path(settings.GOOGLE_CREDENTIALS_PATH)
    if not creds_path.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {creds_path}. "
            "Set GOOGLE_CREDENTIALS_PATH in .env."
        )

    print("Opening browser — sign in as cm.assistant.art@gmail.com")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), CHAT_SCOPES)
    creds = flow.run_local_server(port=0)

    out = Path("assistant_token.json")
    out.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved {out.resolve()}")


if __name__ == "__main__":
    main()
