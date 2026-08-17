from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import secrets
import shutil
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from . import __version__
from .analytics import BotStatistics
from .analyzer import (
    AnalysisError,
    Analyzer,
    InputFormatError,
    NormalizedInput,
    Transcriber,
    TranscriptionError,
    normalize_input,
)
from .meal_grouping import MAX_WEEKLY_MEAL_GROUPS, MealGrouper, MealGroupingError
from .models import (
    MAX_SAVED_MEAL_NAME_LENGTH,
    MAX_WEIGHT_G,
    CalculatedFoodItem,
    LLMMetadata,
    MealIconSuggestion,
    MealResult,
    NutritionSummary,
    RecentMeal,
    SavedMeal,
    StoredMeal,
    calculate_meal,
    format_simple_meal_request,
    item_calorie_total_estimated,
    item_nutrient_total_estimated,
    nutrition_summary,
    parse_simple_meal_request,
    round_whole,
    scale_meal,
)
from .saved_meals import (
    SavedMealsReadError,
    SavedMealStore,
    SavedMealsWriteError,
)
from .sheets import (
    DayMeal,
    MealDeletion,
    MealStore,
    PeriodMeal,
    SheetsReadError,
    SheetState,
    SheetsWriteError,
    SheetsWriteUncertainError,
    accounting_date,
)
from .users import (
    MAX_USER_DISPLAY_NAME_LENGTH,
    InviteUnavailableError,
    UserAlreadyRegisteredError,
    UserRecord,
    UserRegistry,
    UserRegistryError,
)

LOGGER = logging.getLogger(__name__)
SAVED_MEAL_ICON_CONFIDENCE = 0.8
DAILY_TOTAL_DELETE_BATCH_SIZE = 100

HELP_TEXT_FILE = Path(__file__).with_name("help.txt")
START_TEXT_FILE = Path(__file__).with_name("start.txt")
TIPS_TEXT_FILE = Path(__file__).with_name("tips.txt")
ADMIN_HELP_TEXT_FILE = Path(__file__).with_name("admin_help.txt")
START_FALLBACK_TEXT = "Напиши, що ти з’їв, — я порахую калорії та БЖВ."
HELP_FALLBACK_TEXT = "Напиши, що ти з’їв, або надішли фото. Команда: /day"
TIPS_FALLBACK_TEXT = "Використовуй цілі числа; до фото можна додати вагу."
ADMIN_HELP_FALLBACK_TEXT = "Команди адміністратора тимчасово недоступні в довідці."
NOT_FOOD_TEXT = (
    "Не вдалося зрозуміти, що саме було з’їдено. Спробуй описати страву інакше."
)
PHOTO_NOT_FOOD_TEXT = (
    "Не вдалося розпізнати страву. Спробуй описати її текстом або надіслати інше фото."
)
FORMAT_ERROR_TEXT = (
    "Не вдалося зрозуміти кількість.\n\nНапиши ціле число, наприклад:\nсир 150 г"
)
READ_ERROR_TEXT = "Не вдалося прочитати дані. Спробуй ще раз."
WRITE_ERROR_TEXT = (
    "Не вдалося зберегти запис. Нічого не зараховано — надішли повідомлення ще раз."
)
UNCERTAIN_WRITE_TEXT = (
    "Не вдалося підтвердити запис. Перевір /day перед повторним надсиланням."
)
ANALYSIS_ERROR_TEXT = "Не вдалося порахувати КБЖВ. Спробуй ще раз."
GOAL_ERROR_TEXT = "Не вдалося змінити денну ціль. Спробуй ще раз."
WEEK_ERROR_TEXT = "Не вдалося сформувати підсумок за 7 днів. Спробуй ще раз."
VOICE_ERROR_TEXT = "Не вдалося розпізнати голосове повідомлення. Спробуй ще раз."
LONG_OPERATION_TEXT = "⏳ Хвилинку, думаю. . ."
LLM_OPERATION_TEXT = "⏳ Хвилинку, раджусь з AI. . ."
DELETE_ERROR_TEXT = "Не вдалося видалити запис. Спробуй ще раз."
DELETE_CALLBACK_PREFIX = "delete:"
SAVE_CALLBACK_PREFIX = "save:"
ADMIN_DELETE_CALLBACK_PREFIX = "admin-delete:"
ADMIN_CANCEL_CALLBACK_PREFIX = "admin-cancel:"
GOAL_DISABLE_CALLBACK_PREFIX = "goal-disable:"
PROTEIN_GOAL_DISABLE_CALLBACK_PREFIX = "protein-goal-disable:"
GOAL_WAITING_KEY = "awaiting_daily_kcal_goal"
MEAL_WEIGHT_WAITING_KEY = "awaiting_meal_weight"
INVITE_WAITING_KEY = "awaiting_invite_name"
INVITE_ONLY_TEXT = "Доступ лише за запрошенням."
BLOCKED_TEXT = "Доступ до бота вимкнено."
ACCESS_ERROR_TEXT = "Не вдалося перевірити доступ. Спробуй ще раз."
ACTIVATION_ERROR_TEXT = "Не вдалося активувати доступ. Спробуй ще раз."
ADMIN_ERROR_TEXT = "Не вдалося виконати команду. Спробуй ще раз."
GARMIN_READ_ERROR_TEXT = (
    "Не вдалося прочитати локальні дані Garmin. "
    "Перевір журнал оновлення або спробуй після наступного оновлення доби."
)
WEEK_DAYS = 7
SUMMARY_EXPLANATION = "КБЖВ — у підсумку, біля страв — лише калорії."
UKRAINIAN_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


class GarminCalories(Protocol):
    def format_weekly_report(self) -> str: ...

    def get_daily_calories(self) -> dict[date, int]: ...


class NotFoodError(ValueError):
    """Raised when the message does not describe consumed food."""


class SavedMealNameError(ValueError):
    """Raised when an explicitly chosen saved-meal name is unavailable."""


class CompositeMealWeightError(ValueError):
    """Raised when weight editing is requested for a composite meal."""


class MealWeightUnchangedError(ValueError):
    """Raised when a meal already has the requested weight."""

    def __init__(self, weight_g: int) -> None:
        self.weight_g = weight_g
        super().__init__(f"Meal already weighs {weight_g} grams")


@dataclass(frozen=True)
class MealReply:
    text: str
    telegram_message_id: int
    accounting_day: date
    can_save: bool = True
    can_change_weight: bool = True
    daily_total_text: str | None = None


def _load_content(path: Path, fallback: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        LOGGER.exception("Could not read bot content file: %s", path)
        return fallback
    if not text:
        LOGGER.error("Bot content file is empty: %s", path)
        return fallback
    return text


def load_start_text() -> str:
    return _load_content(START_TEXT_FILE, START_FALLBACK_TEXT)


def load_tips_text() -> str:
    return _load_content(TIPS_TEXT_FILE, TIPS_FALLBACK_TEXT)


def load_help_text(*, admin: bool = False) -> str:
    text = _load_content(HELP_TEXT_FILE, HELP_FALLBACK_TEXT)
    if admin:
        admin_text = _load_content(ADMIN_HELP_TEXT_FILE, ADMIN_HELP_FALLBACK_TEXT)
        return f"{text}\n\n{admin_text}"
    return text


def _split_telegram_text(text: str, limit: int = 4000) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in text.splitlines():
        added = len(line) + (1 if current else 0)
        if current and current_length + added > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if current_length else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _highlight_calories(value: str) -> str:
    return f"<b><u>{value}</u></b>"


def _format_nutrition_value(value: float | None, estimated: bool = False) -> str:
    if value is None:
        return "—"
    prefix = "≈" if estimated else ""
    return f"{prefix}{round_whole(value)}"


def _format_nutrition(summary: NutritionSummary) -> str:
    return (
        f"К:{_format_nutrition_value(summary.kcal, summary.kcal_estimated)} "
        f"Б:{_format_nutrition_value(summary.protein_g, summary.protein_estimated)} "
        f"Ж:{_format_nutrition_value(summary.fat_g, summary.fat_estimated)} "
        f"В:{_format_nutrition_value(summary.carbs_g, summary.carbs_estimated)}"
    )


def _summary_for_item(item: CalculatedFoodItem) -> NutritionSummary:
    return NutritionSummary(
        kcal=item.calories,
        protein_g=item.protein_g,
        fat_g=item.fat_g,
        carbs_g=item.carbs_g,
        kcal_estimated=item_calorie_total_estimated(item),
        protein_estimated=item_nutrient_total_estimated(item, "protein"),
        fat_estimated=item_nutrient_total_estimated(item, "fat"),
        carbs_estimated=item_nutrient_total_estimated(item, "carbs"),
    )


def _per_100g_summary(item: CalculatedFoodItem) -> NutritionSummary:
    return NutritionSummary(
        kcal=item.kcal_per_100g,
        protein_g=item.protein_per_100g,
        fat_g=item.fat_per_100g,
        carbs_g=item.carbs_per_100g,
        kcal_estimated=item.kcal_estimated,
        protein_estimated=(
            item.protein_per_100g is not None and item.protein_estimated
        ),
        fat_estimated=item.fat_per_100g is not None and item.fat_estimated,
        carbs_estimated=item.carbs_per_100g is not None and item.carbs_estimated,
    )


def _portion_includes_weight(portion: str, weight_g: float) -> bool:
    weights = re.finditer(
        r"(?<!\d)(?P<value>\d+(?:[.,]\d+)?)\s*"
        r"(?:г|гр|грам|грами|грама|грамів)\b",
        portion,
        flags=re.IGNORECASE,
    )
    return any(
        abs(float(match.group("value").replace(",", ".")) - weight_g) < 0.5
        for match in weights
    )


def _format_item_calculation(
    item: CalculatedFoodItem, *, highlight_total: bool = True
) -> str:
    display_name = item.name[:1].upper() + item.name[1:]
    weight_prefix = "≈" if item.weight_origin != "user_text" else ""
    heading = html.escape(display_name)
    if item.portion_display:
        heading += f" {html.escape(item.portion_display)}"
    if not item.portion_display or not _portion_includes_weight(
        item.portion_display, item.weight_g
    ):
        separator = " · " if item.portion_display else " "
        heading += f"{separator}{weight_prefix}{round_whole(item.weight_g)} г"
    total = _format_nutrition(_summary_for_item(item))
    if highlight_total:
        total = _highlight_calories(total)
    return (
        f"<b>{heading}</b>\n"
        f"{total}\n"
        f"На 100 г: {_format_nutrition(_per_100g_summary(item))}"
    )


def _as_summary(value: float | NutritionSummary) -> NutritionSummary:
    if isinstance(value, NutritionSummary):
        return value
    return NutritionSummary.unknown_macros(float(value))


def _state_nutrition(state: SheetState) -> NutritionSummary:
    nutrition = state.today_nutrition
    if isinstance(nutrition, NutritionSummary):
        return nutrition
    total = float(state.today_total)
    return NutritionSummary() if total == 0 else NutritionSummary.unknown_macros(total)


def _format_progress_value(
    value: float | None,
    goal: int | None,
    *,
    emoji: str,
    label: str = "",
    unit: str,
) -> str:
    prefix = f"{emoji} {label}" if label else f"{emoji} "
    if value is None:
        return f"{prefix}— {unit}"
    rounded = round_whole(value)
    if goal is None:
        return f"{prefix}{rounded} {unit}"
    return f"{prefix}{rounded} / {goal} {unit}  {rounded - goal:+d}"


def _daily_progress_lines(
    summary: NutritionSummary,
    daily_kcal_goal: int | None,
    daily_protein_goal: int | None,
) -> list[str]:
    return [
        _format_progress_value(summary.kcal, daily_kcal_goal, emoji="🔥", unit="ккал"),
        _format_progress_value(
            summary.protein_g,
            daily_protein_goal,
            emoji="🥩",
            label="Б ",
            unit="г",
        ),
        _format_progress_value(summary.fat_g, None, emoji="🥑", label="Ж ", unit="г"),
        _format_progress_value(summary.carbs_g, None, emoji="🍞", label="В ", unit="г"),
    ]


def format_daily_total(
    today_total: float | NutritionSummary,
    daily_kcal_goal: int | None,
    daily_protein_goal: int | None = None,
) -> str:
    summary = _as_summary(today_total)
    return "\n".join(
        _daily_progress_lines(summary, daily_kcal_goal, daily_protein_goal)
    )


def format_reply(meal: MealResult) -> str:
    if len(meal.items) > 1:
        meal_name = html.escape(meal.meal_name[:1].upper() + meal.meal_name[1:])
        calculations = [
            _format_item_calculation(item, highlight_total=False) for item in meal.items
        ]
        body = [
            f"<b>{meal_name} {round_whole(meal.total_weight_g)} г</b>",
            _highlight_calories(_format_nutrition(nutrition_summary(meal))),
            *(
                f"• {calculation.replace(chr(10), chr(10) + '  ')}"
                for calculation in calculations
            ),
        ]
    else:
        body = [_format_item_calculation(meal.items[0])]
    return "\n".join(body)


def format_weekly_calories_reply(
    end_day: date,
    totals: Mapping[date, float | NutritionSummary],
    burned_totals: dict[date, int] | None = None,
) -> str:
    start_day = end_day - timedelta(days=WEEK_DAYS - 1)
    days = [start_day + timedelta(days=offset) for offset in range(WEEK_DAYS)]
    consumed = {
        day: (_as_summary(totals[day]) if day in totals else NutritionSummary())
        for day in days
    }
    if burned_totals is not None:
        missing = [day for day in days if day not in burned_totals]
        if missing:
            raise ValueError("Burned-calorie data does not cover the completed week")

    legend = "+спожито   −витрачено   =баланс"
    lines = [
        "КБЖВ за тиждень",
        legend,
        "КБЖВ — у підсумку, за днями — лише калорії.",
        "",
    ]
    for day in days:
        prefix = f"• {UKRAINIAN_WEEKDAYS[day.weekday()]} {day:%d.%m}: "
        consumed_kcal = round_whole(consumed[day].kcal)
        consumed_text = f"К:{consumed_kcal:+d}"
        if burned_totals is None:
            lines.append(prefix + consumed_text)
            continue
        burned = burned_totals[day]
        balance = consumed_kcal - burned
        lines.append(prefix + f"{consumed_text} {-burned:+d} ={balance:+d}")

    total = NutritionSummary()
    for day in days:
        total += consumed[day]

    def average_value(value: float | None) -> float | None:
        return None if value is None else value / WEEK_DAYS

    average_summary = NutritionSummary(
        kcal=total.kcal / WEEK_DAYS,
        protein_g=average_value(total.protein_g),
        fat_g=average_value(total.fat_g),
        carbs_g=average_value(total.carbs_g),
        kcal_estimated=total.kcal_estimated,
        protein_estimated=total.protein_estimated,
        fat_estimated=total.fat_estimated,
        carbs_estimated=total.carbs_estimated,
    )
    average_consumed = round_whole(average_summary.kcal)
    average = f"Середнє за день: {_format_nutrition(average_summary)}"
    if burned_totals is not None:
        average_burned = round_whole(
            sum(burned_totals[day] for day in days) / WEEK_DAYS
        )
        average_balance = average_consumed - average_burned
        average += f" · витрачено {-average_burned:+d} · баланс ={average_balance:+d}"
    lines.extend(("", f"Разом: {_format_nutrition(total)}", average))
    return "\n".join(lines)


def _aggregate_weekly_meals(
    meals: list[PeriodMeal],
    group_names: tuple[str, ...] | None = None,
    *,
    collapse_tail: bool = True,
) -> list[tuple[str, float, NutritionSummary]]:
    if group_names is not None and len(group_names) != len(meals):
        raise ValueError("Meal group count does not match meal count")
    if not meals:
        return []

    grouped: dict[str, tuple[str, float, NutritionSummary]] = {}
    for index, meal in enumerate(meals):
        source_name = group_names[index] if group_names is not None else meal.meal_name
        display_name = re.sub(r"\s+", " ", source_name).strip()
        key = display_name.casefold()
        meal_nutrition = meal.nutrition or NutritionSummary.unknown_macros(
            meal.meal_kcal
        )
        if key in grouped:
            original_name, weight, nutrients = grouped[key]
            grouped[key] = (
                original_name,
                weight + meal.total_weight_g,
                nutrients + meal_nutrition,
            )
        else:
            grouped[key] = (display_name, meal.total_weight_g, meal_nutrition)

    aggregated = sorted(
        grouped.values(), key=lambda item: (-item[2].kcal, item[0].casefold())
    )
    if collapse_tail and len(aggregated) > MAX_WEEKLY_MEAL_GROUPS:
        head = aggregated[: MAX_WEEKLY_MEAL_GROUPS - 1]
        tail = aggregated[MAX_WEEKLY_MEAL_GROUPS - 1 :]
        head.append(
            (
                f"Інше ({len(tail)} категорій)",
                sum(item[1] for item in tail),
                sum((item[2] for item in tail), NutritionSummary()),
            )
        )
        aggregated = head
    return aggregated


def format_weekly_meals_reply(
    meals: list[PeriodMeal], group_names: tuple[str, ...] | None = None
) -> str:
    total = sum(
        (
            meal.nutrition or NutritionSummary.unknown_macros(meal.meal_kcal)
            for meal in meals
        ),
        NutritionSummary(),
    )
    average = NutritionSummary(
        kcal=total.kcal / WEEK_DAYS,
        protein_g=None if total.protein_g is None else total.protein_g / WEEK_DAYS,
        fat_g=None if total.fat_g is None else total.fat_g / WEEK_DAYS,
        carbs_g=None if total.carbs_g is None else total.carbs_g / WEEK_DAYS,
        kcal_estimated=total.kcal_estimated,
        protein_estimated=total.protein_estimated,
        fat_estimated=total.fat_estimated,
        carbs_estimated=total.carbs_estimated,
    )
    summary = (
        f"Всього: {_format_nutrition(total)}\n"
        f"Середнє за день: {_format_nutrition(average)}"
    )
    if not meals:
        return f"Страви за тиждень\n\nЗаписів немає.\n\n{summary}"

    aggregated = _aggregate_weekly_meals(meals, group_names)
    lines = ["Страви за тиждень", SUMMARY_EXPLANATION, ""]
    for meal_name, total_weight_g, nutrients in aggregated:
        display_name = meal_name[:1].upper() + meal_name[1:]
        lines.append(
            f"• <b>{html.escape(display_name)}</b> {round_whole(total_weight_g)} г — "
            f"К:{_format_nutrition_value(nutrients.kcal, nutrients.kcal_estimated)}"
        )
    lines.extend(("", summary))
    return "\n".join(lines)


def format_users_reply(users: list[UserRecord]) -> str:
    if not users:
        return "Користувачів ще немає."

    status_labels = {
        "invited": "запрошений",
        "active": "активний",
        "blocked": "заблокований",
    }
    lines = [f"Користувачі ({len(users)}):"]
    for user in users:
        name = re.sub(r"\s+", " ", user.display_name).strip() or "Без імені"
        identity = (
            f"ID {user.telegram_user_id}"
            if user.telegram_user_id is not None
            else "ще не активований"
        )
        if user.telegram_username:
            identity += f" (@{user.telegram_username.lstrip('@')})"
        lines.append(f"• {name} — {status_labels[user.status]} — {identity}")
    return "\n".join(lines)


def format_day_reply(
    meals: list[DayMeal],
    daily_kcal_goal: int | None = None,
    daily_protein_goal: int | None = None,
) -> str:
    total = sum(
        (
            meal.nutrition or NutritionSummary.unknown_macros(meal.meal_kcal)
            for meal in meals
        ),
        NutritionSummary(),
    )
    grouped: dict[str, tuple[str, NutritionSummary, int]] = {}
    for meal in meals:
        display_name = re.sub(r"\s+", " ", meal.meal_name).strip()
        key = display_name.casefold()
        nutrients = meal.nutrition or NutritionSummary.unknown_macros(meal.meal_kcal)
        if key in grouped:
            original_name, current, count = grouped[key]
            grouped[key] = (original_name, current + nutrients, count + 1)
        else:
            grouped[key] = (display_name, nutrients, 1)

    specs = (
        ("kcal", "🔥"),
        ("protein_g", "🥩"),
        ("fat_g", "🥑"),
        ("carbs_g", "🍞"),
    )
    summary_lines = _daily_progress_lines(total, daily_kcal_goal, daily_protein_goal)
    blocks: list[str] = []
    for summary_line, (attribute, emoji) in zip(summary_lines, specs, strict=True):
        contributions: list[tuple[float, str, int]] = []
        for meal_name, nutrients, count in grouped.values():
            value = getattr(nutrients, attribute)
            if value is not None and value > 0:
                contributions.append((value, meal_name, count))
        contributions.sort(key=lambda item: item[0], reverse=True)
        details = []
        for value, meal_name, count in contributions:
            count_suffix = f" ×{count}" if count > 1 else ""
            details.append(
                f"<li>{html.escape(meal_name)}{count_suffix}  "
                f"{emoji}{round_whole(value)}</li>"
            )
        body = f"<ul>{''.join(details)}</ul>" if details else "<p>Немає внесків</p>"
        blocks.append(f"<details><summary>{summary_line}</summary>{body}</details>")
    return "".join(blocks)


class CaloriesService:
    def __init__(
        self,
        analyzer: Analyzer,
        store: MealStore,
        timezone: ZoneInfo,
        day_start_time: time,
        photo_storage_dir: Path,
        daily_kcal_goal: int | None = None,
        saved_store: SavedMealStore | None = None,
        daily_protein_goal: int | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._store = store
        self._timezone = timezone
        self._day_start_time = day_start_time
        self._photo_storage_dir = photo_storage_dir.resolve()
        self._photo_storage_dir.mkdir(parents=True, exist_ok=True)
        self._daily_kcal_goal = daily_kcal_goal
        self._daily_protein_goal = daily_protein_goal
        self._saved_store = saved_store
        # A message analysis can take long enough for a deletion callback to be
        # handled in between its first read and the eventual append.  Keep the
        # read/append and deletion operations mutually exclusive, while doing
        # the expensive analysis outside the lock.
        self._store_lock = threading.RLock()

    def _local_timestamp(self, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=self._timezone)
        return timestamp.astimezone(self._timezone)

    def _accounting_day(self, timestamp: datetime) -> date:
        return accounting_date(
            self._local_timestamp(timestamp),
            self._timezone,
            self._day_start_time,
        )

    def set_daily_kcal_goal(self, goal: int | None) -> None:
        with self._store_lock:
            self._daily_kcal_goal = goal

    def set_daily_protein_goal(self, goal: int | None) -> None:
        with self._store_lock:
            self._daily_protein_goal = goal

    def get_day_summary(self, timestamp: datetime) -> str:
        day = self._accounting_day(timestamp)
        return format_day_reply(
            self._store.get_day_meals(day),
            self._daily_kcal_goal,
            self._daily_protein_goal,
        )

    def get_weekly_calories(
        self,
        timestamp: datetime,
        burned_totals: dict[date, int] | None = None,
    ) -> str:
        end_day = self._accounting_day(timestamp) - timedelta(days=1)
        start_day = end_day - timedelta(days=WEEK_DAYS - 1)
        totals = self._store.get_daily_totals(start_day, end_day)
        return format_weekly_calories_reply(end_day, totals, burned_totals)

    def get_weekly_meals(
        self, timestamp: datetime, meal_grouper: MealGrouper | None = None
    ) -> str:
        end_day = self._accounting_day(timestamp) - timedelta(days=1)
        start_day = end_day - timedelta(days=WEEK_DAYS - 1)
        meals = self._store.get_period_meals(start_day, end_day)
        if not meals or meal_grouper is None:
            return format_weekly_meals_reply(meals)

        exact = _aggregate_weekly_meals(meals, collapse_tail=False)
        exact_meals = [
            PeriodMeal(name, weight, nutrients.kcal, nutrients)
            for name, weight, nutrients in exact
        ]
        try:
            grouping = meal_grouper.group(tuple(meal.meal_name for meal in exact_meals))
            return format_weekly_meals_reply(exact_meals, grouping.group_names)
        except MealGroupingError:
            LOGGER.exception("Could not semantically group weekly meals")
            return format_weekly_meals_reply(exact_meals)

    @staticmethod
    def _component_message_id(source_message_id: int, component_index: int) -> int:
        if component_index == 0:
            return source_message_id
        raw = hashlib.sha256(
            f"meal-component:{source_message_id}:{component_index}".encode()
        ).digest()
        value = int.from_bytes(raw[:7], "big") & ((1 << 52) - 1)
        return -(value or component_index)

    @staticmethod
    def _single_item_meals(meal: MealResult) -> list[MealResult]:
        return [
            MealResult(
                meal_name=item.name,
                items=[item],
                total_weight_g=item.weight_g,
                kcal_per_100g=item.kcal_per_100g,
                meal_kcal=item.calories,
                protein_per_100g=item.protein_per_100g,
                fat_per_100g=item.fat_per_100g,
                carbs_per_100g=item.carbs_per_100g,
                protein_g=item.protein_g,
                fat_g=item.fat_g,
                carbs_g=item.carbs_g,
                estimated=(
                    item.weight_estimated
                    or item.kcal_estimated
                    or item.protein_estimated
                    or item.fat_estimated
                    or item.carbs_estimated
                ),
            )
            for item in meal.items
        ]

    @staticmethod
    def _reply_collection(replies: list[MealReply]) -> MealReply | list[MealReply]:
        return replies[0] if len(replies) == 1 else replies

    def _existing_component_replies(
        self,
        source_message_id: int,
        day: date,
        today_nutrition: NutritionSummary,
        first: StoredMeal,
    ) -> MealReply | list[MealReply] | None:
        marker = parse_simple_meal_request(first.normalized_request)
        if (
            marker is None
            or marker.kind != "analysis"
            or marker.source_message_id != source_message_id
        ):
            return MealReply(
                text=format_reply(first.meal),
                telegram_message_id=source_message_id,
                accounting_day=day,
                can_change_weight=len(first.meal.items) == 1,
                daily_total_text=format_daily_total(
                    today_nutrition,
                    self._daily_kcal_goal,
                    self._daily_protein_goal,
                ),
            )
        components = self._store.get_component_meals(day, source_message_id)
        if len(components) != marker.component_count:
            return None
        replies = []
        for index, (message_id, stored) in enumerate(components):
            replies.append(
                MealReply(
                    text=format_reply(stored.meal),
                    telegram_message_id=message_id,
                    accounting_day=day,
                    daily_total_text=(
                        format_daily_total(
                            today_nutrition,
                            self._daily_kcal_goal,
                            self._daily_protein_goal,
                        )
                        if index == len(components) - 1
                        else None
                    ),
                )
            )
        return self._reply_collection(replies)

    def get_existing_reply(
        self, telegram_message_id: int, timestamp: datetime
    ) -> MealReply | list[MealReply] | None:
        day = self._accounting_day(timestamp)
        with self._store_lock:
            state = self._store.get_state(day, telegram_message_id)
        if state.existing is None:
            return None
        return self._existing_component_replies(
            telegram_message_id,
            day,
            _state_nutrition(state),
            state.existing,
        )

    def process_message(
        self,
        text: str,
        telegram_message_id: int,
        timestamp: datetime,
        image_bytes: bytes | None = None,
    ) -> MealReply | list[MealReply]:
        timestamp = self._local_timestamp(timestamp)
        day = self._accounting_day(timestamp)
        with self._store_lock:
            state = self._store.get_state(day, telegram_message_id)
            if state.existing is not None:
                existing_reply = self._existing_component_replies(
                    telegram_message_id,
                    day,
                    _state_nutrition(state),
                    state.existing,
                )
                if existing_reply is not None:
                    return existing_reply

        normalized = (
            normalize_input(text)
            if text.strip()
            else NormalizedInput(text="", explicit_values=())
        )
        if image_bytes is None and not normalized.text:
            raise InputFormatError("Message is empty")

        result = self._analyzer.analyze(normalized, image_bytes)
        if not result.analysis.is_food:
            raise NotFoodError
        meals = self._single_item_meals(calculate_meal(result.analysis))
        with self._store_lock:
            # Refresh after analysis: a deletion may have completed while the
            # model was working, so the earlier daily total is no longer valid.
            state = self._store.get_state(day, telegram_message_id)
            if state.existing is not None:
                existing_reply = self._existing_component_replies(
                    telegram_message_id,
                    day,
                    _state_nutrition(state),
                    state.existing,
                )
                if existing_reply is not None:
                    return existing_reply

            photo_path: str | None = None
            if image_bytes is not None:
                photo_file = (
                    self._photo_storage_dir
                    / f"{day.isoformat()}-{telegram_message_id}.jpg"
                )
                photo_file.write_bytes(image_bytes)
                photo_path = str(photo_file)
            existing_components: dict[int, tuple[int, StoredMeal]] = {}
            if state.existing is not None:
                for message_id, stored in self._store.get_component_meals(
                    day, telegram_message_id
                ):
                    marker = parse_simple_meal_request(stored.normalized_request)
                    if marker is not None:
                        existing_components[marker.component_index] = (
                            message_id,
                            stored,
                        )
            stored_components: list[tuple[int, StoredMeal]] = []
            today_nutrition = _state_nutrition(state)
            try:
                for index, meal in enumerate(meals):
                    existing = existing_components.get(index)
                    if existing is not None:
                        stored_components.append(existing)
                        continue
                    component_message_id = self._component_message_id(
                        telegram_message_id, index
                    )
                    stored = self._store.append_meal(
                        timestamp,
                        component_message_id,
                        text,
                        format_simple_meal_request(
                            telegram_message_id,
                            index,
                            len(meals),
                            "analysis",
                            normalized.text,
                        ),
                        photo_path,
                        meal,
                        (
                            result.metadata
                            if index == 0
                            else LLMMetadata(
                                model=result.metadata.model,
                                effort=result.metadata.effort,
                            )
                        ),
                    )
                    stored_components.append((component_message_id, stored))
                    today_nutrition += nutrition_summary(stored.meal)
            except SheetsWriteUncertainError:
                raise
            except SheetsWriteError as exc:
                if photo_path is not None and not stored_components:
                    self._delete_photo(photo_path)
                if stored_components:
                    raise SheetsWriteUncertainError(
                        "Only part of the component meal was stored"
                    ) from exc
                raise
            replies = []
            for index, (component_message_id, stored) in enumerate(stored_components):
                replies.append(
                    MealReply(
                        text=format_reply(stored.meal),
                        telegram_message_id=component_message_id,
                        accounting_day=day,
                        daily_total_text=(
                            format_daily_total(
                                today_nutrition,
                                self._daily_kcal_goal,
                                self._daily_protein_goal,
                            )
                            if index == len(stored_components) - 1
                            else None
                        ),
                    )
                )
            return self._reply_collection(replies)

    def _saved(self) -> SavedMealStore:
        if self._saved_store is None:
            raise SavedMealsReadError("Saved-meal store is unavailable")
        return self._saved_store

    @staticmethod
    def _normalize_saved_name(name: str) -> str:
        normalized = " ".join(name.split())
        if not normalized or len(normalized) > MAX_SAVED_MEAL_NAME_LENGTH:
            raise SavedMealNameError(
                f"Назва має містити від 1 до {MAX_SAVED_MEAL_NAME_LENGTH} символів."
            )
        return normalized

    def list_saved_meals(self) -> list[SavedMeal]:
        with self._store_lock:
            return self._saved().list_meals()

    def get_saved_meal(self, saved_meal_id: str) -> SavedMeal | None:
        with self._store_lock:
            return self._saved().get(saved_meal_id)

    def list_recent_meals(self) -> list[RecentMeal]:
        with self._store_lock:
            return self._store.get_recent_meals(8)

    def get_recent_meal(self, message_id: int, day: date) -> RecentMeal | None:
        with self._store_lock:
            stored = self._store.get_meal(day, message_id)
        if stored is None:
            return None
        return RecentMeal(
            telegram_message_id=message_id,
            day=day,
            meal=stored.meal,
            normalized_request=stored.normalized_request,
        )

    def save_source_meal(
        self,
        message_id: int,
        day: date,
        name: str | None = None,
    ) -> tuple[SavedMeal | None, bool]:
        explicit_name = name is not None
        with self._store_lock:
            saved_store = self._saved()
            existing = saved_store.find_by_source(message_id)
            if existing is not None:
                return existing, False
            source = self._store.get_meal(day, message_id)
            if source is None:
                return None, False
            marker = parse_simple_meal_request(source.normalized_request)
            if marker is None:
                return None, False
            if marker.kind == "saved":
                source_id = marker.payload.split(":", maxsplit=1)[0]
                return saved_store.get(source_id), False
            meals = saved_store.list_meals()
            used_names = {meal.display_name.casefold() for meal in meals}
            if explicit_name:
                base_name = self._normalize_saved_name(name or "")
            else:
                base_name = " ".join(source.meal.meal_name.split())[
                    :MAX_SAVED_MEAL_NAME_LENGTH
                ]
                if not base_name:
                    raise SavedMealNameError("Не вдалося визначити назву страви.")
            display_name = base_name
            if display_name.casefold() in used_names:
                if explicit_name:
                    raise SavedMealNameError(
                        "Страва з такою назвою вже є. Вибери іншу назву."
                    )
                suffix = 2
                while True:
                    candidate = f"{base_name} ({suffix})"
                    if len(candidate) > MAX_SAVED_MEAL_NAME_LENGTH:
                        prefix_length = (
                            MAX_SAVED_MEAL_NAME_LENGTH - len(str(suffix)) - 3
                        )
                        candidate = f"{base_name[:prefix_length]} ({suffix})"
                    if candidate.casefold() not in used_names:
                        display_name = candidate
                        break
                    suffix += 1
            for _ in range(5):
                saved_meal_id = secrets.token_urlsafe(8)
                if saved_store.get(saved_meal_id) is None:
                    break
            else:
                raise SavedMealsWriteError("Could not generate a saved-meal ID")
            icon = self._suggest_saved_meal_icon(source.meal)
            saved = SavedMeal(
                saved_meal_id=saved_meal_id,
                source_message_id=message_id,
                display_name=display_name,
                default_total_weight_g=round_whole(source.meal.total_weight_g),
                base_meal=source.meal,
                icon=icon,
            )
            return saved_store.append(saved), True

    def _suggest_saved_meal_icon(self, meal: MealResult) -> str | None:
        try:
            suggestion = self._analyzer.suggest_meal_icon(meal)
        except Exception:
            LOGGER.warning("Could not generate a saved-meal icon", exc_info=True)
            return None
        if not isinstance(suggestion, MealIconSuggestion):
            return None
        if suggestion.confidence < SAVED_MEAL_ICON_CONFIDENCE:
            return None
        return suggestion.emoji

    def save_latest_meal(
        self, name: str | None = None
    ) -> tuple[SavedMeal | None, bool]:
        with self._store_lock:
            latest = self._store.get_latest_meal()
        if latest is None:
            return None, False
        return self.save_source_meal(latest.telegram_message_id, latest.day, name)

    def _append_reused_meal(
        self,
        meal: MealResult,
        event_id: int,
        timestamp: datetime,
        request_kind: str,
        request_payload: str,
        metadata_model: str,
        *,
        can_save: bool,
    ) -> MealReply:
        timestamp = self._local_timestamp(timestamp)
        day = self._accounting_day(timestamp)
        with self._store_lock:
            state = self._store.get_state(day, event_id)
            if state.existing is None:
                stored = self._store.append_meal(
                    timestamp,
                    event_id,
                    "",
                    format_simple_meal_request(
                        event_id, 0, 1, request_kind, request_payload
                    ),
                    None,
                    meal,
                    LLMMetadata(model=metadata_model, effort="none"),
                )
                total = _state_nutrition(state) + nutrition_summary(stored.meal)
            else:
                stored = state.existing
                total = _state_nutrition(state)
        return MealReply(
            text=format_reply(stored.meal),
            telegram_message_id=event_id,
            accounting_day=day,
            can_save=can_save,
            can_change_weight=len(stored.meal.items) == 1,
            daily_total_text=format_daily_total(
                total, self._daily_kcal_goal, self._daily_protein_goal
            ),
        )

    def add_saved_meal(
        self,
        saved_meal_id: str,
        weight_g: int,
        event_id: int,
        timestamp: datetime,
    ) -> MealReply | None:
        with self._store_lock:
            saved = self._saved().get(saved_meal_id)
        if saved is None:
            return None
        meal = scale_meal(saved.base_meal, weight_g, meal_name=saved.display_name)
        return self._append_reused_meal(
            meal,
            event_id,
            timestamp,
            "saved",
            f"{saved_meal_id}:{weight_g}g",
            "saved_meal",
            can_save=False,
        )

    def add_recent_meal(
        self,
        message_id: int,
        source_day: date,
        weight_g: int,
        event_id: int,
        timestamp: datetime,
    ) -> MealReply | None:
        recent = self.get_recent_meal(message_id, source_day)
        if recent is None:
            return None
        with self._store_lock:
            already_saved = self._saved().find_by_source(message_id) is not None
        meal = scale_meal(recent.meal, weight_g)
        return self._append_reused_meal(
            meal,
            event_id,
            timestamp,
            "recent",
            f"{message_id}:{weight_g}g",
            "recent_meal",
            can_save=(
                not already_saved
                and (
                    (marker := parse_simple_meal_request(recent.normalized_request))
                    is not None
                    and marker.kind != "saved"
                )
            ),
        )

    def change_meal_weight(
        self, message_id: int, day: date, weight_g: int
    ) -> MealReply | None:
        with self._store_lock:
            source = self._store.get_meal(day, message_id)
            if source is None:
                return None
            if len(source.meal.items) != 1:
                raise CompositeMealWeightError
            if round_whole(source.meal.total_weight_g) == weight_g:
                raise MealWeightUnchangedError(weight_g)
            updated = self._store.update_meal(
                day, message_id, scale_meal(source.meal, weight_g)
            )
            if updated is None:
                return None
            marker = parse_simple_meal_request(source.normalized_request)
            can_save = (
                marker is not None
                and marker.kind != "saved"
                and self._saved().find_by_source(message_id) is None
            )
        return MealReply(
            text=format_reply(updated.meal.meal),
            telegram_message_id=message_id,
            accounting_day=updated.accounting_day,
            can_save=can_save,
            can_change_weight=True,
            daily_total_text=format_daily_total(
                updated.day_nutrition
                or NutritionSummary.unknown_macros(updated.day_total),
                self._daily_kcal_goal,
                self._daily_protein_goal,
            ),
        )

    def delete_saved_meal(self, saved_meal_id: str) -> bool:
        with self._store_lock:
            return self._saved().delete(saved_meal_id)

    def delete_message(
        self, telegram_message_id: int, fallback_day: date
    ) -> MealDeletion:
        with self._store_lock:
            deletion = self._store.delete_meal(telegram_message_id, fallback_day)
        if deletion.photo_path:
            self._delete_photo(deletion.photo_path)
        elif not deletion.deleted:
            self._delete_photo(
                str(
                    self._photo_storage_dir
                    / f"{fallback_day.isoformat()}-{telegram_message_id}.jpg"
                )
            )
        return deletion

    def format_deletion_reply(self, deletion: MealDeletion) -> str:
        if not deletion.deleted:
            return "Цей запис уже видалено"
        if not deletion.meal_name:
            return "Видалено страву"
        meal_name = deletion.meal_name
        display_name = meal_name[:1].upper() + meal_name[1:]
        return f"Видалено {html.escape(display_name)}"

    def format_deletion_daily_total(self, deletion: MealDeletion) -> str:
        nutrition = deletion.day_nutrition or NutritionSummary.unknown_macros(
            deletion.day_total
        )
        daily_total = format_daily_total(
            nutrition, self._daily_kcal_goal, self._daily_protein_goal
        )
        return f"Оновлено після видалення\n{daily_total}"

    def _delete_photo(self, photo_path: str) -> None:
        try:
            candidate = Path(photo_path).resolve()
            if not candidate.is_relative_to(self._photo_storage_dir):
                LOGGER.warning(
                    "Refusing to delete photo outside storage: %s", candidate
                )
                return
            candidate.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Could not delete stored meal photo: %s", photo_path)


class Workspace(Protocol):
    def open_meal_store(
        self, spreadsheet_id: str, day_start: time, telegram_user_id: int
    ) -> MealStore: ...

    def open_saved_meal_store(self, spreadsheet_id: str) -> SavedMealStore: ...

    def create_personal_spreadsheet(
        self, title: str, day_start: time, telegram_user_id: int
    ) -> str: ...

    def delete_personal_spreadsheet(self, spreadsheet_id: str) -> None: ...


class UserManager:
    def __init__(
        self,
        analyzer: Analyzer,
        registry: UserRegistry,
        workspace: Workspace,
        timezone: ZoneInfo,
        default_day_start: time,
        photo_storage_dir: Path,
    ) -> None:
        self._analyzer = analyzer
        self._registry = registry
        self._workspace = workspace
        self._timezone = timezone
        self._default_day_start = default_day_start
        self._photo_storage_dir = photo_storage_dir.resolve()
        self._photo_storage_dir.mkdir(parents=True, exist_ok=True)
        self._services: dict[tuple[int, str, time], CaloriesService] = {}
        self._lock = threading.RLock()

    def prepare_release_storage(self) -> None:
        """Apply idempotent per-user storage upgrades before polling starts."""
        for user in self._registry.list_users():
            if not user.spreadsheet_id:
                continue
            try:
                self._workspace.open_saved_meal_store(user.spreadsheet_id)
            except Exception:
                LOGGER.exception(
                    "Could not prepare saved-meal storage for user %s",
                    user.telegram_user_id,
                )

    def get_user(self, telegram_user_id: int) -> UserRecord | None:
        return self._registry.get_user(telegram_user_id)

    def list_users(self) -> list[UserRecord]:
        return self._registry.list_users()

    def service_for(self, user: UserRecord) -> CaloriesService:
        if (
            user.status != "active"
            or user.telegram_user_id is None
            or not user.spreadsheet_id
        ):
            raise UserRegistryError("Active user context is incomplete")
        key = (user.telegram_user_id, user.spreadsheet_id, user.day_start)
        with self._lock:
            service = self._services.get(key)
            if service is None:
                store = self._workspace.open_meal_store(
                    user.spreadsheet_id,
                    user.day_start,
                    user.telegram_user_id,
                )
                saved_store = self._workspace.open_saved_meal_store(user.spreadsheet_id)
                service = CaloriesService(
                    self._analyzer,
                    store,
                    self._timezone,
                    user.day_start,
                    self._photo_storage_dir / str(user.telegram_user_id),
                    user.daily_kcal_goal,
                    saved_store,
                    user.daily_protein_goal,
                )
                self._services[key] = service
            else:
                service.set_daily_kcal_goal(user.daily_kcal_goal)
                service.set_daily_protein_goal(user.daily_protein_goal)
            return service

    def create_invite(self, display_name: str) -> str:
        token = secrets.token_urlsafe(24)
        self._registry.create_invite(display_name, token, self._default_day_start)
        return token

    def activate(
        self, token: str, telegram_user_id: int, telegram_username: str
    ) -> UserRecord:
        with self._lock:
            existing = self._registry.get_user(telegram_user_id)
            if existing is not None:
                if existing.status == "active":
                    return existing
                raise UserAlreadyRegisteredError("Blocked user cannot activate invite")
            invite = self._registry.get_invite(token)
            if invite is None:
                raise InviteUnavailableError("Invite is not available")
            if (
                invite.telegram_user_id is not None
                and invite.telegram_user_id != telegram_user_id
            ):
                raise InviteUnavailableError("Invite belongs to another user")
            spreadsheet_id = invite.spreadsheet_id
            if not spreadsheet_id:
                title = f"{invite.display_name} — {telegram_user_id}"
                spreadsheet_id = self._workspace.create_personal_spreadsheet(
                    title, invite.day_start, telegram_user_id
                )
            prepared = self._registry.prepare_activation(
                invite,
                telegram_user_id,
                telegram_username,
                spreadsheet_id,
            )
            return self._registry.complete_activation(prepared)

    def set_status(self, telegram_user_id: int, status: str) -> UserRecord:
        return self._registry.set_status(telegram_user_id, status)

    def set_daily_kcal_goal(
        self, telegram_user_id: int, goal: int | None
    ) -> UserRecord:
        updated = self._registry.set_daily_kcal_goal(telegram_user_id, goal)
        with self._lock:
            for key, service in self._services.items():
                if key[0] == telegram_user_id:
                    service.set_daily_kcal_goal(goal)
        return updated

    def set_daily_protein_goal(
        self, telegram_user_id: int, goal: int | None
    ) -> UserRecord:
        updated = self._registry.set_daily_protein_goal(telegram_user_id, goal)
        with self._lock:
            for key, service in self._services.items():
                if key[0] == telegram_user_id:
                    service.set_daily_protein_goal(goal)
        return updated

    def delete_user(self, telegram_user_id: int) -> None:
        with self._lock:
            user = self._registry.get_user(telegram_user_id)
            if user is None:
                raise UserRegistryError("User not found")
            user = self._registry.set_status(telegram_user_id, "blocked")
            if user.spreadsheet_id:
                self._workspace.delete_personal_spreadsheet(user.spreadsheet_id)
            user_dir = (self._photo_storage_dir / str(telegram_user_id)).resolve()
            if not user_dir.is_relative_to(self._photo_storage_dir):
                raise UserRegistryError("Invalid personal photo directory")
            try:
                if user_dir.exists():
                    shutil.rmtree(user_dir)
            except OSError as exc:
                raise UserRegistryError(
                    "Could not delete personal photo directory"
                ) from exc
            self._registry.delete_user(telegram_user_id)
            for key in list(self._services):
                if key[0] == telegram_user_id:
                    del self._services[key]


class TelegramHandlers:
    def __init__(
        self,
        admin_user_id: int,
        manager: UserManager,
        meal_weight_presets: tuple[int, ...] = (50, 100, 150, 200),
        statistics: BotStatistics | None = None,
        garmin_calories: GarminCalories | None = None,
        transcriber: Transcriber | None = None,
        meal_grouper: MealGrouper | None = None,
    ) -> None:
        self._admin_user_id = admin_user_id
        self._manager = manager
        self._meal_weight_presets = meal_weight_presets
        self._statistics = statistics
        self._garmin_calories = garmin_calories
        self._transcriber = transcriber
        self._meal_grouper = meal_grouper

    @staticmethod
    @asynccontextmanager
    async def _temporary_status(
        message: object | None, *, llm: bool = False
    ) -> AsyncIterator[None]:
        status: object | None = None
        if message is not None:
            try:
                reply_text = getattr(message, "reply_text", None)
                if callable(reply_text):
                    status = await reply_text(
                        LLM_OPERATION_TEXT if llm else LONG_OPERATION_TEXT,
                        do_quote=False,
                    )
            except Exception:
                LOGGER.warning(
                    "Could not send a temporary operation status", exc_info=True
                )
        try:
            yield
        finally:
            if status is not None:
                try:
                    delete = getattr(status, "delete", None)
                    if callable(delete):
                        await delete()
                except Exception:
                    LOGGER.warning(
                        "Could not delete a temporary operation status",
                        exc_info=True,
                    )

    async def track_interaction(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        if (
            self._statistics is None
            or update.effective_user is None
            or (update.message is None and update.callback_query is None)
        ):
            return
        user = update.effective_user
        received_at = (
            update.message.date if update.message is not None else datetime.now(UTC)
        )
        try:
            await asyncio.to_thread(
                self._statistics.record_message,
                update.update_id,
                received_at,
                user.id,
                user.full_name,
                user.username or "",
            )
        except Exception:
            LOGGER.exception("Could not record an incoming Telegram interaction")

    @staticmethod
    def _is_private(update: Update) -> bool:
        return (
            update.effective_user is not None
            and update.effective_chat is not None
            and update.effective_chat.type == ChatType.PRIVATE
        )

    def _is_admin(self, update: Update) -> bool:
        return (
            self._is_private(update)
            and update.effective_user is not None
            and update.effective_user.id == self._admin_user_id
        )

    @staticmethod
    def _user_state(
        context: ContextTypes.DEFAULT_TYPE | object | None,
    ) -> dict[str, object]:
        data = getattr(context, "user_data", None)
        return data if isinstance(data, dict) else {}

    @classmethod
    def _clear_pending_input(
        cls, context: ContextTypes.DEFAULT_TYPE | object | None
    ) -> None:
        state = cls._user_state(context)
        state.pop(GOAL_WAITING_KEY, None)
        state.pop(MEAL_WEIGHT_WAITING_KEY, None)
        state.pop(INVITE_WAITING_KEY, None)

    @classmethod
    def _start_waiting(
        cls,
        context: ContextTypes.DEFAULT_TYPE | object | None,
        key: str,
        value: object = True,
    ) -> None:
        cls._clear_pending_input(context)
        cls._user_state(context)[key] = value

    @staticmethod
    def _remember_prompt(state: dict[str, object], prompt: object) -> None:
        message_id = getattr(prompt, "message_id", None)
        chat_id = getattr(prompt, "chat_id", None)
        if isinstance(message_id, int) and isinstance(chat_id, int):
            state["prompt_message_id"] = message_id
            state["prompt_chat_id"] = chat_id

    @staticmethod
    async def _edit_waiting_prompt(
        context: ContextTypes.DEFAULT_TYPE | object | None,
        state: dict[str, object],
        text: str,
    ) -> bool:
        bot = getattr(context, "bot", None)
        if bot is None:
            return False
        try:
            chat_id = int(str(state["prompt_chat_id"]))
            message_id = int(str(state["prompt_message_id"]))
        except (KeyError, TypeError, ValueError):
            return False
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=None,
            )
        except Exception:
            LOGGER.warning("Could not close a waiting prompt", exc_info=True)
            return False
        return True

    @staticmethod
    async def _remove_waiting_prompt_buttons(
        context: ContextTypes.DEFAULT_TYPE | object | None,
        state: dict[str, object],
    ) -> None:
        bot = getattr(context, "bot", None)
        if bot is None:
            return
        try:
            await bot.edit_message_reply_markup(
                chat_id=int(str(state["prompt_chat_id"])),
                message_id=int(str(state["prompt_message_id"])),
                reply_markup=None,
            )
        except (KeyError, TypeError, ValueError):
            return
        except Exception:
            LOGGER.warning("Could not remove waiting-prompt buttons", exc_info=True)

    async def _active_service(
        self, update: Update, *, callback: bool = False
    ) -> CaloriesService | None:
        user = await self._active_user(update, callback=callback)
        if user is None:
            return None
        try:
            return await asyncio.to_thread(self._manager.service_for, user)
        except Exception:
            LOGGER.exception("Could not build current user context")
            await self._send_access_text(update, ACCESS_ERROR_TEXT, callback)
            return None

    async def _active_user(
        self, update: Update, *, callback: bool = False
    ) -> UserRecord | None:
        if not self._is_private(update) or update.effective_user is None:
            return None
        try:
            user = await asyncio.to_thread(
                self._manager.get_user, update.effective_user.id
            )
        except Exception:
            LOGGER.exception("Could not resolve current user")
            await self._send_access_text(update, ACCESS_ERROR_TEXT, callback)
            return None
        if user is None:
            await self._send_access_text(update, INVITE_ONLY_TEXT, callback)
            return None
        if user.status == "blocked":
            await self._send_access_text(update, BLOCKED_TEXT, callback)
            return None
        return user

    @staticmethod
    async def _send_access_text(update: Update, text: str, callback: bool) -> None:
        if callback and update.callback_query is not None:
            await update.callback_query.answer(text, show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(text, do_quote=False)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        user = update.effective_user
        if not self._is_private(update) or message is None or user is None:
            return
        try:
            existing = await asyncio.to_thread(self._manager.get_user, user.id)
        except Exception:
            LOGGER.exception("Could not resolve user for /start")
            await message.reply_text(ACCESS_ERROR_TEXT, do_quote=False)
            return
        if existing is not None:
            text = load_start_text() if existing.status == "active" else BLOCKED_TEXT
            await message.reply_text(text, do_quote=False)
            return
        token = context.args[0] if context.args else ""
        if not token:
            await message.reply_text(INVITE_ONLY_TEXT, do_quote=False)
            return
        try:
            await asyncio.to_thread(
                self._manager.activate,
                token,
                user.id,
                user.username or "",
            )
        except (InviteUnavailableError, UserAlreadyRegisteredError):
            await message.reply_text(INVITE_ONLY_TEXT, do_quote=False)
            return
        except Exception:
            LOGGER.exception("Could not activate invite")
            await message.reply_text(ACTIVATION_ERROR_TEXT, do_quote=False)
            return
        await message.reply_text(load_start_text(), do_quote=False)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None:
            return
        if self._is_admin(update):
            await message.reply_text(load_help_text(admin=True), do_quote=False)
            return
        if await self._active_service(update) is not None:
            await message.reply_text(load_help_text(), do_quote=False)

    async def tips(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None:
            return
        if self._is_admin(update) or await self._active_service(update) is not None:
            await message.reply_text(load_tips_text(), do_quote=False)

    async def info(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        if self._statistics is None:
            await message.reply_text(f"Версія: {__version__}", do_quote=False)
            return
        async with self._temporary_status(message):
            try:
                info = await asyncio.to_thread(
                    self._statistics.format_info, __version__
                )
            except Exception:
                LOGGER.exception("Could not build /info statistics")
                info = f"Версія: {__version__}\nСтатистика тимчасово недоступна."
            for chunk in _split_telegram_text(info):
                await message.reply_text(chunk, do_quote=False)

    async def burned(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        if self._garmin_calories is None:
            await message.reply_text(GARMIN_READ_ERROR_TEXT, do_quote=False)
            return
        async with self._temporary_status(message):
            try:
                report = await asyncio.to_thread(
                    self._garmin_calories.format_weekly_report
                )
            except Exception:
                LOGGER.exception("Could not read cached Garmin calorie data")
                report = GARMIN_READ_ERROR_TEXT
            await message.reply_text(report, do_quote=False)

    async def day(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        async with self._temporary_status(message):
            try:
                reply = await asyncio.to_thread(service.get_day_summary, message.date)
            except SheetsReadError:
                LOGGER.exception("Could not read the calorie log for /day")
                reply = READ_ERROR_TEXT
            except Exception:
                LOGGER.exception("Unexpected error while handling /day")
                reply = READ_ERROR_TEXT
            is_rich_day = reply.startswith("<details>")
            if is_rich_day:
                try:
                    bot = getattr(context, "bot", None)
                    chat = update.effective_chat
                    if bot is None or chat is None:
                        raise RuntimeError("Rich-message bot context is unavailable")
                    sent = await bot.do_api_request(
                        "sendRichMessage",
                        api_kwargs={
                            "chat_id": chat.id,
                            "rich_message": {"html": reply},
                        },
                        return_type=Message,
                    )
                except Exception:
                    LOGGER.exception("Could not send rich /day summary")
                    compact = "\n".join(re.findall(r"<summary>(.*?)</summary>", reply))
                    sent = await message.reply_text(compact, do_quote=False)
            else:
                sent = await message.reply_text(reply, do_quote=False)
            if is_rich_day:
                await self._remember_daily_total_message(sent)

    async def weekly_calories(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        async with self._temporary_status(message):
            try:
                burned_totals = None
                if self._is_admin(update):
                    if self._garmin_calories is None:
                        raise RuntimeError("Garmin calorie store is unavailable")
                    burned_totals = await asyncio.to_thread(
                        self._garmin_calories.get_daily_calories
                    )
                reply = await asyncio.to_thread(
                    service.get_weekly_calories,
                    message.date,
                    burned_totals,
                )
            except Exception:
                LOGGER.exception("Could not build /weekly_calories")
                reply = WEEK_ERROR_TEXT
            await message.reply_text(reply, do_quote=False)

    async def weekly_meals(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        async with self._temporary_status(message, llm=self._meal_grouper is not None):
            try:
                reply = await asyncio.to_thread(
                    service.get_weekly_meals, message.date, self._meal_grouper
                )
            except Exception:
                LOGGER.exception("Could not build /weekly_meals")
                reply = WEEK_ERROR_TEXT
            for chunk in _split_telegram_text(reply):
                await message.reply_text(
                    chunk, parse_mode=ParseMode.HTML, do_quote=False
                )

    @staticmethod
    def _short_button_name(name: str, limit: int = 34) -> str:
        return name if len(name) <= limit else f"{name[: limit - 1]}…"

    @classmethod
    def _saved_meals_keyboard(cls, meals: list[SavedMeal]) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    f"➕ {f'{meal.icon} ' if meal.icon else ''}"
                    f"{cls._short_button_name(meal.display_name)} · "
                    f"{meal.default_total_weight_g} г",
                    callback_data=(
                        f"saved-add:{meal.saved_meal_id}:{meal.default_total_weight_g}"
                    ),
                )
            ]
            for meal in meals
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    "🗑 Видалити із збережених", callback_data="meals-manage"
                )
            ]
        )
        return InlineKeyboardMarkup(rows)

    @classmethod
    def _recent_meals_keyboard(cls, recent: list[RecentMeal]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"➕ {cls._short_button_name(item.meal.meal_name)} · "
                        f"{round_whole(item.meal.total_weight_g)} г",
                        callback_data=(
                            f"recent-add:{item.telegram_message_id}:"
                            f"{item.day.isoformat()}:"
                            f"{round_whole(item.meal.total_weight_g)}"
                        ),
                    )
                ]
                for item in recent
            ]
        )

    async def meals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        async with self._temporary_status(message):
            try:
                meals = await asyncio.to_thread(service.list_saved_meals)
            except Exception:
                LOGGER.exception("Could not list saved meals")
                await message.reply_text(
                    "Не вдалося прочитати збережені страви. Спробуй ще раз.",
                    do_quote=False,
                )
                return
            text = (
                "Збережені страви — натисни, щоб додати:"
                if meals
                else (
                    "Збережених страв ще немає.\n"
                    "Після розрахунку натисни «⭐ Зберегти», і страва з’явиться тут."
                )
            )
            await message.reply_text(
                text,
                reply_markup=self._saved_meals_keyboard(meals) if meals else None,
                do_quote=False,
            )

    async def recent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        async with self._temporary_status(message):
            try:
                recent = await asyncio.to_thread(service.list_recent_meals)
            except Exception:
                LOGGER.exception("Could not list recent meals")
                await message.reply_text(
                    "Не вдалося прочитати нещодавні страви. Спробуй ще раз.",
                    do_quote=False,
                )
                return
            text = (
                "Повторити нещодавню страву:" if recent else "Нещодавніх страв немає."
            )
            await message.reply_text(
                text,
                reply_markup=self._recent_meals_keyboard(recent) if recent else None,
                do_quote=False,
            )

    async def save(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        name = " ".join(context.args or []).strip() or None
        async with self._temporary_status(message, llm=True):
            try:
                saved, created = await asyncio.to_thread(service.save_latest_meal, name)
            except SavedMealNameError as exc:
                await message.reply_text(str(exc), do_quote=False)
                return
            except Exception:
                LOGGER.exception("Could not save the latest meal")
                await message.reply_text(
                    "Не вдалося зберегти страву. Спробуй ще раз.", do_quote=False
                )
                return
            if saved is None:
                reply = "Немає запису, який можна зберегти."
            elif created:
                reply = f"Збережено: {saved.display_name} ✓"
            else:
                reply = f"Ця страва вже збережена: {saved.display_name}."
            await message.reply_text(reply, do_quote=False)

    async def goal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        current = await self._active_user(update)
        if current is None:
            self._clear_pending_input(context)
            return
        args = list(getattr(context, "args", None) or [])
        if args:
            self._clear_pending_input(context)
            if len(args) != 1:
                await message.reply_text(self._goal_validation_text(), do_quote=False)
                return
            await self._save_goal(update, args[0])
            return

        waiting_state: dict[str, object] = {"kind": "daily_goal"}
        self._start_waiting(context, GOAL_WAITING_KEY, waiting_state)
        rows: list[list[InlineKeyboardButton]] = []
        if current.daily_kcal_goal is None:
            reply = "Яка твоя денна ціль калорій?\nНадішли ціле число, наприклад: 2000"
        else:
            reply = (
                f"Поточна денна ціль: {current.daily_kcal_goal} кк.\n"
                "Надішли нове ціле число або вимкни ціль."
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "🚫 Вимкнути ціль",
                        callback_data=f"{GOAL_DISABLE_CALLBACK_PREFIX}{user.id}",
                    )
                ]
            )
        rows.append([InlineKeyboardButton("❌ Скасувати", callback_data="wait-cancel")])
        prompt = await message.reply_text(
            reply, reply_markup=InlineKeyboardMarkup(rows), do_quote=False
        )
        self._remember_prompt(waiting_state, prompt)

    async def protein_goal(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        current = await self._active_user(update)
        if current is None:
            self._clear_pending_input(context)
            return
        args = list(getattr(context, "args", None) or [])
        if args:
            self._clear_pending_input(context)
            if len(args) != 1:
                await message.reply_text(
                    self._goal_validation_text("protein_goal"), do_quote=False
                )
                return
            await self._save_goal(update, args[0], "protein_goal")
            return

        waiting_state: dict[str, object] = {"kind": "protein_goal"}
        self._start_waiting(context, GOAL_WAITING_KEY, waiting_state)
        rows: list[list[InlineKeyboardButton]] = []
        if current.daily_protein_goal is None:
            reply = "Яка твоя денна ціль білка?\nНадішли ціле число, наприклад: 100"
        else:
            reply = (
                f"Поточна ціль білка: {current.daily_protein_goal} г.\n"
                "Надішли нове ціле число або вимкни ціль."
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "🚫 Вимкнути ціль",
                        callback_data=(
                            f"{PROTEIN_GOAL_DISABLE_CALLBACK_PREFIX}{user.id}"
                        ),
                    )
                ]
            )
        rows.append([InlineKeyboardButton("❌ Скасувати", callback_data="wait-cancel")])
        prompt = await message.reply_text(
            reply, reply_markup=InlineKeyboardMarkup(rows), do_quote=False
        )
        self._remember_prompt(waiting_state, prompt)

    async def goal_disable_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        if await self._active_user(update, callback=True) is None:
            return
        try:
            target_id = int(
                (query.data or "").removeprefix(GOAL_DISABLE_CALLBACK_PREFIX)
            )
            if (
                not (query.data or "").startswith(GOAL_DISABLE_CALLBACK_PREFIX)
                or target_id != user.id
            ):
                raise ValueError
        except ValueError:
            await query.answer("Некоректна кнопка.", show_alert=True)
            return
        async with self._temporary_status(query.message):
            try:
                await asyncio.to_thread(
                    self._manager.set_daily_kcal_goal, user.id, None
                )
            except Exception:
                LOGGER.exception("Could not disable daily goal")
                await query.answer(GOAL_ERROR_TEXT, show_alert=True)
                return
            self._clear_pending_input(context)
            await query.answer()
            await query.edit_message_text("Денну ціль вимкнено ✓", reply_markup=None)

    async def protein_goal_disable_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        if await self._active_user(update, callback=True) is None:
            return
        try:
            target_id = int(
                (query.data or "").removeprefix(PROTEIN_GOAL_DISABLE_CALLBACK_PREFIX)
            )
            if (
                not (query.data or "").startswith(PROTEIN_GOAL_DISABLE_CALLBACK_PREFIX)
                or target_id != user.id
            ):
                raise ValueError
        except ValueError:
            await query.answer("Некоректна кнопка.", show_alert=True)
            return
        async with self._temporary_status(query.message):
            try:
                await asyncio.to_thread(
                    self._manager.set_daily_protein_goal, user.id, None
                )
            except Exception:
                LOGGER.exception("Could not disable daily protein goal")
                await query.answer(GOAL_ERROR_TEXT, show_alert=True)
                return
            self._clear_pending_input(context)
            await query.answer()
            await query.edit_message_text("Ціль по білку вимкнено ✓", reply_markup=None)

    async def delete(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        show_status: bool = True,
    ) -> None:
        query = update.callback_query
        if query is None:
            return
        if show_status:
            async with self._temporary_status(query.message):
                await self.delete(update, context, show_status=False)
            return
        service = await self._active_service(update, callback=True)
        if service is None:
            return
        try:
            telegram_message_id, fallback_day = self._parse_delete_callback(query.data)
        except (TypeError, ValueError):
            await query.answer("Некоректна кнопка.", show_alert=True)
            return
        try:
            deletion = await asyncio.to_thread(
                service.delete_message, telegram_message_id, fallback_day
            )
        except (SheetsReadError, SheetsWriteError):
            LOGGER.exception("Could not delete the calorie log row")
            await query.answer(DELETE_ERROR_TEXT, show_alert=True)
            return
        except Exception:
            LOGGER.exception("Unexpected error while deleting a calorie log row")
            await query.answer(DELETE_ERROR_TEXT, show_alert=True)
            return
        await query.answer()
        try:
            reply = await asyncio.to_thread(service.format_deletion_reply, deletion)
            await query.edit_message_text(
                reply,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except Exception:
            LOGGER.warning("Could not edit deletion confirmation", exc_info=True)
        message = query.message
        source_message_id = getattr(message, "message_id", None)
        chat_id = getattr(message, "chat_id", None)
        if not isinstance(chat_id, int) and update.effective_chat is not None:
            chat_id = update.effective_chat.id
        if isinstance(chat_id, int) and isinstance(source_message_id, int):
            await self._delete_daily_totals_after(
                context.bot, chat_id, source_message_id
            )
        if not isinstance(chat_id, int):
            LOGGER.warning("Could not send updated daily total without a chat ID")
            return
        try:
            daily_total = await asyncio.to_thread(
                service.format_deletion_daily_total, deletion
            )
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=daily_total,
                parse_mode=ParseMode.HTML,
            )
            await self._remember_daily_total_message(sent)
        except Exception:
            LOGGER.warning(
                "Could not send the updated daily total after deletion",
                exc_info=True,
            )

    async def save_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        query = update.callback_query
        if query is None:
            return
        service = await self._active_service(update, callback=True)
        if service is None:
            return
        async with self._temporary_status(query.message, llm=True):
            try:
                message_id, day = self._parse_source_callback(
                    query.data, SAVE_CALLBACK_PREFIX
                )
                saved, created = await asyncio.to_thread(
                    service.save_source_meal, message_id, day
                )
            except SavedMealNameError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            except Exception:
                LOGGER.exception("Could not save meal from callback")
                await query.answer(
                    "Не вдалося зберегти страву. Спробуй ще раз.",
                    show_alert=True,
                )
                return
            if saved is None:
                await query.answer(
                    "Цього запису вже немає. Онови список.", show_alert=True
                )
                return
            await query.answer(
                f"{'Збережено' if created else 'Вже збережено'}: {saved.display_name}"
            )
            try:
                await query.edit_message_reply_markup(
                    reply_markup=self._meal_reply_markup(
                        MealReply(
                            text="",
                            telegram_message_id=message_id,
                            accounting_day=day,
                            can_save=False,
                            can_change_weight=len(saved.base_meal.items) == 1,
                        )
                    )
                )
            except Exception:
                LOGGER.warning("Could not hide saved-meal button", exc_info=True)

    async def meal_weight_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        show_status: bool = True,
    ) -> None:
        query = update.callback_query
        if query is None:
            return
        if show_status:
            async with self._temporary_status(query.message):
                await self.meal_weight_callback(update, context, show_status=False)
            return
        service = await self._active_service(update, callback=True)
        if service is None:
            return
        try:
            message_id, day = self._parse_source_callback(query.data, "meal-weight:")
            source = await asyncio.to_thread(service.get_recent_meal, message_id, day)
        except Exception:
            LOGGER.exception("Could not prepare meal weight editing")
            await query.answer("Не вдалося прочитати страву.", show_alert=True)
            return
        if source is None:
            await query.answer("Цього запису вже немає.", show_alert=True)
            return
        if len(source.meal.items) != 1:
            await query.answer(
                "Змінити вагу можна лише для страви з одного компонента.",
                show_alert=True,
            )
            return
        if not isinstance(query.message, Message):
            await query.answer("Це повідомлення вже недоступне.", show_alert=True)
            return
        waiting_state: dict[str, object] = {
            "kind": "meal_weight",
            "message_id": message_id,
            "day": day.isoformat(),
            "result_chat_id": query.message.chat_id,
            "result_message_id": query.message.message_id,
            "accepts_text": False,
        }
        self._start_waiting(context, MEAL_WEIGHT_WAITING_KEY, waiting_state)
        await query.answer()
        prompt = await query.message.reply_text(
            "⚖️ Обери нову вагу:",
            reply_markup=self._weight_choice_markup(message_id, day),
            do_quote=False,
        )
        self._remember_prompt(waiting_state, prompt)

    def _weight_choice_markup(self, message_id: int, day: date) -> InlineKeyboardMarkup:
        preset_buttons = [
            InlineKeyboardButton(
                f"{weight}г",
                callback_data=(
                    f"meal-weight-set:{message_id}:{day.isoformat()}:{weight}"
                ),
            )
            for weight in self._meal_weight_presets
        ]
        rows = [
            preset_buttons[index : index + 4]
            for index in range(0, len(preset_buttons), 4)
        ]
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        "Інша вага",
                        callback_data=(
                            f"meal-weight-other:{message_id}:{day.isoformat()}"
                        ),
                    )
                ],
                [InlineKeyboardButton("❌ Скасувати", callback_data="wait-cancel")],
            ]
        )
        return InlineKeyboardMarkup(rows)

    async def meal_weight_choice_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None:
            return
        service = await self._active_service(update, callback=True)
        if service is None:
            return
        state = self._user_state(context).get(MEAL_WEIGHT_WAITING_KEY)
        if not isinstance(state, dict) or state.get("kind") != "meal_weight":
            await query.answer("Цей вибір уже неактивний.", show_alert=True)
            return
        data = query.data or ""
        try:
            if data.startswith("meal-weight-set:"):
                message_raw, day_raw, weight_raw = data.removeprefix(
                    "meal-weight-set:"
                ).split(":", maxsplit=2)
                weight_g = self._parse_weight(weight_raw)
                is_other = False
            elif data.startswith("meal-weight-other:"):
                message_raw, day_raw = data.removeprefix("meal-weight-other:").split(
                    ":", maxsplit=1
                )
                weight_g = None
                is_other = True
            else:
                raise ValueError
            message_id = int(message_raw)
            day = date.fromisoformat(day_raw)
            if message_id != int(str(state["message_id"])) or day.isoformat() != str(
                state["day"]
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            await query.answer("Некоректна кнопка.", show_alert=True)
            return
        if is_other:
            state["accepts_text"] = True
            await query.answer()
            await query.edit_message_text(
                f"⚖️ Введи вагу від 1 до {MAX_WEIGHT_G} г, наприклад: 350 г",
                reply_markup=self._cancel_markup(),
            )
            return
        if not isinstance(query.message, Message) or weight_g is None:
            await query.answer("Це повідомлення вже недоступне.", show_alert=True)
            return
        await query.answer()
        await self._apply_meal_weight(query.message, context, service, state, weight_g)

    async def _edit_saved_meals_menu(
        self, query: object, service: CaloriesService
    ) -> None:
        meals = await asyncio.to_thread(service.list_saved_meals)
        text = (
            "Збережені страви — натисни, щоб додати:"
            if meals
            else (
                "Збережених страв ще немає.\n"
                "Після розрахунку натисни «⭐ Зберегти», і страва з’явиться тут."
            )
        )
        await query.edit_message_text(  # type: ignore[attr-defined]
            text,
            reply_markup=self._saved_meals_keyboard(meals) if meals else None,
        )

    async def _edit_manage_menu(self, query: object, service: CaloriesService) -> None:
        meals = await asyncio.to_thread(service.list_saved_meals)
        rows = [
            [
                InlineKeyboardButton(
                    f"{f'{meal.icon} ' if meal.icon else ''}"
                    f"{self._short_button_name(meal.display_name)}",
                    callback_data=f"manage-delete:{meal.saved_meal_id}",
                )
            ]
            for meal in meals
        ]
        rows.append([InlineKeyboardButton("↩️ Назад", callback_data="meals-back")])
        text = "Яку страву видалити?" if meals else "Збережених страв ще немає."
        await query.edit_message_text(  # type: ignore[attr-defined]
            text, reply_markup=InlineKeyboardMarkup(rows)
        )

    async def _edit_delete_confirmation(
        self, query: object, service: CaloriesService, saved_meal_id: str
    ) -> bool:
        meals = await asyncio.to_thread(service.list_saved_meals)
        meal = next(
            (item for item in meals if item.saved_meal_id == saved_meal_id), None
        )
        if meal is None:
            return False
        await query.edit_message_text(  # type: ignore[attr-defined]
            f"Видалити «{meal.display_name}» із збережених?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 Видалити",
                            callback_data=f"manage-delete-do:{saved_meal_id}",
                        ),
                        InlineKeyboardButton(
                            "❌ Скасувати", callback_data="meals-manage"
                        ),
                    ],
                ]
            ),
        )
        return True

    async def library_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        show_status: bool = True,
    ) -> None:
        query = update.callback_query
        if query is None:
            return
        data = query.data or ""
        if data in {"wait-cancel", "invite-cancel"}:
            self._clear_pending_input(context)
            await query.answer()
            await query.edit_message_text("Скасовано.", reply_markup=None)
            return
        if show_status:
            async with self._temporary_status(query.message):
                await self.library_callback(update, context, show_status=False)
            return
        service = await self._active_service(update, callback=True)
        if service is None:
            return
        try:
            if data in {"meals-back", "meals-manage"}:
                self._clear_pending_input(context)
                await query.answer()
                if data == "meals-manage":
                    await self._edit_manage_menu(query, service)
                else:
                    await self._edit_saved_meals_menu(query, service)
                return
            if data.startswith("saved-add:"):
                _, saved_id, weight_raw = data.split(":", maxsplit=2)
                event_id = self._callback_event_id(query.id)
                result = await asyncio.to_thread(
                    service.add_saved_meal,
                    saved_id,
                    self._parse_weight(weight_raw),
                    event_id,
                    datetime.now(tz=ZoneInfo("UTC")),
                )
                if result is None:
                    raise LookupError
                await query.answer("Додано")
                if isinstance(query.message, Message):
                    await self._send_meal_replies(query.message, result)
                return
            if data.startswith("recent-add:"):
                message_id_raw, day_raw, weight_raw = data.removeprefix(
                    "recent-add:"
                ).split(":", maxsplit=2)
                message_id = int(message_id_raw)
                day = date.fromisoformat(day_raw)
                result = await asyncio.to_thread(
                    service.add_recent_meal,
                    message_id,
                    day,
                    self._parse_weight(weight_raw),
                    self._callback_event_id(query.id),
                    datetime.now(tz=ZoneInfo("UTC")),
                )
                if result is None:
                    raise LookupError
                await query.answer("Додано")
                if isinstance(query.message, Message):
                    await self._send_meal_replies(query.message, result)
                return
            if data.startswith("manage-delete:"):
                saved_id = data.split(":", maxsplit=1)[1]
                await query.answer()
                if not await self._edit_delete_confirmation(query, service, saved_id):
                    raise LookupError
                return
            if data.startswith("manage-delete-do:"):
                saved_id = data.removeprefix("manage-delete-do:")
                deleted = await asyncio.to_thread(service.delete_saved_meal, saved_id)
                await query.answer("Видалено" if deleted else "Уже видалено")
                await self._edit_manage_menu(query, service)
                return
            raise ValueError
        except LookupError:
            await query.answer("Цього запису вже немає. Онови список.", show_alert=True)
        except (ValueError, TypeError):
            await query.answer("Некоректна кнопка.", show_alert=True)
        except Exception:
            LOGGER.exception("Could not handle saved/recent meal callback")
            await query.answer(
                "Не вдалося виконати дію. Спробуй ще раз.", show_alert=True
            )

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        if self._is_admin(update) and self._user_state(context).get(INVITE_WAITING_KEY):
            await self._finish_invite(update, context, message.text)
            return
        service = await self._active_service(update)
        if service is None:
            return
        goal_waiting = self._user_state(context).get(GOAL_WAITING_KEY)
        if goal_waiting:
            goal_kind = (
                str(goal_waiting.get("kind", "daily_goal"))
                if isinstance(goal_waiting, dict)
                else "daily_goal"
            )
            if await self._save_goal(update, message.text, goal_kind):
                if isinstance(goal_waiting, dict):
                    await self._remove_waiting_prompt_buttons(context, goal_waiting)
                self._clear_pending_input(context)
            return
        meal_weight_state = self._user_state(context).get(MEAL_WEIGHT_WAITING_KEY)
        if isinstance(meal_weight_state, dict):
            await self._handle_meal_weight_input(
                update, context, service, meal_weight_state, message.text
            )
            return
        await self._process(service, message, message.text)

    async def _handle_meal_weight_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        service: CaloriesService,
        state: dict[str, object],
        text: str,
    ) -> None:
        message = update.effective_message
        if message is None:
            return
        if state.get("kind") != "meal_weight":
            self._clear_pending_input(context)
            return
        if state.get("accepts_text") is not True:
            await message.reply_text(
                "Спочатку обери вагу кнопкою або натисни «Інша вага».",
                do_quote=False,
            )
            return
        try:
            weight_g = self._parse_weight(text)
        except ValueError:
            await message.reply_text(
                f"Введи вагу від 1 до {MAX_WEIGHT_G} г, наприклад: 350 г",
                do_quote=False,
            )
            return
        await self._apply_meal_weight(message, context, service, state, weight_g)

    async def _apply_meal_weight(
        self,
        message: Message,
        context: ContextTypes.DEFAULT_TYPE,
        service: CaloriesService,
        state: dict[str, object],
        weight_g: int,
        *,
        show_status: bool = True,
    ) -> None:
        if show_status:
            async with self._temporary_status(message):
                await self._apply_meal_weight(
                    message,
                    context,
                    service,
                    state,
                    weight_g,
                    show_status=False,
                )
            return
        try:
            result = await asyncio.to_thread(
                service.change_meal_weight,
                int(str(state["message_id"])),
                date.fromisoformat(str(state["day"])),
                weight_g,
            )
            if result is None:
                raise LookupError
            await self._send_meal_replies(message, result)
            self._clear_pending_input(context)
            try:
                await context.bot.edit_message_text(
                    chat_id=int(str(state["result_chat_id"])),
                    message_id=int(str(state["result_message_id"])),
                    text="Розрахунок оновлено нижче ↓",
                    reply_markup=None,
                )
            except Exception:
                LOGGER.warning(
                    "Meal weight was updated but the old reply could not be archived",
                    exc_info=True,
                )
            await self._edit_waiting_prompt(
                context,
                state,
                f"✅ Вагу змінено на {weight_g} г",
            )
        except MealWeightUnchangedError as exc:
            self._clear_pending_input(context)
            unchanged_text = f"Вага вже становить {exc.weight_g} г."
            if not await self._edit_waiting_prompt(context, state, unchanged_text):
                await message.reply_text(unchanged_text, do_quote=False)
        except CompositeMealWeightError:
            self._clear_pending_input(context)
            await message.reply_text(
                "Змінити вагу можна лише для страви з одного компонента.",
                do_quote=False,
            )
        except LookupError:
            self._clear_pending_input(context)
            await message.reply_text(
                "Цього запису вже немає. Відкрий /meals ще раз.", do_quote=False
            )
        except Exception:
            LOGGER.exception("Could not finish saved-meal input")
            await message.reply_text(
                "Не вдалося виконати дію. Спробуй ще раз або скасуй.",
                reply_markup=self._cancel_markup(),
                do_quote=False,
            )

    @staticmethod
    def _parse_goal(raw: str, kind: str = "daily_goal") -> int:
        text = raw.strip()
        if not text.isascii() or not text.isdecimal():
            raise ValueError
        from .users import (
            MAX_DAILY_KCAL_GOAL,
            MAX_DAILY_PROTEIN_GOAL,
            MIN_DAILY_KCAL_GOAL,
            MIN_DAILY_PROTEIN_GOAL,
        )

        goal = int(text)
        limits = (
            (MIN_DAILY_PROTEIN_GOAL, MAX_DAILY_PROTEIN_GOAL)
            if kind == "protein_goal"
            else (MIN_DAILY_KCAL_GOAL, MAX_DAILY_KCAL_GOAL)
        )
        if not limits[0] <= goal <= limits[1]:
            raise ValueError
        return goal

    @staticmethod
    def _goal_validation_text(kind: str = "daily_goal") -> str:
        from .users import (
            MAX_DAILY_KCAL_GOAL,
            MAX_DAILY_PROTEIN_GOAL,
            MIN_DAILY_KCAL_GOAL,
            MIN_DAILY_PROTEIN_GOAL,
        )

        if kind == "protein_goal":
            return (
                f"Надішли ціле число від {MIN_DAILY_PROTEIN_GOAL} до "
                f"{MAX_DAILY_PROTEIN_GOAL}, наприклад: 100"
            )

        return (
            f"Надішли ціле число від {MIN_DAILY_KCAL_GOAL} до "
            f"{MAX_DAILY_KCAL_GOAL}, наприклад: 2000"
        )

    async def _save_goal(
        self, update: Update, raw: str, kind: str = "daily_goal"
    ) -> bool:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return False
        try:
            goal = self._parse_goal(raw, kind)
        except ValueError:
            await message.reply_text(self._goal_validation_text(kind), do_quote=False)
            return False
        async with self._temporary_status(message):
            try:
                setter = (
                    self._manager.set_daily_protein_goal
                    if kind == "protein_goal"
                    else self._manager.set_daily_kcal_goal
                )
                await asyncio.to_thread(setter, user.id, goal)
            except Exception:
                LOGGER.exception("Could not save daily goal")
                await message.reply_text(GOAL_ERROR_TEXT, do_quote=False)
                return False
            confirmation = (
                f"Ціль по білку встановлено: {goal} г ✓"
                if kind == "protein_goal"
                else f"Денну ціль встановлено: {goal} кк ✓"
            )
            await message.reply_text(confirmation, do_quote=False)
            return True

    async def photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None or not message.photo or message.media_group_id is not None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        try:
            existing = await asyncio.to_thread(
                service.get_existing_reply, message.message_id, message.date
            )
        except SheetsReadError:
            LOGGER.exception("Could not check whether the photo was already stored")
            await message.reply_text(READ_ERROR_TEXT, do_quote=False)
            return
        if existing is not None:
            await self._send_meal_replies(message, existing)
            return
        async with self._temporary_status(message, llm=True):
            try:
                telegram_file = await message.photo[-1].get_file()
                image_bytes = bytes(await telegram_file.download_as_bytearray())
            except Exception:
                LOGGER.exception("Could not download the Telegram photo")
                await message.reply_text(ANALYSIS_ERROR_TEXT, do_quote=False)
                return
            await self._process(
                service,
                message,
                message.caption or "",
                image_bytes,
                show_status=False,
            )

    async def voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if message is None or message.voice is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        if self._transcriber is None:
            await message.reply_text(VOICE_ERROR_TEXT, do_quote=False)
            return
        try:
            existing = await asyncio.to_thread(
                service.get_existing_reply, message.message_id, message.date
            )
        except SheetsReadError:
            LOGGER.exception("Could not check whether the voice message was stored")
            await message.reply_text(READ_ERROR_TEXT, do_quote=False)
            return
        if existing is not None:
            await self._send_meal_replies(message, existing)
            return
        async with self._temporary_status(message, llm=True):
            try:
                telegram_file = await message.voice.get_file()
                audio_bytes = bytes(await telegram_file.download_as_bytearray())
                transcript = await asyncio.to_thread(
                    self._transcriber.transcribe, audio_bytes
                )
            except TranscriptionError:
                LOGGER.exception("Could not transcribe a Telegram voice message")
                await message.reply_text(VOICE_ERROR_TEXT, do_quote=False)
                return
            except Exception:
                LOGGER.exception("Could not download or transcribe a voice message")
                await message.reply_text(VOICE_ERROR_TEXT, do_quote=False)
                return
            await self._process(service, message, transcript, show_status=False)

    @staticmethod
    def _meal_reply_markup(result: MealReply) -> InlineKeyboardMarkup:
        delete_callback = (
            f"{DELETE_CALLBACK_PREFIX}{result.telegram_message_id}:"
            f"{result.accounting_day.isoformat()}"
        )
        first_row: list[InlineKeyboardButton] = []
        if result.can_save:
            first_row.append(
                InlineKeyboardButton(
                    "⭐ Зберегти",
                    callback_data=(
                        f"{SAVE_CALLBACK_PREFIX}{result.telegram_message_id}:"
                        f"{result.accounting_day.isoformat()}"
                    ),
                )
            )
        if result.can_change_weight:
            first_row.append(
                InlineKeyboardButton(
                    "⚖️ Змінити вагу",
                    callback_data=(
                        f"meal-weight:{result.telegram_message_id}:"
                        f"{result.accounting_day.isoformat()}"
                    ),
                )
            )
        rows = [first_row] if first_row else []
        rows.append([InlineKeyboardButton("🗑 Видалити", callback_data=delete_callback)])
        return InlineKeyboardMarkup(rows)

    async def _send_meal_reply(self, message: Message, result: MealReply) -> None:
        await message.reply_text(
            result.text,
            parse_mode=ParseMode.HTML,
            reply_markup=self._meal_reply_markup(result),
            do_quote=False,
        )

    async def _send_meal_replies(
        self, message: Message, result: MealReply | list[MealReply]
    ) -> None:
        replies = result if isinstance(result, list) else [result]
        for reply in replies:
            await self._send_meal_reply(message, reply)
        daily_total_text = next(
            (
                reply.daily_total_text
                for reply in reversed(replies)
                if reply.daily_total_text is not None
            ),
            None,
        )
        if daily_total_text is not None:
            sent = await message.reply_text(
                daily_total_text,
                parse_mode=ParseMode.HTML,
                do_quote=False,
            )
            await self._remember_daily_total_message(sent)

    async def _remember_daily_total_message(self, message: Message) -> None:
        if self._statistics is None:
            return
        try:
            await asyncio.to_thread(
                self._statistics.record_daily_total_message,
                message.chat_id,
                message.message_id,
                message.date,
            )
        except Exception:
            LOGGER.warning("Could not remember a daily-total message", exc_info=True)

    async def _delete_daily_totals_after(
        self, bot: Bot, chat_id: int, telegram_message_id: int
    ) -> None:
        if self._statistics is None:
            return
        try:
            message_ids = await asyncio.to_thread(
                self._statistics.daily_total_message_ids_after,
                chat_id,
                telegram_message_id,
            )
        except Exception:
            LOGGER.warning("Could not read daily-total messages", exc_info=True)
            return
        for start in range(0, len(message_ids), DAILY_TOTAL_DELETE_BATCH_SIZE):
            batch = message_ids[start : start + DAILY_TOTAL_DELETE_BATCH_SIZE]
            deleted_ids: tuple[int, ...] = ()
            try:
                deleted = await bot.delete_messages(chat_id=chat_id, message_ids=batch)
                if not deleted:
                    raise RuntimeError("Telegram did not delete daily-total messages")
                deleted_ids = batch
            except Exception:
                LOGGER.warning(
                    "Could not bulk-delete daily-total messages; retrying separately",
                    exc_info=True,
                )
                individually_deleted: list[int] = []
                for message_id in batch:
                    try:
                        if await bot.delete_message(chat_id, message_id):
                            individually_deleted.append(message_id)
                    except Exception:
                        LOGGER.warning(
                            "Could not delete daily-total message %s",
                            message_id,
                            exc_info=True,
                        )
                deleted_ids = tuple(individually_deleted)
            if not deleted_ids:
                continue
            try:
                await asyncio.to_thread(
                    self._statistics.forget_daily_total_messages,
                    chat_id,
                    deleted_ids,
                )
            except Exception:
                LOGGER.warning(
                    "Could not forget deleted daily-total messages", exc_info=True
                )

    @staticmethod
    def _callback_event_id(callback_query_id: str) -> int:
        raw = hashlib.sha256(callback_query_id.encode()).digest()
        value = int.from_bytes(raw[:7], "big") & ((1 << 52) - 1)
        return -(value or 1)

    async def _process(
        self,
        service: CaloriesService,
        message: Message,
        text: str,
        image_bytes: bytes | None = None,
        *,
        show_status: bool = True,
    ) -> None:
        if show_status:
            async with self._temporary_status(message, llm=True):
                await self._process(
                    service,
                    message,
                    text,
                    image_bytes,
                    show_status=False,
                )
            return
        try:
            result = await asyncio.to_thread(
                service.process_message,
                text,
                message.message_id,
                message.date,
                image_bytes,
            )
        except InputFormatError:
            reply = FORMAT_ERROR_TEXT
        except NotFoodError:
            reply = PHOTO_NOT_FOOD_TEXT if image_bytes is not None else NOT_FOOD_TEXT
        except SheetsReadError:
            LOGGER.exception("Could not read the calorie log")
            reply = READ_ERROR_TEXT
        except SheetsWriteUncertainError:
            LOGGER.exception("Could not verify the calorie log write")
            reply = UNCERTAIN_WRITE_TEXT
        except SheetsWriteError:
            LOGGER.exception("Could not write the calorie log")
            reply = WRITE_ERROR_TEXT
        except AnalysisError:
            LOGGER.exception("Could not analyze the food message")
            reply = ANALYSIS_ERROR_TEXT
        except Exception:
            LOGGER.exception("Unexpected error while handling a Telegram message")
            reply = ANALYSIS_ERROR_TEXT
        else:
            await self._send_meal_replies(message, result)
            return
        await message.reply_text(reply, do_quote=False)

    async def invite(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        display_name = " ".join(context.args or []).strip()
        if not display_name:
            self._start_waiting(context, INVITE_WAITING_KEY)
            await message.reply_text(
                "Введи ім’я нового користувача.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "❌ Скасувати", callback_data="invite-cancel"
                            )
                        ]
                    ]
                ),
                do_quote=False,
            )
            return
        await self._finish_invite(update, context, display_name)

    async def _finish_invite(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        display_name: str,
    ) -> None:
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        display_name = " ".join(display_name.split())
        if not display_name or len(display_name) > MAX_USER_DISPLAY_NAME_LENGTH:
            await message.reply_text(
                "Ім’я має містити від 1 до "
                f"{MAX_USER_DISPLAY_NAME_LENGTH} символів. Введи інше ім’я або скасуй.",
                reply_markup=self._cancel_markup(invite=True),
                do_quote=False,
            )
            return
        async with self._temporary_status(message):
            try:
                token = await asyncio.to_thread(
                    self._manager.create_invite, display_name
                )
                bot_user = await context.bot.get_me()
                if not bot_user.username:
                    raise RuntimeError("Bot has no username")
            except Exception:
                LOGGER.exception("Could not create invite")
                await message.reply_text(ADMIN_ERROR_TEXT, do_quote=False)
                return
            self._clear_pending_input(context)
            await message.reply_text(
                f"https://t.me/{bot_user.username}?start={token}", do_quote=False
            )

    async def users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        async with self._temporary_status(message):
            try:
                users = await asyncio.to_thread(self._manager.list_users)
            except Exception:
                LOGGER.exception("Could not list users")
                await message.reply_text(ADMIN_ERROR_TEXT, do_quote=False)
                return
            await message.reply_text(format_users_reply(users), do_quote=False)

    async def block(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_status_command(update, context, "blocked")

    async def unblock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_status_command(update, context, "active")

    async def _set_status_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, status: str
    ) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        try:
            telegram_user_id = self._parse_admin_user_id(context.args)
        except ValueError:
            command = "block" if status == "blocked" else "unblock"
            await message.reply_text(
                f"Формат: /{command} <telegram_user_id>", do_quote=False
            )
            return
        async with self._temporary_status(message):
            try:
                await asyncio.to_thread(
                    self._manager.set_status, telegram_user_id, status
                )
            except Exception:
                LOGGER.exception("Could not change user status")
                await message.reply_text(ADMIN_ERROR_TEXT, do_quote=False)
                return
            label = "Заблоковано" if status == "blocked" else "Розблоковано"
            await message.reply_text(f"{label}: {telegram_user_id}", do_quote=False)

    async def delete_user_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._clear_pending_input(context)
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        try:
            telegram_user_id = self._parse_admin_user_id(context.args)
        except ValueError:
            await message.reply_text(
                "Формат: /delete <telegram_user_id>", do_quote=False
            )
            return
        async with self._temporary_status(message):
            try:
                user = await asyncio.to_thread(self._manager.get_user, telegram_user_id)
                if user is None:
                    raise UserRegistryError("User not found")
            except Exception:
                LOGGER.exception("Could not prepare user deletion")
                await message.reply_text(ADMIN_ERROR_TEXT, do_quote=False)
                return
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 Підтвердити видалення",
                            callback_data=(
                                f"{ADMIN_DELETE_CALLBACK_PREFIX}{telegram_user_id}"
                            ),
                        ),
                        InlineKeyboardButton(
                            "❌ Скасувати",
                            callback_data=(
                                f"{ADMIN_CANCEL_CALLBACK_PREFIX}{telegram_user_id}"
                            ),
                        ),
                    ]
                ]
            )
            await message.reply_text(
                f"Повністю видалити користувача {telegram_user_id}?",
                reply_markup=keyboard,
                do_quote=False,
            )

    async def admin_delete_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        query = update.callback_query
        if query is None:
            return
        if not self._is_admin(update):
            await query.answer("Недоступно.", show_alert=True)
            return
        data = query.data or ""
        if data.startswith(ADMIN_CANCEL_CALLBACK_PREFIX):
            await query.answer()
            await query.edit_message_text("Видалення скасовано.", reply_markup=None)
            return
        try:
            telegram_user_id = int(data.removeprefix(ADMIN_DELETE_CALLBACK_PREFIX))
            if telegram_user_id <= 0 or not data.startswith(
                ADMIN_DELETE_CALLBACK_PREFIX
            ):
                raise ValueError
        except ValueError:
            await query.answer("Некоректна кнопка.", show_alert=True)
            return
        async with self._temporary_status(query.message):
            try:
                await asyncio.to_thread(self._manager.delete_user, telegram_user_id)
            except Exception:
                LOGGER.exception("Could not fully delete user")
                await query.answer(ADMIN_ERROR_TEXT, show_alert=True)
                return
            await query.answer()
            await query.edit_message_text(
                f"Користувача {telegram_user_id} видалено.", reply_markup=None
            )

    async def _reject_admin_command(self, update: Update) -> None:
        if not self._is_private(update):
            return
        await self._send_access_text(update, "Недоступно.", callback=False)

    async def cancel_pending_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del update
        self._clear_pending_input(context)

    @staticmethod
    def _parse_admin_user_id(args: list[str] | None) -> int:
        if args is None:
            raise ValueError
        if len(args) != 1:
            raise ValueError
        value = int(args[0])
        if value <= 0:
            raise ValueError
        return value

    @staticmethod
    def _parse_weight(raw: str) -> int:
        match = re.fullmatch(
            r"\s*(?P<weight>[0-9]+)\s*"
            r"(?:г|гр|грам|грами|грама|грамів)?\.?\s*",
            raw,
            flags=re.IGNORECASE,
        )
        if match is None:
            raise ValueError
        weight = int(match.group("weight"))
        if not 1 <= weight <= MAX_WEIGHT_G:
            raise ValueError
        return weight

    @staticmethod
    def _cancel_markup(*, invite: bool = False) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "❌ Скасувати",
                        callback_data="invite-cancel" if invite else "wait-cancel",
                    )
                ]
            ]
        )

    @staticmethod
    def _parse_source_callback(data: str | None, prefix: str) -> tuple[int, date]:
        if data is None or not data.startswith(prefix):
            raise ValueError("Unknown callback")
        message_id_raw, day_raw = data.removeprefix(prefix).split(":", maxsplit=1)
        return int(message_id_raw), date.fromisoformat(day_raw)

    @staticmethod
    def _parse_delete_callback(data: str | None) -> tuple[int, date]:
        if data is None or not data.startswith(DELETE_CALLBACK_PREFIX):
            raise ValueError("Unknown callback")
        message_id_raw, day_raw = data.removeprefix(DELETE_CALLBACK_PREFIX).split(
            ":", maxsplit=1
        )
        message_id = int(message_id_raw)
        if message_id == 0:
            raise ValueError("Invalid Telegram message ID")
        return message_id, date.fromisoformat(day_raw)
