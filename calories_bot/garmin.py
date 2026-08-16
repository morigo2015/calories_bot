from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from garminconnect import Garmin

from .sheets import accounting_date

LOGGER = logging.getLogger(__name__)
GARMIN_CACHE_SCHEMA_VERSION = 1
GARMIN_WEEK_DAYS = 7
UKRAINIAN_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


def _format_number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


class GarminCacheError(RuntimeError):
    """Raised when cached Garmin calorie data cannot be read."""


class GarminDataError(RuntimeError):
    """Raised when Garmin does not return a usable daily summary."""


@dataclass(frozen=True)
class GarminDailyCalories:
    day: str
    total_kcal: int


@dataclass(frozen=True)
class GarminCalorieSnapshot:
    refresh_day: str
    refreshed_at: str
    days: tuple[GarminDailyCalories, ...]


class GarminCalorieStore:
    """Fetch and locally cache a minimal seven-day Garmin calorie snapshot."""

    def __init__(
        self,
        tokenstore: Path,
        cache_path: Path,
        timezone: ZoneInfo,
        day_start: time,
    ) -> None:
        self._tokenstore = tokenstore.expanduser().resolve()
        self._cache_path = cache_path.expanduser().resolve()
        self._timezone = timezone
        self._day_start = day_start
        self._lock = threading.Lock()

    def refresh_if_due(self, now: datetime | None = None) -> bool:
        """Refresh once for each accounting day; preserve the cache on failure."""

        current = now or datetime.now(self._timezone)
        refresh_day = accounting_date(current, self._timezone, self._day_start)
        with self._lock:
            try:
                existing = self._read_snapshot()
            except GarminCacheError:
                LOGGER.warning(
                    "Ignoring an invalid Garmin calorie cache", exc_info=True
                )
                existing = None
            if existing is not None and existing.refresh_day == refresh_day.isoformat():
                return False
            snapshot = self._fetch_snapshot(refresh_day, current)
            self._write_snapshot(snapshot)
            return True

    def format_weekly_report(self) -> str:
        snapshot = self._read_snapshot()
        if snapshot is None:
            raise GarminCacheError("Garmin calorie cache has not been created yet")

        lines = ["🔥 Витрата калорій за останні 7 днів (Garmin):"]
        total = 0
        for entry in snapshot.days:
            day = date.fromisoformat(entry.day)
            total += entry.total_kcal
            lines.append(
                f"• {day:%d.%m}, {UKRAINIAN_WEEKDAYS[day.weekday()]} — "
                f"{_format_number(entry.total_kcal)} ккал"
            )
        average = round(total / len(snapshot.days))
        refreshed_at = datetime.fromisoformat(snapshot.refreshed_at).astimezone(
            self._timezone
        )
        lines.extend(
            (
                "",
                f"Разом: {_format_number(total)} ккал",
                f"У середньому: {_format_number(average)} ккал/день",
                f"Оновлено: {refreshed_at:%d.%m.%Y %H:%M}",
            )
        )
        return "\n".join(lines)

    def get_daily_calories(self) -> dict[date, int]:
        snapshot = self._read_snapshot()
        if snapshot is None:
            raise GarminCacheError("Garmin calorie cache has not been created yet")
        return {
            date.fromisoformat(entry.day): entry.total_kcal for entry in snapshot.days
        }

    def _fetch_snapshot(
        self, refresh_day: date, refreshed_at: datetime
    ) -> GarminCalorieSnapshot:
        client = Garmin(retry_attempts=2)
        client.login(str(self._tokenstore))
        last_day = refresh_day - timedelta(days=1)
        first_day = last_day - timedelta(days=GARMIN_WEEK_DAYS - 1)
        days: list[GarminDailyCalories] = []
        for offset in range(GARMIN_WEEK_DAYS):
            day = first_day + timedelta(days=offset)
            summary = client.get_user_summary(day.isoformat())
            days.append(
                GarminDailyCalories(
                    day=day.isoformat(),
                    total_kcal=self._parse_total_kcal(summary, day),
                )
            )
        return GarminCalorieSnapshot(
            refresh_day=refresh_day.isoformat(),
            refreshed_at=refreshed_at.astimezone(self._timezone).isoformat(),
            days=tuple(days),
        )

    @staticmethod
    def _parse_total_kcal(summary: Any, day: date) -> int:
        if not isinstance(summary, dict):
            raise GarminDataError(f"Garmin summary for {day} is not an object")
        value = summary.get("totalKilocalories")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise GarminDataError(
                f"Garmin summary for {day} has no valid totalKilocalories"
            )
        return round(value)

    def _read_snapshot(self) -> GarminCalorieSnapshot | None:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise GarminCacheError("Could not read Garmin calorie cache") from exc
        try:
            if raw["schema_version"] != GARMIN_CACHE_SCHEMA_VERSION:
                raise ValueError("unsupported schema version")
            refresh_day = date.fromisoformat(raw["refresh_day"]).isoformat()
            refreshed_at = datetime.fromisoformat(raw["refreshed_at"]).isoformat()
            if datetime.fromisoformat(refreshed_at).tzinfo is None:
                raise ValueError("refreshed_at must include a timezone")
            days = tuple(
                GarminDailyCalories(
                    day=date.fromisoformat(item["day"]).isoformat(),
                    total_kcal=self._validate_cached_kcal(item["total_kcal"]),
                )
                for item in raw["days"]
            )
            if len(days) != GARMIN_WEEK_DAYS:
                raise ValueError("snapshot must contain seven days")
            parsed_days = tuple(date.fromisoformat(entry.day) for entry in days)
            expected_days = tuple(
                parsed_days[0] + timedelta(days=offset)
                for offset in range(GARMIN_WEEK_DAYS)
            )
            if parsed_days != expected_days:
                raise ValueError("snapshot days are not consecutive")
        except (KeyError, TypeError, ValueError) as exc:
            raise GarminCacheError(
                "Garmin calorie cache has an invalid schema"
            ) from exc
        return GarminCalorieSnapshot(refresh_day, refreshed_at, days)

    @staticmethod
    def _validate_cached_kcal(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid cached calorie value")
        return value

    def _write_snapshot(self, snapshot: GarminCalorieSnapshot) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": GARMIN_CACHE_SCHEMA_VERSION,
            "refresh_day": snapshot.refresh_day,
            "refreshed_at": snapshot.refreshed_at,
            "days": [asdict(entry) for entry in snapshot.days],
        }
        temporary = self._cache_path.with_name(f".{self._cache_path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, self._cache_path)
        except OSError as exc:
            raise GarminCacheError("Could not write Garmin calorie cache") from exc
