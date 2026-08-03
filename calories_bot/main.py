from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .analyzer import OpenAIAnalyzer
from .bot import CaloriesService, TelegramHandlers
from .config import Settings
from .sheets import GoogleSheetsStore


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
            "OpenAI token pricing is incomplete; llm_cost_usd will be blank"
        )
    analyzer = OpenAIAnalyzer(
        settings.openai_api_key,
        settings.openai_model,
        settings.openai_reasoning_effort,
        settings.openai_pricing,
    )
    store = GoogleSheetsStore(
        credentials_file=settings.google_service_account_file,
        spreadsheet_id=settings.google_spreadsheet_id,
        worksheet_name=settings.google_sheet_name,
        timezone=settings.timezone,
        day_start_time=settings.day_start_time,
    )
    service = CaloriesService(
        analyzer,
        store,
        settings.timezone,
        settings.day_start_time,
        settings.photo_storage_dir,
    )
    handlers = TelegramHandlers(
        settings.telegram_user_id, settings.telegram_chat_id, service
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(False)
        .build()
    )
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help))
    application.add_handler(MessageHandler(filters.PHOTO, handlers.photo))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text)
    )
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
