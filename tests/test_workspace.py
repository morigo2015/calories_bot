from datetime import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from gspread.exceptions import APIError

from calories_bot.workspace import GoogleWorkspace, GoogleWorkspaceError


class Client:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.delete_error = None

    def create(self, title, folder_id=None):
        self.created.append((title, folder_id))
        return SimpleNamespace(id="sheet-123")

    def del_spreadsheet(self, spreadsheet_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(spreadsheet_id)


def workspace(tmp_path, client):
    return GoogleWorkspace(
        client,
        "folder",
        "food_log",
        ZoneInfo("Europe/Kyiv"),
        tmp_path / "photos",
    )


def test_open_store_uses_shared_client_and_runs_legacy_photo_migration(
    monkeypatch, tmp_path
) -> None:
    created = {}

    class Store:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.migrations = []

        def migrate_legacy_photos(self, root, user_id):
            self.migrations.append((root, user_id))

    monkeypatch.setattr("calories_bot.workspace.GoogleSheetsStore", Store)
    client = Client()

    store = workspace(tmp_path, client).open_meal_store("sheet", time(3), 123)

    assert created["client"] is client
    assert created["spreadsheet_id"] == "sheet"
    assert created["day_start_time"] == time(3)
    assert store.migrations == [((tmp_path / "photos").resolve(), 123)]


def test_open_saved_store_uses_shared_client(monkeypatch, tmp_path) -> None:
    created = {}

    class Store:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setattr("calories_bot.workspace.GoogleSavedMealStore", Store)
    client = Client()

    workspace(tmp_path, client).open_saved_meal_store("sheet")

    assert created["client"] is client
    assert created["spreadsheet_id"] == "sheet"


def test_create_sheet_places_it_in_folder_and_initializes_meal_schema(
    monkeypatch, tmp_path
) -> None:
    client = Client()
    google = workspace(tmp_path, client)
    opened = []
    monkeypatch.setattr(
        google,
        "open_meal_store",
        lambda *args: opened.append(args) or SimpleNamespace(),
    )
    saved_opened = []
    monkeypatch.setattr(
        google,
        "open_saved_meal_store",
        lambda *args: saved_opened.append(args) or SimpleNamespace(),
    )
    burned_opened = []
    monkeypatch.setattr(
        google,
        "open_burned_calorie_store",
        lambda *args: burned_opened.append(args) or SimpleNamespace(),
    )

    spreadsheet_id = google.create_personal_spreadsheet("Вася — 123", time(1), 123)

    assert spreadsheet_id == "sheet-123"
    assert client.created == [("Вася — 123", "folder")]
    assert opened == [("sheet-123", time(1), 123)]
    assert saved_opened == [("sheet-123",)]
    assert burned_opened == [("sheet-123",)]


def test_workspace_wraps_create_and_open_errors(monkeypatch, tmp_path) -> None:
    client = Client()
    google = workspace(tmp_path, client)
    monkeypatch.setattr(
        client,
        "create",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad")),
    )

    with pytest.raises(GoogleWorkspaceError, match="create"):
        google.create_personal_spreadsheet("name", time(1), 123)

    monkeypatch.setattr(
        "calories_bot.workspace.GoogleSheetsStore",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bad")),
    )
    with pytest.raises(GoogleWorkspaceError, match="open"):
        google.open_meal_store("sheet", time(1), 123)


def test_delete_is_idempotent_for_missing_spreadsheet(tmp_path) -> None:
    class Response:
        status_code = 404
        text = "not found"

        @staticmethod
        def json():
            return {"error": {"code": 404, "message": "not found"}}

    client = Client()
    client.delete_error = APIError(Response())

    workspace(tmp_path, client).delete_personal_spreadsheet("missing")


def test_delete_wraps_non_404_errors(tmp_path) -> None:
    class Response:
        status_code = 403
        text = "forbidden"

        @staticmethod
        def json():
            return {"error": {"code": 403, "message": "forbidden"}}

    client = Client()
    client.delete_error = APIError(Response())

    with pytest.raises(GoogleWorkspaceError, match="delete"):
        workspace(tmp_path, client).delete_personal_spreadsheet("sheet")
