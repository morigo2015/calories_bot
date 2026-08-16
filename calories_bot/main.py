from __future__ import annotations

import asyncio
import logging
from datetime import time
from functools import partial
from typing import Any

import gspread
from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .analytics import AnalyticsStore, BotStatistics, OpenAICostClient
from .analyzer import OpenAIAnalyzer, OpenAITranscriber
from .bot import TelegramHandlers, UserManager
from .config import Settings
from .garmin import GarminCalorieStore
from .meal_grouping import OpenAIMealGrouper
from .users import GoogleUserRegistry
from .workspace import GoogleWorkspace


async def configure_bot_commands(
    application: Application[Any, Any, Any, Any, Any, Any],
    admin_user_id: int,
    garmin_calories: GarminCalorieStore | None = None,
    garmin_refresh_time: time | None = None,
) -> None:
    user_commands = [
        BotCommand("meals", "⭐ збережені страви"),
        BotCommand("recent", "🕘 нещодавні страви"),
        BotCommand("day", "📅 Сьогодні"),
        BotCommand("weekly_calories", "📊 Калорії за тиждень"),
        BotCommand("weekly_meals", "🍽 Страви за тиждень"),
        BotCommand("goal", "🎯 встановити денну ціль"),
        BotCommand("help", "❓ як користуватися ботом"),
        BotCommand("tips", "💡 додаткові можливості"),
    ]
    admin_commands = [
        *user_commands,
        BotCommand("burned", "🔥 витрата калорій"),
        BotCommand("invite", "➕ додати користувача"),
        BotCommand("info", "ℹ️ інформація про реліз"),
        BotCommand("users", "👥 показати перелік користувачів"),
        BotCommand("block", "⛔ заблокувати користувача"),
        BotCommand("unblock", "✅ розблокувати користувача"),
        BotCommand("delete", "🗑 повністю видалити користувача"),
    ]
    await application.bot.set_my_commands(user_commands)
    await application.bot.set_my_commands(
        admin_commands,
        scope=BotCommandScopeChat(chat_id=admin_user_id),
    )
    if garmin_calories is not None:
        if application.job_queue is None or garmin_refresh_time is None:
            raise RuntimeError("Garmin refresh requires JobQueue and a refresh time")
        application.job_queue.run_daily(
            refresh_garmin_calories,
            time=garmin_refresh_time,
            data=garmin_calories,
            name="garmin-calorie-refresh",
        )
        await _refresh_garmin_calories(garmin_calories)


async def refresh_garmin_calories(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    store = job.data if job is not None else None
    if not isinstance(store, GarminCalorieStore):
        logging.getLogger(__name__).error("Garmin refresh job has no calorie store")
        return
    await _refresh_garmin_calories(store)


async def _refresh_garmin_calories(store: GarminCalorieStore) -> None:
    try:
        refreshed = await asyncio.to_thread(store.refresh_if_due)
    except Exception:
        logging.getLogger(__name__).exception("Could not refresh Garmin calorie cache")
        return
    if refreshed:
        logging.getLogger(__name__).info("Garmin calorie cache refreshed")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full Telegram Bot API URLs at INFO level. Those URLs contain
    # the bot token, so network-library request logs must never be emitted.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    settings = Settings.from_env()
    if not settings.openai_pricing.complete:
        logging.getLogger(__name__).warning(
            "OpenAI input/output token pricing is incomplete; "
            "llm_cost_usd will be blank"
        )
    if not settings.weekly_meals_llm_pricing.complete:
        logging.getLogger(__name__).warning(
            "Weekly-meal LLM input/output token pricing is incomplete; "
            "its llm_cost_usd will be blank"
        )
    statistics = BotStatistics(
        AnalyticsStore(settings.statistics_db_path),
        settings.timezone,
        OpenAICostClient(
            settings.openai_admin_api_key,
            settings.openai_project_id,
            timeout_seconds=min(settings.openai_timeout_seconds, 15),
        ),
    )
    analyzer = OpenAIAnalyzer(
        settings.openai_api_key,
        settings.openai_model,
        settings.openai_reasoning_effort,
        settings.openai_timeout_seconds,
        settings.openai_pricing,
        statistics,
    )
    transcriber = OpenAITranscriber(
        settings.openai_api_key,
        settings.openai_timeout_seconds,
    )
    meal_grouper = OpenAIMealGrouper(
        settings.openai_api_key,
        settings.weekly_meals_llm_model,
        settings.weekly_meals_llm_reasoning_effort,
        settings.openai_timeout_seconds,
        settings.weekly_meals_llm_pricing,
        statistics,
    )
    google_client = gspread.service_account(
        filename=str(settings.google_service_account_file)
    )
    registry = GoogleUserRegistry(
        google_client,
        settings.users_spreadsheet_id,
        settings.users_sheet_name,
    )
    workspace = GoogleWorkspace(
        google_client,
        settings.google_drive_folder_id,
        settings.meal_sheet_name,
        timezone=settings.timezone,
        photo_storage_dir=settings.photo_storage_dir,
    )
    manager = UserManager(
        analyzer,
        registry,
        workspace,
        settings.timezone,
        settings.default_day_start,
        settings.photo_storage_dir,
    )
    manager.prepare_release_storage()
    garmin_calories = GarminCalorieStore(
        settings.garmin_tokenstore,
        settings.garmin_calorie_cache_path,
        settings.timezone,
        settings.default_day_start,
    )
    handlers = TelegramHandlers(
        settings.admin_telegram_user_id,
        manager,
        settings.meal_weight_presets,
        statistics,
        garmin_calories,
        transcriber,
        meal_grouper,
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(False)
        .post_init(
            partial(
                configure_bot_commands,
                admin_user_id=settings.admin_telegram_user_id,
                garmin_calories=garmin_calories,
                garmin_refresh_time=settings.default_day_start.replace(
                    tzinfo=settings.timezone
                ),
            )
        )
        .build()
    )
    message_update = filters.UpdateType.MESSAGE
    application.add_handler(TypeHandler(Update, handlers.track_interaction), group=-1)
    application.add_handler(
        CommandHandler("start", handlers.start, filters=message_update)
    )
    application.add_handler(
        CommandHandler("help", handlers.help, filters=message_update)
    )
    application.add_handler(
        CommandHandler("tips", handlers.tips, filters=message_update)
    )
    application.add_handler(CommandHandler("day", handlers.day, filters=message_update))
    application.add_handler(
        CommandHandler(
            "weekly_calories", handlers.weekly_calories, filters=message_update
        )
    )
    application.add_handler(
        CommandHandler("weekly_meals", handlers.weekly_meals, filters=message_update)
    )
    application.add_handler(
        CommandHandler("goal", handlers.goal, filters=message_update)
    )
    application.add_handler(
        CommandHandler("meals", handlers.meals, filters=message_update)
    )
    application.add_handler(
        CommandHandler("recent", handlers.recent, filters=message_update)
    )
    application.add_handler(
        CommandHandler("save", handlers.save, filters=message_update)
    )
    application.add_handler(
        CommandHandler("info", handlers.info, filters=message_update)
    )
    application.add_handler(
        CommandHandler("burned", handlers.burned, filters=message_update)
    )
    application.add_handler(
        CommandHandler("invite", handlers.invite, filters=message_update)
    )
    application.add_handler(
        CommandHandler("users", handlers.users, filters=message_update)
    )
    application.add_handler(
        CommandHandler("block", handlers.block, filters=message_update)
    )
    application.add_handler(
        CommandHandler("unblock", handlers.unblock, filters=message_update)
    )
    application.add_handler(
        CommandHandler("delete", handlers.delete_user_command, filters=message_update)
    )
    application.add_handler(
        MessageHandler(message_update & filters.COMMAND, handlers.cancel_pending_input)
    )
    application.add_handler(
        CallbackQueryHandler(
            handlers.delete, pattern=r"^delete:-?\d+:\d{4}-\d{2}-\d{2}$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handlers.save_callback,
            pattern=r"^save:-?\d+:\d{4}-\d{2}-\d{2}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handlers.meal_weight_callback,
            pattern=r"^meal-weight:-?\d+:\d{4}-\d{2}-\d{2}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handlers.meal_weight_choice_callback,
            pattern=(
                r"^meal-weight-(?:set|other):-?\d+:\d{4}-\d{2}-\d{2}"
                r"(?::\d+)?$"
            ),
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handlers.library_callback,
            pattern=r"^(?:meals-|saved-|recent-|manage-|wait-cancel$|invite-cancel$)",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handlers.admin_delete_callback,
            pattern=r"^admin-(?:delete|cancel):\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handlers.goal_disable_callback,
            pattern=r"^goal-disable:\d+$",
        )
    )
    application.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & filters.PHOTO, handlers.photo)
    )
    application.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & filters.VOICE, handlers.voice)
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND,
            handlers.text,
        )
    )
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
