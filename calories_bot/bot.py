from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import shutil
import threading
from dataclasses import dataclass
from datetime import date, datetime, time
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
from .models import CalculatedFoodItem, MealResult, calculate_meal, round_whole
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

HELP_TEXT_FILE = Path(__file__).with_name("help.txt")
ADMIN_HELP_TEXT_FILE = Path(__file__).with_name("admin_help.txt")
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
ADMIN_DELETE_CALLBACK_PREFIX = "admin-delete:"
ADMIN_CANCEL_CALLBACK_PREFIX = "admin-cancel:"
INVITE_ONLY_TEXT = "Доступ лише за запрошенням."
BLOCKED_TEXT = "Доступ до бота вимкнено."
ACCESS_ERROR_TEXT = "Не вдалося перевірити доступ. Спробуйте ще раз."
ACTIVATION_ERROR_TEXT = "Не вдалося активувати доступ. Спробуйте ще раз."
ADMIN_ERROR_TEXT = "Не вдалося виконати команду. Спробуйте ще раз."

class NotFoodError(ValueError):
    """Raised when the message does not describe consumed food."""


@dataclass(frozen=True)
class MealReply:
    text: str
    telegram_message_id: int
    accounting_day: date


def load_help_text(*, admin: bool = False) -> str:
    text = HELP_TEXT_FILE.read_text(encoding="utf-8").strip()
    if admin:
        admin_text = ADMIN_HELP_TEXT_FILE.read_text(encoding="utf-8").strip()
        return f"{text}\n\n{admin_text}"
    return text


def _format_item_calculation(item: CalculatedFoodItem) -> str:
    weight_g = item.weight_g
    kcal_per_100g = item.kcal_per_100g
    calories = item.calories
    weight_estimated = item.weight_estimated
    kcal_estimated = item.kcal_estimated
    result_estimated = weight_estimated or kcal_estimated
    weight_prefix = "≈" if weight_estimated else ""
    kcal_prefix = "≈" if kcal_estimated else ""
    result_prefix = "≈" if result_estimated else ""
    display_name = item.name[:1].upper() + item.name[1:]
    name = html.escape(display_name)
    return (
        f"{name} {result_prefix}{round_whole(calories)} кк = "
        f"{weight_prefix}{round_whole(weight_g)} г × "
        f"{kcal_prefix}{round_whole(kcal_per_100g)} кк/100 г"
    )


def format_reply(meal: MealResult, today_total: int) -> str:
    lines = [
        *(_format_item_calculation(item) for item in meal.items),
        f"=== {today_total} кк",
    ]
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
        lines.append(f"• {meal_kcal} кк {meal_name}{count_suffix}")
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
                return MealReply(
                    text=format_reply(state.existing.meal, state.today_total),
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
                return MealReply(
                    text=format_reply(state.existing.meal, state.today_total),
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
            return MealReply(
                text=format_reply(stored.meal, today_total),
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


class Workspace(Protocol):
    def open_meal_store(
        self, spreadsheet_id: str, day_start: time, telegram_user_id: int
    ) -> MealStore: ...

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
                service = CaloriesService(
                    self._analyzer,
                    store,
                    self._timezone,
                    user.day_start,
                    self._photo_storage_dir / str(user.telegram_user_id),
                )
                self._services[key] = service
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

    async def _active_service(
        self, update: Update, *, callback: bool = False
    ) -> CaloriesService | None:
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
        try:
            return await asyncio.to_thread(self._manager.service_for, user)
        except Exception:
            LOGGER.exception("Could not build current user context")
            await self._send_access_text(update, ACCESS_ERROR_TEXT, callback)
            return None

    @staticmethod
    async def _send_access_text(update: Update, text: str, callback: bool) -> None:
        if callback and update.callback_query is not None:
            await update.callback_query.answer(text, show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(text, do_quote=False)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            text = (
                load_help_text(admin=True)
                if existing.status == "active" and self._is_admin(update)
                else load_help_text() if existing.status == "active" else BLOCKED_TEXT
            )
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
        await message.reply_text(load_help_text(), do_quote=False)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        message = update.effective_message
        if message is None:
            return
        if self._is_admin(update):
            await message.reply_text(load_help_text(admin=True), do_quote=False)
            return
        if await self._active_service(update) is not None:
            await message.reply_text(load_help_text(), do_quote=False)

    async def day(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
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
        await message.reply_text(reply, do_quote=False)

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
                f"Видалено\n=== {deletion.day_total} кк", reply_markup=None
            )
        except Exception:
            LOGGER.warning("Could not edit deletion confirmation", exc_info=True)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        message = update.effective_message
        if message is None or not message.text:
            return
        service = await self._active_service(update)
        if service is not None:
            await self._process(service, message, message.text)

    async def photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        message = update.effective_message
        if message is None or not message.photo or message.media_group_id is not None:
            return
        service = await self._active_service(update)
        if service is None:
            return
        try:
            telegram_file = await message.photo[-1].get_file()
            image_bytes = bytes(await telegram_file.download_as_bytearray())
        except Exception:
            LOGGER.exception("Could not download the Telegram photo")
            await message.reply_text(ANALYSIS_ERROR_TEXT, do_quote=False)
            return
        await self._process(service, message, message.caption or "", image_bytes)

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
                do_quote=False,
            )
            return
        await message.reply_text(reply, do_quote=False)

    async def invite(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not self._is_admin(update) or message is None:
            await self._reject_admin_command(update)
            return
        display_name = " ".join(context.args or []).strip()
        if not display_name:
            await message.reply_text("Формат: /invite <імʼя>", do_quote=False)
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
        await message.reply_text(
            f"https://t.me/{bot_user.username}?start={token}", do_quote=False
        )

    async def users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
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
                        "Підтвердити видалення",
                        callback_data=f"{ADMIN_DELETE_CALLBACK_PREFIX}{telegram_user_id}",
                    ),
                    InlineKeyboardButton(
                        "Скасувати",
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
