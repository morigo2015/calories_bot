import json
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from calories_bot import garmin as garmin_module
from calories_bot.garmin import GarminCacheError, GarminCalorieStore, GarminDataError

TZ = ZoneInfo("Europe/Kyiv")


class FakeGarmin:
    instances = []

    def __init__(self, retry_attempts):
        self.retry_attempts = retry_attempts
        self.login_path = None
        self.requested_days = []
        self.__class__.instances.append(self)

    def login(self, path):
        self.login_path = path

    def get_user_summary(self, day):
        self.requested_days.append(day)
        return {
            "calendarDate": day,
            "totalKilocalories": 2000 + len(self.requested_days),
        }


def build_store(tmp_path):
    return GarminCalorieStore(
        tmp_path / "tokens",
        tmp_path / "garmin-calories.json",
        TZ,
        time(1),
    )


def test_refreshes_seven_completed_days_and_formats_cache(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)
    now = datetime(2026, 8, 14, 1, 0, tzinfo=TZ)

    assert store.refresh_if_due(now) is True
    assert store.refresh_if_due(datetime(2026, 8, 14, 1, 30, tzinfo=TZ)) is False

    assert len(FakeGarmin.instances) == 1
    assert FakeGarmin.instances[0].requested_days == [
        "2026-08-07",
        "2026-08-08",
        "2026-08-09",
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
    ]
    report = store.format_weekly_report()
    assert report.startswith("🔥 Витрата калорій за останні 7 днів (Garmin):")
    assert "• 07.08, пт — 2 001 ккал" in report
    assert "• 13.08, чт — 2 007 ккал" in report
    assert "Разом: 14 028 ккал" in report
    assert "У середньому: 2 004 ккал/день" in report
    assert "Оновлено: 14.08.2026 01:00" in report
    daily = store.get_daily_calories()
    assert daily[datetime(2026, 8, 7).date()] == 2001
    assert daily[datetime(2026, 8, 13).date()] == 2007
    assert (tmp_path / "garmin-calories.json").stat().st_mode & 0o777 == 0o600


def test_rechecks_latest_completed_day_hourly(monkeypatch, tmp_path):
    class UpdatingGarmin(FakeGarmin):
        latest_values = iter((1776, 2732))

        def get_user_summary(self, day):
            self.requested_days.append(day)
            total = next(self.latest_values) if day == "2026-08-13" else 2000
            return {"calendarDate": day, "totalKilocalories": total}

    UpdatingGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", UpdatingGarmin)
    store = build_store(tmp_path)

    assert store.refresh_if_due(datetime(2026, 8, 14, 1, 0, tzinfo=TZ)) is True
    assert store.get_daily_calories()[datetime(2026, 8, 13).date()] == 1776
    assert store.refresh_if_due(datetime(2026, 8, 14, 1, 59, tzinfo=TZ)) is False
    assert store.refresh_if_due(datetime(2026, 8, 14, 2, 0, tzinfo=TZ)) is True

    assert len(UpdatingGarmin.instances) == 2
    assert UpdatingGarmin.instances[1].requested_days == ["2026-08-13"]
    assert store.get_daily_calories()[datetime(2026, 8, 13).date()] == 2732
    assert "Оновлено: 14.08.2026 02:00" in store.format_weekly_report()


def test_before_cutoff_uses_previous_accounting_day(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)

    store.refresh_if_due(datetime(2026, 8, 14, 0, 30, tzinfo=TZ))

    assert FakeGarmin.instances[0].requested_days[-1] == "2026-08-12"


def test_failed_refresh_preserves_previous_snapshot(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)
    store.refresh_if_due(datetime(2026, 8, 14, 1, tzinfo=TZ))
    cache_path = tmp_path / "garmin-calories.json"
    original = cache_path.read_text(encoding="utf-8")

    class BrokenGarmin(FakeGarmin):
        def get_user_summary(self, day):
            return {"calendarDate": day}

    monkeypatch.setattr(garmin_module, "Garmin", BrokenGarmin)
    with pytest.raises(GarminDataError):
        store.refresh_if_due(datetime(2026, 8, 15, 1, tzinfo=TZ))

    assert cache_path.read_text(encoding="utf-8") == original


def test_failed_latest_day_recheck_preserves_previous_snapshot(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)
    store.refresh_if_due(datetime(2026, 8, 14, 1, tzinfo=TZ))
    cache_path = tmp_path / "garmin-calories.json"
    original = cache_path.read_text(encoding="utf-8")

    class BrokenGarmin(FakeGarmin):
        def get_user_summary(self, day):
            return {"calendarDate": day}

    monkeypatch.setattr(garmin_module, "Garmin", BrokenGarmin)
    with pytest.raises(GarminDataError):
        store.refresh_if_due(datetime(2026, 8, 14, 2, tzinfo=TZ))

    assert cache_path.read_text(encoding="utf-8") == original


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
