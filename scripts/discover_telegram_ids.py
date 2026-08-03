from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List Telegram chat/user IDs from pending bot updates"
    )
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()

    values = {**dotenv_values(args.env_file), **os.environ}
    token = values.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing in the supplied env file")

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Telegram returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach Telegram: {exc.reason}") from None

    found: set[tuple[int, str, int | None, str]] = set()
    for update in payload.get("result", []):
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
        )
        if not message:
            continue
        chat = message.get("chat", {})
        sender = message.get("from", {})
        found.add(
            (
                chat.get("id"),
                chat.get("type", "unknown"),
                sender.get("id"),
                sender.get("username", ""),
            )
        )

    if not found:
        raise SystemExit(
            "No pending messages found. Stop the bot, send a group message, and retry."
        )
    for chat_id, chat_type, user_id, username in sorted(found):
        print(
            f"chat_id={chat_id} chat_type={chat_type} "
            f"user_id={user_id} username={username or '-'}"
        )


if __name__ == "__main__":
    main()
