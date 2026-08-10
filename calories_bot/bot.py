from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import secrets
import shutil
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from .analyzer import (
    AnalysisError,
    Analyzer,
    InputFormatError,
    NormalizedInput,
    normalize_input,
)
from .models import (
    MAX_SAVED_MEAL_NAME_LENGTH,
    MAX_WEIGHT_G,
    CalculatedFoodItem,
    LLMMetadata,
    MealIconSuggestion,
    MealResult,
    RecentMeal,
    SavedMeal,
    calculate_meal,
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
    SheetsReadError,
    SheetsWriteError,
    SheetsWriteUncertainError,
    accounting_date,
)
from .users import (
    InviteUnavailableError,
    UserAlreadyRegisteredError,
    UserRecord,
    UserRegistry,
    UserRegistryError,
)

LOGGER = logging.getLogger(__name__)
SAVED_MEAL_ICON_CONFIDENCE = 0.8

HELP_TEXT_FILE = Path(__file__).with_name("help.txt")
START_TEXT_FILE = Path(__file__).with_name("start.txt")
TIPS_TEXT_FILE = Path(__file__).with_name("tips.txt")
ADMIN_HELP_TEXT_FILE = Path(__file__).with_name("admin_help.txt")
START_FALLBACK_TEXT = "Напиши, що ти з’їв, або надішли фото страви."
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
READ_ERROR_TEXT = "Не вдалося прочитати Google Sheets. Спробуйте ще раз."
WRITE_ERROR_TEXT = (
    "Не вдалося зберегти запис. Нічого не зараховано — надішліть повідомлення ще раз."
)
UNCERTAIN_WRITE_TEXT = (
    "Не вдалося підтвердити запис у Google Sheets. Перевірте останній рядок "
    "таблиці перед повторним надсиланням."
)
ANALYSIS_ERROR_TEXT = "Не вдалося порахувати калорії. Спробуйте ще раз."
GOAL_ERROR_TEXT = "Не вдалося змінити денну ціль. Спробуй ще раз."
WEEK_ERROR_TEXT = "Не вдалося сформувати підсумок за 7 днів. Спробуй ще раз."
DELETE_ERROR_TEXT = "Не вдалося видалити запис. Спробуйте ще раз."
DELETE_CALLBACK_PREFIX = "delete:"
SAVE_CALLBACK_PREFIX = "save:"
ADMIN_DELETE_CALLBACK_PREFIX = "admin-delete:"
ADMIN_CANCEL_CALLBACK_PREFIX = "admin-cancel:"
GOAL_DISABLE_CALLBACK_PREFIX = "goal-disable:"
GOAL_WAITING_KEY = "awaiting_daily_kcal_goal"
SAVED_MEAL_WAITING_KEY = "awaiting_saved_meal_value"
INVITE_WAITING_KEY = "awaiting_invite_name"
INVITE_ONLY_TEXT = "Доступ лише за запрошенням."
BLOCKED_TEXT = "Доступ до бота вимкнено."
ACCESS_ERROR_TEXT = "Не вдалося перевірити доступ. Спробуйте ще раз."
ACTIVATION_ERROR_TEXT = "Не вдалося активувати доступ. Спробуйте ще раз."
ADMIN_ERROR_TEXT = "Не вдалося виконати команду. Спробуйте ще раз."


class NotFoodError(ValueError):
    """Raised when the message does not describe consumed food."""


class SavedMealNameError(ValueError):
    """Raised when an explicitly chosen saved-meal name is unavailable."""


class CompositeMealWeightError(ValueError):
    """Raised when weight editing is requested for a composite meal."""


@dataclass(frozen=True)
class MealReply:
    text: str
    telegram_message_id: int
    accounting_day: date
    can_save: bool = True


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


def _highlight_calories(value: str) -> str:
    return f"<b><u>{value}</u></b>"


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
    weight_g = item.weight_g
    kcal_per_100g = item.kcal_per_100g
    calories = item.calories
    weight_estimated = item.weight_origin != "user_text"
    kcal_estimated = item.kcal_origin != "user_text"
    result_estimated = weight_estimated or kcal_estimated
    weight_prefix = "≈" if weight_estimated else ""
    kcal_prefix = "≈" if kcal_estimated else ""
    result_prefix = "≈" if result_estimated else ""
    display_name = item.name[:1].upper() + item.name[1:]
    name = html.escape(display_name)
    calorie_total = f"{result_prefix}{round_whole(calories)} кк"
    if highlight_total:
        calorie_total = _highlight_calories(calorie_total)
    if item.portion_display:
        portion = html.escape(item.portion_display)
        count_match = re.fullmatch(r"(?P<count>\d+)\s*шт\.?", item.portion_display)
        if count_match:
            count = int(count_match.group("count"))
            if count > 0:
                unit_weight = weight_g / count
                return (
                    f"{name} {calorie_total}\n"
                    f"({portion} × {weight_prefix}{round_whole(unit_weight)} г/шт. × "
                    f"{kcal_prefix}{round_whole(kcal_per_100g)} кк/100 г)"
                )
        if _portion_includes_weight(item.portion_display, weight_g):
            return (
                f"{name} {calorie_total}\n"
                f"({portion} × "
                f"{kcal_prefix}{round_whole(kcal_per_100g)} кк/100 г)"
            )
        return (
            f"{name} {calorie_total}\n"
            f"({portion} ({weight_prefix}{round_whole(weight_g)} г) × "
            f"{kcal_prefix}{round_whole(kcal_per_100g)} кк/100 г)"
        )
    return (
        f"{name} {calorie_total}\n"
        f"({weight_prefix}{round_whole(weight_g)} г × "
        f"{kcal_prefix}{round_whole(kcal_per_100g)} кк/100 г)"
    )


def _format_daily_progress(today_total: float, daily_kcal_goal: int | None) -> str:
    rounded_total = round_whole(today_total)
    if daily_kcal_goal is None:
        return _highlight_calories(f"{rounded_total} кк")
    line = f"{_highlight_calories(str(rounded_total))} із {daily_kcal_goal} кк"
    if rounded_total <= daily_kcal_goal:
        line += f" · залишилось {daily_kcal_goal - rounded_total} кк"
    return line


def format_daily_total(today_total: float, daily_kcal_goal: int | None) -> str:
    return f"За день: {_format_daily_progress(today_total, daily_kcal_goal)}"


def format_reply(
    meal: MealResult, today_total: float, daily_kcal_goal: int | None = None
) -> str:
    if len(meal.items) > 1:
        prefix = "≈" if meal.estimated else ""
        meal_name = html.escape(meal.meal_name[:1].upper() + meal.meal_name[1:])
        calculations = [
            _format_item_calculation(item, highlight_total=False) for item in meal.items
        ]
        body = [
            f"{meal_name} "
            f"{_highlight_calories(f'{prefix}{round_whole(meal.meal_kcal)} кк')}",
            *(
                f"• {calculation.replace(chr(10), chr(10) + '  ')}"
                for calculation in calculations
            ),
        ]
    else:
        body = [_format_item_calculation(meal.items[0])]
    return "\n".join([*body, "", format_daily_total(today_total, daily_kcal_goal)])


def format_week_reply(
    end_day: date,
    totals: dict[date, float],
    daily_kcal_goal: int | None = None,
) -> str:
    start_day = end_day - timedelta(days=6)
    days = [start_day + timedelta(days=offset) for offset in range(7)]
    present = {day: totals[day] for day in days if day in totals}
    if not present:
        return "За останні 7 днів записів немає."

    rounded = {day: round_whole(total) for day, total in present.items()}
    lines = ["Останні 7 днів", ""]
    for day in days:
        value = f"{rounded[day]} кк" if day in rounded else "немає записів"
        lines.append(f"• {day.strftime('%d.%m')}: {value}")
    exact_values = list(present.values())
    rounded_values = list(rounded.values())
    average = round_whole(sum(exact_values) / len(exact_values))
    lines.extend(
        [
            "",
            f"Заповнено: {len(present)} із 7 днів",
            f"У середньому: {average} кк/день",
        ]
    )
    if daily_kcal_goal is not None:
        within_goal = sum(value <= daily_kcal_goal for value in exact_values)
        lines.extend(
            [
                f"Денна ціль: {daily_kcal_goal} кк",
                f"У межах цілі: {within_goal} із {len(present)} заповнених днів",
            ]
        )
    lines.extend(
        [
            f"Найменше: {min(rounded_values)} кк",
            f"Найбільше: {max(rounded_values)} кк",
        ]
    )
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


def format_day_reply(meals: list[DayMeal], daily_kcal_goal: int | None = None) -> str:
    if not meals:
        if daily_kcal_goal is not None:
            return (
                f"=== {_format_daily_progress(0, daily_kcal_goal)}\n"
                "Сьогодні ще немає записів."
            )
        return "Сьогодні ще немає записів."

    grouped: dict[str, tuple[str, float, int]] = {}
    for meal in meals:
        display_name = re.sub(r"\s+", " ", meal.meal_name).strip()
        key = display_name.casefold()
        if key in grouped:
            original_name, calories, count = grouped[key]
            grouped[key] = (original_name, calories + meal.meal_kcal, count + 1)
        else:
            grouped[key] = (display_name, meal.meal_kcal, 1)

    total = sum(meal.meal_kcal for meal in meals)
    lines = [f"=== {_format_daily_progress(total, daily_kcal_goal)}"]
    for meal_name, meal_kcal, count in grouped.values():
        count_suffix = f" — ×{count}" if count > 1 else ""
        lines.append(f"• {round_whole(meal_kcal)} кк {meal_name}{count_suffix}")
    return "\n".join(lines)


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
    ) -> None:
        self._analyzer = analyzer
        self._store = store
        self._timezone = timezone
        self._day_start_time = day_start_time
        self._photo_storage_dir = photo_storage_dir.resolve()
        self._photo_storage_dir.mkdir(parents=True, exist_ok=True)
        self._daily_kcal_goal = daily_kcal_goal
        self._saved_store = saved_store
        # A message analysis can take long enough for a deletion callback to be
        # handled in between its first read and the eventual append.  Keep the
        # read/append and deletion operations mutually exclusive, while doing
        # the expensive analysis outside the lock.
        self._store_lock = threading.RLock()

    def set_daily_kcal_goal(self, goal: int | None) -> None:
        with self._store_lock:
            self._daily_kcal_goal = goal

    def get_day_summary(self, timestamp: datetime) -> str:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=self._timezone)
        timestamp = timestamp.astimezone(self._timezone)
        day = accounting_date(timestamp, self._timezone, self._day_start_time)
        return format_day_reply(
            self._store.get_day_meals(day),
            self._daily_kcal_goal,
        )

    def get_week_summary(self, timestamp: datetime) -> str:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=self._timezone)
        timestamp = timestamp.astimezone(self._timezone)
        end_day = accounting_date(timestamp, self._timezone, self._day_start_time)
        start_day = end_day - timedelta(days=6)
        totals = self._store.get_daily_totals(start_day, end_day)
        return format_week_reply(end_day, totals, self._daily_kcal_goal)

    def get_existing_reply(
        self, telegram_message_id: int, timestamp: datetime
    ) -> MealReply | None:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=self._timezone)
        timestamp = timestamp.astimezone(self._timezone)
        day = accounting_date(timestamp, self._timezone, self._day_start_time)
        with self._store_lock:
            state = self._store.get_state(day, telegram_message_id)
        if state.existing is None:
            return None
        return MealReply(
            text=format_reply(
                state.existing.meal,
                state.today_total,
                self._daily_kcal_goal,
            ),
            telegram_message_id=telegram_message_id,
            accounting_day=day,
        )

    def process_message(
        self,
        text: str,
        telegram_message_id: int,
        timestamp: datetime,
        image_bytes: bytes | None = None,
    ) -> MealReply:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=self._timezone)
        timestamp = timestamp.astimezone(self._timezone)

        day = accounting_date(timestamp, self._timezone, self._day_start_time)
        with self._store_lock:
            state = self._store.get_state(day, telegram_message_id)
            if state.existing is not None:
                return MealReply(
                    text=format_reply(
                        state.existing.meal,
                        state.today_total,
                        self._daily_kcal_goal,
                    ),
                    telegram_message_id=telegram_message_id,
                    accounting_day=day,
                )

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
        meal = calculate_meal(result.analysis)
        with self._store_lock:
            # Refresh after analysis: a deletion may have completed while the
            # model was working, so the earlier daily total is no longer valid.
            state = self._store.get_state(day, telegram_message_id)
            if state.existing is not None:
                return MealReply(
                    text=format_reply(
                        state.existing.meal,
                        state.today_total,
                        self._daily_kcal_goal,
                    ),
                    telegram_message_id=telegram_message_id,
                    accounting_day=day,
                )

            photo_path: str | None = None
            if image_bytes is not None:
                photo_file = (
                    self._photo_storage_dir
                    / f"{day.isoformat()}-{telegram_message_id}.jpg"
                )
                photo_file.write_bytes(image_bytes)
                photo_path = str(photo_file)
            stored = self._store.append_meal(
                timestamp,
                telegram_message_id,
                text,
                normalized.text,
                photo_path,
                meal,
                result.metadata,
            )
            today_total = state.today_total + stored.meal.meal_kcal
            return MealReply(
                text=format_reply(stored.meal, today_total, self._daily_kcal_goal),
                telegram_message_id=telegram_message_id,
                accounting_day=day,
            )

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
            if source.normalized_request.startswith("saved_meal:"):
                source_id = source.normalized_request.split(":", maxsplit=2)[1]
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
        suggest = getattr(self._analyzer, "suggest_meal_icon", None)
        if not callable(suggest):
            return None
        try:
            suggestion = suggest(meal)
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
        normalized_request: str,
        metadata_model: str,
        *,
        can_save: bool,
    ) -> MealReply:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=self._timezone)
        timestamp = timestamp.astimezone(self._timezone)
        day = accounting_date(timestamp, self._timezone, self._day_start_time)
        with self._store_lock:
            state = self._store.get_state(day, event_id)
            if state.existing is None:
                stored = self._store.append_meal(
                    timestamp,
                    event_id,
                    "",
                    normalized_request,
                    None,
                    meal,
                    LLMMetadata(model=metadata_model, effort="none"),
                )
                total = state.today_total + stored.meal.meal_kcal
            else:
                stored = state.existing
                total = state.today_total
        return MealReply(
            text=format_reply(stored.meal, total, self._daily_kcal_goal),
            telegram_message_id=event_id,
            accounting_day=day,
            can_save=can_save,
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
            f"saved_meal:{saved_meal_id}:{weight_g}g",
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
            f"recent_meal:{message_id}:{weight_g}g",
            "recent_meal",
            can_save=(
                not already_saved
                and not recent.normalized_request.startswith("saved_meal:")
            ),
        )

    def rename_saved_meal(self, saved_meal_id: str, name: str) -> SavedMeal | None:
        normalized = self._normalize_saved_name(name)
        with self._store_lock:
            store = self._saved()
            current = store.get(saved_meal_id)
            if current is None:
                return None
            if any(
                meal.saved_meal_id != saved_meal_id
                and meal.display_name.casefold() == normalized.casefold()
                for meal in store.list_meals()
            ):
                raise SavedMealNameError(
                    "Страва з такою назвою вже є. Вибери іншу назву."
                )
            return store.rename(saved_meal_id, normalized)

    def set_saved_meal_weight(
        self, saved_meal_id: str, weight_g: int
    ) -> SavedMeal | None:
        if not 1 <= weight_g <= MAX_WEIGHT_G:
            raise ValueError
        with self._store_lock:
            store = self._saved()
            saved = store.get(saved_meal_id)
            if saved is None:
                return None
            if len(saved.base_meal.items) != 1:
                raise CompositeMealWeightError
            return store.set_default_weight(saved_meal_id, weight_g)

    def change_meal_weight(
        self, message_id: int, day: date, weight_g: int
    ) -> MealReply | None:
        with self._store_lock:
            source = self._store.get_meal(day, message_id)
            if source is None:
                return None
            if len(source.meal.items) != 1:
                raise CompositeMealWeightError
            updated = self._store.update_meal(
                day, message_id, scale_meal(source.meal, weight_g)
            )
            if updated is None:
                return None
            can_save = (
                not source.normalized_request.startswith("saved_meal:")
                and self._saved().find_by_source(message_id) is None
            )
        return MealReply(
            text=format_reply(
                updated.meal.meal, updated.day_total, self._daily_kcal_goal
            ),
            telegram_message_id=message_id,
            accounting_day=updated.accounting_day,
            can_save=can_save,
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
                )
                self._services[key] = service
            else:
                service.set_daily_kcal_goal(user.daily_kcal_goal)
            return service

    def create_invite(self, display_name: str) -> str:
        for _ in range(3):
            token = secrets.token_urlsafe(24)
            try:
                self._registry.create_invite(
                    display_name, token, self._default_day_start
                )
                return token
            except UserRegistryError as exc:
                if "collision" not in str(exc).lower():
                    raise
        raise UserRegistryError("Could not generate a unique invite token")

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
    def __init__(self, admin_user_id: int, manager: UserManager) -> None:
        self._admin_user_id = admin_user_id
        self._manager = manager

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
    def _goal_state(
        context: ContextTypes.DEFAULT_TYPE | object | None,
    ) -> dict[str, object]:
        data = getattr(context, "user_data", None)
        return data if isinstance(data, dict) else {}

    @classmethod
    def _cancel_goal_wait(
        cls, context: ContextTypes.DEFAULT_TYPE | object | None
    ) -> None:
        state = cls._goal_state(context)
        state.pop(GOAL_WAITING_KEY, None)
        state.pop(SAVED_MEAL_WAITING_KEY, None)
        state.pop(INVITE_WAITING_KEY, None)

    @classmethod
    def _start_waiting(
        cls,
        context: ContextTypes.DEFAULT_TYPE | object | None,
        key: str,
        value: object = True,
    ) -> None:
        cls._cancel_goal_wait(context)
        cls._goal_state(context)[key] = value

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
        self._cancel_goal_wait(context)
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
        self._cancel_goal_wait(context)
        message = update.effective_message
        if message is None:
            return
        if self._is_admin(update):
            await message.reply_text(load_help_text(admin=True), do_quote=False)
            return
        if await self._active_service(update) is not None:
            await message.reply_text(load_help_text(), do_quote=False)

    async def tips(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._cancel_goal_wait(context)
        message = update.effective_message
        if message is None:
            return
        if self._is_admin(update) or await self._active_service(update) is not None:
            await message.reply_text(load_tips_text(), do_quote=False)

    async def day(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._cancel_goal_wait(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        try:
            reply = await asyncio.to_thread(service.get_day_summary, message.date)
        except SheetsReadError:
            LOGGER.exception("Could not read the calorie log for /day")
            reply = READ_ERROR_TEXT
        except Exception:
            LOGGER.exception("Unexpected error while handling /day")
            reply = READ_ERROR_TEXT
        await message.reply_text(reply, parse_mode=ParseMode.HTML, do_quote=False)

    async def week(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._cancel_goal_wait(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        try:
            reply = await asyncio.to_thread(service.get_week_summary, message.date)
        except Exception:
            LOGGER.exception("Could not read the calorie log for /week")
            reply = WEEK_ERROR_TEXT
        await message.reply_text(reply, do_quote=False)

    @staticmethod
    def _short_button_name(name: str, limit: int = 34) -> str:
        return name if len(name) <= limit else f"{name[: limit - 1]}…"

    @classmethod
    def _saved_meals_keyboard(cls, meals: list[SavedMeal]) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    f"{f'{meal.icon} ' if meal.icon else ''}"
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
                        f"🍽️ {cls._short_button_name(item.meal.meal_name)} · "
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
        self._cancel_goal_wait(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        try:
            meals = await asyncio.to_thread(service.list_saved_meals)
        except Exception:
            LOGGER.exception("Could not list saved meals")
            await message.reply_text(
                "Не вдалося прочитати збережені страви. Спробуй ще раз.",
                do_quote=False,
            )
            return
        text = "Збережені страви:" if meals else "Збережених страв ще немає."
        await message.reply_text(
            text, reply_markup=self._saved_meals_keyboard(meals), do_quote=False
        )

    async def recent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._cancel_goal_wait(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        try:
            recent = await asyncio.to_thread(service.list_recent_meals)
        except Exception:
            LOGGER.exception("Could not list recent meals")
            await message.reply_text(
                "Не вдалося прочитати нещодавні страви. Спробуй ще раз.",
                do_quote=False,
            )
            return
        text = "Нещодавні страви:" if recent else "Нещодавніх страв немає."
        await message.reply_text(
            text,
            reply_markup=self._recent_meals_keyboard(recent) if recent else None,
            do_quote=False,
        )

    async def save(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._cancel_goal_wait(context)
        message = update.effective_message
        if message is None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        name = " ".join(context.args or []).strip() or None
        try:
            saved, created = await asyncio.to_thread(service.save_latest_meal, name)
        except SavedMealNameError as exc:
            await message.reply_text(str(exc), do_quote=False)
            return
        except Exception:
            LOGGER.exception("Could not save the latest meal")
            await message.reply_text(
                "Не вдалося запам’ятати страву. Спробуй ще раз.", do_quote=False
            )
            return
        if saved is None:
            reply = "Немає запису, який можна запам’ятати."
        elif created:
            reply = f"Запам’ятано: {saved.display_name} ✓"
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
            self._cancel_goal_wait(context)
            return
        args = list(getattr(context, "args", None) or [])
        if args:
            self._cancel_goal_wait(context)
            if len(args) != 1:
                await message.reply_text(self._goal_validation_text(), do_quote=False)
                return
            await self._save_goal(update, args[0])
            return

        self._start_waiting(context, GOAL_WAITING_KEY)
        keyboard = None
        if current.daily_kcal_goal is None:
            reply = "Яка твоя денна ціль калорій?\nНадішли ціле число, наприклад: 2000"
        else:
            reply = (
                f"Поточна денна ціль: {current.daily_kcal_goal} кк.\n"
                "Надішли нове ціле число або вимкни ціль."
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🚫 Вимкнути ціль",
                            callback_data=f"{GOAL_DISABLE_CALLBACK_PREFIX}{user.id}",
                        )
                    ]
                ]
            )
        await message.reply_text(reply, reply_markup=keyboard, do_quote=False)

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
        try:
            await asyncio.to_thread(self._manager.set_daily_kcal_goal, user.id, None)
        except Exception:
            LOGGER.exception("Could not disable daily goal")
            await query.answer(GOAL_ERROR_TEXT, show_alert=True)
            return
        self._cancel_goal_wait(context)
        await query.answer()
        await query.edit_message_text("Денну ціль вимкнено ✓", reply_markup=None)

    async def delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None:
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
            await query.edit_message_text(
                f"Видалено\n=== {_format_daily_progress(deletion.day_total, None)}",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except Exception:
            LOGGER.warning("Could not edit deletion confirmation", exc_info=True)

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
                "Не вдалося запам’ятати страву. Спробуй ще раз.",
                show_alert=True,
            )
            return
        if saved is None:
            await query.answer("Цього запису вже немає. Онови список.", show_alert=True)
            return
        await query.answer(
            f"{'Запам’ятано' if created else 'Вже збережено'}: {saved.display_name}"
        )
        try:
            await query.edit_message_reply_markup(
                reply_markup=self._meal_reply_markup(
                    MealReply(
                        text="",
                        telegram_message_id=message_id,
                        accounting_day=day,
                        can_save=False,
                    )
                )
            )
        except Exception:
            LOGGER.warning("Could not hide saved-meal button", exc_info=True)

    async def meal_weight_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None:
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
        self._start_waiting(
            context,
            SAVED_MEAL_WAITING_KEY,
            {
                "kind": "meal_weight",
                "message_id": message_id,
                "day": day.isoformat(),
                "result_chat_id": query.message.chat_id,
                "result_message_id": query.message.message_id,
            },
        )
        await query.answer()
        await query.message.reply_text(
            "⚖️ Введи нову вагу в грамах, наприклад: 350",
            reply_markup=self._cancel_markup(),
            do_quote=False,
        )

    async def _edit_saved_meals_menu(
        self, query: object, service: CaloriesService
    ) -> None:
        meals = await asyncio.to_thread(service.list_saved_meals)
        text = "Збережені страви:" if meals else "Збережених страв ще немає."
        await query.edit_message_text(  # type: ignore[attr-defined]
            text, reply_markup=self._saved_meals_keyboard(meals)
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
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None:
            return
        data = query.data or ""
        if data in {"wait-cancel", "invite-cancel"}:
            self._cancel_goal_wait(context)
            await query.answer()
            await query.edit_message_text("Скасовано.", reply_markup=None)
            return
        service = await self._active_service(update, callback=True)
        if service is None:
            return
        try:
            if data in {"meals-back", "meals-manage"}:
                self._cancel_goal_wait(context)
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
                    await self._send_meal_reply(query.message, result)
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
                    await self._send_meal_reply(query.message, result)
                return
            if data.startswith(("manage-open:", "manage-delete:")):
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
        if self._is_admin(update) and self._goal_state(context).get(INVITE_WAITING_KEY):
            await self._finish_invite(update, context, message.text)
            return
        service = await self._active_service(update)
        if service is None:
            return
        if self._goal_state(context).get(GOAL_WAITING_KEY):
            if await self._save_goal(update, message.text):
                self._cancel_goal_wait(context)
            return
        saved_state = self._goal_state(context).get(SAVED_MEAL_WAITING_KEY)
        if isinstance(saved_state, dict):
            await self._handle_saved_waiting(
                update, context, service, saved_state, message.text
            )
            return
        await self._process(service, message, message.text)

    async def _handle_saved_waiting(
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
        kind = str(state.get("kind", ""))
        try:
            if kind == "meal_weight":
                result = await asyncio.to_thread(
                    service.change_meal_weight,
                    int(str(state["message_id"])),
                    date.fromisoformat(str(state["day"])),
                    self._parse_weight(text),
                )
                if result is None:
                    raise LookupError
                self._cancel_goal_wait(context)
                try:
                    await context.bot.edit_message_text(
                        chat_id=int(str(state["result_chat_id"])),
                        message_id=int(str(state["result_message_id"])),
                        text=result.text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=self._meal_reply_markup(result),
                    )
                except Exception:
                    LOGGER.warning(
                        "Meal weight was updated but the reply could not be edited",
                        exc_info=True,
                    )
                await message.reply_text(
                    result.text,
                    parse_mode=ParseMode.HTML,
                    do_quote=False,
                )
                return
            if kind == "rename":
                saved = await asyncio.to_thread(
                    service.rename_saved_meal,
                    str(state["saved_meal_id"]),
                    text,
                )
                if saved is None:
                    raise LookupError
                self._cancel_goal_wait(context)
                await message.reply_text(
                    f"Перейменовано: {saved.display_name} ✓", do_quote=False
                )
                return
            if kind == "default_weight":
                saved = await asyncio.to_thread(
                    service.set_saved_meal_weight,
                    str(state["saved_meal_id"]),
                    self._parse_weight(text),
                )
                if saved is None:
                    raise LookupError
                self._cancel_goal_wait(context)
                await message.reply_text(
                    f"Стандартна вага: {saved.default_total_weight_g} г ✓",
                    do_quote=False,
                )
                return
            raise ValueError
        except CompositeMealWeightError:
            self._cancel_goal_wait(context)
            await message.reply_text(
                "Змінити вагу можна лише для страви з одного компонента.",
                do_quote=False,
            )
        except SavedMealNameError as exc:
            await message.reply_text(
                f"{exc}\nСпробуй ще раз або натисни «Скасувати».",
                reply_markup=self._cancel_markup(),
                do_quote=False,
            )
        except ValueError:
            await message.reply_text(
                f"Введи ціле число від 1 до {MAX_WEIGHT_G}, наприклад: 350",
                reply_markup=self._cancel_markup(),
                do_quote=False,
            )
        except LookupError:
            self._cancel_goal_wait(context)
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
    def _parse_goal(raw: str) -> int:
        text = raw.strip()
        if not text.isascii() or not text.isdecimal():
            raise ValueError
        from .users import MAX_DAILY_KCAL_GOAL, MIN_DAILY_KCAL_GOAL

        goal = int(text)
        if not MIN_DAILY_KCAL_GOAL <= goal <= MAX_DAILY_KCAL_GOAL:
            raise ValueError
        return goal

    @staticmethod
    def _goal_validation_text() -> str:
        from .users import MAX_DAILY_KCAL_GOAL, MIN_DAILY_KCAL_GOAL

        return (
            f"Надішли ціле число від {MIN_DAILY_KCAL_GOAL} до "
            f"{MAX_DAILY_KCAL_GOAL}, наприклад: 2000"
        )

    async def _save_goal(self, update: Update, raw: str) -> bool:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return False
        try:
            goal = self._parse_goal(raw)
        except ValueError:
            await message.reply_text(self._goal_validation_text(), do_quote=False)
            return False
        try:
            await asyncio.to_thread(self._manager.set_daily_kcal_goal, user.id, goal)
        except Exception:
            LOGGER.exception("Could not save daily goal")
            await message.reply_text(GOAL_ERROR_TEXT, do_quote=False)
            return False
        await message.reply_text(f"Денну ціль встановлено: {goal} кк ✓", do_quote=False)
        return True

    async def photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._cancel_goal_wait(context)
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
            await self._send_meal_reply(message, existing)
            return
        try:
            telegram_file = await message.photo[-1].get_file()
            image_bytes = bytes(await telegram_file.download_as_bytearray())
        except Exception:
            LOGGER.exception("Could not download the Telegram photo")
            await message.reply_text(ANALYSIS_ERROR_TEXT, do_quote=False)
            return
        await self._process(service, message, message.caption or "", image_bytes)

    @staticmethod
    def _meal_reply_markup(result: MealReply) -> InlineKeyboardMarkup:
        delete_callback = (
            f"{DELETE_CALLBACK_PREFIX}{result.telegram_message_id}:"
            f"{result.accounting_day.isoformat()}"
        )
        weight_callback = (
            f"meal-weight:{result.telegram_message_id}:"
            f"{result.accounting_day.isoformat()}"
        )
        first_row: list[InlineKeyboardButton] = []
        if result.can_save:
            first_row.append(
                InlineKeyboardButton(
                    "⭐ Запам’ятати",
                    callback_data=(
                        f"{SAVE_CALLBACK_PREFIX}{result.telegram_message_id}:"
                        f"{result.accounting_day.isoformat()}"
                    ),
                )
            )
        first_row.append(
            InlineKeyboardButton("⚖️ Змінити вагу", callback_data=weight_callback)
        )
        return InlineKeyboardMarkup(
            [
                first_row,
                [InlineKeyboardButton("🗑 Видалити", callback_data=delete_callback)],
            ]
        )

    @classmethod
    async def _send_meal_reply(cls, message: Message, result: MealReply) -> None:
        await message.reply_text(
            result.text,
            parse_mode=ParseMode.HTML,
            reply_markup=cls._meal_reply_markup(result),
            do_quote=False,
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
    ) -> None:
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
            await self._send_meal_reply(message, result)
            return
        await message.reply_text(reply, do_quote=False)

    async def invite(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._cancel_goal_wait(context)
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
        if not display_name:
            await message.reply_text(
                "Ім’я не може бути порожнім. Введи ім’я або скасуй.",
                reply_markup=self._cancel_markup(invite=True),
                do_quote=False,
            )
            return
        try:
            token = await asyncio.to_thread(self._manager.create_invite, display_name)
            bot_user = await context.bot.get_me()
            if not bot_user.username:
                raise RuntimeError("Bot has no username")
        except Exception:
            LOGGER.exception("Could not create invite")
            await message.reply_text(ADMIN_ERROR_TEXT, do_quote=False)
            return
        self._cancel_goal_wait(context)
        await message.reply_text(
            f"https://t.me/{bot_user.username}?start={token}", do_quote=False
        )

    async def users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._cancel_goal_wait(context)
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
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
        self._cancel_goal_wait(context)
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        try:
            telegram_user_id = self._parse_admin_user_id(context.args)
            await asyncio.to_thread(self._manager.set_status, telegram_user_id, status)
        except ValueError:
            command = "block" if status == "blocked" else "unblock"
            await message.reply_text(
                f"Формат: /{command} <telegram_user_id>", do_quote=False
            )
            return
        except Exception:
            LOGGER.exception("Could not change user status")
            await message.reply_text(ADMIN_ERROR_TEXT, do_quote=False)
            return
        label = "Заблоковано" if status == "blocked" else "Розблоковано"
        await message.reply_text(f"{label}: {telegram_user_id}", do_quote=False)

    async def delete_user_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._cancel_goal_wait(context)
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        try:
            telegram_user_id = self._parse_admin_user_id(context.args)
            user = await asyncio.to_thread(self._manager.get_user, telegram_user_id)
            if user is None:
                raise UserRegistryError("User not found")
        except ValueError:
            await message.reply_text(
                "Формат: /delete <telegram_user_id>", do_quote=False
            )
            return
        except Exception:
            LOGGER.exception("Could not prepare user deletion")
            await message.reply_text(ADMIN_ERROR_TEXT, do_quote=False)
            return
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🗑 Підтвердити видалення",
                        callback_data=f"{ADMIN_DELETE_CALLBACK_PREFIX}{telegram_user_id}",
                    ),
                    InlineKeyboardButton(
                        "❌ Скасувати",
                        callback_data=f"{ADMIN_CANCEL_CALLBACK_PREFIX}{telegram_user_id}",
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

    async def cancel_goal_waiting(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del update
        self._cancel_goal_wait(context)

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
        text = raw.strip()
        if not text.isascii() or not text.isdecimal():
            raise ValueError
        weight = int(text)
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
