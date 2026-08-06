from __future__ import annotations

import asyncio
import html
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
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
from .models import MealResult, calculate_meal, round_whole
from .sheets import (
    DayMeal,
    MealDeletion,
    MealStore,
    SheetsReadError,
    SheetsWriteError,
    SheetsWriteUncertainError,
    accounting_date,
)

LOGGER = logging.getLogger(__name__)

HELP_TEXT = (
    "Опишіть те, що ви зʼїли, або надішліть одне фото страви. Приклади:\n"
    "• сир 50\n"
    "• сир 50г 120#\n"
    "• #120 сир 50 г\n"
    "• 2 яйця, хліб 50\n"
    "• фото з підписом 200 г\n\n"
    "#120 і 120# означають 120 кк/100 г. Використовуйте лише цілі числа.\n\n"
    "/day — показати всі прийоми їжі за сьогодні."
)
NOT_FOOD_TEXT = "Не вдалося розпізнати страву. Уточніть, що саме ви зʼїли."
FORMAT_ERROR_TEXT = (
    "Некоректний формат. Використовуйте цілі числа, наприклад: "
    "сир 50г 120# або #120 сир 50."
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
DELETE_ERROR_TEXT = "Не вдалося видалити запис. Спробуйте ще раз."
DELETE_CALLBACK_PREFIX = "delete:"

_DISPLAY_KCAL = re.compile(r"(?<!\w)\d+ ккал/100г(?!\w)")
_DISPLAY_WEIGHT = re.compile(r"(?<!\w)(\d+) гр(?!\w)")


class NotFoodError(ValueError):
    """Raised when the message does not describe consumed food."""


@dataclass(frozen=True)
class MealReply:
    text: str
    telegram_message_id: int
    accounting_day: date


def _compact_description(meal: MealResult, normalized_request: str) -> str:
    description = _DISPLAY_KCAL.sub("", normalized_request)
    description = _DISPLAY_WEIGHT.sub(r"\1 г", description)
    description = re.sub(r"[ \t]+", " ", description)
    description = re.sub(r"\s+([,;:.!?])", r"\1", description).strip()
    description = description.strip(" ,;") or meal.meal_name

    parts = [description]
    if any(item.weight_estimated for item in meal.items):
        parts.append(f"≈{round_whole(meal.total_weight_g)} г")
    parts.append(f"#{round_whole(meal.kcal_per_100g)}")
    return html.escape(" ".join(parts))


def format_reply(meal: MealResult, today_total: int, normalized_request: str) -> str:
    return "\n".join(
        [
            f"<b>{round_whole(meal.meal_kcal)} кк</b>",
            _compact_description(meal, normalized_request),
            f"=== {today_total} кк",
        ]
    )


def format_day_reply(meals: list[DayMeal]) -> str:
    if not meals:
        return "Сьогодні ще немає записів."

    grouped: dict[str, tuple[str, int, int]] = {}
    for meal in meals:
        display_name = re.sub(r"\s+", " ", meal.meal_name).strip()
        key = display_name.casefold()
        if key in grouped:
            original_name, calories, count = grouped[key]
            grouped[key] = (original_name, calories + meal.meal_kcal, count + 1)
        else:
            grouped[key] = (display_name, meal.meal_kcal, 1)

    lines = [f"=== {sum(meal.meal_kcal for meal in meals)} кк"]
    for meal_name, meal_kcal, count in grouped.values():
        count_suffix = f" — ×{count}" if count > 1 else ""
        lines.append(f"{meal_kcal} кк {meal_name}{count_suffix}")
    return "\n".join(lines)


class CaloriesService:
    def __init__(
        self,
        analyzer: Analyzer,
        store: MealStore,
        timezone: ZoneInfo,
        day_start_time: time,
        photo_storage_dir: Path,
    ) -> None:
        self._analyzer = analyzer
        self._store = store
        self._timezone = timezone
        self._day_start_time = day_start_time
        self._photo_storage_dir = photo_storage_dir.resolve()
        self._photo_storage_dir.mkdir(parents=True, exist_ok=True)
        # A message analysis can take long enough for a deletion callback to be
        # handled in between its first read and the eventual append.  Keep the
        # read/append and deletion operations mutually exclusive, while doing
        # the expensive analysis outside the lock.
        self._store_lock = threading.RLock()

    def get_day_summary(self, timestamp: datetime) -> str:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=self._timezone)
        timestamp = timestamp.astimezone(self._timezone)
        day = accounting_date(timestamp, self._timezone, self._day_start_time)
        return format_day_reply(self._store.get_day_meals(day))

    def process_message(
        self,
        text: str,
        telegram_message_id: int,
        timestamp: datetime,
        image_bytes: bytes | None = None,
    ) -> MealReply:
        normalized = (
            normalize_input(text)
            if text.strip()
            else NormalizedInput(text="", explicit_values=())
        )
        if image_bytes is None and not normalized.text:
            raise InputFormatError("Message is empty")
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=self._timezone)
        timestamp = timestamp.astimezone(self._timezone)

        day = accounting_date(timestamp, self._timezone, self._day_start_time)
        with self._store_lock:
            state = self._store.get_state(day, telegram_message_id)
            if state.existing is not None:
                display_request = (
                    state.existing.normalized_request
                    or state.existing.meal.meal_name
                )
                return MealReply(
                    text=format_reply(
                        state.existing.meal,
                        state.today_total,
                        display_request,
                    ),
                    telegram_message_id=telegram_message_id,
                    accounting_day=day,
                )

        result = self._analyzer.analyze(normalized, image_bytes)
        if not result.analysis.is_food:
            raise NotFoodError
        meal = calculate_meal(result.analysis)
        with self._store_lock:
            # Refresh after analysis: a deletion may have completed while the
            # model was working, so the earlier daily total is no longer valid.
            state = self._store.get_state(day, telegram_message_id)
            if state.existing is not None:
                display_request = (
                    state.existing.normalized_request
                    or state.existing.meal.meal_name
                )
                return MealReply(
                    text=format_reply(
                        state.existing.meal,
                        state.today_total,
                        display_request,
                    ),
                    telegram_message_id=telegram_message_id,
                    accounting_day=day,
                )

            photo_path: str | None = None
            if image_bytes is not None:
                photo_file = self._photo_storage_dir / f"{telegram_message_id}.jpg"
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
            today_total = state.today_total + round_whole(stored.meal.meal_kcal)
            display_request = stored.normalized_request or stored.meal.meal_name
            return MealReply(
                text=format_reply(stored.meal, today_total, display_request),
                telegram_message_id=telegram_message_id,
                accounting_day=day,
            )

    def delete_message(
        self, telegram_message_id: int, fallback_day: date
    ) -> MealDeletion:
        with self._store_lock:
            deletion = self._store.delete_meal(telegram_message_id, fallback_day)
        if deletion.photo_path:
            self._delete_photo(deletion.photo_path)
        elif not deletion.deleted:
            self._delete_photo(
                str(self._photo_storage_dir / f"{telegram_message_id}.jpg")
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


class TelegramHandlers:
    def __init__(
        self, allowed_user_id: int, allowed_chat_id: int, service: CaloriesService
    ) -> None:
        self._allowed_user_id = allowed_user_id
        self._allowed_chat_id = allowed_chat_id
        self._service = service

    def _is_allowed(self, update: Update) -> bool:
        return (
            update.effective_user is not None
            and update.effective_user.id == self._allowed_user_id
            and update.effective_chat is not None
            and update.effective_chat.id == self._allowed_chat_id
            and update.effective_chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._is_allowed(update) or update.effective_message is None:
            return
        await update.effective_message.reply_text(HELP_TEXT)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.start(update, context)

    async def day(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        message = update.effective_message
        if not self._is_allowed(update) or message is None:
            return

        try:
            reply = await asyncio.to_thread(
                self._service.get_day_summary,
                message.date,
            )
        except SheetsReadError:
            LOGGER.exception("Could not read the calorie log for /day")
            reply = READ_ERROR_TEXT
        except Exception:
            LOGGER.exception("Unexpected error while handling /day")
            reply = READ_ERROR_TEXT
        await message.reply_text(reply)

    async def delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._is_allowed(update):
            await query.answer("Недоступно.", show_alert=True)
            return

        try:
            telegram_message_id, fallback_day = self._parse_delete_callback(query.data)
        except (TypeError, ValueError):
            await query.answer("Некоректна кнопка.", show_alert=True)
            return

        try:
            deletion = await asyncio.to_thread(
                self._service.delete_message,
                telegram_message_id,
                fallback_day,
            )
        except (SheetsReadError, SheetsWriteError):
            LOGGER.exception("Could not delete the calorie log row")
            await query.answer(DELETE_ERROR_TEXT, show_alert=True)
            return
        except Exception:
            LOGGER.exception("Unexpected error while deleting a calorie log row")
            await query.answer(DELETE_ERROR_TEXT, show_alert=True)
            return

        try:
            await query.answer()
        except Exception:
            LOGGER.warning("Could not acknowledge deletion callback", exc_info=True)
        try:
            await context.bot.delete_message(
                chat_id=self._allowed_chat_id,
                message_id=telegram_message_id,
            )
        except Exception:
            LOGGER.warning(
                "Could not delete Telegram message %s",
                telegram_message_id,
                exc_info=True,
            )
        try:
            await query.edit_message_text(
                f"Видалено\n=== {deletion.day_total} кк",
                reply_markup=None,
            )
        except Exception:
            LOGGER.warning("Could not edit deletion confirmation", exc_info=True)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        message = update.effective_message
        if not self._is_allowed(update) or message is None or not message.text:
            return

        await self._process(message, message.text)

    async def photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        message = update.effective_message
        if (
            not self._is_allowed(update)
            or message is None
            or not message.photo
            or message.media_group_id is not None
        ):
            return

        try:
            telegram_file = await message.photo[-1].get_file()
            image_bytes = bytes(await telegram_file.download_as_bytearray())
        except Exception:
            LOGGER.exception("Could not download the Telegram photo")
            await message.reply_text(ANALYSIS_ERROR_TEXT)
            return

        await self._process(message, message.caption or "", image_bytes)

    async def _process(
        self, message: Message, text: str, image_bytes: bytes | None = None
    ) -> None:
        try:
            result = await asyncio.to_thread(
                self._service.process_message,
                text,
                message.message_id,
                message.date,
                image_bytes,
            )
        except InputFormatError:
            reply = FORMAT_ERROR_TEXT
        except NotFoodError:
            reply = NOT_FOOD_TEXT
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
            callback_data = (
                f"{DELETE_CALLBACK_PREFIX}{result.telegram_message_id}:"
                f"{result.accounting_day.isoformat()}"
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Видалити", callback_data=callback_data)]]
            )
            await message.reply_text(
                result.text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        await message.reply_text(reply)

    @staticmethod
    def _parse_delete_callback(data: str | None) -> tuple[int, date]:
        if data is None or not data.startswith(DELETE_CALLBACK_PREFIX):
            raise ValueError("Unknown callback")
        message_id_raw, day_raw = data.removeprefix(DELETE_CALLBACK_PREFIX).split(
            ":", maxsplit=1
        )
        message_id = int(message_id_raw)
        if message_id <= 0:
            raise ValueError("Invalid Telegram message ID")
        return message_id, date.fromisoformat(day_raw)
