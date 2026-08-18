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
from calories_bot.saved_meals import GoogleSavedMealStore
from calories_bot.sheets import (
    DAY_COLUMN,
    HEADERS,
    MEAL_KCAL_COLUMN,
    MEAL_NAME_COLUMN,
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


def _has_button(message: Message, expected_text: str) -> bool:
    return any(expected_text in text for text in _button_texts(message))


def _button_callback_data(message: Message, expected_text: str) -> str | None:
    for row in message.buttons or []:
        for button in row:
            if expected_text not in button.text:
                continue
            data = getattr(button, "data", None)
            if isinstance(data, bytes):
                return data.decode("utf-8", errors="replace")
            if data is not None:
                return str(data)
    return None


def _response_with_terms(
    responses: list[Message], alternatives: tuple[str, ...]
) -> Message:
    matches = [
        response
        for response in responses
        if any(
            term.casefold() in _message_text(response).casefold()
            for term in alternatives
        )
    ]
    _require(
        len(matches) == 1,
        f"expected one component matching {alternatives}, got {len(matches)}",
    )
    return matches[0]


def _expected_day_summary(today_total: float, daily_kcal_goal: int | None) -> str:
    rounded_total = round_whole(today_total)
    if daily_kcal_goal is None:
        return f"Сьогодні: {rounded_total} кк"
    return f"Сьогодні: {rounded_total} із {daily_kcal_goal} кк"


def _split_meal_responses(
    responses: list[Message],
) -> tuple[list[Message], list[Message]]:
    meals = [response for response in responses if _has_button(response, "Видалити")]
    summaries = [
        response
        for response in responses
        if _message_text(response).startswith("За день:")
    ]
    return meals, summaries


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

    async def send_text_responses(
        self, text: str, *, idle_seconds: float = 2.5, maximum: int = 10
    ) -> tuple[Message, list[Message]]:
        async with self.client.conversation(
            self.bot_username, timeout=self.timeout_seconds
        ) as conversation:
            sent = await conversation.send_message(text)
            responses = [await conversation.get_response()]
            while len(responses) < maximum:
                try:
                    response = await asyncio.wait_for(
                        conversation.get_response(), timeout=idle_seconds
                    )
                except TimeoutError:
                    break
                responses.append(response)
        return sent, responses

    async def send_photo(self, path: Path, caption: str) -> tuple[Message, Message]:
        async with self.client.conversation(
            self.bot_username, timeout=self.timeout_seconds
        ) as conversation:
            sent = await conversation.send_file(path, caption=caption)
            response = await conversation.get_response()
        return sent, response

    async def click_and_get_response(
        self, message: Message, button_text: str
    ) -> Message:
        _require(_has_button(message, button_text), f"button missing: {button_text}")
        latest = await self.client.get_messages(self.bot_username, limit=1)
        latest_id = latest[0].id if latest else 0
        await message.click(text=lambda text: button_text in text)
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            recent = await self.client.get_messages(self.bot_username, limit=10)
            candidates = [
                candidate
                for candidate in recent
                if candidate.id > latest_id and not candidate.out
            ]
            if candidates:
                return min(candidates, key=lambda candidate: candidate.id)
            await asyncio.sleep(0.25)
        raise E2EFailure(f"no new message after clicking {button_text}")

    async def click_and_wait_for_edit_message(
        self, message: Message, button_text: str
    ) -> Message:
        before = _message_text(message)
        _require(_has_button(message, button_text), f"button missing: {button_text}")
        callback = await message.click(text=lambda text: button_text in text)
        callback_text = getattr(callback, "message", None) or ""
        if callback_text.startswith("Не вдалося"):
            raise E2EFailure(
                f"clicking {button_text} on message {message.id} failed: "
                f"{callback_text}"
            )
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            current = await self.client.get_messages(self.bot_username, ids=message.id)
            current_text = _message_text(current)
            if current_text != before or not _has_button(current, button_text):
                return current
            await asyncio.sleep(0.25)
        callback_text = callback_text or "<empty>"
        raise E2EFailure(
            f"message {message.id} ({before[:80]!r}) was not edited after "
            f"clicking {button_text}; callback={callback_text!r}"
        )

    async def click_and_wait_for_edit(self, message: Message, button_text: str) -> str:
        edited = await self.click_and_wait_for_edit_message(message, button_text)
        return _message_text(edited)


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
        self._saved_meals = GoogleSavedMealStore(
            credentials_file=None,
            spreadsheet_id=user.spreadsheet_id,
            client=client,
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

    def saved_meals(self):
        return self._saved_meals.list_meals()

    def delete_saved_meal(self, saved_meal_id: str) -> bool:
        return self._saved_meals.delete(saved_meal_id)

    def remove_rows_after_baseline(self, baseline_rows: list[list[object]]) -> int:
        rows = self.rows()
        if rows[: len(baseline_rows)] != baseline_rows:
            raise E2EFailure("cannot safely restore Sheets: baseline prefix changed")
        extra_count = len(rows) - len(baseline_rows)
        if extra_count <= 0:
            return 0
        self._worksheet.delete_rows(len(baseline_rows) + 2, len(rows) + 1)
        return extra_count

    def accounting_day(self, sent: Message) -> date:
        return accounting_date(sent.date, self.settings.timezone, self.user.day_start)

    def row_for_request(
        self, rows: list[list[object]], sent: Message, request: str
    ) -> list[object] | None:
        matches = self.rows_for_request(rows, sent, request)
        return matches[0] if matches else None

    def rows_for_request(
        self, rows: list[list[object]], sent: Message, request: str
    ) -> list[list[object]]:
        day = self.accounting_day(sent)
        sent_at = sent.date.astimezone(self.settings.timezone)
        matches: list[list[object]] = []
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
                matches.append(row)
        return matches

    def wait_for_request_count(
        self,
        sent: Message,
        request: str,
        expected_count: int,
        timeout_seconds: float,
    ) -> list[list[object]]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            matches = self.rows_for_request(self.rows(), sent, request)
            if len(matches) == expected_count:
                return matches
            time.sleep(2)
        matches = self.rows_for_request(self.rows(), sent, request)
        raise E2EFailure(
            f"expected {expected_count} Sheets row(s) for {request!r}, "
            f"got {len(matches)}"
        )

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
    baseline_saved_ids: set[str] = set()

    def track_created(*responses: Message) -> None:
        for response in responses:
            callback_data = _button_callback_data(response, "Видалити")
            _require(callback_data is not None, "meal reply has no delete callback")
            created[:] = [
                current
                for current in created
                if _button_callback_data(current, "Видалити") != callback_data
            ]
            created.append(response)

    def untrack_created(response: Message) -> None:
        callback_data = _button_callback_data(response, "Видалити")
        created[:] = [
            current
            for current in created
            if _button_callback_data(current, "Видалити") != callback_data
        ]

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
        baseline_saved = await asyncio.to_thread(probe.saved_meals)
        baseline_saved_ids = {meal.saved_meal_id for meal in baseline_saved}
        original_goal = probe.user.daily_kcal_goal
        metadata = {
            "telegram_user_id": account.id,
            "telegram_username": account.username,
            "bot_username": bot_username,
            "original_goal": original_goal,
            "baseline_meal_rows": len(baseline_rows),
            "baseline_saved_meals": len(baseline_saved),
        }

        async def static_commands() -> None:
            for command, marker in (
                ("/start", "Напиши"),
                ("/help", "/day"),
                ("/tips", "Скорочений запис"),
                ("/week", "За 7 днів"),
            ):
                _, response = await driver.send_text(command)
                _require(
                    marker.casefold() in _message_text(response).casefold(),
                    f"{command}: unexpected response",
                )

        await _run_step(results, "start/help/tips/weekly reports", static_commands)

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
                track_created(response)
                created_rows.append((sent, text))
                exact_messages.append((sent, response))
                response_text = _message_text(response)
                _require(
                    _has_button(response, "Видалити"),
                    f"{text}: delete button missing",
                )
                for marker in markers:
                    _require(marker in response_text, f"{text}: missing {marker}")
            day = probe.accounting_day(exact_messages[0][0])
            baseline_total = probe.day_total(day, baseline_rows)
            expected_total = baseline_total + sum(case[1] for case in exact_cases)
            _, day_response = await driver.send_text("/day")
            day_text = _message_text(day_response)
            expected_summary = _expected_day_summary(expected_total, test_goal)
            _require(
                expected_summary in day_text,
                f"/day total does not match Sheets: expected "
                f"{expected_summary!r}, got {day_text!r}",
            )

        await _run_step(results, "two foods + /day + Sheets", exact_foods)

        async def syrnyky_formula() -> None:
            text = "Сирники калорійністю 25 кілокалорій на 100 грамів, п'ять штук"
            sent, response = await driver.send_text(text)
            track_created(response)
            created_rows.append((sent, text))
            response_text = _message_text(response)
            for marker in ("5 шт.", "г/шт.", "25 кк/100 г", "×"):
                _require(marker in response_text, f"syrnyky formula missing {marker}")

        await _run_step(results, "portion calculation formula", syrnyky_formula)

        async def invalid_and_non_food() -> None:
            invalid_sent, invalid = await driver.send_text("сир 50.5 г")
            if _has_button(invalid, "Видалити"):
                track_created(invalid)
                created_rows.append((invalid_sent, "сир 50.5 г"))
            _require(
                _message_text(invalid) == FORMAT_ERROR_TEXT,
                "invalid number error mismatch",
            )
            non_food_sent, non_food = await driver.send_text(
                "Як справи? Це автоматичний E2E тест."
            )
            if _has_button(non_food, "Видалити"):
                track_created(non_food)
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
            track_created(response)
            created_rows.append((sent, "350 г"))
            response_text = _message_text(response)
            _require("350 г" in response_text, "photo response lost explicit weight")
            _require("кк/100 г" in response_text, "photo calculation is incomplete")
            _require(_has_button(response, "Видалити"), "photo delete button missing")

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
                if row is None:
                    raise E2EFailure(f"Sheets row missing for {request}")
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

        async def exact_composite_lifecycle() -> None:
            text = "гречка 180 г, куряче філе 120 г і салат 100 г"
            sent, all_responses = await driver.send_text_responses(text)
            responses, summaries = _split_meal_responses(all_responses)
            _require(
                len(responses) == 3,
                f"exact composite returned {len(responses)} responses instead of 3",
            )
            _require(
                len(summaries) == 1,
                f"exact composite returned {len(summaries)} daily totals instead of 1",
            )
            track_created(*responses)
            created_rows.append((sent, text))
            grain = _response_with_terms(responses, ("греч",))
            chicken = _response_with_terms(responses, ("кур", "філе"))
            salad = _response_with_terms(responses, ("салат",))
            for response, weight in ((grain, 180), (chicken, 120), (salad, 100)):
                response_text = _message_text(response)
                _require(f"{weight} г" in response_text, f"component lost {weight} g")
                for button in ("Зберегти", "Змінити вагу", "Видалити"):
                    _require(
                        _has_button(response, button),
                        f"component action missing: {button}",
                    )

            rows = await asyncio.to_thread(
                probe.wait_for_request_count,
                sent,
                text,
                3,
                min(timeout_seconds, 20),
            )
            weights_by_name = {
                str(row[MEAL_NAME_COLUMN]).casefold(): float(
                    str(row[TOTAL_WEIGHT_COLUMN])
                )
                for row in rows
            }
            for term, expected_weight in (("греч", 180), ("кур", 120), ("салат", 100)):
                matching = [
                    weight for name, weight in weights_by_name.items() if term in name
                ]
                _require(
                    matching == [expected_weight],
                    f"Sheets component {term!r}: got weights {matching}",
                )

            grain = await driver.click_and_wait_for_edit_message(grain, "Зберегти")
            track_created(grain)
            _require(not _has_button(grain, "Зберегти"), "save button remained visible")
            deadline = time.monotonic() + min(timeout_seconds, 20)
            saved = None
            while time.monotonic() < deadline:
                meals = await asyncio.to_thread(probe.saved_meals)
                new_meals = [
                    meal
                    for meal in meals
                    if meal.saved_meal_id not in baseline_saved_ids
                ]
                if len(new_meals) == 1:
                    saved = new_meals[0]
                    break
                await asyncio.sleep(1)
            _require(saved is not None, "saved component did not appear in storage")
            _require(
                saved.default_total_weight_g == 180,
                "saved component has incorrect default weight",
            )

            target_weight = next(
                weight for weight in settings.meal_weight_presets if weight != 120
            )
            prompt = await driver.click_and_get_response(chicken, "Змінити вагу")
            _require(
                "Обери нову вагу" in _message_text(prompt), "weight prompt mismatch"
            )
            updated_chicken = await driver.click_and_get_response(
                prompt, f"{target_weight}г"
            )
            track_created(updated_chicken)
            _require(
                f"{target_weight} г" in _message_text(updated_chicken),
                "updated component reply has wrong weight",
            )
            _require(
                _has_button(updated_chicken, "Видалити"),
                "updated component cannot be deleted",
            )

            deleted = await driver.click_and_wait_for_edit(salad, "Видалити")
            _require("Видалено" in deleted, "component deletion was not confirmed")
            untrack_created(salad)
            rows = await asyncio.to_thread(
                probe.wait_for_request_count,
                sent,
                text,
                2,
                min(timeout_seconds, 20),
            )
            chicken_rows = [
                row for row in rows if "кур" in str(row[MEAL_NAME_COLUMN]).casefold()
            ]
            _require(len(chicken_rows) == 1, "updated chicken row is missing")
            _require(
                abs(float(str(chicken_rows[0][TOTAL_WEIGHT_COLUMN])) - target_weight)
                < 0.01,
                "updated component weight did not reach Sheets",
            )

            _, menu = await driver.send_text("/meals")
            _require(
                _has_button(menu, saved.display_name),
                "saved component is missing from /meals",
            )
            reused = await driver.click_and_get_response(menu, saved.display_name)
            track_created(reused)
            _require("180 г" in _message_text(reused), "saved meal lost default weight")
            _require(not _has_button(reused, "Зберегти"), "reused meal can be re-saved")
            _require(
                _has_button(reused, "Змінити вагу"), "reused meal weight is locked"
            )
            reused_target = next(
                weight
                for weight in settings.meal_weight_presets
                if weight != saved.default_total_weight_g
            )
            reused_prompt = await driver.click_and_get_response(reused, "Змінити вагу")
            updated_reused = await driver.click_and_get_response(
                reused_prompt, f"{reused_target}г"
            )
            track_created(updated_reused)
            _require(
                f"{reused_target} г" in _message_text(updated_reused),
                "reused saved meal weight was not updated",
            )

            manage = await driver.click_and_wait_for_edit_message(
                menu, "Видалити із збережених"
            )
            confirm = await driver.click_and_wait_for_edit_message(
                manage, saved.display_name
            )
            _require(
                saved.display_name in _message_text(confirm),
                "saved-meal delete confirmation names the wrong meal",
            )
            await driver.click_and_wait_for_edit_message(confirm, "Видалити")
            remaining_ids = {
                meal.saved_meal_id
                for meal in await asyncio.to_thread(probe.saved_meals)
            }
            _require(
                remaining_ids == baseline_saved_ids,
                "saved component was not removed from storage",
            )

        await _run_step(
            results,
            "exact composite: save/change/delete/reuse",
            exact_composite_lifecycle,
        )

        async def malformed_composites() -> None:
            cases = (
                (
                    "зїв рис курку і салат салата небагато",
                    (1, 3),
                    (("рис",), ("кур",), ("салат",)),
                ),
                (
                    "борщ сметана хліб два куски ну і сала трошки",
                    (1, 5),
                    (("борщ",), ("сметан",), ("хліб",), ("сал",)),
                ),
            )
            for text, count_bounds, required in cases:
                sent, all_responses = await driver.send_text_responses(text)
                responses, summaries = _split_meal_responses(all_responses)
                _require(
                    count_bounds[0] <= len(responses) <= count_bounds[1],
                    f"{text!r}: got {len(responses)} components, "
                    f"expected {count_bounds}",
                )
                _require(
                    len(summaries) == 1,
                    f"{text!r}: got {len(summaries)} daily totals instead of 1",
                )
                searchable = " ".join(_message_text(response) for response in responses)
                searchable = searchable.casefold()
                for alternatives in required:
                    _require(
                        any(term in searchable for term in alternatives),
                        f"{text!r}: missing named food {alternatives}",
                    )
                for response in responses:
                    for button in ("Зберегти", "Змінити вагу", "Видалити"):
                        _require(
                            _has_button(response, button),
                            f"{text!r}: component action missing: {button}",
                        )
                await asyncio.to_thread(
                    probe.wait_for_request_count,
                    sent,
                    text,
                    len(responses),
                    min(timeout_seconds, 20),
                )
                track_created(*responses)
                created_rows.append((sent, text))

        await _run_step(
            results, "malformed composite recognition + actions", malformed_composites
        )

    finally:
        if client.is_connected():
            if "driver" in locals():
                await asyncio.sleep(5)
                for response in reversed(created):
                    current = await client.get_messages(
                        driver.bot_username, ids=response.id
                    )
                    if not _has_button(current, "Видалити"):
                        continue
                    response = current
                    last_error: Exception | None = None
                    for attempt in range(3):
                        try:
                            edited = await driver.click_and_wait_for_edit(
                                response, "Видалити"
                            )
                            if "Видалено" not in edited:
                                raise E2EFailure("delete confirmation missing")
                            last_error = None
                            await asyncio.sleep(2)
                            break
                        except Exception as exc:
                            last_error = exc
                            if attempt < 2:
                                await asyncio.sleep(8 * (attempt + 1))
                    if last_error is not None:
                        results.append(
                            StepResult("cleanup meal", False, 0, str(last_error))
                        )
                        print(f"FAIL cleanup meal: {last_error}")
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
                try:
                    saved_meals = await asyncio.to_thread(probe.saved_meals)
                    for saved in saved_meals:
                        if saved.saved_meal_id not in baseline_saved_ids:
                            await asyncio.to_thread(
                                probe.delete_saved_meal, saved.saved_meal_id
                            )
                except Exception as exc:
                    results.append(StepResult("cleanup saved meal", False, 0, str(exc)))
                    print(f"FAIL cleanup saved meal: {exc}")
                try:
                    removed = await asyncio.to_thread(
                        probe.remove_rows_after_baseline, baseline_rows
                    )
                    if removed:
                        detail = f"directly removed {removed} row(s) after UI cleanup"
                        results.append(
                            StepResult("fallback Sheets cleanup", False, 0, detail)
                        )
                        print(f"FAIL fallback Sheets cleanup: {detail}")
                except Exception as exc:
                    results.append(
                        StepResult("fallback Sheets cleanup", False, 0, str(exc))
                    )
                    print(f"FAIL fallback Sheets cleanup: {exc}")
            await client.disconnect()

    if "probe" in locals():

        async def verify_cleanup() -> None:
            rows = await asyncio.to_thread(probe.rows)
            for sent, request in created_rows:
                row = probe.row_for_request(rows, sent, request)
                _require(row is None, f"test row {sent.id} remains in Sheets")
            _require(rows == baseline_rows, "test meal rows were not fully restored")
            saved_ids = {
                meal.saved_meal_id
                for meal in await asyncio.to_thread(probe.saved_meals)
            }
            _require(
                saved_ids == baseline_saved_ids,
                "saved-meal library was not fully restored",
            )
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
    print(
        "Plan: live Telegram journey, exact and malformed composites, "
        "save/change/delete/reuse actions, temporary goal change, cleanup."
    )
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
