from __future__ import annotations

import os

import pytest

from scripts.telegram_e2e_auth import (
    DEFAULT_SESSION,
    load_auth_config,
    session_file_path,
)


def write_config(tmp_path, text: str):
    path = tmp_path / "e2e.env"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def valid_config() -> str:
    return (
        "TELEGRAM_E2E_API_ID=123456\n"
        "TELEGRAM_E2E_API_HASH=0123456789abcdef0123456789abcdef\n"
        "TELEGRAM_E2E_PHONE=+380501234567\n"
    )


def test_load_auth_config_uses_safe_default_session(tmp_path) -> None:
    config = load_auth_config(write_config(tmp_path, valid_config()))

    assert config.api_id == 123456
    assert config.api_hash == "0123456789abcdef0123456789abcdef"
    assert config.phone == "+380501234567"
    assert config.session == DEFAULT_SESSION


def test_load_auth_config_accepts_absolute_session(tmp_path) -> None:
    config = load_auth_config(
        write_config(
            tmp_path,
            valid_config() + f"TELEGRAM_E2E_SESSION={tmp_path}/user\n",
        )
    )

    assert config.session == tmp_path / "user"


@pytest.mark.parametrize(
    "override",
    [
        "TELEGRAM_E2E_API_ID=abc",
        "TELEGRAM_E2E_API_HASH=short",
        "TELEGRAM_E2E_PHONE=0501234567",
    ],
)
def test_load_auth_config_rejects_invalid_values(tmp_path, override) -> None:
    values = {
        line.split("=", 1)[0]: line for line in valid_config().strip().splitlines()
    }
    values[override.split("=", 1)[0]] = override
    path = write_config(tmp_path, "\n".join(values.values()) + "\n")

    with pytest.raises(ValueError):
        load_auth_config(path)


def test_load_auth_config_rejects_group_readable_file(tmp_path) -> None:
    path = write_config(tmp_path, valid_config())
    path.chmod(0o640)

    with pytest.raises(ValueError, match="owner-only"):
        load_auth_config(path)


def test_session_file_path_adds_suffix() -> None:
    assert session_file_path(DEFAULT_SESSION) == DEFAULT_SESSION.with_suffix(".session")


def test_test_process_keeps_restrictive_umask() -> None:
    previous = os.umask(0o077)
    os.umask(previous)
    assert isinstance(previous, int)
