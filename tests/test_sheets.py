from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from calories_bot.models import FoodAnalysis, FoodItem, LLMMetadata, calculate_meal
from calories_bot.sheets import (
    HEADERS,
    GoogleSheetsStore,
    SheetSchemaError,
    SheetsReadError,
    SheetsWriteError,
    SheetsWriteUncertainError,
    _datetime_from_sheet_serial,
    _datetime_to_sheet_serial,
    accounting_date,
)

TZ = ZoneInfo("Europe/Kyiv")
METADATA = LLMMetadata(
    model="gpt-test",
    effort="none",
    input_tokens=100,
    output_tokens=20,
    llm_cost_usd=Decimal("0.00123456789"),
)


def make_meal():
    return calculate_meal(
        FoodAnalysis(
            is_food=True,
            meal_name="сир",
            items=[
                FoodItem(
                    name="сир",
                    weight_g=50,
                    weight_estimated=False,
                    kcal_per_100g=120,
                    kcal_estimated=False,
                )
            ],
        )
    )


class FakeWorksheet:
    def __init__(self, rows, fail_after_append=False, fail_reads=False):
        self.rows = rows
        self.appended_row = None
        self.fail_after_append = fail_after_append
        self.fail_reads = fail_reads
        self.formatted = []

    def get_all_values(self, **kwargs):
        if self.fail_reads:
            raise TimeoutError("read failed")
        return self.rows

    def append_row(self, row, value_input_option="RAW"):
        self.appended_row = row
        self.rows.append(list(row))
        if self.fail_after_append:
            raise TimeoutError("response lost")

    def format(self, range_name, cell_format):
        self.formatted.append((range_name, cell_format))


def build_store(rows):
    store = GoogleSheetsStore.__new__(GoogleSheetsStore)
    store._timezone = TZ
    store._day_start_time = time(1)
    store._worksheet = FakeWorksheet(rows)
    return store


def stored_row(
    timestamp: datetime,
    message_id: int,
    kcal: int = 60,
    photo_path: str = "",
):
    return [
        _datetime_to_sheet_serial(timestamp, TZ),
        "сир",
        50,
        kcal,
        120,
        message_id,
        "сир 50 гр 120 ккал/100г",
        "сир 50г 120#",
        photo_path,
        '[{"name":"сир","weight_g":50,"weight_estimated":false,'
        '"kcal_per_100g":120,"kcal_estimated":false,"calories":60}]',
        False,
        "gpt-test",
        "none",
        100,
        20,
        0.00123457,
    ]


def test_exact_header_order() -> None:
    assert HEADERS == [
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


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (datetime(2026, 8, 2, 0, 59, 59, tzinfo=TZ), "2026-08-01"),
        (datetime(2026, 8, 2, 1, 0, 0, tzinfo=TZ), "2026-08-02"),
        (datetime(2026, 3, 29, 0, 30, tzinfo=TZ), "2026-03-28"),
        (datetime(2026, 3, 29, 23, 30, tzinfo=TZ), "2026-03-29"),
    ],
)
def test_accounting_date_cutoff_and_dst(timestamp, expected) -> None:
    assert accounting_date(timestamp, TZ, time(1)).isoformat() == expected


def test_accounting_date_accepts_naive_timestamp() -> None:
    assert accounting_date(datetime(2026, 8, 2, 0, 30), TZ, time(1)).isoformat() == (
        "2026-08-01"
    )


def test_sheet_serial_round_trip_preserves_local_datetime() -> None:
    timestamp = datetime(2026, 8, 2, 12, 34, 56, tzinfo=TZ)
    serial = _datetime_to_sheet_serial(timestamp, TZ)
    restored = _datetime_from_sheet_serial(serial, TZ)
    assert restored == timestamp


def test_daily_total_uses_numeric_timestamp_and_shifted_kyiv_date() -> None:
    rows = [
        HEADERS,
        stored_row(datetime(2026, 8, 2, 0, 30, tzinfo=TZ), 1, 100),
        stored_row(datetime(2026, 8, 2, 1, 0, tzinfo=TZ), 2, 200),
        stored_row(datetime(2026, 8, 2, 23, 30, tzinfo=TZ), 3, 300),
    ]
    state = build_store(rows).get_state(datetime(2026, 8, 2).date(), 999)
    assert state.today_total == 500


def test_duplicate_restores_photo_path_and_metadata() -> None:
    state = build_store(
        [
            HEADERS,
            stored_row(
                datetime(2026, 8, 2, 12, tzinfo=TZ),
                42,
                photo_path="/srv/photos/42.jpg",
            ),
        ]
    ).get_state(datetime(2026, 8, 2).date(), 42)
    assert state.existing is not None
    assert state.existing.normalized_request == "сир 50 гр 120 ккал/100г"
    assert state.existing.photo_path == "/srv/photos/42.jpg"
    assert state.existing.metadata.input_tokens == 100


def test_malformed_rows_are_skipped_in_total() -> None:
    rows = [
        HEADERS,
        ["bad timestamp", "сир", 50, 100, 120, 1],
        stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 2, 100),
    ]
    state = build_store(rows).get_state(datetime(2026, 8, 2).date(), 99)
    assert state.today_total == 100


def test_append_stores_new_schema_native_timestamp_and_photo_path() -> None:
    store = build_store([HEADERS])
    store.append_meal(
        datetime(2026, 8, 2, 12, tzinfo=TZ),
        42,
        "сир 50г 120#",
        "сир 50 гр 120 ккал/100г",
        "/srv/photos/42.jpg",
        make_meal(),
        METADATA,
    )
    row = store._worksheet.rows[1]
    assert len(row) == len(HEADERS)
    assert isinstance(row[0], float)
    assert _datetime_from_sheet_serial(row[0], TZ) == datetime(
        2026, 8, 2, 12, tzinfo=TZ
    )
    assert row[1:9] == [
        "сир",
        50,
        60,
        120,
        42,
        "сир 50 гр 120 ккал/100г",
        "сир 50г 120#",
        "/srv/photos/42.jpg",
    ]
    assert row[11:15] == ["gpt-test", "none", 100, 20]
    assert row[15] == 0.00123457


def test_append_allows_blank_photo_usage_and_cost() -> None:
    store = build_store([HEADERS])
    metadata = LLMMetadata(model="gpt-test", effort="none")
    store.append_meal(
        datetime(2026, 8, 2, 12, tzinfo=TZ),
        43,
        "сир",
        "сир",
        None,
        make_meal(),
        metadata,
    )
    row = store._worksheet.rows[1]
    assert row[8] == ""
    assert row[13:16] == ["", "", ""]


def test_empty_sheet_gets_headers_and_date_time_format() -> None:
    store = build_store([])
    store._ensure_headers()
    store._format_timestamp_column()
    assert store._worksheet.rows == [HEADERS]
    assert store._worksheet.formatted == [
        (
            "A2:A",
            {
                "numberFormat": {
                    "type": "DATE_TIME",
                    "pattern": "yyyy-mm-dd hh:mm:ss",
                }
            },
        )
    ]


@pytest.mark.parametrize("blank_rows", [[[]], [["", ""]], [[], []]])
def test_logically_empty_sheet_gets_headers(blank_rows) -> None:
    store = build_store(blank_rows)
    store._ensure_headers()
    assert store._worksheet.rows[-1] == HEADERS


def test_incompatible_headers_fail_without_migration() -> None:
    store = build_store([["timestamp", "telegram_message_id"]])
    with pytest.raises(SheetSchemaError):
        store._ensure_headers()


def test_runtime_header_change_is_reported_as_read_error() -> None:
    store = build_store([["wrong", "headers"]])
    with pytest.raises(SheetsReadError):
        store.get_state(datetime(2026, 8, 2).date(), 42)


def test_ambiguous_append_error_is_verified_as_success() -> None:
    store = build_store([HEADERS])
    store._worksheet.fail_after_append = True
    saved = store.append_meal(
        datetime(2026, 8, 2, 12, tzinfo=TZ),
        42,
        "сир 50г 120#",
        "сир 50 гр 120 ккал/100г",
        None,
        make_meal(),
        METADATA,
    )
    assert saved.meal.meal_kcal == 60
    assert len(store._worksheet.rows) == 2


def test_unverifiable_append_has_distinct_error() -> None:
    store = build_store([HEADERS])
    store._worksheet.fail_after_append = True
    store._worksheet.get_all_values = lambda **kwargs: (_ for _ in ()).throw(
        TimeoutError("verification failed")
    )
    with pytest.raises(SheetsWriteUncertainError):
        store.append_meal(
            datetime(2026, 8, 2, 12, tzinfo=TZ),
            43,
            "сир",
            "сир",
            None,
            make_meal(),
            METADATA,
        )


def test_exact_append_failure_is_distinct() -> None:
    store = build_store([HEADERS])

    def fail_before_append(row, value_input_option="RAW"):
        raise TimeoutError("not accepted")

    store._worksheet.append_row = fail_before_append
    with pytest.raises(SheetsWriteError):
        store.append_meal(
            datetime(2026, 8, 2, 12, tzinfo=TZ),
            44,
            "сир",
            "сир",
            None,
            make_meal(),
            METADATA,
        )
