import asyncio
import logging
from datetime import UTC, datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram import Chat, Message, MessageEntity, Update, User
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
        admin_telegram_user_id=999,
        openai_api_key="key",
        openai_model="model",
        openai_reasoning_effort="none",
        openai_timeout_seconds=90,
        openai_pricing=ModelPricing(None, None, None),
        google_service_account_file="credentials.json",
        users_spreadsheet_id="users-sheet",
        users_sheet_name="users",
        google_drive_folder_id="folder",
        meal_sheet_name="food_log",
        photo_storage_dir=Path("data/photos"),
        timezone=ZoneInfo("Europe/Kyiv"),
        default_day_start=time(1),
    )
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: settings)

    created = {}
    monkeypatch.setattr(
        main_module,
        "OpenAIAnalyzer",
        lambda *args: created.setdefault("analyzer", SimpleNamespace()),
    )
    monkeypatch.setattr(
        main_module.gspread,
        "service_account",
        lambda **kwargs: created.setdefault("google_client", SimpleNamespace()),
    )
    monkeypatch.setattr(
        main_module,
        "GoogleUserRegistry",
        lambda *args: created.setdefault("registry", SimpleNamespace()),
    )
    monkeypatch.setattr(
        main_module,
        "GoogleWorkspace",
        lambda *args, **kwargs: created.setdefault("workspace", SimpleNamespace()),
    )
    monkeypatch.setattr(
        main_module,
        "UserManager",
        lambda *args: created.setdefault("manager", SimpleNamespace()),
    )
    handlers = SimpleNamespace(
        start=lambda: None,
        help=lambda: None,
        tips=lambda: None,
        day=lambda: None,
        week=lambda: None,
        goal=lambda: None,
        meals=lambda: None,
        recent=lambda: None,
        save=lambda: None,
        delete=lambda: None,
        save_callback=lambda: None,
        meal_weight_callback=lambda: None,
        library_callback=lambda: None,
        invite=lambda: None,
        users=lambda: None,
        block=lambda: None,
        unblock=lambda: None,
        delete_user_command=lambda: None,
        cancel_pending_input=lambda: None,
        admin_delete_callback=lambda: None,
        goal_disable_callback=lambda: None,
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
        def __init__(self):
            self.post_init_callback = None

        def token(self, value):
            assert value == "token"
            return self

        def concurrent_updates(self, value):
            assert value is False
            return self

        def post_init(self, callback):
            self.post_init_callback = callback
            created["post_init"] = callback
            return self

        def build(self):
            return app

    monkeypatch.setattr(main_module.Application, "builder", lambda: FakeBuilder())

    main_module.main()
    assert len(app.handlers) == 23
    assert app.polling == {"drop_pending_updates": False}
    assert created["post_init"].func is main_module.configure_bot_commands
    assert created["post_init"].keywords == {"admin_user_id": 999}

    message = Message(
        message_id=1,
        date=datetime(2026, 8, 2, tzinfo=UTC),
        chat=Chat(123, ChatType.PRIVATE),
        from_user=User(123, "Igor", False),
        text="сир 50",
    )
    assert app.handlers[-1].check_update(Update(1, message=message))
    assert not app.handlers[-1].check_update(Update(2, edited_message=message))

    command = Message(
        message_id=2,
        date=datetime(2026, 8, 2, tzinfo=UTC),
        chat=Chat(123, ChatType.PRIVATE),
        from_user=User(123, "Igor", False),
        text="/day",
        entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, 4)],
    )
    command.set_bot(SimpleNamespace(username="calorie_bot"))
    assert app.handlers[3].check_update(Update(3, message=command))
    assert not app.handlers[3].check_update(Update(4, edited_message=command))


def test_configure_bot_commands_registers_user_and_admin_menus() -> None:
    class FakeBot:
        def __init__(self):
            self.calls = []

        async def set_my_commands(self, commands, **kwargs):
            self.calls.append((commands, kwargs))

    bot = FakeBot()
    asyncio.run(
        main_module.configure_bot_commands(SimpleNamespace(bot=bot), admin_user_id=999)
    )

    assert [(command.command, command.description) for command in bot.calls[0][0]] == [
        ("meals", "⭐ збережені страви"),
        ("recent", "🕘 нещодавні страви"),
        ("day", "📅 Сьогодні"),
        ("week", "📊 За тиждень"),
        ("goal", "🎯 встановити денну ціль"),
        ("help", "❓ як користуватися ботом"),
        ("tips", "💡 додаткові можливості"),
    ]
    assert [command.command for command in bot.calls[1][0]] == [
        "meals",
        "recent",
        "day",
        "week",
        "goal",
        "help",
        "tips",
        "invite",
        "users",
        "block",
        "unblock",
        "delete",
    ]
    assert bot.calls[1][1]["scope"].chat_id == 999
