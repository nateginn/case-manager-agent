"""
One-shot script to delete all messages from Claire's Google Chat spaces.

Usage:
    python cleanup_chat.py --dry-run          # preview only, no deletions
    python cleanup_chat.py                    # actually delete

Spaces cleaned by default (from .env):
    CLAIRE_ALERT_SPACE_ID  — main Claire notification space
    GOOGLE_CHAT_SPACE_DENVER
    GOOGLE_CHAT_SPACE_GREELEY

Add --spaces AAQABJJCGpA AAQAKvzY_ug  to override which spaces to clean.

Uses assistant_token.json (cm.assistant.art@gmail.com) for auth.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Bootstrap path so we can import project modules directly.
sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from tools.google_chat_tool import CHAT_ONLY_SCOPES, _get_credentials


def _build_service():
    token_path = Path(settings.CLAIRE_ASSISTANT_TOKEN_PATH)
    creds = _get_credentials(token_path=token_path, scopes=CHAT_ONLY_SCOPES)
    return build("chat", "v1", credentials=creds)


def list_all_messages(service, space_id: str) -> list[dict]:
    """Page through all messages in a space. Returns list of {name, text_preview}."""
    messages = []
    page_token = None
    while True:
        kwargs = {
            "parent": f"spaces/{space_id}",
            "pageSize": 100,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            result = service.spaces().messages().list(**kwargs).execute()
        except HttpError as exc:
            print(f"  ERROR listing messages in {space_id}: {exc}")
            break
        for m in result.get("messages", []):
            text = m.get("text", "")
            messages.append({
                "name": m.get("name", ""),
                "preview": text[:80].replace("\n", " "),
            })
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def delete_message(service, message_name: str) -> bool:
    try:
        service.spaces().messages().delete(name=message_name).execute()
        return True
    except HttpError as exc:
        print(f"  SKIP {message_name}: {exc.status_code}")
        return False


def clean_space(service, space_id: str, dry_run: bool) -> int:
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Space: {space_id}")
    messages = list_all_messages(service, space_id)
    if not messages:
        print("  (no messages found)")
        return 0

    print(f"  Found {len(messages)} message(s):")
    for m in messages:
        print(f"    {m['name']}")
        print(f"      \"{m['preview']}{'...' if len(m['preview']) == 80 else ''}\"")

    if dry_run:
        print(f"  [DRY RUN] Would delete {len(messages)} message(s). Re-run without --dry-run to delete.")
        return 0

    print(f"\n  Deleting {len(messages)} message(s)...")
    deleted = 0
    for m in messages:
        if delete_message(service, m["name"]):
            deleted += 1
            time.sleep(0.2)  # stay well under Chat API rate limit
    print(f"  Done — {deleted}/{len(messages)} deleted.")
    return deleted


def main():
    parser = argparse.ArgumentParser(description="Delete all messages from Claire Chat spaces.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be deleted without actually deleting anything."
    )
    parser.add_argument(
        "--spaces", nargs="+", metavar="SPACE_ID",
        help="Override which space IDs to clean (default: CLAIRE_ALERT + Denver + Greeley)."
    )
    args = parser.parse_args()

    space_ids = args.spaces or [
        s for s in [
            settings.CLAIRE_ALERT_SPACE_ID,
            settings.GOOGLE_CHAT_SPACE_DENVER,
            settings.GOOGLE_CHAT_SPACE_GREELEY,
        ] if s
    ]

    if not space_ids:
        print("ERROR: No space IDs found. Check .env for CLAIRE_ALERT_SPACE_ID etc.")
        sys.exit(1)

    print(f"{'DRY RUN — ' if args.dry_run else ''}Cleaning {len(space_ids)} space(s): {space_ids}")
    service = _build_service()

    total = 0
    for sid in space_ids:
        total += clean_space(service, sid, dry_run=args.dry_run)

    if not args.dry_run:
        print(f"\nTotal deleted: {total} message(s) across {len(space_ids)} space(s).")


if __name__ == "__main__":
    main()
