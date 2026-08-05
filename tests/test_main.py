import logging
from datetime import UTC, datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram import Chat, Message, Update, User
from telegram.constants import ChatType

from calories_bot import main as main_module
from calories_bot.analyzer import ModelPricing


def test_configure_logging_suppresses_network_request_urls() -> None:
    main_module.configure_logging()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_main_wires_dependencies_and_starts_polling(monkeypatch) -> None:
    settings = SimpleNamespace(
        telegram_bot_token="token",
        telegram_user_id=123,
        telegram_chat_id=-1001,
        openai_api_key="key",
        openai_model="model",
        openai_reasoning_effort="none",
        openai_pricing=ModelPricing(None, None, None),
        google_service_account_file="credentials.json",
        google_spreadsheet_id="sheet",
        google_sheet_name="food_log",
        photo_storage_dir=Path("data/photos"),
        timezone=ZoneInfo("Europe/Kyiv"),
        day_start_time=time(1),
    )
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: settings)

    created = {}
    monkeypatch.setattr(
        main_module,
        "OpenAIAnalyzer",
        lambda *args: created.setdefault("analyzer", SimpleNamespace()),
    )
    monkeypatch.setattr(
        main_module,
        "GoogleSheetsStore",
        lambda **kwargs: created.setdefault("store", SimpleNamespace()),
    )
    monkeypatch.setattr(
        main_module,
        "CaloriesService",
        lambda *args: created.setdefault("service", SimpleNamespace()),
    )
    handlers = SimpleNamespace(
        start=lambda: None,
        help=lambda: None,
        day=lambda: None,
        delete=lambda: None,
        text=lambda: None,
        photo=lambda: None,
    )
    monkeypatch.setattr(
        main_module,
        "TelegramHandlers",
        lambda *args: handlers,
    )

    class FakeApplication:
        def __init__(self):
            self.handlers = []
            self.polling = None

        def add_handler(self, handler):
            self.handlers.append(handler)

        def run_polling(self, **kwargs):
            self.polling = kwargs

    app = FakeApplication()

    class FakeBuilder:
        def token(self, value):
            assert value == "token"
            return self

        def concurrent_updates(self, value):
            assert value is False
            return self

        def build(self):
            return app

    monkeypatch.setattr(main_module.Application, "builder", lambda: FakeBuilder())

    main_module.main()
    assert len(app.handlers) == 6
    assert app.polling == {"drop_pending_updates": False}

    message = Message(
        message_id=1,
        date=datetime(2026, 8, 2, tzinfo=UTC),
        chat=Chat(-1001, ChatType.SUPERGROUP),
        from_user=User(123, "Igor", False),
        text="сир 50",
    )
    assert app.handlers[-1].check_update(Update(1, message=message))
    assert not app.handlers[-1].check_update(Update(2, edited_message=message))
