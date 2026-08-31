from __future__ import annotations

from pathlib import Path
from typing import Protocol

import gspread
from gspread.utils import ValueInputOption, ValueRenderOption

from .models import MealResult, SavedMeal, round_meal_nutrition

LEGACY_SAVED_MEALS_HEADERS = [
    "saved_meal_id",
    "source_message_id",
    "display_name",
    "default_total_weight_g",
    "meal_json",
]
PREVIOUS_SAVED_MEALS_HEADERS = [*LEGACY_SAVED_MEALS_HEADERS, "icon"]
SAVED_MEALS_HEADERS = [
    "saved_meal_id",
    "source_message_id",
    "display_name",
    "default_total_weight_g",
    "simple_meal_json",
    "icon",
]


class SavedMealsError(RuntimeError):
    """Base error for saved-meal operations."""


class SavedMealsSchemaError(SavedMealsError):
    """Raised when the saved-meals worksheet has an unexpected schema."""


class SavedMealsReadError(SavedMealsError):
    """Raised when saved meals cannot be read."""


class SavedMealsWriteError(SavedMealsError):
    """Raised when a saved-meal change is known not to have completed."""


class SavedMealsWriteUncertainError(SavedMealsWriteError):
    """Raised when a saved-meal change cannot be verified."""


class SavedMealStore(Protocol):
    def list_meals(self) -> list[SavedMeal]: ...

    def get(self, saved_meal_id: str) -> SavedMeal | None: ...

    def find_by_source(self, source_message_id: int) -> SavedMeal | None: ...

    def append(self, saved_meal: SavedMeal) -> SavedMeal: ...

    def update(
        self,
        saved_meal_id: str,
        *,
        display_name: str | None = None,
        default_total_weight_g: int | None = None,
    ) -> SavedMeal | None: ...

    def delete(self, saved_meal_id: str) -> bool: ...


def _saved_meal_from_row(row: list[object]) -> SavedMeal:
    padded = row + [""] * (len(SAVED_MEALS_HEADERS) - len(row))
    return SavedMeal(
        saved_meal_id=str(padded[0]),
        source_message_id=int(str(padded[1])),
        display_name=str(padded[2]),
        default_total_weight_g=int(str(padded[3])),
        base_meal=round_meal_nutrition(MealResult.model_validate_json(str(padded[4]))),
        icon=str(padded[5]).strip() or None,
    )


class GoogleSavedMealStore:
    def __init__(
        self,
        credentials_file: Path | None,
        spreadsheet_id: str,
        worksheet_name: str = "saved_meals",
        *,
        client: gspread.Client | None = None,
    ) -> None:
        if client is None:
            if credentials_file is None:
                raise ValueError("credentials_file is required without a client")
            client = gspread.service_account(filename=str(credentials_file))
        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            self._worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            self._worksheet = spreadsheet.add_worksheet(
                title=worksheet_name, rows=200, cols=len(SAVED_MEALS_HEADERS)
            )
        self._ensure_headers()

    def _ensure_headers(self) -> None:
        rows = self._worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted
        )
        if not rows or not any(str(cell).strip() for row in rows for cell in row):
            self._worksheet.append_row(
                SAVED_MEALS_HEADERS, value_input_option=ValueInputOption.raw
            )
            return
        if rows[0] in (LEGACY_SAVED_MEALS_HEADERS, PREVIOUS_SAVED_MEALS_HEADERS):
            # Release 1.0.0 intentionally starts the saved-meal library over:
            # historical rows may contain composite meals, while the new
            # contract only permits one item per saved entry.  The renamed
            # JSON column is the persistent one-time migration marker.
            if rows[0] == LEGACY_SAVED_MEALS_HEADERS:
                self._worksheet.add_cols(1)
            self._worksheet.clear()
            self._worksheet.append_row(
                SAVED_MEALS_HEADERS, value_input_option=ValueInputOption.raw
            )
            rows = [SAVED_MEALS_HEADERS]
        if rows[0] != SAVED_MEALS_HEADERS:
            raise SavedMealsSchemaError(
                "Saved-meals worksheet headers are incompatible"
            )

    def _rows(self) -> list[list[object]]:
        rows = self._worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted
        )
        if not rows or rows[0] != SAVED_MEALS_HEADERS:
            raise SavedMealsSchemaError(
                "Saved-meals worksheet headers changed while the bot was running"
            )
        return rows[1:]

    def _read_all(self) -> list[tuple[int, SavedMeal]]:
        try:
            return [
                (row_number, _saved_meal_from_row(row))
                for row_number, row in enumerate(self._rows(), start=2)
            ]
        except Exception as exc:
            if isinstance(exc, SavedMealsSchemaError):
                raise
            raise SavedMealsReadError("Could not read saved meals") from exc

    def list_meals(self) -> list[SavedMeal]:
        return [meal for _, meal in reversed(self._read_all())]

    def get(self, saved_meal_id: str) -> SavedMeal | None:
        return next(
            (
                meal
                for _, meal in self._read_all()
                if meal.saved_meal_id == saved_meal_id
            ),
            None,
        )

    def find_by_source(self, source_message_id: int) -> SavedMeal | None:
        return next(
            (
                meal
                for _, meal in self._read_all()
                if meal.source_message_id == source_message_id
            ),
            None,
        )

    def append(self, saved_meal: SavedMeal) -> SavedMeal:
        saved_meal = saved_meal.model_copy(
            update={"base_meal": round_meal_nutrition(saved_meal.base_meal)}
        )
        existing = self.get(saved_meal.saved_meal_id)
        if existing is not None:
            return existing
        row: list[str | int | float] = [
            saved_meal.saved_meal_id,
            saved_meal.source_message_id,
            saved_meal.display_name,
            saved_meal.default_total_weight_g,
            saved_meal.base_meal.model_dump_json(),
            saved_meal.icon or "",
        ]
        try:
            self._worksheet.append_row(row, value_input_option=ValueInputOption.raw)
        except Exception as append_error:
            try:
                existing = self.get(saved_meal.saved_meal_id)
            except Exception as verify_error:
                raise SavedMealsWriteUncertainError(
                    "Could not verify whether the saved meal was added"
                ) from verify_error
            if existing is not None:
                return existing
            raise SavedMealsWriteError("Could not add saved meal") from append_error
        try:
            stored = self.get(saved_meal.saved_meal_id)
        except Exception as verify_error:
            raise SavedMealsWriteUncertainError(
                "The saved meal was added but could not be verified"
            ) from verify_error
        if stored is None:
            raise SavedMealsWriteError("Saved meal was not found after append")
        return stored

    def update(
        self,
        saved_meal_id: str,
        *,
        display_name: str | None = None,
        default_total_weight_g: int | None = None,
    ) -> SavedMeal | None:
        target = next(
            (
                (row_number, meal)
                for row_number, meal in self._read_all()
                if meal.saved_meal_id == saved_meal_id
            ),
            None,
        )
        if target is None:
            return None
        row_number, current = target
        updated = SavedMeal.model_validate(
            {
                **current.model_dump(),
                "display_name": (
                    current.display_name if display_name is None else display_name
                ),
                "default_total_weight_g": (
                    current.default_total_weight_g
                    if default_total_weight_g is None
                    else default_total_weight_g
                ),
            }
        )
        updates = []
        if updated.display_name != current.display_name:
            updates.append(
                {"range": f"C{row_number}", "values": [[updated.display_name]]}
            )
        if updated.default_total_weight_g != current.default_total_weight_g:
            updates.append(
                {
                    "range": f"D{row_number}",
                    "values": [[str(updated.default_total_weight_g)]],
                }
            )
        if not updates:
            return current
        try:
            self._worksheet.batch_update(updates, raw=True)
        except Exception as update_error:
            try:
                stored = self.get(saved_meal_id)
            except Exception as verify_error:
                raise SavedMealsWriteUncertainError(
                    "Could not verify whether the saved meal was updated"
                ) from verify_error
            if stored == updated:
                return stored
            raise SavedMealsWriteError("Could not update saved meal") from update_error
        try:
            stored = self.get(saved_meal_id)
        except Exception as verify_error:
            raise SavedMealsWriteUncertainError(
                "The saved meal was updated but could not be verified"
            ) from verify_error
        if stored != updated:
            raise SavedMealsWriteError("Saved meal did not match the requested update")
        return stored

    def delete(self, saved_meal_id: str) -> bool:
        target = next(
            (
                (row_number, meal)
                for row_number, meal in self._read_all()
                if meal.saved_meal_id == saved_meal_id
            ),
            None,
        )
        if target is None:
            return False
        row_number, _ = target
        try:
            self._worksheet.delete_rows(row_number)
        except Exception as delete_error:
            try:
                existing = self.get(saved_meal_id)
            except Exception as verify_error:
                raise SavedMealsWriteUncertainError(
                    "Could not verify the saved-meal deletion"
                ) from verify_error
            if existing is not None:
                raise SavedMealsWriteError(
                    "Could not delete saved meal"
                ) from delete_error
        try:
            if self.get(saved_meal_id) is not None:
                raise SavedMealsWriteError("Saved meal still exists after deletion")
        except SavedMealsWriteError:
            raise
        except Exception as verify_error:
            raise SavedMealsWriteUncertainError(
                "Saved meal was deleted but could not be verified"
            ) from verify_error
        return True
