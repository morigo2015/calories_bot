from __future__ import annotations

import json
import logging
import os
import threading
import time as time_module
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from garminconnect import Garmin

from .sheets import accounting_date

LOGGER = logging.getLogger(__name__)
GARMIN_CACHE_SCHEMA_VERSION = 4
GARMIN_LEGACY_MONTH_DAYS = 30
GARMIN_CACHE_DAYS = 90
GARMIN_WEEK_DAYS = 7
GARMIN_RECENT_DAYS_TO_REFRESH = 3
GARMIN_WEIGHT_HISTORY_START = date(2000, 1, 1)
GARMIN_BACKFILL_DELAY_SECONDS = 1.0
GARMIN_TOKEN_FILENAME = "garmin_tokens.json"
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
class GarminDailyWeight:
    day: str
    weight_kg: float


@dataclass(frozen=True)
class GarminCalorieSnapshot:
    refresh_day: str
    refreshed_at: str
    days: tuple[GarminDailyCalories, ...]
    weights: tuple[GarminDailyWeight, ...]
    weight_history_imported: bool = True


class GarminCalorieStore:
    """Fetch a rolling Garmin calorie archive used by weekly/monthly reports."""

    def __init__(
        self,
        tokenstore: Path,
        cache_path: Path,
        timezone: ZoneInfo,
        day_start: time,
        request_delay_seconds: float = GARMIN_BACKFILL_DELAY_SECONDS,
    ) -> None:
        self._tokenstore = tokenstore.expanduser().resolve()
        self._cache_path = cache_path.expanduser().resolve()
        self._timezone = timezone
        self._day_start = day_start
        self._request_delay_seconds = request_delay_seconds
        self._lock = threading.Lock()

    def refresh_if_due(
        self, now: datetime | None = None, *, force: bool = False
    ) -> bool:
        """Refresh at most once per accounting day and resume partial backfills."""

        current = now or datetime.now(self._timezone)
        if current.tzinfo is None:
            current = current.replace(tzinfo=self._timezone)
        else:
            current = current.astimezone(self._timezone)
        refresh_day = accounting_date(current, self._timezone, self._day_start)
        with self._lock:
            try:
                existing = self._read_snapshot()
            except GarminCacheError:
                LOGGER.warning(
                    "Ignoring an invalid Garmin calorie cache", exc_info=True
                )
                existing = None
            if (
                existing is not None
                and existing.refresh_day == refresh_day.isoformat()
                and len(existing.days) == GARMIN_CACHE_DAYS
                and existing.weight_history_imported
                and not force
            ):
                return False
            self._fetch_snapshot(
                refresh_day,
                current,
                existing,
                force_recent=force,
            )
            return True

    def format_weekly_report(self) -> str:
        snapshot = self._read_snapshot()
        if snapshot is None:
            raise GarminCacheError("Garmin calorie cache has not been created yet")

        lines = ["🔥 Витрата калорій за останні 7 днів (Garmin):"]
        total = 0
        latest_days = snapshot.days[-GARMIN_WEEK_DAYS:]
        if not latest_days:
            raise GarminCacheError("Garmin calorie cache is incomplete")
        for entry in latest_days:
            day = date.fromisoformat(entry.day)
            total += entry.total_kcal
            lines.append(
                f"• {day:%d.%m}, {UKRAINIAN_WEEKDAYS[day.weekday()]} — "
                f"{_format_number(entry.total_kcal)} ккал"
            )
        average = round(total / len(latest_days))
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

    def get_daily_weights(self) -> dict[date, float]:
        """Return daily average weights, carrying the latest value forward."""

        snapshot = self._read_snapshot()
        if snapshot is None:
            raise GarminCacheError("Garmin cache has not been created yet")
        if not snapshot.weights:
            return {}

        recorded = {
            date.fromisoformat(entry.day): entry.weight_kg for entry in snapshot.weights
        }
        first_day = min(recorded)
        last_day = date.fromisoformat(snapshot.refresh_day) - timedelta(days=1)
        result: dict[date, float] = {}
        latest: float | None = None
        day = first_day
        while day <= last_day:
            if day in recorded:
                latest = recorded[day]
            if latest is not None:
                result[day] = latest
            day += timedelta(days=1)
        return result

    def get_recorded_daily_weights(self) -> dict[date, float]:
        """Return only dates on which Garmin has an actual weight measurement."""

        snapshot = self._read_snapshot()
        if snapshot is None:
            raise GarminCacheError("Garmin cache has not been created yet")
        return {
            date.fromisoformat(entry.day): entry.weight_kg for entry in snapshot.weights
        }

    def _fetch_snapshot(
        self,
        refresh_day: date,
        refreshed_at: datetime,
        existing: GarminCalorieSnapshot | None = None,
        *,
        force_recent: bool = False,
    ) -> GarminCalorieSnapshot:
        last_day = refresh_day - timedelta(days=1)
        first_day = last_day - timedelta(days=GARMIN_CACHE_DAYS - 1)
        cached = {
            date.fromisoformat(entry.day): entry
            for entry in (() if existing is None else existing.days)
            if first_day <= date.fromisoformat(entry.day) <= last_day
        }
        requested_days = tuple(
            first_day + timedelta(days=offset) for offset in range(GARMIN_CACHE_DAYS)
        )
        missing_days = tuple(day for day in requested_days if day not in cached)
        needs_full_weight_history = (
            existing is None or not existing.weight_history_imported
        )
        if needs_full_weight_history:
            weight_start = GARMIN_WEIGHT_HISTORY_START
        else:
            assert existing is not None
            weight_start = date.fromisoformat(existing.refresh_day)
        needs_weights = weight_start <= last_day
        client: Garmin | None = None
        refreshes_new_day = (
            existing is not None
            and date.fromisoformat(existing.refresh_day) < refresh_day
        )
        recent_days = (
            tuple(
                last_day - timedelta(days=offset)
                for offset in range(GARMIN_RECENT_DAYS_TO_REFRESH)
            )
            if refreshes_new_day or force_recent
            else ()
        )
        calorie_days = tuple(sorted({*missing_days, *recent_days}, reverse=True))
        if calorie_days or needs_weights:
            client = self._connect()

        existing_weights = {
            date.fromisoformat(entry.day): entry
            for entry in (() if existing is None else existing.weights)
        }
        if needs_weights:
            assert client is not None
            fetched_weights = self._fetch_weights(client, weight_start, last_day)
            existing_weights = {
                day: entry
                for day, entry in existing_weights.items()
                if not weight_start <= day <= last_day
            }
            existing_weights.update(
                {date.fromisoformat(entry.day): entry for entry in fetched_weights}
            )
        weights = tuple(existing_weights[day] for day in sorted(existing_weights))

        def checkpoint() -> GarminCalorieSnapshot:
            snapshot = GarminCalorieSnapshot(
                refresh_day=refresh_day.isoformat(),
                refreshed_at=refreshed_at.isoformat(),
                days=tuple(cached[day] for day in sorted(cached)),
                weights=weights,
            )
            self._write_snapshot(snapshot)
            return snapshot

        snapshot = checkpoint()
        for index, day in enumerate(calorie_days):
            assert client is not None
            cached[day] = self._fetch_day(client, day)
            snapshot = checkpoint()
            if index < len(calorie_days) - 1 and self._request_delay_seconds > 0:
                time_module.sleep(self._request_delay_seconds)
        return snapshot

    def _connect(self) -> Garmin:
        client = Garmin(retry_attempts=2)
        client.login(str(self._tokenstore))
        return client

    def _fetch_day(self, client: Garmin, day: date) -> GarminDailyCalories:
        summary = client.get_user_summary(day.isoformat())
        return GarminDailyCalories(
            day=day.isoformat(),
            total_kcal=self._parse_total_kcal(summary, day),
        )

    def _fetch_weights(
        self, client: Garmin, start_day: date, end_day: date
    ) -> tuple[GarminDailyWeight, ...]:
        payload = client.get_body_composition(
            start_day.isoformat(), end_day.isoformat()
        )
        return self._parse_daily_weights(payload, start_day, end_day)

    @staticmethod
    def _parse_daily_weights(
        payload: Any, start_day: date, end_day: date
    ) -> tuple[GarminDailyWeight, ...]:
        if not isinstance(payload, dict):
            raise GarminDataError("Garmin weight response is not an object")
        raw_entries = payload.get("dateWeightList")
        if not isinstance(raw_entries, list):
            raise GarminDataError("Garmin weight response has no measurement list")

        grouped: dict[date, list[float]] = {}
        for item in raw_entries:
            if not isinstance(item, dict):
                raise GarminDataError("Garmin weight measurement is not an object")
            try:
                day = date.fromisoformat(str(item["calendarDate"]))
            except (KeyError, ValueError) as exc:
                raise GarminDataError(
                    "Garmin weight measurement has no valid date"
                ) from exc
            value = item.get("weight")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
            ):
                raise GarminDataError(
                    f"Garmin weight measurement for {day} has no valid weight"
                )
            if start_day <= day <= end_day:
                # Garmin returns body weight in grams.
                grouped.setdefault(day, []).append(float(value) / 1000)

        return tuple(
            GarminDailyWeight(
                day=day.isoformat(),
                weight_kg=round(sum(values) / len(values), 3),
            )
            for day, values in sorted(grouped.items())
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
            schema_version = raw["schema_version"]
            if schema_version not in {1, 2, 3, GARMIN_CACHE_SCHEMA_VERSION}:
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
            parsed_days = tuple(date.fromisoformat(entry.day) for entry in days)
            refresh_date = date.fromisoformat(refresh_day)
            if schema_version == GARMIN_CACHE_SCHEMA_VERSION:
                if len(days) > GARMIN_CACHE_DAYS:
                    raise ValueError("snapshot contains too many calorie days")
                if parsed_days != tuple(sorted(set(parsed_days))):
                    raise ValueError("calorie days must be unique and sorted")
                first_allowed = refresh_date - timedelta(days=GARMIN_CACHE_DAYS)
                if any(not first_allowed <= day < refresh_date for day in parsed_days):
                    raise ValueError("calorie day is outside the cache window")
            else:
                expected_count = (
                    GARMIN_WEEK_DAYS
                    if schema_version == 1
                    else GARMIN_LEGACY_MONTH_DAYS
                )
                if len(days) != expected_count:
                    raise ValueError(f"snapshot must contain {expected_count} days")
                expected_days = tuple(
                    parsed_days[0] + timedelta(days=offset)
                    for offset in range(expected_count)
                )
                if parsed_days != expected_days:
                    raise ValueError("snapshot days are not consecutive")
                if parsed_days[-1] != refresh_date - timedelta(days=1):
                    raise ValueError(
                        "snapshot does not end on the latest completed day"
                    )
            weights = (
                tuple(
                    GarminDailyWeight(
                        day=date.fromisoformat(item["day"]).isoformat(),
                        weight_kg=self._validate_cached_weight(item["weight_kg"]),
                    )
                    for item in raw["weights"]
                )
                if schema_version >= 3
                else ()
            )
            weight_days = tuple(date.fromisoformat(entry.day) for entry in weights)
            if weight_days != tuple(sorted(set(weight_days))):
                raise ValueError("weight days must be unique and sorted")
            if weight_days and weight_days[-1] >= date.fromisoformat(refresh_day):
                raise ValueError("weight history includes an unfinished day")
        except (KeyError, TypeError, ValueError) as exc:
            raise GarminCacheError(
                "Garmin calorie cache has an invalid schema"
            ) from exc
        return GarminCalorieSnapshot(
            refresh_day,
            refreshed_at,
            days,
            weights,
            weight_history_imported=schema_version >= 3,
        )

    @staticmethod
    def _validate_cached_kcal(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid cached calorie value")
        return value

    @staticmethod
    def _validate_cached_weight(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError("invalid cached weight value")
        return float(value)

    def _write_snapshot(self, snapshot: GarminCalorieSnapshot) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": GARMIN_CACHE_SCHEMA_VERSION,
            "refresh_day": snapshot.refresh_day,
            "refreshed_at": snapshot.refreshed_at,
            "days": [asdict(entry) for entry in snapshot.days],
            "weights": [asdict(entry) for entry in snapshot.weights],
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


class GarminStoreProvider:
    """Resolve an isolated Garmin store for a Telegram user."""

    def __init__(
        self,
        data_dir: Path,
        timezone: ZoneInfo,
        *,
        fallback_user_id: int | None = None,
        fallback_tokenstore: Path | None = None,
        fallback_cache_path: Path | None = None,
        request_delay_seconds: float = GARMIN_BACKFILL_DELAY_SECONDS,
    ) -> None:
        self._data_dir = data_dir.expanduser().resolve()
        self._timezone = timezone
        self._fallback_user_id = fallback_user_id
        self._fallback_tokenstore = (
            None
            if fallback_tokenstore is None
            else fallback_tokenstore.expanduser().resolve()
        )
        self._fallback_cache_path = (
            None
            if fallback_cache_path is None
            else fallback_cache_path.expanduser().resolve()
        )
        self._request_delay_seconds = request_delay_seconds
        self._stores: dict[tuple[int, time, Path], GarminCalorieStore] = {}
        self._lock = threading.Lock()

    def user_dir(self, telegram_user_id: int) -> Path:
        return self._data_dir / str(telegram_user_id)

    def personal_tokenstore(self, telegram_user_id: int) -> Path:
        return self.user_dir(telegram_user_id) / "tokens"

    def personal_cache_path(self, telegram_user_id: int) -> Path:
        return self.user_dir(telegram_user_id) / "cache.json"

    @staticmethod
    def _has_tokens(path: Path | None) -> bool:
        if path is None:
            return False
        if path.is_file():
            return True
        return (path / GARMIN_TOKEN_FILENAME).is_file()

    def tokenstore_for(self, telegram_user_id: int) -> Path | None:
        personal = self.personal_tokenstore(telegram_user_id)
        if self._has_tokens(personal):
            return personal
        if telegram_user_id == self._fallback_user_id and self._has_tokens(
            self._fallback_tokenstore
        ):
            return self._fallback_tokenstore
        return None

    def has_integration(self, telegram_user_id: int) -> bool:
        return self.tokenstore_for(telegram_user_id) is not None

    def store_for(
        self, telegram_user_id: int, day_start: time
    ) -> GarminCalorieStore | None:
        tokenstore = self.tokenstore_for(telegram_user_id)
        if tokenstore is None:
            return None
        cache_path = self.personal_cache_path(telegram_user_id)
        if (
            telegram_user_id == self._fallback_user_id
            and tokenstore == self._fallback_tokenstore
            and self._fallback_cache_path is not None
        ):
            cache_path = self._fallback_cache_path
        key = (telegram_user_id, day_start, tokenstore)
        with self._lock:
            store = self._stores.get(key)
            if store is None:
                store = GarminCalorieStore(
                    tokenstore,
                    cache_path,
                    self._timezone,
                    day_start,
                    request_delay_seconds=self._request_delay_seconds,
                )
                self._stores[key] = store
            return store
