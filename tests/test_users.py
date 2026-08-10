from datetime import time

import pytest

from calories_bot.users import (
    LEGACY_USER_HEADERS,
    USER_HEADERS,
    GoogleUserRegistry,
    InviteUnavailableError,
    UserAlreadyRegisteredError,
    UserRegistryError,
    parse_daily_kcal_goal,
    parse_day_start,
)


class FakeWorksheet:
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
        if range_name == "H1:H1":
            self.rows[0].append(values[0][0])
            return
        row_number = int(range_name.split(":", maxsplit=1)[0][1:])
        self.rows[row_number - 1] = list(values[0])

    def delete_rows(self, row_number):
        self.rows.pop(row_number - 1)


def build_registry(rows=None):
    registry = GoogleUserRegistry.__new__(GoogleUserRegistry)
    import threading

    registry._lock = threading.RLock()
    registry._worksheet = FakeWorksheet(rows if rows is not None else [USER_HEADERS])
    return registry


def test_empty_registry_gets_exact_headers() -> None:
    registry = build_registry([])
    registry._ensure_headers()
    assert registry._worksheet.rows == [USER_HEADERS]


def test_incompatible_registry_headers_fail() -> None:
    registry = build_registry([["telegram_user_id", "status"]])
    with pytest.raises(UserRegistryError, match="headers"):
        registry._ensure_headers()


def test_known_legacy_registry_header_is_migrated_without_changing_rows() -> None:
    row = [123, "A", "a", "active", "", "sheet", "01:00"]
    registry = build_registry([list(LEGACY_USER_HEADERS), list(row)])

    registry._ensure_headers()

    assert registry._worksheet.rows == [USER_HEADERS, row]
    assert registry.get_user(123).daily_kcal_goal is None


def test_invalid_day_start_and_malformed_rows_fail() -> None:
    with pytest.raises(UserRegistryError, match="day_start"):
        parse_day_start("1:00")

    invalid_id = build_registry(
        [USER_HEADERS, ["abc", "A", "", "active", "", "sheet", "01:00"]]
    )
    with pytest.raises(UserRegistryError, match="telegram_user_id"):
        invalid_id.get_user(123)

    invalid_status = build_registry(
        [USER_HEADERS, [123, "A", "", "unknown", "", "sheet", "01:00"]]
    )
    with pytest.raises(UserRegistryError, match="status"):
        invalid_status.get_user(123)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", time(0)),
        ("0.041666666666666664", time(1)),
        (0.125, time(3)),
    ],
)
def test_google_sheets_native_time_value_is_accepted(value, expected) -> None:
    assert parse_day_start(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "20001", "1.5", "two"])
def test_invalid_daily_goal_is_rejected(value) -> None:
    with pytest.raises(UserRegistryError, match="daily_kcal_goal"):
        parse_daily_kcal_goal(value)


def test_daily_goal_is_read_written_disabled_and_verified() -> None:
    registry = build_registry(
        [USER_HEADERS, [123, "A", "", "active", "", "sheet", "01:00", ""]]
    )

    enabled = registry.set_daily_kcal_goal(123, 2000)
    disabled = registry.set_daily_kcal_goal(123, None)

    assert enabled.daily_kcal_goal == 2000
    assert disabled.daily_kcal_goal is None


def test_invite_activation_is_one_time_and_retains_personal_context() -> None:
    registry = build_registry()
    invite = registry.create_invite("Вася", "token", time(3))

    prepared = registry.prepare_activation(invite, 123, "vasya", "sheet-123")
    active = registry.complete_activation(prepared)

    assert active.telegram_user_id == 123
    assert active.status == "active"
    assert active.invite_token == ""
    assert active.spreadsheet_id == "sheet-123"
    assert active.day_start == time(3)
    assert registry.get_invite("token") is None
    assert registry.get_user(123) == active


def test_partial_activation_can_reuse_saved_spreadsheet() -> None:
    registry = build_registry()
    invite = registry.create_invite("Вася", "token", time(1))
    prepared = registry.prepare_activation(invite, 123, "vasya", "sheet-123")

    loaded = registry.get_invite("token")

    assert loaded == prepared
    assert loaded.spreadsheet_id == "sheet-123"
    assert loaded.status == "invited"


def test_invite_cannot_activate_another_or_already_registered_user() -> None:
    claimed = build_registry(
        [USER_HEADERS, [123, "A", "", "invited", "token", "sheet", "01:00"]]
    )
    invite = claimed.get_invite("token")
    with pytest.raises(InviteUnavailableError, match="another"):
        claimed.prepare_activation(invite, 999, "other", "sheet")

    rows = [
        USER_HEADERS,
        ["", "A", "", "invited", "token", "", "01:00"],
        [999, "B", "", "active", "", "other-sheet", "01:00"],
    ]
    registered = build_registry(rows)
    with pytest.raises(UserAlreadyRegisteredError, match="registered"):
        registered.prepare_activation(
            registered.get_invite("token"), 999, "other", "sheet"
        )


def test_activation_requires_saved_user_and_spreadsheet() -> None:
    registry = build_registry(
        [USER_HEADERS, ["", "A", "", "invited", "token", "", "01:00"]]
    )
    invite = registry.get_invite("token")

    with pytest.raises(UserRegistryError, match="incomplete"):
        registry.complete_activation(invite)


def test_duplicate_retained_user_is_rejected() -> None:
    row = [123, "A", "a", "active", "", "sheet-a", "01:00"]
    other = [123, "B", "b", "blocked", "", "sheet-b", "02:00"]
    registry = build_registry([USER_HEADERS, row, other])

    with pytest.raises(UserRegistryError, match="Duplicate"):
        registry.get_user(123)


def test_list_users_returns_invited_active_and_blocked_rows() -> None:
    rows = [
        USER_HEADERS,
        ["", "Invite", "", "invited", "token", "", "01:00"],
        [123, "Active", "active", "active", "", "sheet-a", "01:00"],
        [456, "Blocked", "blocked", "blocked", "", "sheet-b", "02:00"],
    ]
    users = build_registry(rows).list_users()

    assert [user.status for user in users] == ["invited", "active", "blocked"]


def test_block_unblock_preserves_spreadsheet_and_delete_removes_row() -> None:
    row = [123, "A", "a", "active", "", "sheet-a", "01:00"]
    registry = build_registry([USER_HEADERS, row])

    blocked = registry.set_status(123, "blocked")
    active = registry.set_status(123, "active")
    registry.delete_user(123)

    assert blocked.spreadsheet_id == active.spreadsheet_id == "sheet-a"
    assert registry.get_user(123) is None


def test_status_and_invite_input_validation() -> None:
    registry = build_registry()

    with pytest.raises(ValueError, match="display_name"):
        registry.create_invite(" ", "token", time(1))
    with pytest.raises(ValueError, match="display_name"):
        registry.create_invite("x" * 101, "token", time(1))
    with pytest.raises(ValueError, match="statuses"):
        registry.set_status(123, "invited")
    with pytest.raises(UserRegistryError, match="not found"):
        registry.set_status(123, "blocked")

    registry.delete_user(123)
