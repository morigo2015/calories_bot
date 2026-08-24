from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .analyzer import ModelPricing

DEFAULT_MEAL_WEIGHT_PRESETS = (50, 100, 150, 200)
MAX_MEAL_WEIGHT_PRESETS = 8


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    admin_telegram_user_id: int
    openai_api_key: str
    openai_admin_api_key: str
    openai_project_id: str
    openai_model: str
    openai_reasoning_effort: str
    openai_timeout_seconds: float
    openai_pricing: ModelPricing
    weekly_meals_llm_model: str
    weekly_meals_llm_reasoning_effort: str
    weekly_meals_llm_pricing: ModelPricing
    google_service_account_file: Path
    users_spreadsheet_id: str
    users_sheet_name: str
    google_drive_folder_id: str
    meal_sheet_name: str
    photo_storage_dir: Path
    statistics_db_path: Path
    garmin_tokenstore: Path
    garmin_calorie_cache_path: Path
    timezone: ZoneInfo
    default_day_start: time
    meal_weight_presets: tuple[int, ...]
    nutrition_mismatch_threshold_percent: float

    @classmethod
    def from_env(cls) -> Settings:
        env_file = os.getenv("CALORIES_BOT_ENV_FILE")
        load_dotenv(env_file or None)

        required = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
            "ADMIN_TELEGRAM_USER_ID": os.getenv("ADMIN_TELEGRAM_USER_ID"),
            "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
            "GOOGLE_SERVICE_ACCOUNT_FILE": os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"),
            "USERS_SPREADSHEET_ID": os.getenv("USERS_SPREADSHEET_ID"),
            "GOOGLE_DRIVE_FOLDER_ID": os.getenv("GOOGLE_DRIVE_FOLDER_ID"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        try:
            admin_telegram_user_id = int(required["ADMIN_TELEGRAM_USER_ID"] or "")
        except ValueError as exc:
            raise ConfigError("ADMIN_TELEGRAM_USER_ID must be an integer") from exc

        allowed_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}

        def reasoning_effort(name: str, default: str) -> str:
            value = os.getenv(name, default) or default
            if value not in allowed_efforts:
                raise ConfigError(
                    f"{name} must be one of: " + ", ".join(sorted(allowed_efforts))
                )
            return value

        effort = reasoning_effort("OPENAI_REASONING_EFFORT", "low")

        timeout_raw = os.getenv("OPENAI_TIMEOUT_SECONDS", "90") or "90"
        try:
            openai_timeout_seconds = float(timeout_raw)
        except ValueError as exc:
            raise ConfigError("OPENAI_TIMEOUT_SECONDS must be a number") from exc
        if not 0 < openai_timeout_seconds <= 600:
            raise ConfigError(
                "OPENAI_TIMEOUT_SECONDS must be greater than 0 and at most 600"
            )

        def optional_price(name: str) -> Decimal | None:
            raw = os.getenv(name, "").strip()
            if not raw:
                return None
            try:
                value = Decimal(raw)
            except InvalidOperation as exc:
                raise ConfigError(f"{name} must be a decimal number") from exc
            if not value.is_finite() or value < 0:
                raise ConfigError(f"{name} must be a non-negative finite number")
            return value

        pricing = ModelPricing(
            input_per_1m=optional_price("OPENAI_INPUT_COST_PER_1M"),
            cached_input_per_1m=optional_price("OPENAI_CACHED_INPUT_COST_PER_1M"),
            output_per_1m=optional_price("OPENAI_OUTPUT_COST_PER_1M"),
        )

        def grouping_price(name: str, fallback: Decimal | None) -> Decimal | None:
            if os.getenv(name, "").strip():
                return optional_price(name)
            return fallback

        weekly_pricing = ModelPricing(
            input_per_1m=grouping_price(
                "WEEKLY_MEALS_LLM_INPUT_COST_PER_1M", pricing.input_per_1m
            ),
            cached_input_per_1m=grouping_price(
                "WEEKLY_MEALS_LLM_CACHED_INPUT_COST_PER_1M",
                None,
            ),
            output_per_1m=grouping_price(
                "WEEKLY_MEALS_LLM_OUTPUT_COST_PER_1M", pricing.output_per_1m
            ),
        )
        openai_model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
        weekly_meals_model = (
            os.getenv("WEEKLY_MEALS_LLM_MODEL", "").strip() or openai_model
        )
        weekly_meals_effort = reasoning_effort(
            "WEEKLY_MEALS_LLM_REASONING_EFFORT", effort
        )

        timezone_name = os.getenv("APP_TIMEZONE", "Europe/Kyiv")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"Unknown APP_TIMEZONE: {timezone_name}") from exc

        day_start_raw = os.getenv("DEFAULT_DAY_START", "01:00") or "01:00"
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", day_start_raw):
            raise ConfigError("DEFAULT_DAY_START must use HH:MM in 24-hour format")
        default_day_start = time.fromisoformat(day_start_raw)

        presets_raw = os.getenv(
            "MEAL_WEIGHT_PRESETS",
            ",".join(str(value) for value in DEFAULT_MEAL_WEIGHT_PRESETS),
        )
        try:
            meal_weight_presets = tuple(
                int(value.strip()) for value in presets_raw.split(",")
            )
        except ValueError as exc:
            raise ConfigError(
                "MEAL_WEIGHT_PRESETS must be a comma-separated list of integers"
            ) from exc
        if (
            not meal_weight_presets
            or len(meal_weight_presets) > MAX_MEAL_WEIGHT_PRESETS
            or len(set(meal_weight_presets)) != len(meal_weight_presets)
            or any(not 1 <= value <= 10_000 for value in meal_weight_presets)
        ):
            raise ConfigError(
                "MEAL_WEIGHT_PRESETS must contain 1 to "
                f"{MAX_MEAL_WEIGHT_PRESETS} unique values from 1 to 10000"
            )

        mismatch_raw = os.getenv("NUTRITION_MISMATCH_THRESHOLD_PERCENT", "10").strip()
        try:
            nutrition_mismatch_threshold_percent = float(mismatch_raw)
        except ValueError as exc:
            raise ConfigError(
                "NUTRITION_MISMATCH_THRESHOLD_PERCENT must be a number"
            ) from exc
        if not isfinite(nutrition_mismatch_threshold_percent) or not (
            0 <= nutrition_mismatch_threshold_percent <= 100
        ):
            raise ConfigError(
                "NUTRITION_MISMATCH_THRESHOLD_PERCENT must be from 0 to 100"
            )

        credentials_path = Path(
            required["GOOGLE_SERVICE_ACCOUNT_FILE"] or ""
        ).expanduser()
        if not credentials_path.is_file():
            raise ConfigError(
                f"GOOGLE_SERVICE_ACCOUNT_FILE does not exist: {credentials_path}"
            )

        return cls(
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"] or "",
            admin_telegram_user_id=admin_telegram_user_id,
            openai_api_key=required["OPENAI_API_KEY"] or "",
            openai_admin_api_key=os.getenv("OPENAI_ADMIN_API_KEY", "").strip(),
            openai_project_id=os.getenv("OPENAI_PROJECT_ID", "").strip(),
            openai_model=openai_model,
            openai_reasoning_effort=effort,
            openai_timeout_seconds=openai_timeout_seconds,
            openai_pricing=pricing,
            weekly_meals_llm_model=weekly_meals_model,
            weekly_meals_llm_reasoning_effort=weekly_meals_effort,
            weekly_meals_llm_pricing=weekly_pricing,
            google_service_account_file=credentials_path,
            users_spreadsheet_id=required["USERS_SPREADSHEET_ID"] or "",
            users_sheet_name=os.getenv("USERS_SHEET_NAME", "users"),
            google_drive_folder_id=required["GOOGLE_DRIVE_FOLDER_ID"] or "",
            meal_sheet_name=os.getenv("MEAL_SHEET_NAME", "food_log"),
            photo_storage_dir=Path(os.getenv("PHOTO_STORAGE_DIR", "./data/photos"))
            .expanduser()
            .resolve(),
            statistics_db_path=Path(
                os.getenv("STATISTICS_DB_PATH", "./data/statistics.sqlite3")
            )
            .expanduser()
            .resolve(),
            garmin_tokenstore=Path(os.getenv("GARMIN_TOKENSTORE", "~/.garminconnect"))
            .expanduser()
            .resolve(),
            garmin_calorie_cache_path=Path(
                os.getenv(
                    "GARMIN_CALORIE_CACHE_PATH",
                    "./data/garmin_calories.json",
                )
            )
            .expanduser()
            .resolve(),
            timezone=timezone,
            default_day_start=default_day_start,
            meal_weight_presets=meal_weight_presets,
            nutrition_mismatch_threshold_percent=(nutrition_mismatch_threshold_percent),
        )
