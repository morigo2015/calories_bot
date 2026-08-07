from __future__ import annotations

import logging
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread

from .sheets import GoogleSheetsStore

LOGGER = logging.getLogger(__name__)


class GoogleWorkspaceError(RuntimeError):
    """Raised when a personal spreadsheet cannot be managed."""


class GoogleWorkspace:
    def __init__(
        self,
        client: gspread.Client,
        drive_folder_id: str,
        meal_sheet_name: str,
        timezone: ZoneInfo,
        photo_storage_dir: Path,
    ) -> None:
        self._client = client
        self._drive_folder_id = drive_folder_id
        self._meal_sheet_name = meal_sheet_name
        self._timezone = timezone
        self._photo_storage_dir = photo_storage_dir.resolve()

    @property
    def client(self) -> gspread.Client:
        return self._client

    def open_meal_store(
        self,
        spreadsheet_id: str,
        day_start: time,
        telegram_user_id: int,
    ) -> GoogleSheetsStore:
        try:
            store = GoogleSheetsStore(
                credentials_file=None,
                spreadsheet_id=spreadsheet_id,
                worksheet_name=self._meal_sheet_name,
                timezone=self._timezone,
                day_start_time=day_start,
                client=self._client,
            )
            store.migrate_legacy_photos(self._photo_storage_dir, telegram_user_id)
            return store
        except Exception as exc:
            raise GoogleWorkspaceError("Could not open personal spreadsheet") from exc

    def create_personal_spreadsheet(
        self, title: str, day_start: time, telegram_user_id: int
    ) -> str:
        try:
            spreadsheet = self._client.create(title, folder_id=self._drive_folder_id)
            # Opening the store creates and validates the meal worksheet before
            # the registry is allowed to mark the user active.
            self.open_meal_store(spreadsheet.id, day_start, telegram_user_id)
            return spreadsheet.id
        except Exception as exc:
            raise GoogleWorkspaceError("Could not create personal spreadsheet") from exc

    def delete_personal_spreadsheet(self, spreadsheet_id: str) -> None:
        try:
            self._client.del_spreadsheet(spreadsheet_id)
        except gspread.exceptions.APIError as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) == 404:
                return
            raise GoogleWorkspaceError("Could not delete personal spreadsheet") from exc
        except Exception as exc:
            raise GoogleWorkspaceError("Could not delete personal spreadsheet") from exc
