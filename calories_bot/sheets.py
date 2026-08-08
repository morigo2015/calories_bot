from __future__ import annotations

import json
import logging
import shutil
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

LEGACY_HEADERS = [
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
HEADERS = ["timestamp", "day", *LEGACY_HEADERS[1:]]

TIMESTAMP_COLUMN = 0
DAY_COLUMN = 1
MEAL_NAME_COLUMN = 2
TOTAL_WEIGHT_COLUMN = 3
MEAL_KCAL_COLUMN = 4
KCAL_PER_100G_COLUMN = 5
MESSAGE_ID_COLUMN = 6
NORMALIZED_REQUEST_COLUMN = 7
REQUEST_COLUMN = 8
PHOTO_PATH_COLUMN = 9
ITEMS_JSON_COLUMN = 10
ESTIMATED_COLUMN = 11
MODEL_COLUMN = 12
EFFORT_COLUMN = 13
INPUT_TOKENS_COLUMN = 14
OUTPUT_TOKENS_COLUMN = 15
LLM_COST_COLUMN = 16

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
    today_total: float
    existing: StoredMeal | None


@dataclass(frozen=True)
class DayMeal:
    meal_name: str
    meal_kcal: float


@dataclass(frozen=True)
class MealDeletion:
    accounting_day: date
    day_total: int
    photo_path: str | None
    deleted: bool


class MealStore(Protocol):
    def get_state(self, day: date, telegram_message_id: int) -> SheetState: ...

    def get_day_meals(self, day: date) -> list[DayMeal]: ...

    def get_daily_totals(self, start_day: date, end_day: date) -> dict[date, float]: ...

    def delete_meal(
        self, telegram_message_id: int, fallback_day: date
    ) -> MealDeletion: ...

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


def _date_to_sheet_serial(value: date) -> int:
    return (datetime.combine(value, time()) - _SHEETS_EPOCH).days


def _date_from_sheet_serial(value: object) -> date:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Google Sheets day must be numeric")
    return (_SHEETS_EPOCH + timedelta(days=float(value))).date()


def _meal_from_row(row: list[object]) -> StoredMeal:
    padded = row + [""] * (len(HEADERS) - len(row))
    items_data = json.loads(str(padded[ITEMS_JSON_COLUMN]))
    items = [CalculatedFoodItem.model_validate(item) for item in items_data]
    meal = MealResult(
        meal_name=str(padded[MEAL_NAME_COLUMN]),
        items=items,
        total_weight_g=float(str(padded[TOTAL_WEIGHT_COLUMN])),
        kcal_per_100g=float(str(padded[KCAL_PER_100G_COLUMN])),
        meal_kcal=float(str(padded[MEAL_KCAL_COLUMN])),
        estimated=_parse_bool(padded[ESTIMATED_COLUMN]),
    )
    metadata = LLMMetadata(
        model=str(padded[MODEL_COLUMN]),
        effort=str(padded[EFFORT_COLUMN]),
        input_tokens=_optional_int(padded[INPUT_TOKENS_COLUMN]),
        output_tokens=_optional_int(padded[OUTPUT_TOKENS_COLUMN]),
        llm_cost_usd=_optional_decimal(padded[LLM_COST_COLUMN]),
    )
    return StoredMeal(
        normalized_request=str(padded[NORMALIZED_REQUEST_COLUMN]),
        meal=meal,
        metadata=metadata,
        photo_path=str(padded[PHOTO_PATH_COLUMN]) or None,
    )


class GoogleSheetsStore:
    def __init__(
        self,
        credentials_file: Path | None,
        spreadsheet_id: str,
        worksheet_name: str,
        timezone: ZoneInfo,
        day_start_time: time,
        *,
        client: gspread.Client | None = None,
    ) -> None:
        self._timezone = timezone
        self._day_start_time = day_start_time
        if client is None:
            if credentials_file is None:
                raise ValueError("credentials_file is required without a client")
            client = gspread.service_account(filename=str(credentials_file))
        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            self._worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            self._worksheet = spreadsheet.add_worksheet(
                title=worksheet_name, rows=1000, cols=len(HEADERS)
            )
        self._ensure_headers()
        self._format_date_columns()

    def migrate_legacy_photos(
        self, photo_storage_dir: Path, telegram_user_id: int
    ) -> None:
        """Move exact legacy root photos into the user's isolated directory."""
        root = photo_storage_dir.resolve()
        user_dir = (root / str(telegram_user_id)).resolve()
        if not user_dir.is_relative_to(root):
            raise ValueError("Invalid Telegram user photo directory")
        updates: list[dict[str, object]] = []
        for row_number, row in enumerate(self._data_rows(), start=2):
            if len(row) <= PHOTO_PATH_COLUMN or not str(row[PHOTO_PATH_COLUMN]).strip():
                continue
            source = Path(str(row[PHOTO_PATH_COLUMN])).resolve()
            if source.parent != root:
                continue
            destination = user_dir / source.name
            if source.exists():
                user_dir.mkdir(parents=True, exist_ok=True)
                if destination.exists() and not source.samefile(destination):
                    raise SheetsWriteError(
                        f"Cannot migrate photo because it already exists: {destination}"
                    )
                shutil.move(str(source), str(destination))
            elif not destination.exists():
                continue
            updates.append(
                {
                    "range": f"J{row_number}",
                    "values": [[str(destination)]],
                }
            )
        if updates:
            try:
                self._worksheet.batch_update(updates, raw=True)
            except Exception as exc:
                raise SheetsWriteError("Could not update migrated photo paths") from exc

    def _ensure_headers(self) -> None:
        rows = self._worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted
        )
        if not rows or not any(str(cell).strip() for row in rows for cell in row):
            self._worksheet.append_row(HEADERS, value_input_option=ValueInputOption.raw)
            return
        if rows[0] == LEGACY_HEADERS:
            self._worksheet.insert_cols(
                [["day"]], col=2, value_input_option=ValueInputOption.raw
            )
            rows = self._worksheet.get_all_values(
                value_render_option=ValueRenderOption.unformatted
            )
        if rows[0] != HEADERS:
            raise SheetSchemaError(
                "Worksheet headers are incompatible. Expected: " + ", ".join(HEADERS)
            )
        self._backfill_days(rows[1:])

    def _backfill_days(self, rows: list[list[object]]) -> None:
        updates: list[dict[str, object]] = []
        for row_number, row in enumerate(rows, start=2):
            padded = row + [""] * (len(HEADERS) - len(row))
            if str(padded[DAY_COLUMN]).strip():
                continue
            try:
                timestamp = _datetime_from_sheet_serial(
                    padded[TIMESTAMP_COLUMN], self._timezone
                )
                day = accounting_date(timestamp, self._timezone, self._day_start_time)
            except (TypeError, ValueError):
                LOGGER.warning(
                    "Could not backfill day for malformed Google Sheets row: %r", row
                )
                continue
            updates.append(
                {"range": f"B{row_number}", "values": [[_date_to_sheet_serial(day)]]}
            )
        if updates:
            self._worksheet.batch_update(updates, raw=True)

    def _format_date_columns(self) -> None:
        self._worksheet.format(
            "A2:A",
            {
                "numberFormat": {
                    "type": "DATE_TIME",
                    "pattern": "yyyy-mm-dd hh:mm:ss",
                }
            },
        )
        self._worksheet.format(
            "B2:B",
            {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}},
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
        if len(row) <= MESSAGE_ID_COLUMN:
            return None
        try:
            return int(str(row[MESSAGE_ID_COLUMN]))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _row_day(row: list[object]) -> date:
        if len(row) <= DAY_COLUMN:
            raise ValueError("Google Sheets row has no day")
        return _date_from_sheet_serial(row[DAY_COLUMN])

    def _matches_message(
        self, row: list[object], telegram_message_id: int, day: date
    ) -> bool:
        try:
            return (
                self._message_id(row) == telegram_message_id
                and self._row_day(row) == day
            )
        except (TypeError, ValueError):
            return False

    def _day_total(self, rows: list[list[object]], day: date) -> float:
        total = 0.0
        for row in rows:
            if len(row) <= MEAL_KCAL_COLUMN:
                continue
            try:
                if self._row_day(row) == day:
                    total += float(str(row[MEAL_KCAL_COLUMN]))
            except (TypeError, ValueError):
                LOGGER.warning("Skipping malformed Google Sheets row: %r", row)
        return total

    def _find_by_message_id(
        self,
        rows: list[list[object]],
        telegram_message_id: int,
        day: date,
    ) -> StoredMeal | None:
        for row in rows:
            if self._matches_message(row, telegram_message_id, day):
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
            existing = self._find_by_message_id(rows, telegram_message_id, day)
            return SheetState(today_total=self._day_total(rows, day), existing=existing)
        except Exception as exc:
            raise SheetsReadError("Could not read Google Sheets") from exc

    def get_day_meals(self, day: date) -> list[DayMeal]:
        try:
            meals: list[DayMeal] = []
            for row in self._data_rows():
                if len(row) <= MEAL_KCAL_COLUMN:
                    continue
                try:
                    if self._row_day(row) != day:
                        continue
                    meal_name = str(row[MEAL_NAME_COLUMN]).strip()
                    if not meal_name:
                        raise ValueError("Meal name is empty")
                    meals.append(
                        DayMeal(
                            meal_name=meal_name,
                            meal_kcal=float(str(row[MEAL_KCAL_COLUMN])),
                        )
                    )
                except (TypeError, ValueError):
                    LOGGER.warning("Skipping malformed Google Sheets row: %r", row)
            return meals
        except Exception as exc:
            raise SheetsReadError("Could not read Google Sheets") from exc

    def get_daily_totals(self, start_day: date, end_day: date) -> dict[date, float]:
        if end_day < start_day:
            raise ValueError("end_day cannot be before start_day")
        try:
            totals: dict[date, float] = {}
            # One worksheet read per report; grouping happens entirely in memory.
            for row in self._data_rows():
                if len(row) <= MEAL_KCAL_COLUMN:
                    continue
                try:
                    row_day = self._row_day(row)
                    if start_day <= row_day <= end_day:
                        totals[row_day] = totals.get(row_day, 0.0) + float(
                            str(row[MEAL_KCAL_COLUMN])
                        )
                except (TypeError, ValueError):
                    LOGGER.warning("Skipping malformed Google Sheets row: %r", row)
            return totals
        except Exception as exc:
            raise SheetsReadError("Could not read Google Sheets") from exc

    def delete_meal(self, telegram_message_id: int, fallback_day: date) -> MealDeletion:
        try:
            rows = self._data_rows()
        except Exception as exc:
            raise SheetsReadError("Could not read Google Sheets") from exc

        target_index: int | None = None
        for index, row in enumerate(rows):
            if self._matches_message(row, telegram_message_id, fallback_day):
                target_index = index
                break

        if target_index is None:
            return MealDeletion(
                accounting_day=fallback_day,
                day_total=round_whole(self._day_total(rows, fallback_day)),
                photo_path=None,
                deleted=False,
            )

        target = rows[target_index]
        try:
            day = self._row_day(target)
        except (TypeError, ValueError):
            day = fallback_day
        photo_path = (
            str(target[PHOTO_PATH_COLUMN]) or None
            if len(target) > PHOTO_PATH_COLUMN
            else None
        )

        try:
            self._worksheet.delete_rows(target_index + 2)
        except Exception as delete_error:
            try:
                verified_rows = self._data_rows()
            except Exception as verify_error:
                raise SheetsWriteUncertainError(
                    "Could not verify whether Google Sheets deleted the row"
                ) from verify_error
            if any(
                self._matches_message(row, telegram_message_id, fallback_day)
                for row in verified_rows
            ):
                raise SheetsWriteError(
                    "Google Sheets did not delete the row"
                ) from delete_error
            rows = verified_rows
        else:
            try:
                rows = self._data_rows()
            except Exception as verify_error:
                raise SheetsWriteUncertainError(
                    "The row was deleted but the new daily total could not be read"
                ) from verify_error

        return MealDeletion(
            accounting_day=day,
            day_total=round_whole(self._day_total(rows, day)),
            photo_path=photo_path,
            deleted=True,
        )

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
            _date_to_sheet_serial(
                accounting_date(timestamp, self._timezone, self._day_start_time)
            ),
            meal.meal_name,
            meal.total_weight_g,
            meal.meal_kcal,
            meal.kcal_per_100g,
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
                    self._data_rows(),
                    telegram_message_id,
                    accounting_date(timestamp, self._timezone, self._day_start_time),
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
