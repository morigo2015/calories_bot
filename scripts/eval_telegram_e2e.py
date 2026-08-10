from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import gspread
from gspread.utils import ValueRenderOption
from telegram import Bot
from telethon import TelegramClient
from telethon.tl.custom.message import Message

from calories_bot.bot import FORMAT_ERROR_TEXT, NOT_FOOD_TEXT
from calories_bot.config import Settings
from calories_bot.models import round_whole
from calories_bot.sheets import (
    DAY_COLUMN,
    HEADERS,
    MEAL_KCAL_COLUMN,
    REQUEST_COLUMN,
    TIMESTAMP_COLUMN,
    TOTAL_WEIGHT_COLUMN,
    _date_from_sheet_serial,
    _datetime_from_sheet_serial,
    accounting_date,
)
from calories_bot.users import GoogleUserRegistry, UserRecord
from scripts.telegram_e2e_auth import DEFAULT_ENV_FILE, load_auth_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PHOTO = ROOT / "evals" / "images" / "meal-chicken-pasta.png"
DEFAULT_REPORT = ROOT / "eval-results" / "telegram-e2e.json"


class E2EFailure(RuntimeError):
    """Raised when a user-visible E2E invariant fails."""


@dataclass(frozen=True)
class StepResult:
    name: str
    passed: bool
    seconds: float
    detail: str


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise E2EFailure(detail)


def _message_text(message: Message) -> str:
    return (message.raw_text or "").strip()


def _button_texts(message: Message) -> list[str]:
    if not message.buttons:
        return []
    return [button.text for row in message.buttons for button in row]


class TelegramDriver:
    def __init__(
        self, client: TelegramClient, bot_username: str, timeout_seconds: float
    ) -> None:
        self.client = client
        self.bot_username = bot_username
        self.timeout_seconds = timeout_seconds

    async def send_text(self, text: str) -> tuple[Message, Message]:
        async with self.client.conversation(
            self.bot_username, timeout=self.timeout_seconds
        ) as conversation:
            sent = await conversation.send_message(text)
            response = await conversation.get_response()
        return sent, response

    async def send_photo(self, path: Path, caption: str) -> tuple[Message, Message]:
        async with self.client.conversation(
            self.bot_username, timeout=self.timeout_seconds
        ) as conversation:
            sent = await conversation.send_file(path, caption=caption)
            response = await conversation.get_response()
        return sent, response

    async def click_and_wait_for_edit(self, message: Message, button_text: str) -> str:
        before = _message_text(message)
        _require(
            button_text in _button_texts(message), f"button missing: {button_text}"
        )
        await message.click(text=button_text)
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            current = await self.client.get_messages(self.bot_username, ids=message.id)
            current_text = _message_text(current)
            if current_text != before or button_text not in _button_texts(current):
                return current_text
            await asyncio.sleep(0.25)
        raise E2EFailure(f"message was not edited after clicking {button_text}")


class SheetProbe:
    def __init__(self, settings: Settings, telegram_user_id: int) -> None:
        self.settings = settings
        client = gspread.service_account(
            filename=str(settings.google_service_account_file)
        )
        registry = GoogleUserRegistry(
            client, settings.users_spreadsheet_id, settings.users_sheet_name
        )
        user = registry.get_user(telegram_user_id)
        if user is None or user.status != "active" or not user.spreadsheet_id:
            raise E2EFailure("test Telegram account is not an active bot user")
        self.user = user
        self._registry = registry
        self._worksheet = client.open_by_key(user.spreadsheet_id).worksheet(
            settings.meal_sheet_name
        )

    def rows(self) -> list[list[object]]:
        rows = self._worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted
        )
        if not rows or rows[0] != HEADERS:
            raise E2EFailure("test user's Google Sheet has unexpected headers")
        return rows[1:]

    def current_user(self) -> UserRecord:
        user = self._registry.get_user(self.user.telegram_user_id or 0)
        if user is None:
            raise E2EFailure("test user disappeared from the registry")
        return user

    def accounting_day(self, sent: Message) -> date:
        return accounting_date(sent.date, self.settings.timezone, self.user.day_start)

    def row_for_request(
        self, rows: list[list[object]], sent: Message, request: str
    ) -> list[object] | None:
        day = self.accounting_day(sent)
        sent_at = sent.date.astimezone(self.settings.timezone)
        for row in rows:
            if len(row) <= REQUEST_COLUMN or str(row[REQUEST_COLUMN]) != request:
                continue
            try:
                row_day = _date_from_sheet_serial(row[DAY_COLUMN])
                row_at = _datetime_from_sheet_serial(
                    row[TIMESTAMP_COLUMN], self.settings.timezone
                )
            except (TypeError, ValueError):
                continue
            if row_day == day and abs((row_at - sent_at).total_seconds()) <= 120:
                return row
        return None

    def wait_for_requests(
        self,
        expected: list[tuple[Message, str]],
        timeout_seconds: float,
    ) -> list[list[object]]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            rows = self.rows()
            if all(
                self.row_for_request(rows, sent, request) for sent, request in expected
            ):
                return rows
            time.sleep(3)
        rows = self.rows()
        missing = [
            request
            for sent, request in expected
            if self.row_for_request(rows, sent, request) is None
        ]
        raise E2EFailure(f"Google Sheets rows missing for requests: {missing}")

    def day_total(self, day: date, rows: list[list[object]]) -> float:
        total = 0.0
        for row in rows:
            if len(row) <= MEAL_KCAL_COLUMN:
                continue
            try:
                if _date_from_sheet_serial(row[DAY_COLUMN]) == day:
                    total += float(str(row[MEAL_KCAL_COLUMN]))
            except (TypeError, ValueError):
                continue
        return total


async def _run_step(results: list[StepResult], name: str, operation: Any) -> Any | None:
    started = time.monotonic()
    try:
        value = await operation()
    except Exception as exc:
        results.append(StepResult(name, False, time.monotonic() - started, str(exc)))
        print(f"FAIL {name}: {exc}")
        return None
    results.append(StepResult(name, True, time.monotonic() - started, "ok"))
    print(f"PASS {name}")
    return value


async def run_journey(
    env_file: Path, photo: Path, timeout_seconds: float
) -> tuple[list[StepResult], dict[str, Any]]:
    auth = load_auth_config(env_file)
    settings = Settings.from_env()
    client = TelegramClient(str(auth.session), auth.api_id, auth.api_hash)
    results: list[StepResult] = []
    created: list[Message] = []
    created_rows: list[tuple[Message, str]] = []
    original_goal: int | None = None
    goal_changed = False
    metadata: dict[str, Any] = {}

    await client.connect()
    try:
        _require(
            await client.is_user_authorized(), "Telegram E2E session is not authorized"
        )
        account = await client.get_me()
        async with Bot(settings.telegram_bot_token) as bot:
            bot_info = await bot.get_me()
        _require(bool(bot_info.username), "the bot has no username")
        bot_username = f"@{bot_info.username}"
        driver = TelegramDriver(client, bot_username, timeout_seconds)
        probe = await asyncio.to_thread(SheetProbe, settings, account.id)
        baseline_rows = await asyncio.to_thread(probe.rows)
        original_goal = probe.user.daily_kcal_goal
        metadata = {
            "telegram_user_id": account.id,
            "telegram_username": account.username,
            "bot_username": bot_username,
            "original_goal": original_goal,
        }

        async def static_commands() -> None:
            for command, marker in (
                ("/start", "Напиши"),
                ("/help", "/day"),
                ("/tips", "Скорочений запис"),
                ("/week", "За тиждень"),
            ):
                _, response = await driver.send_text(command)
                _require(
                    marker.casefold() in _message_text(response).casefold(),
                    f"{command}: unexpected response",
                )

        await _run_step(results, "start/help/tips/week", static_commands)

        test_goal = 1600 if original_goal != 1600 else 1700

        async def set_goal() -> None:
            nonlocal goal_changed
            _, response = await driver.send_text(f"/goal {test_goal}")
            _require(
                str(test_goal) in _message_text(response), "goal confirmation missing"
            )
            goal_changed = True

        await _run_step(results, "set daily goal", set_goal)

        exact_cases = (
            ("Сир 100 г 200#", 200.0, ("100 г", "200 кк/100 г")),
            ("Йогурт 200 г 60#", 120.0, ("200 г", "60 кк/100 г")),
        )
        exact_messages: list[tuple[Message, Message]] = []

        async def exact_foods() -> None:
            for text, _expected_kcal, markers in exact_cases:
                sent, response = await driver.send_text(text)
                created.append(response)
                created_rows.append((sent, text))
                exact_messages.append((sent, response))
                response_text = _message_text(response)
                _require(
                    "Видалити" in _button_texts(response),
                    f"{text}: delete button missing",
                )
                for marker in markers:
                    _require(marker in response_text, f"{text}: missing {marker}")
            day = probe.accounting_day(exact_messages[0][0])
            baseline_total = probe.day_total(day, baseline_rows)
            expected_total = baseline_total + sum(case[1] for case in exact_cases)
            _, day_response = await driver.send_text("/day")
            _require(
                f"Сьогодні: {round_whole(expected_total)} кк"
                in _message_text(day_response),
                "/day total does not match Sheets",
            )

        await _run_step(results, "two foods + /day + Sheets", exact_foods)

        async def syrnyky_formula() -> None:
            text = "Сирники калорійністю 25 кілокалорій на 100 грамів, п'ять штук"
            sent, response = await driver.send_text(text)
            created.append(response)
            created_rows.append((sent, text))
            response_text = _message_text(response)
            for marker in ("5 шт.", "г/шт.", "25 кк/100 г", "×"):
                _require(marker in response_text, f"syrnyky formula missing {marker}")

        await _run_step(results, "portion calculation formula", syrnyky_formula)

        async def invalid_and_non_food() -> None:
            invalid_sent, invalid = await driver.send_text("сир 50.5 г")
            if "Видалити" in _button_texts(invalid):
                created.append(invalid)
                created_rows.append((invalid_sent, "сир 50.5 г"))
            _require(
                _message_text(invalid) == FORMAT_ERROR_TEXT,
                "invalid number error mismatch",
            )
            non_food_sent, non_food = await driver.send_text(
                "Як справи? Це автоматичний E2E тест."
            )
            if "Видалити" in _button_texts(non_food):
                created.append(non_food)
                created_rows.append(
                    (non_food_sent, "Як справи? Це автоматичний E2E тест.")
                )
            _require(
                _message_text(non_food) == NOT_FOOD_TEXT, "non-food response mismatch"
            )

        await _run_step(results, "invalid and non-food messages", invalid_and_non_food)

        async def photo_with_caption() -> None:
            _require(photo.is_file(), f"photo fixture missing: {photo}")
            sent, response = await driver.send_photo(photo, "350 г")
            created.append(response)
            created_rows.append((sent, "350 г"))
            response_text = _message_text(response)
            _require("350 г" in response_text, "photo response lost explicit weight")
            _require("кк/100 г" in response_text, "photo calculation is incomplete")
            _require(
                "Видалити" in _button_texts(response), "photo delete button missing"
            )

        await _run_step(results, "photo with explicit caption", photo_with_caption)

        async def verify_sheets_snapshot() -> None:
            rows = await asyncio.to_thread(
                probe.wait_for_requests,
                created_rows,
                min(timeout_seconds, 15),
            )
            expected_kcal = {case[0]: case[1] for case in exact_cases}
            for sent, request in created_rows:
                row = probe.row_for_request(rows, sent, request)
                _require(row is not None, f"Sheets row missing for {request}")
                if request in expected_kcal:
                    _require(
                        abs(float(str(row[MEAL_KCAL_COLUMN])) - expected_kcal[request])
                        < 0.01,
                        f"incorrect kcal in Sheets for {request}",
                    )
                elif request.startswith("Сирники"):
                    _require(
                        float(str(row[MEAL_KCAL_COLUMN])) > 0,
                        "syrnyky kcal is zero",
                    )
                elif request == "350 г":
                    _require(
                        abs(float(str(row[TOTAL_WEIGHT_COLUMN])) - 350) < 0.01,
                        "photo weight mismatch in Sheets",
                    )

        await _run_step(results, "Google Sheets snapshot", verify_sheets_snapshot)

    finally:
        if client.is_connected():
            if "driver" in locals():
                for response in reversed(created):
                    try:
                        edited = await driver.click_and_wait_for_edit(
                            response, "Видалити"
                        )
                        if "Видалено" not in edited:
                            raise E2EFailure("delete confirmation missing")
                    except Exception as exc:
                        results.append(StepResult("cleanup meal", False, 0, str(exc)))
                        print(f"FAIL cleanup meal: {exc}")
                if goal_changed:
                    try:
                        if original_goal is None:
                            _, prompt = await driver.send_text("/goal")
                            edited = await driver.click_and_wait_for_edit(
                                prompt, "Вимкнути ціль"
                            )
                            _require(
                                "вимкнено" in edited.casefold(),
                                "goal disable confirmation missing",
                            )
                        else:
                            _, response = await driver.send_text(
                                f"/goal {original_goal}"
                            )
                            _require(
                                str(original_goal) in _message_text(response),
                                "original goal restore confirmation missing",
                            )
                    except Exception as exc:
                        results.append(StepResult("restore goal", False, 0, str(exc)))
                        print(f"FAIL restore goal: {exc}")
            await client.disconnect()

    if "probe" in locals():

        async def verify_cleanup() -> None:
            rows = await asyncio.to_thread(probe.rows)
            for sent, request in created_rows:
                row = probe.row_for_request(rows, sent, request)
                _require(row is None, f"test row {sent.id} remains in Sheets")
            current = await asyncio.to_thread(probe.current_user)
            _require(
                current.daily_kcal_goal == original_goal, "daily goal was not restored"
            )

        await _run_step(results, "verify cleanup", verify_cleanup)
    return results, metadata


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the live Telegram user-journey E2E suite"
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--photo", type=Path, default=DEFAULT_PHOTO)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="confirm live Telegram messages, LLM calls and temporary Sheets writes",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print("Plan: live Telegram journey, temporary goal change, 4 food rows, cleanup.")
    if not args.confirm:
        print("Nothing sent. Re-run with --confirm.")
        return 2
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    os.umask(0o077)
    started = time.time()
    try:
        results, metadata = asyncio.run(
            run_journey(args.env_file.expanduser(), args.photo.resolve(), args.timeout)
        )
    except Exception as exc:
        print(f"E2E setup failed: {exc}", file=sys.stderr)
        return 2
    passed = all(result.passed for result in results) and bool(results)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(
            {
                "passed": passed,
                "started_at_unix": started,
                "metadata": metadata,
                "results": [asdict(result) for result in results],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Result: {'PASS' if passed else 'FAIL'}; report={args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
