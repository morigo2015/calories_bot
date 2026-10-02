import json
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from calories_bot import garmin as garmin_module
from calories_bot.garmin import (
    GARMIN_CACHE_DAYS,
    GarminCacheError,
    GarminCalorieStore,
    GarminDataError,
    GarminStoreProvider,
)

TZ = ZoneInfo("Europe/Kyiv")


class FakeGarmin:
    instances = []

    def __init__(self, retry_attempts):
        self.retry_attempts = retry_attempts
        self.login_path = None
        self.requested_days = []
        self.requested_weight_ranges = []
        self.__class__.instances.append(self)

    def login(self, path):
        self.login_path = path

    def get_user_summary(self, day):
        self.requested_days.append(day)
        return {
            "calendarDate": day,
            "totalKilocalories": 2000 + len(self.requested_days),
        }

    def get_body_composition(self, start_day, end_day):
        self.requested_weight_ranges.append((start_day, end_day))
        return {"dateWeightList": []}


def build_store(tmp_path):
    return GarminCalorieStore(
        tmp_path / "tokens",
        tmp_path / "garmin-calories.json",
        TZ,
        time(1),
        request_delay_seconds=0,
    )


def test_refreshes_ninety_days_and_formats_latest_week(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)
    now = datetime(2026, 8, 14, 1, 0, tzinfo=TZ)

    assert store.refresh_if_due(now) is True
    assert store.refresh_if_due(datetime(2026, 8, 14, 1, 30, tzinfo=TZ)) is False

    assert len(FakeGarmin.instances) == 1
    assert len(FakeGarmin.instances[0].requested_days) == GARMIN_CACHE_DAYS
    assert FakeGarmin.instances[0].requested_days[0] == "2026-08-13"
    assert FakeGarmin.instances[0].requested_days[-1] == "2026-05-16"
    assert FakeGarmin.instances[0].requested_weight_ranges == [
        ("2000-01-01", "2026-08-13")
    ]
    report = store.format_weekly_report()
    assert report.startswith("🔥 Витрата калорій за останні 7 днів (Garmin):")
    assert "• 07.08, пт — 2 007 ккал" in report
    assert "• 13.08, чт — 2 001 ккал" in report
    assert "Разом: 14 028 ккал" in report
    assert "У середньому: 2 004 ккал/день" in report
    assert "Оновлено: 14.08.2026 01:00" in report
    daily = store.get_daily_calories()
    assert len(daily) == GARMIN_CACHE_DAYS
    assert daily[datetime(2026, 5, 16).date()] == 2090
    assert daily[datetime(2026, 8, 13).date()] == 2001
    assert (tmp_path / "garmin-calories.json").stat().st_mode & 0o777 == 0o600


def test_upgrades_legacy_week_cache_by_fetching_only_missing_days(
    monkeypatch, tmp_path
):
    cache_path = tmp_path / "garmin-calories.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "refresh_day": "2026-08-14",
                "refreshed_at": "2026-08-14T01:00:00+03:00",
                "days": [
                    {"day": f"2026-08-{day:02d}", "total_kcal": 1900 + day}
                    for day in range(7, 14)
                ],
            }
        ),
        encoding="utf-8",
    )
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)

    assert store.refresh_if_due(datetime(2026, 8, 14, 1, 30, tzinfo=TZ)) is True

    assert len(FakeGarmin.instances) == 1
    assert len(FakeGarmin.instances[0].requested_days) == 83
    assert FakeGarmin.instances[0].requested_days[0] == "2026-08-06"
    assert FakeGarmin.instances[0].requested_days[-1] == "2026-05-16"
    assert store.get_daily_calories()[datetime(2026, 8, 13).date()] == 1913
    assert json.loads(cache_path.read_text(encoding="utf-8"))["schema_version"] == 4


def test_next_day_refreshes_new_and_recent_days(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)
    store.refresh_if_due(datetime(2026, 8, 14, 1, tzinfo=TZ))
    FakeGarmin.instances.clear()

    assert store.refresh_if_due(datetime(2026, 8, 15, 1, tzinfo=TZ)) is True

    assert len(FakeGarmin.instances) == 1
    assert FakeGarmin.instances[0].requested_days == [
        "2026-08-14",
        "2026-08-13",
        "2026-08-12",
    ]
    assert FakeGarmin.instances[0].requested_weight_ranges == [
        ("2026-08-14", "2026-08-14")
    ]
    assert len(store.get_daily_calories()) == GARMIN_CACHE_DAYS


def test_imports_all_weights_averages_each_day_and_fills_gaps(monkeypatch, tmp_path):
    class WeightGarmin(FakeGarmin):
        def get_body_composition(self, start_day, end_day):
            self.requested_weight_ranges.append((start_day, end_day))
            return {
                "dateWeightList": [
                    {"calendarDate": "2026-08-09", "weight": 80_000},
                    {"calendarDate": "2026-08-09", "weight": 81_000},
                    {"calendarDate": "2026-08-12", "weight": 79_500},
                ]
            }

    WeightGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", WeightGarmin)
    store = build_store(tmp_path)

    store.refresh_if_due(datetime(2026, 8, 14, 1, tzinfo=TZ))

    assert WeightGarmin.instances[0].requested_weight_ranges == [
        ("2000-01-01", "2026-08-13")
    ]
    assert store.get_daily_weights() == {
        datetime(2026, 8, day).date(): weight
        for day, weight in (
            (9, 80.5),
            (10, 80.5),
            (11, 80.5),
            (12, 79.5),
            (13, 79.5),
        )
    }
    assert store.get_recorded_daily_weights() == {
        datetime(2026, 8, 9).date(): 80.5,
        datetime(2026, 8, 12).date(): 79.5,
    }


def test_does_not_poll_garmin_again_during_same_accounting_day(monkeypatch, tmp_path):
    class UpdatingGarmin(FakeGarmin):
        def get_user_summary(self, day):
            self.requested_days.append(day)
            total = 1776 if day == "2026-08-13" else 2000
            return {"calendarDate": day, "totalKilocalories": total}

    UpdatingGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", UpdatingGarmin)
    store = build_store(tmp_path)

    assert store.refresh_if_due(datetime(2026, 8, 14, 1, 0, tzinfo=TZ)) is True
    assert store.get_daily_calories()[datetime(2026, 8, 13).date()] == 1776
    assert store.refresh_if_due(datetime(2026, 8, 14, 1, 59, tzinfo=TZ)) is False
    assert store.refresh_if_due(datetime(2026, 8, 14, 2, 0, tzinfo=TZ)) is False

    assert len(UpdatingGarmin.instances) == 1
    assert store.get_daily_calories()[datetime(2026, 8, 13).date()] == 1776
    assert "Оновлено: 14.08.2026 01:00" in store.format_weekly_report()


def test_force_refreshes_only_three_recent_days(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)
    store.refresh_if_due(datetime(2026, 8, 14, 1, tzinfo=TZ))
    FakeGarmin.instances.clear()

    assert store.refresh_if_due(datetime(2026, 8, 14, 2, tzinfo=TZ), force=True) is True

    assert FakeGarmin.instances[0].requested_days == [
        "2026-08-13",
        "2026-08-12",
        "2026-08-11",
    ]
    assert FakeGarmin.instances[0].requested_weight_ranges == []


def test_before_cutoff_uses_previous_accounting_day(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)

    store.refresh_if_due(datetime(2026, 8, 14, 0, 30, tzinfo=TZ))

    assert FakeGarmin.instances[0].requested_days[0] == "2026-08-12"


def test_failed_refresh_checkpoints_and_resumes_backfill(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)
    store.refresh_if_due(datetime(2026, 8, 14, 1, tzinfo=TZ))
    cache_path = tmp_path / "garmin-calories.json"

    class BrokenGarmin(FakeGarmin):
        def get_user_summary(self, day):
            return {"calendarDate": day}

    monkeypatch.setattr(garmin_module, "Garmin", BrokenGarmin)
    with pytest.raises(GarminDataError):
        store.refresh_if_due(datetime(2026, 8, 15, 1, tzinfo=TZ))

    partial = json.loads(cache_path.read_text(encoding="utf-8"))
    assert partial["refresh_day"] == "2026-08-15"
    assert len(partial["days"]) == GARMIN_CACHE_DAYS - 1

    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    FakeGarmin.instances.clear()
    assert store.refresh_if_due(datetime(2026, 8, 15, 2, tzinfo=TZ)) is True

    assert FakeGarmin.instances[0].requested_days == ["2026-08-14"]
    assert len(store.get_daily_calories()) == GARMIN_CACHE_DAYS


def test_provider_resolves_personal_store_for_non_admin(tmp_path):
    tokenstore = tmp_path / "42" / "tokens"
    tokenstore.mkdir(parents=True)
    (tokenstore / "garmin_tokens.json").write_text("{}", encoding="utf-8")
    provider = GarminStoreProvider(tmp_path, TZ, fallback_user_id=1)

    store = provider.store_for(42, time(3))

    assert store is not None
    assert provider.has_integration(42) is True
    assert provider.store_for(43, time(3)) is None


def test_rejects_invalid_cache(tmp_path):
    cache_path = tmp_path / "garmin-calories.json"
    cache_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    store = build_store(tmp_path)

    with pytest.raises(GarminCacheError):
        store.format_weekly_report()


@pytest.mark.parametrize("value", [None, True, -1, "2000"])
def test_rejects_invalid_total_calories(value):
    with pytest.raises(GarminDataError):
        GarminCalorieStore._parse_total_kcal(
            {"totalKilocalories": value}, SimpleNamespace()
        )
