from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_ENV_FILE = Path("/home/igor/.config/calories-bot/e2e.env")
DEFAULT_SESSION = Path("/home/igor/.local/state/calories-bot-e2e/test-user")


@dataclass(frozen=True)
class E2EAuthConfig:
    api_id: int
    api_hash: str
    phone: str
    session: Path


def _owner_only(path: Path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def load_auth_config(path: Path) -> E2EAuthConfig:
    if not path.is_file():
        raise ValueError(f"Config file does not exist: {path}")
    if not _owner_only(path):
        raise ValueError(f"Config file must be owner-only (chmod 600): {path}")

    values = dotenv_values(path)
    missing = [
        name
        for name in (
            "TELEGRAM_E2E_API_ID",
            "TELEGRAM_E2E_API_HASH",
            "TELEGRAM_E2E_PHONE",
        )
        if not str(values.get(name, "")).strip()
    ]
    if missing:
        raise ValueError("Missing variables: " + ", ".join(missing))

    api_id_raw = str(values["TELEGRAM_E2E_API_ID"]).strip()
    if not api_id_raw.isascii() or not api_id_raw.isdecimal():
        raise ValueError("TELEGRAM_E2E_API_ID must be a positive integer")
    api_id = int(api_id_raw)
    if api_id <= 0:
        raise ValueError("TELEGRAM_E2E_API_ID must be a positive integer")

    api_hash = str(values["TELEGRAM_E2E_API_HASH"]).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("TELEGRAM_E2E_API_HASH must contain 32 hexadecimal characters")

    phone = str(values["TELEGRAM_E2E_PHONE"]).strip()
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise ValueError(
            "TELEGRAM_E2E_PHONE must use international format, e.g. +380..."
        )

    session_raw = str(values.get("TELEGRAM_E2E_SESSION", "")).strip()
    session = Path(session_raw).expanduser() if session_raw else DEFAULT_SESSION
    if not session.is_absolute():
        raise ValueError("TELEGRAM_E2E_SESSION must be an absolute path")
    return E2EAuthConfig(api_id, api_hash, phone, session)


def session_file_path(session: Path) -> Path:
    return session if session.suffix == ".session" else Path(f"{session}.session")


async def authorize(config: E2EAuthConfig, *, check_only: bool) -> int:
    try:
        from telethon import TelegramClient
        from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError
    except ImportError:
        print(
            "Telethon is not installed. Run: "
            ".venv/bin/pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
        return 2

    config.session.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.session.parent.chmod(0o700)
    client = TelegramClient(str(config.session), config.api_id, config.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            if check_only:
                print("Telegram E2E session is not authorized.", file=sys.stderr)
                return 1
            await client.send_code_request(config.phone)
            code = input("Код із Telegram: ").strip().replace(" ", "")
            try:
                await client.sign_in(phone=config.phone, code=code)
            except PhoneCodeInvalidError:
                print("Telegram відхилив код авторизації.", file=sys.stderr)
                return 1
            except SessionPasswordNeededError:
                password = getpass.getpass("Пароль Telegram 2FA: ")
                await client.sign_in(password=password)

        account = await client.get_me()
        username = f"@{account.username}" if account.username else "без username"
        print(f"Авторизовано тестовий акаунт: id={account.id}, {username}")
        return 0
    finally:
        await client.disconnect()
        session_file = session_file_path(config.session)
        if session_file.exists():
            session_file.chmod(0o600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Authorize or verify the dedicated Telegram E2E user session"
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify an existing session without requesting a login code",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    os.umask(0o077)
    try:
        config = load_auth_config(args.env_file.expanduser())
        return asyncio.run(authorize(config, check_only=args.check))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nАвторизацію скасовано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
