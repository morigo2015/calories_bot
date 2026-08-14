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


def test_refreshes_seven_completed_days_once_and_formats_cache(monkeypatch, tmp_path):
    FakeGarmin.instances.clear()
    monkeypatch.setattr(garmin_module, "Garmin", FakeGarmin)
    store = build_store(tmp_path)
    now = datetime(2026, 8, 14, 1, 0, tzinfo=TZ)

    assert store.refresh_if_due(now) is True
    assert store.refresh_if_due(datetime(2026, 8, 14, 20, 0, tzinfo=TZ)) is False

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
    assert (tmp_path / "garmin-calories.json").stat().st_mode & 0o777 == 0o600


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
