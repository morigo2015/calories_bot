from datetime import UTC, date, datetime

import pytest

from calories_bot.burned import (
    HEADERS,
    BodyProfile,
    BurnedCaloriesError,
    GoogleBurnedCalorieStore,
    build_burned_entry,
    calculate_resting_kcal,
)


class Worksheet:
    def __init__(self, rows=None):
        self.rows = rows or []

    def get_all_values(self, **kwargs):
        del kwargs
        return self.rows

    def append_row(self, row, **kwargs):
        del kwargs
        self.rows.append(list(row))

    def update(self, values, range_name=None, **kwargs):
        del kwargs
        row_number = int(range_name.split(":", 1)[0][1:])
        self.rows[row_number - 1] = list(values[0])

    def delete_rows(self, row_number):
        self.rows.pop(row_number - 1)


class Spreadsheet:
    def __init__(self, worksheet):
        self.value = worksheet

    def worksheet(self, name):
        del name
        return self.value


class Client:
    def __init__(self, worksheet):
        self.sheet = Spreadsheet(worksheet)

    def open_by_key(self, spreadsheet_id):
        assert spreadsheet_id == "sheet"
        return self.sheet


def profile(*, sex="male") -> BodyProfile:
    return BodyProfile(
        sex=sex,
        birth_date=date(1990, 5, 15),
        height_cm=180,
        weight_kg=80,
    )


def test_mifflin_st_jeor_uses_age_on_selected_day() -> None:
    assert calculate_resting_kcal(profile(), date(2026, 8, 27)) == 1750
    assert calculate_resting_kcal(profile(sex="female"), date(2026, 8, 27)) == 1584


def test_active_entry_snapshots_profile_and_adds_resting_calories() -> None:
    body = profile()
    entry = build_burned_entry(
        date(2026, 8, 27), "active", 680, datetime(2026, 8, 28, tzinfo=UTC), body
    )

    assert entry.input_kcal == 680
    assert entry.resting_kcal == 1750
    assert entry.effective_total_kcal == 2430
    assert entry.profile_snapshot == body


def test_total_entry_does_not_require_or_store_body_profile() -> None:
    entry = build_burned_entry(
        date(2026, 8, 27), "total", 2500, datetime(2026, 8, 28, tzinfo=UTC)
    )

    assert entry.resting_kcal == 0
    assert entry.effective_total_kcal == 2500
    assert entry.profile_snapshot is None


@pytest.mark.parametrize("value", [0, 20_001])
def test_entry_rejects_values_outside_supported_range(value) -> None:
    with pytest.raises(ValueError):
        build_burned_entry(
            date(2026, 8, 27), "total", value, datetime(2026, 8, 28, tzinfo=UTC)
        )


def test_active_entry_rejects_missing_profile() -> None:
    with pytest.raises(ValueError, match="profile"):
        build_burned_entry(
            date(2026, 8, 27), "active", 500, datetime(2026, 8, 28, tzinfo=UTC)
        )


def test_google_store_upserts_reads_ranges_and_deletes_entries() -> None:
    worksheet = Worksheet([list(HEADERS)])
    store = GoogleBurnedCalorieStore("sheet", client=Client(worksheet))
    first = build_burned_entry(
        date(2026, 8, 26), "total", 2300, datetime(2026, 8, 28, tzinfo=UTC)
    )
    active = build_burned_entry(
        date(2026, 8, 27),
        "active",
        680,
        datetime(2026, 8, 28, tzinfo=UTC),
        profile(),
    )

    assert store.upsert(first) == first
    assert store.upsert(active) == active
    assert store.get(date(2026, 8, 27)) == active
    assert store.get_range(date(2026, 8, 27), date(2026, 8, 28)) == {active.day: active}

    replacement = build_burned_entry(
        first.day, "total", 2500, datetime(2026, 8, 29, tzinfo=UTC)
    )
    assert store.upsert(replacement) == replacement
    assert store.get(first.day) == replacement
    assert store.delete(first.day) is True
    assert store.delete(first.day) is False


def test_google_store_initializes_empty_worksheet() -> None:
    worksheet = Worksheet()

    GoogleBurnedCalorieStore("sheet", client=Client(worksheet))

    assert worksheet.rows == [HEADERS]


def test_google_store_rejects_incompatible_or_duplicate_rows() -> None:
    with pytest.raises(BurnedCaloriesError, match="headers"):
        GoogleBurnedCalorieStore("sheet", client=Client(Worksheet([["bad"]])))

    entry = build_burned_entry(
        date(2026, 8, 27), "total", 2500, datetime(2026, 8, 28, tzinfo=UTC)
    )
    worksheet = Worksheet([list(HEADERS)])
    store = GoogleBurnedCalorieStore("sheet", client=Client(worksheet))
    values = store._values(entry)
    worksheet.rows.extend([values, values])
    with pytest.raises(BurnedCaloriesError, match="Duplicate"):
        store.get(entry.day)
