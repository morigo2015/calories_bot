from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .analyzer import ModelPricing


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    admin_telegram_user_id: int
    openai_api_key: str
    openai_model: str
    openai_reasoning_effort: str
    openai_pricing: ModelPricing
    google_service_account_file: Path
    users_spreadsheet_id: str
    users_sheet_name: str
    google_drive_folder_id: str
    meal_sheet_name: str
    photo_storage_dir: Path
    timezone: ZoneInfo
    default_day_start: time

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

        effort = os.getenv("OPENAI_REASONING_EFFORT", "low") or "low"
        allowed_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if effort not in allowed_efforts:
            raise ConfigError(
                "OPENAI_REASONING_EFFORT must be one of: "
                + ", ".join(sorted(allowed_efforts))
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

        timezone_name = os.getenv("APP_TIMEZONE", "Europe/Kyiv")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"Unknown APP_TIMEZONE: {timezone_name}") from exc

        day_start_raw = os.getenv("DEFAULT_DAY_START", "01:00") or "01:00"
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", day_start_raw):
            raise ConfigError("DEFAULT_DAY_START must use HH:MM in 24-hour format")
        default_day_start = time.fromisoformat(day_start_raw)

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
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
            openai_reasoning_effort=effort,
            openai_pricing=pricing,
            google_service_account_file=credentials_path,
            users_spreadsheet_id=required["USERS_SPREADSHEET_ID"] or "",
            users_sheet_name=os.getenv("USERS_SHEET_NAME", "users"),
            google_drive_folder_id=required["GOOGLE_DRIVE_FOLDER_ID"] or "",
            meal_sheet_name=os.getenv("MEAL_SHEET_NAME", "food_log"),
            photo_storage_dir=Path(os.getenv("PHOTO_STORAGE_DIR", "./data/photos"))
            .expanduser()
            .resolve(),
            timezone=timezone,
            default_day_start=default_day_start,
        )
