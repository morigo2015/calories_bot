from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import gspread
from gspread.utils import ValueInputOption, ValueRenderOption

from .models import (
    CalculatedFoodItem,
    LLMMetadata,
    MealResult,
    StoredMeal,
    round_whole,
)

LOGGER = logging.getLogger(__name__)

HEADERS = [
    "timestamp",
    "meal_name",
    "total_weight_g",
    "meal_kcal",
    "kcal_per_100g",
    "telegram_message_id",
    "normalized_request",
    "request",
    "photo_path",
    "items_json",
    "estimated",
    "model",
    "effort",
    "input_tokens",
    "output_tokens",
    "llm_cost_usd",
]

_SHEETS_EPOCH = datetime(1899, 12, 30)


class SheetsError(RuntimeError):
    """Base error for Google Sheets operations."""


class SheetSchemaError(SheetsError):
    """Raised when the worksheet header is incompatible."""


class SheetsReadError(SheetsError):
    """Raised when stored meals cannot be read."""


class SheetsWriteError(SheetsError):
    """Raised when a meal cannot be reliably stored."""


class SheetsWriteUncertainError(SheetsWriteError):
    """Raised when Google may have stored a row but verification also failed."""


@dataclass(frozen=True)
class SheetState:
    today_total: int
    existing: StoredMeal | None


class MealStore(Protocol):
    def get_state(self, day: date, telegram_message_id: int) -> SheetState: ...

    def append_meal(
        self,
        timestamp: datetime,
        telegram_message_id: int,
        request: str,
        normalized_request: str,
        photo_path: str | None,
        meal: MealResult,
        metadata: LLMMetadata,
    ) -> StoredMeal: ...


def accounting_date(timestamp: datetime, timezone: ZoneInfo, day_start: time) -> date:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone)
    local = timestamp.astimezone(timezone)
    result = local.date()
    if local.timetz().replace(tzinfo=None) < day_start:
        result -= timedelta(days=1)
    return result


def _parse_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _optional_int(value: object) -> int | None:
    text = str(value).strip()
    return int(text) if text else None


def _optional_decimal(value: object) -> Decimal | None:
    normalized = str(value).strip().replace(",", ".")
    return Decimal(normalized) if normalized else None


def _datetime_to_sheet_serial(timestamp: datetime, timezone: ZoneInfo) -> float:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone)
    local = timestamp.astimezone(timezone).replace(tzinfo=None)
    return (local - _SHEETS_EPOCH).total_seconds() / 86400


def _datetime_from_sheet_serial(value: object, timezone: ZoneInfo) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Google Sheets timestamp must be numeric")
    return (_SHEETS_EPOCH + timedelta(days=float(value))).replace(tzinfo=timezone)


def _meal_from_row(row: list[object]) -> StoredMeal:
    padded = row + [""] * (len(HEADERS) - len(row))
    items_data = json.loads(str(padded[9]))
    items = [CalculatedFoodItem.model_validate(item) for item in items_data]
    meal = MealResult(
        meal_name=str(padded[1]),
        items=items,
        total_weight_g=float(str(padded[2])),
        kcal_per_100g=float(str(padded[4])),
        meal_kcal=float(str(padded[3])),
        estimated=_parse_bool(padded[10]),
    )
    metadata = LLMMetadata(
        model=str(padded[11]),
        effort=str(padded[12]),
        input_tokens=_optional_int(padded[13]),
        output_tokens=_optional_int(padded[14]),
        llm_cost_usd=_optional_decimal(padded[15]),
    )
    return StoredMeal(
        normalized_request=str(padded[6]),
        meal=meal,
        metadata=metadata,
        photo_path=str(padded[8]) or None,
    )


class GoogleSheetsStore:
    def __init__(
        self,
        credentials_file: Path,
        spreadsheet_id: str,
        worksheet_name: str,
        timezone: ZoneInfo,
        day_start_time: time,
    ) -> None:
        self._timezone = timezone
        self._day_start_time = day_start_time
        client = gspread.service_account(filename=str(credentials_file))
        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            self._worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            self._worksheet = spreadsheet.add_worksheet(
                title=worksheet_name, rows=1000, cols=len(HEADERS)
            )
        self._ensure_headers()
        self._format_timestamp_column()

    def _ensure_headers(self) -> None:
        rows = self._worksheet.get_all_values()
        if not rows or not any(str(cell).strip() for row in rows for cell in row):
            self._worksheet.append_row(HEADERS, value_input_option=ValueInputOption.raw)
            return
        if rows[0] != HEADERS:
            raise SheetSchemaError(
                "Worksheet headers are incompatible. Expected: " + ", ".join(HEADERS)
            )

    def _format_timestamp_column(self) -> None:
        self._worksheet.format(
            "A2:A",
            {
                "numberFormat": {
                    "type": "DATE_TIME",
                    "pattern": "yyyy-mm-dd hh:mm:ss",
                }
            },
        )

    def _data_rows(self) -> list[list[object]]:
        rows = self._worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted
        )
        if not rows or rows[0] != HEADERS:
            raise SheetSchemaError(
                "Worksheet headers changed while the bot was running"
            )
        return rows[1:]

    @staticmethod
    def _message_id(row: list[object]) -> int | None:
        if len(row) < 6:
            return None
        try:
            return int(str(row[5]))
        except (TypeError, ValueError):
            return None

    def _find_by_message_id(
        self, rows: list[list[object]], telegram_message_id: int
    ) -> StoredMeal | None:
        for row in rows:
            if self._message_id(row) == telegram_message_id:
                try:
                    return _meal_from_row(row)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise SheetSchemaError(
                        f"Invalid stored row for Telegram message {telegram_message_id}"
                    ) from exc
        return None

    def get_state(self, day: date, telegram_message_id: int) -> SheetState:
        try:
            rows = self._data_rows()
            existing = self._find_by_message_id(rows, telegram_message_id)
            total = 0.0
            for row in rows:
                if len(row) < 4:
                    continue
                try:
                    timestamp = _datetime_from_sheet_serial(row[0], self._timezone)
                    if (
                        accounting_date(timestamp, self._timezone, self._day_start_time)
                        == day
                    ):
                        total += float(str(row[3]))
                except (TypeError, ValueError):
                    LOGGER.warning("Skipping malformed Google Sheets row: %r", row)
            return SheetState(today_total=round_whole(total), existing=existing)
        except Exception as exc:
            raise SheetsReadError("Could not read Google Sheets") from exc

    def append_meal(
        self,
        timestamp: datetime,
        telegram_message_id: int,
        request: str,
        normalized_request: str,
        photo_path: str | None,
        meal: MealResult,
        metadata: LLMMetadata,
    ) -> StoredMeal:
        items_json = json.dumps(
            [item.model_dump() for item in meal.items],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cost: float | str = ""
        if metadata.llm_cost_usd is not None:
            cost = float(
                metadata.llm_cost_usd.quantize(
                    Decimal("0.00000001"), rounding=ROUND_HALF_UP
                )
            )
        row: list[str | int | float | bool] = [
            _datetime_to_sheet_serial(timestamp, self._timezone),
            meal.meal_name,
            round_whole(meal.total_weight_g),
            round_whole(meal.meal_kcal),
            round_whole(meal.kcal_per_100g),
            telegram_message_id,
            normalized_request,
            request,
            photo_path or "",
            items_json,
            meal.estimated,
            metadata.model,
            metadata.effort,
            metadata.input_tokens if metadata.input_tokens is not None else "",
            metadata.output_tokens if metadata.output_tokens is not None else "",
            cost,
        ]
        stored = StoredMeal(
            normalized_request=normalized_request,
            meal=meal,
            metadata=metadata,
            photo_path=photo_path,
        )
        try:
            self._worksheet.append_row(row, value_input_option=ValueInputOption.raw)
            return stored
        except Exception as append_error:
            try:
                existing = self._find_by_message_id(
                    self._data_rows(), telegram_message_id
                )
            except Exception as verify_error:
                raise SheetsWriteUncertainError(
                    "Could not verify whether Google Sheets accepted the row"
                ) from verify_error
            if existing is not None:
                return existing
            raise SheetsWriteError(
                "Google Sheets did not accept the row"
            ) from append_error
