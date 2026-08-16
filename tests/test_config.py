from decimal import Decimal
from pathlib import Path

import pytest

from calories_bot.analyzer import ModelPricing
from calories_bot.config import ConfigError, Settings

REQUIRED_NAMES = [
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_TELEGRAM_USER_ID",
    "OPENAI_API_KEY",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "USERS_SPREADSHEET_ID",
    "GOOGLE_DRIVE_FOLDER_ID",
]


def set_valid_env(monkeypatch, tmp_path) -> None:
    credentials = tmp_path / "service-account.json"
    credentials.write_text("{}", encoding="utf-8")
    env_file = tmp_path / ".env.test"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("CALORIES_BOT_ENV_FILE", str(env_file))
    values = {
        "TELEGRAM_BOT_TOKEN": "token",
        "ADMIN_TELEGRAM_USER_ID": "123",
        "OPENAI_API_KEY": "key",
        "GOOGLE_SERVICE_ACCOUNT_FILE": str(credentials),
        "USERS_SPREADSHEET_ID": "users-sheet",
        "GOOGLE_DRIVE_FOLDER_ID": "drive-folder",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in (
        "OPENAI_REASONING_EFFORT",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_INPUT_COST_PER_1M",
        "OPENAI_CACHED_INPUT_COST_PER_1M",
        "OPENAI_OUTPUT_COST_PER_1M",
        "WEEKLY_MEALS_LLM_MODEL",
        "WEEKLY_MEALS_LLM_REASONING_EFFORT",
        "WEEKLY_MEALS_LLM_INPUT_COST_PER_1M",
        "WEEKLY_MEALS_LLM_CACHED_INPUT_COST_PER_1M",
        "WEEKLY_MEALS_LLM_OUTPUT_COST_PER_1M",
        "OPENAI_ADMIN_API_KEY",
        "OPENAI_PROJECT_ID",
        "APP_TIMEZONE",
        "DEFAULT_DAY_START",
        "PHOTO_STORAGE_DIR",
        "STATISTICS_DB_PATH",
        "GARMIN_TOKENSTORE",
        "GARMIN_CALORIE_CACHE_PATH",
        "MEAL_WEIGHT_PRESETS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_valid_settings_and_defaults(monkeypatch, tmp_path) -> None:
    set_valid_env(monkeypatch, tmp_path)
    settings = Settings.from_env()
    assert settings.admin_telegram_user_id == 123
    assert settings.openai_reasoning_effort == "low"
    assert settings.openai_timeout_seconds == 90
    assert settings.openai_pricing.complete is False
    assert settings.weekly_meals_llm_model == settings.openai_model
    assert settings.weekly_meals_llm_reasoning_effort == "low"
    assert settings.weekly_meals_llm_pricing.complete is False
    assert settings.openai_admin_api_key == ""
    assert settings.openai_project_id == ""
    assert settings.timezone.key == "Europe/Kyiv"
    assert settings.default_day_start.isoformat(timespec="minutes") == "01:00"
    assert settings.users_sheet_name == "users"
    assert settings.meal_sheet_name == "food_log"
    assert settings.photo_storage_dir == (Path.cwd() / "data" / "photos").resolve()
    assert (
        settings.statistics_db_path
        == (Path.cwd() / "data" / "statistics.sqlite3").resolve()
    )
    assert settings.garmin_tokenstore == Path("~/.garminconnect").expanduser()
    assert (
        settings.garmin_calorie_cache_path
        == (Path.cwd() / "data" / "garmin_calories.json").resolve()
    )
    assert settings.meal_weight_presets == (50, 100, 150, 200)


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
    assert settings.weekly_meals_llm_pricing == ModelPricing(
        Decimal("2.5"), None, Decimal("10")
    )


def test_weekly_meal_grouping_has_independent_model_effort_and_pricing(
    monkeypatch, tmp_path
) -> None:
    set_valid_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_MODEL", "analysis-model")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")
    monkeypatch.setenv("WEEKLY_MEALS_LLM_MODEL", "group-model")
    monkeypatch.setenv("WEEKLY_MEALS_LLM_REASONING_EFFORT", "high")
    monkeypatch.setenv("WEEKLY_MEALS_LLM_INPUT_COST_PER_1M", "1.25")
    monkeypatch.setenv("WEEKLY_MEALS_LLM_CACHED_INPUT_COST_PER_1M", "0")
    monkeypatch.setenv("WEEKLY_MEALS_LLM_OUTPUT_COST_PER_1M", "8")

    settings = Settings.from_env()

    assert settings.openai_model == "analysis-model"
    assert settings.openai_reasoning_effort == "low"
    assert settings.weekly_meals_llm_model == "group-model"
    assert settings.weekly_meals_llm_reasoning_effort == "high"
    assert settings.weekly_meals_llm_pricing == ModelPricing(
        Decimal("1.25"), Decimal("0"), Decimal("8")
    )


def test_weekly_meal_pricing_is_complete_without_cached_rate(
    monkeypatch, tmp_path
) -> None:
    set_valid_env(monkeypatch, tmp_path)
    monkeypatch.setenv("WEEKLY_MEALS_LLM_INPUT_COST_PER_1M", "0.20")
    monkeypatch.setenv("WEEKLY_MEALS_LLM_OUTPUT_COST_PER_1M", "1.20")

    settings = Settings.from_env()

    assert settings.weekly_meals_llm_pricing == ModelPricing(
        Decimal("0.20"), None, Decimal("1.20")
    )
    assert settings.weekly_meals_llm_pricing.complete is True


def test_missing_required_value_is_reported(monkeypatch, tmp_path) -> None:
    set_valid_env(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("ADMIN_TELEGRAM_USER_ID", "user", "ADMIN_TELEGRAM_USER_ID"),
        ("OPENAI_REASONING_EFFORT", "extreme", "OPENAI_REASONING_EFFORT"),
        (
            "WEEKLY_MEALS_LLM_REASONING_EFFORT",
            "extreme",
            "WEEKLY_MEALS_LLM_REASONING_EFFORT",
        ),
        ("OPENAI_TIMEOUT_SECONDS", "never", "OPENAI_TIMEOUT_SECONDS"),
        ("OPENAI_TIMEOUT_SECONDS", "0", "OPENAI_TIMEOUT_SECONDS"),
        ("OPENAI_TIMEOUT_SECONDS", "601", "OPENAI_TIMEOUT_SECONDS"),
        ("DEFAULT_DAY_START", "1:00", "DEFAULT_DAY_START"),
        ("APP_TIMEZONE", "Nowhere/Unknown", "APP_TIMEZONE"),
        ("OPENAI_INPUT_COST_PER_1M", "abc", "decimal"),
        ("OPENAI_INPUT_COST_PER_1M", "-1", "non-negative"),
        ("OPENAI_INPUT_COST_PER_1M", "NaN", "non-negative"),
        ("MEAL_WEIGHT_PRESETS", "50,abc", "comma-separated"),
        ("MEAL_WEIGHT_PRESETS", "50,50", "unique"),
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
