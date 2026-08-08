from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from calories_bot.models import FoodAnalysis, FoodItem, LLMMetadata, calculate_meal
from calories_bot.sheets import (
    HEADERS,
    LEGACY_HEADERS,
    DayMeal,
    GoogleSheetsStore,
    SheetSchemaError,
    SheetsReadError,
    SheetsWriteError,
    SheetsWriteUncertainError,
    _date_from_sheet_serial,
    _date_to_sheet_serial,
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
    def __init__(
        self,
        rows,
        fail_after_append=False,
        fail_after_delete=False,
        fail_reads=False,
    ):
        self.rows = rows
        self.appended_row = None
        self.fail_after_append = fail_after_append
        self.fail_after_delete = fail_after_delete
        self.fail_reads = fail_reads
        self.formatted = []
        self.read_count = 0

    def get_all_values(self, **kwargs):
        self.read_count += 1
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

    def insert_cols(self, values, col=1, **kwargs):
        del kwargs
        offset = col - 1
        width = max((len(row) for row in values), default=0)
        for row in self.rows:
            for _ in range(width):
                row.insert(offset, "")
        for row_index, values_row in enumerate(values):
            while len(self.rows) <= row_index:
                self.rows.append([])
            for column_index, value in enumerate(values_row, start=offset):
                while len(self.rows[row_index]) <= column_index:
                    self.rows[row_index].append("")
                self.rows[row_index][column_index] = value

    def batch_update(self, updates, **kwargs):
        del kwargs
        for update in updates:
            range_name = update["range"]
            assert range_name[0] in {"B", "J"}
            row_index = int(range_name[1:]) - 1
            column_index = 1 if range_name.startswith("B") else 9
            while len(self.rows[row_index]) <= column_index:
                self.rows[row_index].append("")
            self.rows[row_index][column_index] = update["values"][0][0]

    def delete_rows(self, start_index, end_index=None):
        del end_index
        self.rows.pop(start_index - 1)
        if self.fail_after_delete:
            raise TimeoutError("response lost")


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
        _date_to_sheet_serial(accounting_date(timestamp, TZ, time(1))),
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


def legacy_stored_row(
    timestamp: datetime,
    message_id: int,
    kcal: int = 60,
    photo_path: str = "",
):
    row = stored_row(timestamp, message_id, kcal, photo_path)
    del row[1]
    return row


def test_exact_header_order() -> None:
    assert HEADERS == [
        "timestamp",
        "day",
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


def test_day_serial_round_trip() -> None:
    day = datetime(2026, 8, 2).date()
    assert _date_from_sheet_serial(_date_to_sheet_serial(day)) == day


def test_daily_total_uses_numeric_timestamp_and_shifted_kyiv_date() -> None:
    rows = [
        HEADERS,
        stored_row(datetime(2026, 8, 2, 0, 30, tzinfo=TZ), 1, 100),
        stored_row(datetime(2026, 8, 2, 1, 0, tzinfo=TZ), 2, 200),
        stored_row(datetime(2026, 8, 2, 23, 30, tzinfo=TZ), 3, 300),
    ]
    state = build_store(rows).get_state(datetime(2026, 8, 2).date(), 999)
    assert state.today_total == 500


def test_day_meals_use_accounting_day_and_preserve_sheet_order() -> None:
    first = stored_row(datetime(2026, 8, 2, 1, 0, tzinfo=TZ), 1, 320)
    first[2] = "вівсянка з бананом"
    second = stored_row(datetime(2026, 8, 2, 23, 30, tzinfo=TZ), 2, 460)
    second[2] = "курка з рисом"
    rows = [
        HEADERS,
        stored_row(datetime(2026, 8, 2, 0, 30, tzinfo=TZ), 3, 100),
        first,
        second,
        stored_row(datetime(2026, 8, 3, 1, 0, tzinfo=TZ), 4, 200),
    ]

    meals = build_store(rows).get_day_meals(datetime(2026, 8, 2).date())

    assert meals == [
        DayMeal(meal_name="вівсянка з бананом", meal_kcal=320),
        DayMeal(meal_name="курка з рисом", meal_kcal=460),
    ]


def test_daily_totals_read_once_and_keep_exact_values() -> None:
    first = stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 1, 100)
    first[4] = 100.4
    second = stored_row(datetime(2026, 8, 2, 13, tzinfo=TZ), 2, 200)
    second[4] = 200.4
    outside = stored_row(datetime(2026, 8, 9, 12, tzinfo=TZ), 3, 999)
    store = build_store([HEADERS, first, second, outside])

    totals = store.get_daily_totals(
        datetime(2026, 8, 2).date(), datetime(2026, 8, 8).date()
    )

    assert set(totals) == {datetime(2026, 8, 2).date()}
    assert totals[datetime(2026, 8, 2).date()] == pytest.approx(300.8)
    assert store._worksheet.read_count == 1


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


def test_reused_message_id_from_previous_day_is_not_a_duplicate() -> None:
    rows = [
        HEADERS,
        stored_row(datetime(2026, 8, 1, 12, tzinfo=TZ), 42, 100),
        stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 43, 200),
    ]

    state = build_store(rows).get_state(datetime(2026, 8, 2).date(), 42)

    assert state.existing is None
    assert state.today_total == 200


def test_same_message_id_on_same_day_is_a_duplicate() -> None:
    rows = [
        HEADERS,
        stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 42, 100),
    ]

    state = build_store(rows).get_state(datetime(2026, 8, 2).date(), 42)

    assert state.existing is not None
    assert state.existing.meal.meal_kcal == 100


def test_malformed_rows_are_skipped_in_total() -> None:
    rows = [
        HEADERS,
        ["bad timestamp", "", "сир", 50, 100, 120, 1],
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
    assert _date_from_sheet_serial(row[1]).isoformat() == "2026-08-02"
    assert row[2:10] == [
        "сир",
        50,
        60,
        120,
        42,
        "сир 50 гр 120 ккал/100г",
        "сир 50г 120#",
        "/srv/photos/42.jpg",
    ]
    assert row[12:16] == ["gpt-test", "none", 100, 20]
    assert row[16] == 0.00123457


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
    assert row[9] == ""
    assert row[14:17] == ["", "", ""]


def test_empty_sheet_gets_headers_and_date_time_format() -> None:
    store = build_store([])
    store._ensure_headers()
    store._format_date_columns()
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
        ),
        (
            "B2:B",
            {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}},
        ),
    ]


@pytest.mark.parametrize("blank_rows", [[[]], [["", ""]], [[], []]])
def test_logically_empty_sheet_gets_headers(blank_rows) -> None:
    store = build_store(blank_rows)
    store._ensure_headers()
    assert store._worksheet.rows[-1] == HEADERS


def test_incompatible_headers_fail() -> None:
    store = build_store([["timestamp", "telegram_message_id"]])
    with pytest.raises(SheetSchemaError):
        store._ensure_headers()


def test_runtime_header_change_is_reported_as_read_error() -> None:
    store = build_store([["wrong", "headers"]])
    with pytest.raises(SheetsReadError):
        store.get_state(datetime(2026, 8, 2).date(), 42)


def test_runtime_header_change_breaks_day_meals_read() -> None:
    store = build_store([["wrong", "headers"]])
    with pytest.raises(SheetsReadError):
        store.get_day_meals(datetime(2026, 8, 2).date())


def test_legacy_schema_is_migrated_and_rows_are_preserved() -> None:
    before_cutoff = legacy_stored_row(datetime(2026, 8, 2, 0, 30, tzinfo=TZ), 42)
    original_values = list(before_cutoff)
    store = build_store([list(LEGACY_HEADERS), before_cutoff])

    store._ensure_headers()

    assert store._worksheet.rows[0] == HEADERS
    migrated = store._worksheet.rows[1]
    assert _date_from_sheet_serial(migrated[1]).isoformat() == "2026-08-01"
    assert migrated[:1] + migrated[2:] == original_values


def test_partially_migrated_schema_backfills_only_blank_days() -> None:
    first = stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 1)
    first[1] = ""
    second = stored_row(datetime(2026, 8, 3, 12, tzinfo=TZ), 2)
    existing_day = second[1]
    store = build_store([HEADERS, first, second])

    store._ensure_headers()

    assert _date_from_sheet_serial(first[1]).isoformat() == "2026-08-02"
    assert second[1] == existing_day


def test_migration_keeps_malformed_timestamp_with_blank_day() -> None:
    row = legacy_stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 1)
    row[0] = "broken"
    store = build_store([list(LEGACY_HEADERS), row])

    store._ensure_headers()

    assert store._worksheet.rows[1][0:2] == ["broken", ""]


def test_delete_removes_row_and_recalculates_original_day() -> None:
    rows = [
        HEADERS,
        stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 41, 100),
        stored_row(datetime(2026, 8, 2, 13, tzinfo=TZ), 42, 60, "/photos/42.jpg"),
        stored_row(datetime(2026, 8, 3, 12, tzinfo=TZ), 43, 200),
    ]
    store = build_store(rows)

    deletion = store.delete_meal(42, datetime(2026, 8, 2).date())

    assert deletion.deleted is True
    assert deletion.accounting_day.isoformat() == "2026-08-02"
    assert deletion.day_total == 100
    assert deletion.photo_path == "/photos/42.jpg"
    assert [store._message_id(row) for row in store._worksheet.rows[1:]] == [41, 43]


def test_delete_uses_day_when_message_id_was_reused() -> None:
    rows = [
        HEADERS,
        stored_row(datetime(2026, 8, 1, 12, tzinfo=TZ), 42, 100),
        stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 42, 200),
    ]
    store = build_store(rows)

    deletion = store.delete_meal(42, datetime(2026, 8, 2).date())

    assert deletion.deleted is True
    assert deletion.accounting_day == datetime(2026, 8, 2).date()
    assert len(store._worksheet.rows) == 2
    assert (
        _date_from_sheet_serial(store._worksheet.rows[1][1])
        == datetime(2026, 8, 1).date()
    )


def test_repeated_delete_is_idempotent_and_uses_fallback_day() -> None:
    day = datetime(2026, 8, 2).date()
    store = build_store(
        [HEADERS, stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 41, 100)]
    )

    deletion = store.delete_meal(999, day)

    assert deletion.deleted is False
    assert deletion.accounting_day == day
    assert deletion.day_total == 100


def test_ambiguous_delete_error_is_verified_as_success() -> None:
    store = build_store([HEADERS, stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 42)])
    store._worksheet.fail_after_delete = True

    deletion = store.delete_meal(42, datetime(2026, 8, 2).date())

    assert deletion.deleted is True
    assert deletion.day_total == 0


def test_exact_delete_failure_is_reported() -> None:
    store = build_store([HEADERS, stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 42)])
    store._worksheet.delete_rows = lambda *args: (_ for _ in ()).throw(
        TimeoutError("not accepted")
    )

    with pytest.raises(SheetsWriteError):
        store.delete_meal(42, datetime(2026, 8, 2).date())


def test_unverifiable_delete_is_uncertain() -> None:
    store = build_store([HEADERS, stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 42)])

    def fail_and_break_reads(*args):
        store._worksheet.fail_reads = True
        raise TimeoutError("unknown")

    store._worksheet.delete_rows = fail_and_break_reads

    with pytest.raises(SheetsWriteUncertainError):
        store.delete_meal(42, datetime(2026, 8, 2).date())


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


def test_failed_append_does_not_return_old_row_with_reused_message_id() -> None:
    old = stored_row(datetime(2026, 8, 1, 12, tzinfo=TZ), 44, 100)
    store = build_store([HEADERS, old])

    def fail_before_append(row, value_input_option="RAW"):
        raise TimeoutError("not accepted")

    store._worksheet.append_row = fail_before_append

    with pytest.raises(SheetsWriteError):
        store.append_meal(
            datetime(2026, 8, 2, 12, tzinfo=TZ),
            44,
            "каша 500 г 150#",
            "каша 500 гр 150 ккал/100г",
            None,
            make_meal(),
            METADATA,
        )


def test_legacy_root_photo_is_moved_to_personal_directory(tmp_path) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    legacy = root / "42.jpg"
    legacy.write_bytes(b"photo")
    row = stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 42, photo_path=str(legacy))
    store = build_store([HEADERS, row])

    store.migrate_legacy_photos(root, 123)

    destination = root / "123" / "42.jpg"
    assert destination.read_bytes() == b"photo"
    assert not legacy.exists()
    assert row[9] == str(destination)


def test_photo_in_another_users_directory_is_not_migrated(tmp_path) -> None:
    root = tmp_path / "photos"
    other = root / "999" / "42.jpg"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"photo")
    row = stored_row(datetime(2026, 8, 2, 12, tzinfo=TZ), 42, photo_path=str(other))
    store = build_store([HEADERS, row])

    store.migrate_legacy_photos(root, 123)

    assert other.exists()
    assert row[9] == str(other)
