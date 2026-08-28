from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Literal, Protocol

import gspread
from gspread.utils import ValueInputOption, ValueRenderOption

HEADERS = [
    "day",
    "input_type",
    "input_kcal",
    "resting_kcal",
    "effective_total_kcal",
    "profile_snapshot",
    "updated_at",
]

MIN_BURNED_KCAL = 1
MAX_BURNED_KCAL = 20_000


class BurnedCaloriesError(RuntimeError):
    """Raised when burned-calorie data cannot be read or written safely."""


@dataclass(frozen=True)
class BodyProfile:
    sex: Literal["male", "female"]
    birth_date: date
    height_cm: int
    weight_kg: float


@dataclass(frozen=True)
class BurnedCaloriesEntry:
    day: date
    input_type: Literal["total", "active"]
    input_kcal: int
    resting_kcal: int
    effective_total_kcal: int
    profile_snapshot: BodyProfile | None
    updated_at: datetime


class BurnedCalorieStore(Protocol):
    def get(self, day: date) -> BurnedCaloriesEntry | None: ...

    def get_range(
        self, start_day: date, end_day: date
    ) -> dict[date, BurnedCaloriesEntry]: ...

    def upsert(self, entry: BurnedCaloriesEntry) -> BurnedCaloriesEntry: ...

    def delete(self, day: date) -> bool: ...


def calculate_resting_kcal(profile: BodyProfile, day: date) -> int:
    """Calculate a full-day BMR using Mifflin-St Jeor."""

    if profile.sex not in {"male", "female"}:
        raise ValueError("Unsupported sex for BMR formula")
    if not 100 <= profile.height_cm <= 250 or not 20 <= profile.weight_kg <= 500:
        raise ValueError("Body profile is outside the supported range")
    age = (
        day.year
        - profile.birth_date.year
        - ((day.month, day.day) < (profile.birth_date.month, profile.birth_date.day))
    )
    if not 13 <= age <= 120:
        raise ValueError("Age on the selected day must be between 13 and 120")
    sex_offset = 5 if profile.sex == "male" else -161
    return round(
        10 * profile.weight_kg + 6.25 * profile.height_cm - 5 * age + sex_offset
    )


def build_burned_entry(
    day: date,
    input_type: Literal["total", "active"],
    input_kcal: int,
    updated_at: datetime,
    profile: BodyProfile | None = None,
) -> BurnedCaloriesEntry:
    if not MIN_BURNED_KCAL <= input_kcal <= MAX_BURNED_KCAL:
        raise ValueError("Burned calories are outside the supported range")
    if input_type == "active":
        if profile is None:
            raise ValueError("A body profile is required for active calories")
        resting = calculate_resting_kcal(profile, day)
    elif input_type == "total":
        resting = 0
        profile = None
    else:
        raise ValueError("Unsupported burned-calorie input type")
    total = input_kcal + resting
    if total > MAX_BURNED_KCAL:
        raise ValueError("Total burned calories are outside the supported range")
    return BurnedCaloriesEntry(
        day=day,
        input_type=input_type,
        input_kcal=input_kcal,
        resting_kcal=resting,
        effective_total_kcal=total,
        profile_snapshot=profile,
        updated_at=updated_at,
    )


class GoogleBurnedCalorieStore:
    def __init__(
        self,
        spreadsheet_id: str,
        *,
        client: gspread.Client,
        worksheet_name: str = "burned_calories",
    ) -> None:
        self._lock = threading.RLock()
        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            self._worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            self._worksheet = spreadsheet.add_worksheet(
                title=worksheet_name, rows=400, cols=len(HEADERS)
            )
        self._ensure_headers()

    def _ensure_headers(self) -> None:
        rows = self._worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted
        )
        if not rows or not any(str(cell).strip() for row in rows for cell in row):
            self._worksheet.append_row(HEADERS, value_input_option=ValueInputOption.raw)
            return
        if rows[0] != HEADERS:
            raise BurnedCaloriesError(
                "Burned-calorie worksheet headers are incompatible"
            )

    def _rows(self) -> list[list[object]]:
        rows = self._worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted
        )
        if not rows or rows[0] != HEADERS:
            raise BurnedCaloriesError("Burned-calorie worksheet headers changed")
        return rows[1:]

    @staticmethod
    def _entry(row: list[object]) -> BurnedCaloriesEntry:
        padded = row + [""] * (len(HEADERS) - len(row))
        input_type = str(padded[1]).strip()
        if input_type not in {"total", "active"}:
            raise ValueError("Invalid input_type")
        snapshot_raw = str(padded[5]).strip()
        snapshot = None
        if snapshot_raw:
            data = json.loads(snapshot_raw)
            snapshot = BodyProfile(
                sex=data["sex"],
                birth_date=date.fromisoformat(data["birth_date"]),
                height_cm=int(data["height_cm"]),
                weight_kg=float(data["weight_kg"]),
            )
        updated_at = datetime.fromisoformat(str(padded[6]))
        if updated_at.tzinfo is None:
            raise ValueError("updated_at must include a timezone")
        parsed = BurnedCaloriesEntry(
            day=date.fromisoformat(str(padded[0])),
            input_type=input_type,  # type: ignore[arg-type]
            input_kcal=int(str(padded[2])),
            resting_kcal=int(str(padded[3])),
            effective_total_kcal=int(str(padded[4])),
            profile_snapshot=snapshot,
            updated_at=updated_at,
        )
        expected = build_burned_entry(
            parsed.day,
            parsed.input_type,
            parsed.input_kcal,
            parsed.updated_at,
            parsed.profile_snapshot,
        )
        if expected != parsed:
            raise ValueError("Stored burned-calorie calculation is inconsistent")
        return parsed

    @staticmethod
    def _values(entry: BurnedCaloriesEntry) -> list[str | int | float]:
        snapshot = ""
        if entry.profile_snapshot is not None:
            data = asdict(entry.profile_snapshot)
            data["birth_date"] = entry.profile_snapshot.birth_date.isoformat()
            snapshot = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return [
            entry.day.isoformat(),
            entry.input_type,
            entry.input_kcal,
            entry.resting_kcal,
            entry.effective_total_kcal,
            snapshot,
            entry.updated_at.isoformat(),
        ]

    def _entries(self) -> list[tuple[int, BurnedCaloriesEntry]]:
        try:
            entries = [
                (index, self._entry(row)) for index, row in enumerate(self._rows(), 2)
            ]
        except Exception as exc:
            raise BurnedCaloriesError("Could not read burned-calorie entries") from exc
        days = [entry.day for _, entry in entries]
        if len(days) != len(set(days)):
            raise BurnedCaloriesError("Duplicate burned-calorie days")
        return entries

    def get(self, day: date) -> BurnedCaloriesEntry | None:
        with self._lock:
            return next(
                (entry for _, entry in self._entries() if entry.day == day), None
            )

    def get_range(
        self, start_day: date, end_day: date
    ) -> dict[date, BurnedCaloriesEntry]:
        with self._lock:
            return {
                entry.day: entry
                for _, entry in self._entries()
                if start_day <= entry.day <= end_day
            }

    def upsert(self, entry: BurnedCaloriesEntry) -> BurnedCaloriesEntry:
        with self._lock:
            current = next(
                (
                    (row_number, item)
                    for row_number, item in self._entries()
                    if item.day == entry.day
                ),
                None,
            )
            try:
                if current is None:
                    self._worksheet.append_row(
                        self._values(entry), value_input_option=ValueInputOption.raw
                    )
                else:
                    self._worksheet.update(
                        values=[self._values(entry)],
                        range_name=f"A{current[0]}:G{current[0]}",
                        raw=True,
                    )
            except Exception as exc:
                raise BurnedCaloriesError("Could not save burned calories") from exc
            saved = self.get(entry.day)
            if saved != entry:
                raise BurnedCaloriesError("Could not verify burned-calorie update")
            return saved

    def delete(self, day: date) -> bool:
        with self._lock:
            current = next(
                (
                    (row_number, item)
                    for row_number, item in self._entries()
                    if item.day == day
                ),
                None,
            )
            if current is None:
                return False
            try:
                self._worksheet.delete_rows(current[0])
            except Exception as exc:
                raise BurnedCaloriesError("Could not delete burned calories") from exc
            return self.get(day) is None
