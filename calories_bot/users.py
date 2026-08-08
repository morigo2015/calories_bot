from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Protocol

import gspread
from gspread.utils import ValueInputOption, ValueRenderOption

LEGACY_USER_HEADERS = [
    "telegram_user_id",
    "display_name",
    "telegram_username",
    "status",
    "invite_token",
    "spreadsheet_id",
    "day_start",
]
USER_HEADERS = [*LEGACY_USER_HEADERS, "daily_kcal_goal"]

USER_ID_COLUMN = 0
DISPLAY_NAME_COLUMN = 1
USERNAME_COLUMN = 2
STATUS_COLUMN = 3
INVITE_TOKEN_COLUMN = 4
SPREADSHEET_ID_COLUMN = 5
DAY_START_COLUMN = 6
DAILY_KCAL_GOAL_COLUMN = 7

MIN_DAILY_KCAL_GOAL = 1
MAX_DAILY_KCAL_GOAL = 20_000

VALID_STATUSES = {"invited", "active", "blocked"}
_DAY_START_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


class UserRegistryError(RuntimeError):
    """Raised when the user registry cannot be read or updated safely."""


class InviteUnavailableError(UserRegistryError):
    """Raised when an invite does not exist or can no longer be activated."""


class UserAlreadyRegisteredError(UserRegistryError):
    """Raised when a Telegram user already has a retained account."""


@dataclass(frozen=True)
class UserRecord:
    row_number: int
    telegram_user_id: int | None
    display_name: str
    telegram_username: str
    status: str
    invite_token: str
    spreadsheet_id: str
    day_start: time
    daily_kcal_goal: int | None = None


class UserRegistry(Protocol):
    def get_user(self, telegram_user_id: int) -> UserRecord | None: ...

    def list_users(self) -> list[UserRecord]: ...

    def get_invite(self, token: str) -> UserRecord | None: ...

    def create_invite(
        self, display_name: str, token: str, day_start: time
    ) -> UserRecord: ...

    def prepare_activation(
        self,
        invite: UserRecord,
        telegram_user_id: int,
        telegram_username: str,
        spreadsheet_id: str,
    ) -> UserRecord: ...

    def complete_activation(self, invite: UserRecord) -> UserRecord: ...

    def set_status(self, telegram_user_id: int, status: str) -> UserRecord: ...

    def set_daily_kcal_goal(
        self, telegram_user_id: int, goal: int | None
    ) -> UserRecord: ...

    def delete_user(self, telegram_user_id: int) -> None: ...


def parse_day_start(value: object) -> time:
    text = str(value).strip()
    if _DAY_START_RE.fullmatch(text):
        return time.fromisoformat(text)
    # Google Sheets returns a time-only cell as a fraction of a day when data
    # is requested with ValueRenderOption.unformatted: 01:00 becomes
    # 0.041666666666666664. Accept exact minute values in that native form too.
    try:
        day_fraction = Decimal(text)
    except InvalidOperation as exc:
        raise UserRegistryError(
            f"Invalid day_start in user registry: {text!r}"
        ) from exc
    if not day_fraction.is_finite():
        raise UserRegistryError(f"Invalid day_start in user registry: {text!r}")
    seconds = int(
        (day_fraction * Decimal(24 * 60 * 60)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    if not 0 <= seconds < 24 * 60 * 60 or seconds % 60:
        raise UserRegistryError(f"Invalid day_start in user registry: {text!r}")
    return time(hour=seconds // 3600, minute=(seconds % 3600) // 60)


def parse_daily_kcal_goal(value: object) -> int | None:
    text = str(value).strip()
    if not text:
        return None
    if not text.isascii() or not text.isdecimal():
        raise UserRegistryError(f"Invalid daily_kcal_goal in user registry: {text!r}")
    goal = int(text)
    if not MIN_DAILY_KCAL_GOAL <= goal <= MAX_DAILY_KCAL_GOAL:
        raise UserRegistryError(f"Invalid daily_kcal_goal in user registry: {text!r}")
    return goal


class GoogleUserRegistry:
    def __init__(
        self,
        client: gspread.Client,
        spreadsheet_id: str,
        worksheet_name: str,
    ) -> None:
        self._lock = threading.RLock()
        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            self._worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            self._worksheet = spreadsheet.add_worksheet(
                title=worksheet_name, rows=100, cols=len(USER_HEADERS)
            )
        self._ensure_headers()

    def _ensure_headers(self) -> None:
        rows = self._worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted
        )
        if not rows or not any(str(cell).strip() for row in rows for cell in row):
            self._worksheet.append_row(
                USER_HEADERS, value_input_option=ValueInputOption.raw
            )
            return
        if rows[0] == LEGACY_USER_HEADERS:
            try:
                self._worksheet.update(
                    values=[["daily_kcal_goal"]],
                    range_name="H1:H1",
                    raw=True,
                )
            except Exception as exc:
                raise UserRegistryError(
                    "Could not migrate the user registry headers"
                ) from exc
            rows = self._worksheet.get_all_values(
                value_render_option=ValueRenderOption.unformatted
            )
        if rows[0] != USER_HEADERS:
            raise UserRegistryError(
                "User registry headers are incompatible. Expected: "
                + ", ".join(USER_HEADERS)
            )

    def _rows(self) -> list[list[object]]:
        try:
            rows = self._worksheet.get_all_values(
                value_render_option=ValueRenderOption.unformatted
            )
        except Exception as exc:
            raise UserRegistryError("Could not read the user registry") from exc
        if not rows or rows[0] != USER_HEADERS:
            raise UserRegistryError("User registry headers changed while running")
        return rows[1:]

    @staticmethod
    def _record(row_number: int, row: list[object]) -> UserRecord:
        padded = row + [""] * (len(USER_HEADERS) - len(row))
        user_id_text = str(padded[USER_ID_COLUMN]).strip()
        try:
            user_id = int(user_id_text) if user_id_text else None
        except ValueError as exc:
            raise UserRegistryError(
                f"Invalid telegram_user_id on registry row {row_number}"
            ) from exc
        status = str(padded[STATUS_COLUMN]).strip()
        if status not in VALID_STATUSES:
            raise UserRegistryError(
                f"Invalid status on registry row {row_number}: {status!r}"
            )
        return UserRecord(
            row_number=row_number,
            telegram_user_id=user_id,
            display_name=str(padded[DISPLAY_NAME_COLUMN]).strip(),
            telegram_username=str(padded[USERNAME_COLUMN]).strip(),
            status=status,
            invite_token=str(padded[INVITE_TOKEN_COLUMN]).strip(),
            spreadsheet_id=str(padded[SPREADSHEET_ID_COLUMN]).strip(),
            day_start=parse_day_start(padded[DAY_START_COLUMN]),
            daily_kcal_goal=parse_daily_kcal_goal(padded[DAILY_KCAL_GOAL_COLUMN]),
        )

    def _records(self) -> list[UserRecord]:
        return [self._record(index, row) for index, row in enumerate(self._rows(), 2)]

    def get_user(self, telegram_user_id: int) -> UserRecord | None:
        with self._lock:
            matches = [
                record
                for record in self._records()
                if record.telegram_user_id == telegram_user_id
                and record.status in {"active", "blocked"}
            ]
            if len(matches) > 1:
                raise UserRegistryError(
                    f"Duplicate retained user in registry: {telegram_user_id}"
                )
            return matches[0] if matches else None

    def list_users(self) -> list[UserRecord]:
        with self._lock:
            return self._records()

    def get_invite(self, token: str) -> UserRecord | None:
        if not token:
            return None
        with self._lock:
            matches = [
                record
                for record in self._records()
                if record.status == "invited" and record.invite_token == token
            ]
            if len(matches) > 1:
                raise UserRegistryError("Duplicate invite token in registry")
            return matches[0] if matches else None

    def create_invite(
        self, display_name: str, token: str, day_start: time
    ) -> UserRecord:
        name = display_name.strip()
        if not name:
            raise ValueError("display_name cannot be empty")
        with self._lock:
            if any(record.invite_token == token for record in self._records()):
                raise UserRegistryError("Invite token collision")
            row: list[str] = [
                "",
                name,
                "",
                "invited",
                token,
                "",
                day_start.isoformat(timespec="minutes"),
                "",
            ]
            try:
                self._worksheet.append_row(row, value_input_option=ValueInputOption.raw)
            except Exception as exc:
                raise UserRegistryError("Could not create invite") from exc
            record = self.get_invite(token)
            if record is None:
                raise UserRegistryError("Could not verify the new invite")
            return record

    def _update_row(self, row_number: int, values: list[object]) -> UserRecord:
        try:
            self._worksheet.update(
                values=[values],
                range_name=f"A{row_number}:H{row_number}",
                raw=True,
            )
        except Exception as exc:
            raise UserRegistryError("Could not update the user registry") from exc
        for record in self._records():
            if record.row_number == row_number:
                return record
        raise UserRegistryError("Could not verify the user registry update")

    def prepare_activation(
        self,
        invite: UserRecord,
        telegram_user_id: int,
        telegram_username: str,
        spreadsheet_id: str,
    ) -> UserRecord:
        with self._lock:
            current = self.get_invite(invite.invite_token)
            if current is None or current.row_number != invite.row_number:
                raise InviteUnavailableError("Invite is no longer available")
            existing = self.get_user(telegram_user_id)
            if existing is not None:
                raise UserAlreadyRegisteredError("Telegram user is already registered")
            if (
                current.telegram_user_id is not None
                and current.telegram_user_id != telegram_user_id
            ):
                raise InviteUnavailableError("Invite belongs to another Telegram user")
            if current.spreadsheet_id and current.spreadsheet_id != spreadsheet_id:
                raise UserRegistryError("Invite already references another spreadsheet")
            return self._update_row(
                current.row_number,
                [
                    telegram_user_id,
                    current.display_name,
                    telegram_username,
                    "invited",
                    current.invite_token,
                    spreadsheet_id,
                    current.day_start.isoformat(timespec="minutes"),
                    current.daily_kcal_goal or "",
                ],
            )

    def complete_activation(self, invite: UserRecord) -> UserRecord:
        with self._lock:
            current = next(
                (
                    record
                    for record in self._records()
                    if record.row_number == invite.row_number
                ),
                None,
            )
            if current is None or current.status != "invited":
                raise InviteUnavailableError("Invite is no longer available")
            if current.telegram_user_id is None or not current.spreadsheet_id:
                raise UserRegistryError("Invite activation is incomplete")
            return self._update_row(
                current.row_number,
                [
                    current.telegram_user_id,
                    current.display_name,
                    current.telegram_username,
                    "active",
                    "",
                    current.spreadsheet_id,
                    current.day_start.isoformat(timespec="minutes"),
                    current.daily_kcal_goal or "",
                ],
            )

    def set_status(self, telegram_user_id: int, status: str) -> UserRecord:
        if status not in {"active", "blocked"}:
            raise ValueError("Only active and blocked are retained user statuses")
        with self._lock:
            current = self.get_user(telegram_user_id)
            if current is None:
                raise UserRegistryError("User not found")
            return self._update_row(
                current.row_number,
                [
                    telegram_user_id,
                    current.display_name,
                    current.telegram_username,
                    status,
                    "",
                    current.spreadsheet_id,
                    current.day_start.isoformat(timespec="minutes"),
                    current.daily_kcal_goal or "",
                ],
            )

    def set_daily_kcal_goal(
        self, telegram_user_id: int, goal: int | None
    ) -> UserRecord:
        if goal is not None and not MIN_DAILY_KCAL_GOAL <= goal <= MAX_DAILY_KCAL_GOAL:
            raise ValueError(
                f"daily kcal goal must be between {MIN_DAILY_KCAL_GOAL} "
                f"and {MAX_DAILY_KCAL_GOAL}"
            )
        with self._lock:
            current = self.get_user(telegram_user_id)
            if current is None or current.status != "active":
                raise UserRegistryError("Active user not found")
            updated = self._update_row(
                current.row_number,
                [
                    current.telegram_user_id,
                    current.display_name,
                    current.telegram_username,
                    current.status,
                    current.invite_token,
                    current.spreadsheet_id,
                    current.day_start.isoformat(timespec="minutes"),
                    goal or "",
                ],
            )
            if updated.daily_kcal_goal != goal:
                raise UserRegistryError("Could not verify daily kcal goal update")
            return updated

    def delete_user(self, telegram_user_id: int) -> None:
        with self._lock:
            current = self.get_user(telegram_user_id)
            if current is None:
                return
            try:
                self._worksheet.delete_rows(current.row_number)
            except Exception as exc:
                raise UserRegistryError("Could not delete the registry row") from exc
            if self.get_user(telegram_user_id) is not None:
                raise UserRegistryError("Could not verify registry row deletion")
