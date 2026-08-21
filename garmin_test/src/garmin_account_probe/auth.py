"""Garmin authentication without persisting a password."""

from __future__ import annotations

import getpass
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

DEFAULT_TOKENSTORE = Path("~/.garminconnect").expanduser()


@dataclass(frozen=True)
class GarminSession:
    """An authenticated client and non-secret authentication metadata."""

    client: Garmin
    authentication: str
    tokenstore: Path


def _secure_tokenstore(path: Path) -> None:
    """Restrict token files to the current OS user where supported."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(stat.S_IRWXU)
    for child in path.iterdir():
        if child.is_file():
            child.chmod(stat.S_IRUSR | stat.S_IWUSR)


def connect_with_saved_tokens(
    tokenstore: Path = DEFAULT_TOKENSTORE,
    *,
    retry_attempts: int = 2,
) -> GarminSession:
    """Authenticate from saved OAuth tokens; never fall back to a password."""

    resolved = tokenstore.expanduser().resolve()
    client = Garmin(retry_attempts=retry_attempts)
    try:
        client.login(str(resolved))
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as exc:
        raise RuntimeError(
            f"Saved Garmin tokens in {resolved} are missing, expired, or unusable. "
            "Run `garmin-probe login` first."
        ) from exc
    _secure_tokenstore(resolved)
    return GarminSession(client, "saved_tokens", resolved)


def interactive_login(
    tokenstore: Path = DEFAULT_TOKENSTORE,
    *,
    email: str | None = None,
    password: str | None = None,
    input_fn: Callable[[str], str] = input,
    password_fn: Callable[[str], str] = getpass.getpass,
) -> GarminSession:
    """Perform a fresh login, including MFA, and save only renewable tokens.

    ``GARMIN_EMAIL`` and ``GARMIN_PASSWORD`` are accepted for short-lived local
    automation, but the interactive prompts are safer because the password does
    not enter shell history or a project file.
    """

    resolved = tokenstore.expanduser().resolve()
    _secure_tokenstore(resolved)
    account_email = email or os.getenv("GARMIN_EMAIL") or input_fn("Garmin email: ").strip()
    account_password = password or os.getenv("GARMIN_PASSWORD") or password_fn(
        "Garmin password (input is hidden): "
    )
    if not account_email or not account_password:
        raise ValueError("Both Garmin email and password are required for the first login.")

    client = Garmin(
        email=account_email,
        password=account_password,
        prompt_mfa=lambda: input_fn("Garmin one-time verification code: ").strip(),
        retry_attempts=2,
    )
    client.login(str(resolved))
    _secure_tokenstore(resolved)
    return GarminSession(client, "fresh_credentials_then_tokens", resolved)

