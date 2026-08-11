from datetime import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from calories_bot.bot import UserManager
from calories_bot.users import UserRecord


def record(
    *,
    user_id=None,
    status="invited",
    token="token",
    sheet="",
    cutoff=time(1),
    goal=None,
):
    return UserRecord(
        row_number=2,
        telegram_user_id=user_id,
        display_name="Вася",
        telegram_username="vasya" if user_id else "",
        status=status,
        invite_token=token,
        spreadsheet_id=sheet,
        day_start=cutoff,
        daily_kcal_goal=goal,
    )


class Registry:
    def __init__(self, current):
        self.current = current
        self.deleted = False
        self.created_invites = []

    def get_user(self, user_id):
        if self.current.telegram_user_id == user_id and self.current.status in {
            "active",
            "blocked",
        }:
            return self.current
        return None

    def list_users(self):
        return [self.current]

    def get_invite(self, token):
        if self.current.status == "invited" and self.current.invite_token == token:
            return self.current
        return None

    def create_invite(self, display_name, token, cutoff):
        self.created_invites.append((display_name, token, cutoff))
        self.current = record(token=token, cutoff=cutoff)
        return self.current

    def prepare_activation(self, invite, user_id, username, sheet):
        del invite
        self.current = record(
            user_id=user_id,
            status="invited",
            token="token",
            sheet=sheet,
            cutoff=self.current.day_start,
        )
        return self.current

    def complete_activation(self, invite):
        del invite
        self.current = record(
            user_id=self.current.telegram_user_id,
            status="active",
            token="",
            sheet=self.current.spreadsheet_id,
            cutoff=self.current.day_start,
        )
        return self.current

    def set_status(self, user_id, status):
        self.current = record(
            user_id=user_id,
            status=status,
            token="",
            sheet=self.current.spreadsheet_id,
            cutoff=self.current.day_start,
            goal=self.current.daily_kcal_goal,
        )
        return self.current

    def set_daily_kcal_goal(self, user_id, goal):
        self.current = record(
            user_id=user_id,
            status=self.current.status,
            token="",
            sheet=self.current.spreadsheet_id,
            cutoff=self.current.day_start,
            goal=goal,
        )
        return self.current

    def delete_user(self, user_id):
        del user_id
        self.deleted = True


class Workspace:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.fail_delete = False
        self.opened = []
        self.saved_opened = []

    def create_personal_spreadsheet(self, title, cutoff, user_id):
        self.created.append((title, cutoff, user_id))
        return "new-sheet"

    def open_meal_store(self, sheet, cutoff, user_id):
        self.opened.append((sheet, cutoff, user_id))
        return SimpleNamespace(sheet=sheet, cutoff=cutoff, user_id=user_id)

    def open_saved_meal_store(self, sheet):
        self.saved_opened.append(sheet)
        return SimpleNamespace(sheet=sheet)

    def delete_personal_spreadsheet(self, sheet):
        if self.fail_delete:
            raise RuntimeError("failed")
        self.deleted.append(sheet)


def manager(tmp_path, registry, workspace):
    return UserManager(
        SimpleNamespace(),
        registry,
        workspace,
        ZoneInfo("Europe/Kyiv"),
        time(1),
        tmp_path / "photos",
    )


def test_activation_creates_one_sheet_and_repeat_start_reuses_account(tmp_path) -> None:
    registry = Registry(record(cutoff=time(3)))
    workspace = Workspace()
    users = manager(tmp_path, registry, workspace)

    first = users.activate("token", 123, "vasya")
    second = users.activate("token", 123, "vasya")

    assert first == second
    assert workspace.created == [("Вася — 123", time(3), 123)]


def test_prepare_release_storage_opens_active_saved_meal_sheet(tmp_path) -> None:
    registry = Registry(record(user_id=123, status="active", sheet="sheet-123"))
    workspace = Workspace()

    manager(tmp_path, registry, workspace).prepare_release_storage()

    assert workspace.saved_opened == ["sheet-123"]


def test_activation_reuses_spreadsheet_saved_by_partial_attempt(tmp_path) -> None:
    registry = Registry(record(user_id=123, sheet="saved-sheet"))
    workspace = Workspace()

    active = manager(tmp_path, registry, workspace).activate("token", 123, "vasya")

    assert active.spreadsheet_id == "saved-sheet"
    assert workspace.created == []


def test_service_is_cached_per_personal_sheet_and_day_start(tmp_path) -> None:
    registry = Registry(
        record(user_id=123, status="active", token="", sheet="sheet", cutoff=time(3))
    )
    workspace = Workspace()
    users = manager(tmp_path, registry, workspace)

    first = users.service_for(registry.current)
    second = users.service_for(registry.current)

    assert first is second
    assert workspace.opened == [("sheet", time(3), 123)]
    assert workspace.saved_opened == ["sheet"]
    assert first._photo_storage_dir == (tmp_path / "photos" / "123").resolve()


def test_service_rejects_incomplete_or_blocked_context(tmp_path) -> None:
    registry = Registry(record(user_id=123, status="blocked", token="", sheet="sheet"))
    users = manager(tmp_path, registry, Workspace())

    with pytest.raises(RuntimeError, match="incomplete"):
        users.service_for(registry.current)


def test_invite_uses_secure_generated_token(monkeypatch, tmp_path) -> None:
    registry = Registry(record())
    users = manager(tmp_path, registry, Workspace())
    monkeypatch.setattr("calories_bot.bot.secrets.token_urlsafe", lambda size: "secure")

    token = users.create_invite("Вася")

    assert token == "secure"
    assert registry.created_invites == [("Вася", "secure", time(1))]


def test_set_status_delegates_to_registry(tmp_path) -> None:
    registry = Registry(record(user_id=123, status="active", token="", sheet="sheet"))
    users = manager(tmp_path, registry, Workspace())

    blocked = users.set_status(123, "blocked")

    assert blocked.status == "blocked"


def test_goal_update_refreshes_cached_service(tmp_path) -> None:
    registry = Registry(record(user_id=123, status="active", token="", sheet="sheet"))
    users = manager(tmp_path, registry, Workspace())
    service = users.service_for(registry.current)

    updated = users.set_daily_kcal_goal(123, 2000)

    assert updated.daily_kcal_goal == 2000
    assert service._daily_kcal_goal == 2000


def test_delete_failure_keeps_user_blocked_and_registry_row(tmp_path) -> None:
    registry = Registry(record(user_id=123, status="active", token="", sheet="sheet"))
    workspace = Workspace()
    workspace.fail_delete = True
    users = manager(tmp_path, registry, workspace)

    with pytest.raises(RuntimeError, match="failed"):
        users.delete_user(123)

    assert registry.current.status == "blocked"
    assert registry.deleted is False


def test_successful_delete_removes_sheet_photos_and_registry_row(tmp_path) -> None:
    registry = Registry(record(user_id=123, status="active", token="", sheet="sheet"))
    workspace = Workspace()
    users = manager(tmp_path, registry, workspace)
    photo_dir = tmp_path / "photos" / "123"
    photo_dir.mkdir(parents=True)
    (photo_dir / "1.jpg").write_bytes(b"photo")

    users.delete_user(123)

    assert workspace.deleted == ["sheet"]
    assert not photo_dir.exists()
    assert registry.deleted is True
