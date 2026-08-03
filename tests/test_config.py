from decimal import Decimal
from pathlib import Path

import pytest

from calories_bot.config import ConfigError, Settings

REQUIRED_NAMES = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_USER_ID",
    "TELEGRAM_CHAT_ID",
    "OPENAI_API_KEY",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "GOOGLE_SPREADSHEET_ID",
]


def set_valid_env(monkeypatch, tmp_path) -> None:
    credentials = tmp_path / "service-account.json"
    credentials.write_text("{}", encoding="utf-8")
    values = {
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_USER_ID": "123",
        "TELEGRAM_CHAT_ID": "-1001",
        "OPENAI_API_KEY": "key",
        "GOOGLE_SERVICE_ACCOUNT_FILE": str(credentials),
        "GOOGLE_SPREADSHEET_ID": "sheet",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in (
        "OPENAI_REASONING_EFFORT",
        "OPENAI_INPUT_COST_PER_1M",
        "OPENAI_CACHED_INPUT_COST_PER_1M",
        "OPENAI_OUTPUT_COST_PER_1M",
        "APP_TIMEZONE",
        "DAY_START_TIME",
        "PHOTO_STORAGE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_valid_settings_and_defaults(monkeypatch, tmp_path) -> None:
    set_valid_env(monkeypatch, tmp_path)
    settings = Settings.from_env()
    assert settings.telegram_user_id == 123
    assert settings.telegram_chat_id == -1001
    assert settings.openai_reasoning_effort == "none"
    assert settings.openai_pricing.complete is False
    assert settings.timezone.key == "Europe/Kyiv"
    assert settings.day_start_time.isoformat(timespec="minutes") == "01:00"
    assert settings.photo_storage_dir == (Path.cwd() / "data" / "photos").resolve()


def test_photo_storage_dir_is_resolved(monkeypatch, tmp_path) -> None:
    set_valid_env(monkeypatch, tmp_path)
    monkeypatch.setenv("PHOTO_STORAGE_DIR", str(tmp_path / "meal-photos"))

    settings = Settings.from_env()

    assert settings.photo_storage_dir == (tmp_path / "meal-photos").resolve()


def test_explicit_env_file_is_loaded(monkeypatch, tmp_path) -> None:
    set_valid_env(monkeypatch, tmp_path)
    env_file = tmp_path / ".env.live"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("CALORIES_BOT_ENV_FILE", str(env_file))
    seen = []
    monkeypatch.setattr(
        "calories_bot.config.load_dotenv", lambda path: seen.append(path)
    )
    Settings.from_env()
    assert seen == [str(env_file)]


def test_complete_pricing_is_parsed_as_decimal(monkeypatch, tmp_path) -> None:
    set_valid_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_INPUT_COST_PER_1M", "2.5")
    monkeypatch.setenv("OPENAI_CACHED_INPUT_COST_PER_1M", "0")
    monkeypatch.setenv("OPENAI_OUTPUT_COST_PER_1M", "10")
    settings = Settings.from_env()
    assert settings.openai_pricing.complete is True
    assert settings.openai_pricing.input_per_1m == Decimal("2.5")


def test_missing_required_value_is_reported(monkeypatch, tmp_path) -> None:
    set_valid_env(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("TELEGRAM_USER_ID", "user", "TELEGRAM_USER_ID"),
        ("TELEGRAM_CHAT_ID", "chat", "TELEGRAM_CHAT_ID"),
        ("OPENAI_REASONING_EFFORT", "extreme", "OPENAI_REASONING_EFFORT"),
        ("DAY_START_TIME", "1:00", "DAY_START_TIME"),
        ("APP_TIMEZONE", "Nowhere/Unknown", "APP_TIMEZONE"),
        ("OPENAI_INPUT_COST_PER_1M", "abc", "decimal"),
        ("OPENAI_INPUT_COST_PER_1M", "-1", "non-negative"),
        ("OPENAI_INPUT_COST_PER_1M", "NaN", "non-negative"),
    ],
)
def test_invalid_configuration(monkeypatch, tmp_path, name, value, message) -> None:
    set_valid_env(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=message):
        Settings.from_env()


def test_missing_credentials_file_is_reported(monkeypatch, tmp_path) -> None:
    set_valid_env(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "does-not-exist.json")
    )
    with pytest.raises(ConfigError, match="does not exist"):
        Settings.from_env()
