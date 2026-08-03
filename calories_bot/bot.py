from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Message, Update
from telegram.constants import ChatType
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
    "#120 і 120# означають 120 ккал/100 г. Використовуйте лише цілі числа."
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


class NotFoodError(ValueError):
    """Raised when the message does not describe consumed food."""


def format_reply(meal: MealResult, today_total: int, normalized_request: str) -> str:
    approximation = "≈" if meal.estimated else ""
    lines = [
        normalized_request,
        f"Калорії: {approximation}{round_whole(meal.meal_kcal)} ккал",
        f"На 100 г: {approximation}{round_whole(meal.kcal_per_100g)} ккал",
        f"За сьогодні: {today_total} ккал",
    ]

    estimated_weights = [item for item in meal.items if item.weight_estimated]
    if estimated_weights:
        if len(meal.items) == 1:
            lines.append(f"Оцінена вага: ≈{round_whole(meal.total_weight_g)} г")
        else:
            weights = ", ".join(
                f"{item.name} — ≈{round_whole(item.weight_g)} г"
                for item in estimated_weights
            )
            lines.append(f"Оцінена вага: {weights}")

    if len(meal.items) > 1:
        lines.append("Склад:")
        for item in meal.items:
            item_approximation = (
                "≈" if item.weight_estimated or item.kcal_estimated else ""
            )
            lines.append(
                f"• {item.name}: {item_approximation}{round_whole(item.calories)} ккал"
            )
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

    def process_message(
        self,
        text: str,
        telegram_message_id: int,
        timestamp: datetime,
        image_bytes: bytes | None = None,
    ) -> str:
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
        state = self._store.get_state(day, telegram_message_id)
        if state.existing is not None:
            display_request = (
                state.existing.meal.meal_name
                if state.existing.photo_path
                else state.existing.normalized_request
            )
            return format_reply(
                state.existing.meal,
                state.today_total,
                display_request,
            )

        result = self._analyzer.analyze(normalized, image_bytes)
        if not result.analysis.is_food:
            raise NotFoodError
        meal = calculate_meal(result.analysis)
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
        display_request = (
            stored.meal.meal_name if stored.photo_path else stored.normalized_request
        )
        return format_reply(stored.meal, today_total, display_request)


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
            reply = await asyncio.to_thread(
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
        await message.reply_text(reply)
