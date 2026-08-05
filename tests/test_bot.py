import asyncio
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telegram.constants import ChatType, ParseMode

from calories_bot.analyzer import AnalysisError, AnalysisResult, InputFormatError
from calories_bot.bot import (
    ANALYSIS_ERROR_TEXT,
    DELETE_ERROR_TEXT,
    FORMAT_ERROR_TEXT,
    NOT_FOOD_TEXT,
    READ_ERROR_TEXT,
    UNCERTAIN_WRITE_TEXT,
    WRITE_ERROR_TEXT,
    CaloriesService,
    MealReply,
    NotFoodError,
    TelegramHandlers,
    format_day_reply,
    format_reply,
)
from calories_bot.models import (
    FoodAnalysis,
    FoodItem,
    LLMMetadata,
    StoredMeal,
    calculate_meal,
)
from calories_bot.sheets import (
    DayMeal,
    MealDeletion,
    SheetsReadError,
    SheetState,
    SheetsWriteError,
    SheetsWriteUncertainError,
)

TZ = ZoneInfo("Europe/Kyiv")
METADATA = LLMMetadata(model="test", effort="none", input_tokens=10, output_tokens=5)


def food_analysis(*, estimated: bool = False) -> FoodAnalysis:
    return FoodAnalysis(
        is_food=True,
        meal_name="сир",
        items=[
            FoodItem(
                name="сир",
                weight_g=50,
                weight_estimated=estimated,
                kcal_per_100g=120,
                kcal_estimated=estimated,
                weight_source_id=None if estimated else "W1",
                kcal_source_id=None,
            )
        ],
    )


class FakeAnalyzer:
    def __init__(self, analysis: FoodAnalysis) -> None:
        self.analysis = analysis
        self.calls = 0
        self.normalized = None
        self.image_bytes = None

    def analyze(self, normalized, image_bytes=None):
        self.calls += 1
        self.normalized = normalized
        self.image_bytes = image_bytes
        return AnalysisResult(self.analysis, METADATA)


class FakeStore:
    def __init__(
        self, state: SheetState, day_meals: list[DayMeal] | None = None
    ) -> None:
        self.state = state
        self.day_meals = day_meals or []
        self.calls = 0
        self.appended = []
        self.deletion = MealDeletion(
            accounting_day=date(2026, 8, 2),
            day_total=300,
            photo_path=None,
            deleted=True,
        )

    def get_state(self, day, telegram_message_id):
        self.calls += 1
        self.day = day
        return self.state

    def get_day_meals(self, day):
        self.day = day
        return self.day_meals

    def delete_meal(self, telegram_message_id, fallback_day):
        self.deleted = (telegram_message_id, fallback_day)
        return self.deletion

    def append_meal(
        self,
        timestamp,
        telegram_message_id,
        request,
        normalized_request,
        photo_path,
        meal,
        metadata,
    ):
        self.appended.append(
            (
                timestamp,
                telegram_message_id,
                request,
                normalized_request,
                photo_path,
                meal,
                metadata,
            )
        )
        return StoredMeal(
            normalized_request=normalized_request,
            meal=meal,
            metadata=metadata,
            photo_path=photo_path,
        )


def build_service(analyzer, store, tmp_path):
    return CaloriesService(analyzer, store, TZ, time(1), tmp_path / "photos")


def test_format_day_reply_is_readable_and_contains_only_requested_meal_data() -> None:
    reply = format_day_reply(
        [
            DayMeal(meal_name="вівсянка з бананом", meal_kcal=320),
            DayMeal(meal_name="  Вівсянка   з бананом ", meal_kcal=30),
            DayMeal(meal_name="курка з рисом", meal_kcal=460),
        ]
    )

    assert reply == ("=== 810 кк\n350 кк вівсянка з бананом — ×2\n460 кк курка з рисом")


def test_format_day_reply_handles_empty_day() -> None:
    assert format_day_reply([]) == "Сьогодні ще немає записів."


def test_service_day_summary_uses_shifted_accounting_date(tmp_path) -> None:
    store = FakeStore(
        SheetState(today_total=0, existing=None),
        [DayMeal(meal_name="сир", meal_kcal=60)],
    )
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    reply = service.get_day_summary(datetime(2026, 8, 2, 0, 30, tzinfo=TZ))

    assert store.day.isoformat() == "2026-08-01"
    assert reply == "=== 60 кк\n60 кк сир"


def test_service_appends_normalized_request_and_adds_daily_total(tmp_path) -> None:
    analyzer = FakeAnalyzer(food_analysis())
    store = FakeStore(SheetState(today_total=300, existing=None))
    service = build_service(analyzer, store, tmp_path)

    reply = service.process_message("сир 50", 42, datetime(2026, 8, 2, 12, tzinfo=TZ))

    assert reply.text == "<b>60 кк</b>\nсир 50 г #120\n=== 360 кк"
    assert reply.accounting_day == date(2026, 8, 2)
    assert store.appended[0][3] == "сир 50 гр"
    assert analyzer.normalized.text == "сир 50 гр"


def test_duplicate_uses_stored_normalized_text_without_openai_or_append(
    tmp_path,
) -> None:
    existing_meal = calculate_meal(food_analysis())
    existing = StoredMeal(
        normalized_request="сир 50 гр",
        meal=existing_meal,
        metadata=METADATA,
    )
    analyzer = FakeAnalyzer(food_analysis())
    store = FakeStore(SheetState(today_total=360, existing=existing))
    service = build_service(analyzer, store, tmp_path)

    reply = service.process_message("сир 50", 42, datetime(2026, 8, 2, 12, tzinfo=TZ))
    assert reply.text == "<b>60 кк</b>\nсир 50 г #120\n=== 360 кк"
    assert analyzer.calls == 0
    assert store.appended == []


def test_invalid_format_does_not_call_store_or_analyzer(tmp_path) -> None:
    analyzer = FakeAnalyzer(food_analysis())
    store = FakeStore(SheetState(today_total=0, existing=None))
    service = build_service(analyzer, store, tmp_path)
    with pytest.raises(ValueError):
        service.process_message("сир 50.5", 1, datetime(2026, 8, 2, 12, tzinfo=TZ))
    assert analyzer.calls == 0
    assert store.calls == 0


def test_non_food_is_not_appended(tmp_path) -> None:
    analyzer = FakeAnalyzer(FoodAnalysis(is_food=False, meal_name="", items=[]))
    store = FakeStore(SheetState(today_total=0, existing=None))
    service = build_service(analyzer, store, tmp_path)
    with pytest.raises(NotFoodError):
        service.process_message("привіт", 1, datetime(2026, 8, 2, 12, tzinfo=TZ))
    assert store.appended == []


def test_service_saves_photo_and_uses_meal_name_in_reply(tmp_path) -> None:
    analyzer = FakeAnalyzer(food_analysis())
    store = FakeStore(SheetState(today_total=300, existing=None))
    service = build_service(analyzer, store, tmp_path)

    reply = service.process_message(
        "50", 42, datetime(2026, 8, 2, 12, tzinfo=TZ), b"jpeg-data"
    )

    photo_path = store.appended[0][4]
    assert reply.text == "<b>60 кк</b>\n50 г #120\n=== 360 кк"
    assert analyzer.normalized.text == "50 гр"
    assert analyzer.image_bytes == b"jpeg-data"
    assert photo_path == str((tmp_path / "photos" / "42.jpg").resolve())
    assert (tmp_path / "photos" / "42.jpg").read_bytes() == b"jpeg-data"


def test_service_accepts_photo_without_caption(tmp_path) -> None:
    analyzer = FakeAnalyzer(food_analysis(estimated=True))
    store = FakeStore(SheetState(today_total=0, existing=None))
    service = build_service(analyzer, store, tmp_path)

    reply = service.process_message(
        "", 43, datetime(2026, 8, 2, 12, tzinfo=TZ), b"jpeg-data"
    )

    assert reply.text == "<b>60 кк</b>\nсир ≈50 г #120\n=== 60 кк"
    assert analyzer.normalized.text == ""
    assert store.appended[0][2] == ""


def test_estimate_is_marked_in_reply() -> None:
    meal = calculate_meal(food_analysis(estimated=True))
    reply = format_reply(meal, 60, "сир")
    assert reply == "<b>60 кк</b>\nсир ≈50 г #120\n=== 60 кк"


def test_composite_reply_stays_compact() -> None:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="перекус",
        items=[
            FoodItem(
                name="яблуко",
                weight_g=100,
                weight_estimated=True,
                kcal_per_100g=50,
                kcal_estimated=True,
            ),
            FoodItem(
                name="сир",
                weight_g=50,
                weight_estimated=False,
                kcal_per_100g=120,
                kcal_estimated=False,
            ),
        ],
    )
    reply = format_reply(calculate_meal(analysis), 110, "яблуко, сир 50 гр")
    assert reply == "<b>110 кк</b>\nяблуко, сир 50 г ≈150 г #73\n=== 110 кк"


def test_reply_removes_existing_density_and_escapes_html() -> None:
    meal = calculate_meal(food_analysis())
    reply = format_reply(meal, 60, "сир <міцний> 50 гр 120 ккал/100г")
    assert reply == "<b>60 кк</b>\nсир &lt;міцний&gt; 50 г #120\n=== 60 кк"


def test_service_deletes_photo_only_inside_storage(tmp_path) -> None:
    store = FakeStore(SheetState(today_total=0, existing=None))
    photo = tmp_path / "photos" / "42.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"photo")
    store.deletion = MealDeletion(date(2026, 8, 2), 100, str(photo), True)
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    deletion = service.delete_message(42, date(2026, 8, 2))

    assert deletion.day_total == 100
    assert not photo.exists()


def test_service_keeps_photo_outside_storage(tmp_path) -> None:
    store = FakeStore(SheetState(today_total=0, existing=None))
    photo = tmp_path / "outside.jpg"
    photo.write_bytes(b"photo")
    store.deletion = MealDeletion(date(2026, 8, 2), 100, str(photo), True)
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    service.delete_message(42, date(2026, 8, 2))

    assert photo.exists()


def test_google_write_failure_is_propagated_without_changing_state(tmp_path) -> None:
    class FailingStore(FakeStore):
        def append_meal(self, *args):
            raise SheetsWriteError("failed")

    store = FailingStore(SheetState(today_total=300, existing=None))
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)
    with pytest.raises(SheetsWriteError):
        service.process_message("сир 50", 42, datetime(2026, 8, 2, 12, tzinfo=TZ))
    assert store.state.today_total == 300


def make_update(*, user_id=123, chat_id=-1001, chat_type=ChatType.SUPERGROUP):
    class FakeMessage:
        text = "сир 50"
        caption = None
        photo = []
        media_group_id = None
        message_id = 1
        date = datetime(2026, 8, 2, 9, tzinfo=UTC)

        def __init__(self):
            self.replies = []
            self.reply_kwargs = []

        async def reply_text(self, text, **kwargs):
            self.replies.append(text)
            self.reply_kwargs.append(kwargs)

    message = FakeMessage()
    return (
        SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
            effective_message=message,
        ),
        message,
    )


@pytest.mark.parametrize(
    "user_id,chat_id,chat_type",
    [
        (999, -1001, ChatType.SUPERGROUP),
        (123, -1002, ChatType.SUPERGROUP),
        (123, -1001, ChatType.PRIVATE),
        (123, -1001, ChatType.CHANNEL),
    ],
)
def test_unauthorized_sources_do_not_call_service(user_id, chat_id, chat_type) -> None:
    class FakeService:
        def __init__(self):
            self.calls = 0

        def process_message(self, *args):
            self.calls += 1
            return "reply"

    service = FakeService()
    handlers = TelegramHandlers(123, -1001, service)
    update, message = make_update(user_id=user_id, chat_id=chat_id, chat_type=chat_type)
    asyncio.run(handlers.text(update, None))
    assert service.calls == 0
    assert message.replies == []


def test_handler_passes_telegram_message_date() -> None:
    class FakeService:
        def __init__(self):
            self.args = None

        def process_message(self, *args):
            self.args = args
            return MealReply("reply", 1, date(2026, 8, 2))

    service = FakeService()
    handlers = TelegramHandlers(123, -1001, service)
    update, message = make_update()
    asyncio.run(handlers.text(update, None))
    assert service.args[2] == message.date
    assert message.replies == ["reply"]
    assert message.reply_kwargs[0]["parse_mode"] == ParseMode.HTML
    button = message.reply_kwargs[0]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Видалити"
    assert button.callback_data == "delete:1:2026-08-02"


def test_day_handler_passes_telegram_message_date() -> None:
    class FakeService:
        def __init__(self):
            self.timestamp = None

        def get_day_summary(self, timestamp):
            self.timestamp = timestamp
            return "=== 60 кк\n60 кк сир"

    service = FakeService()
    handlers = TelegramHandlers(123, -1001, service)
    update, message = make_update()

    asyncio.run(handlers.day(update, None))

    assert service.timestamp == message.date
    assert message.replies == ["=== 60 кк\n60 кк сир"]


def test_day_handler_maps_read_error_to_user_message() -> None:
    class FailingService:
        def get_day_summary(self, timestamp):
            del timestamp
            raise SheetsReadError("failed")

    handlers = TelegramHandlers(123, -1001, FailingService())
    update, message = make_update()

    asyncio.run(handlers.day(update, None))

    assert message.replies == [READ_ERROR_TEXT]


def make_callback_update(data="delete:42:2026-08-02", *, user_id=123):
    class FakeQuery:
        def __init__(self):
            self.data = data
            self.answers = []
            self.edits = []

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    query = FakeQuery()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP),
        effective_message=SimpleNamespace(),
        callback_query=query,
    )
    return update, query


def test_delete_callback_removes_source_and_leaves_confirmation() -> None:
    class FakeService:
        def __init__(self):
            self.args = None

        def delete_message(self, *args):
            self.args = args
            return MealDeletion(date(2026, 8, 2), 905, None, True)

    class FakeBot:
        def __init__(self):
            self.deleted = []

        async def delete_message(self, **kwargs):
            self.deleted.append(kwargs)

    service = FakeService()
    bot = FakeBot()
    handlers = TelegramHandlers(123, -1001, service)
    update, query = make_callback_update()

    asyncio.run(handlers.delete(update, SimpleNamespace(bot=bot)))

    assert service.args == (42, date(2026, 8, 2))
    assert bot.deleted == [{"chat_id": -1001, "message_id": 42}]
    assert query.answers == [(None, {})]
    assert query.edits == [("Видалено\n=== 905 кк", {"reply_markup": None})]


def test_repeated_delete_callback_uses_idempotent_result() -> None:
    class FakeService:
        def delete_message(self, *args):
            return MealDeletion(date(2026, 8, 2), 905, None, False)

    class FakeBot:
        async def delete_message(self, **kwargs):
            del kwargs
            raise RuntimeError("already gone")

    handlers = TelegramHandlers(123, -1001, FakeService())
    update, query = make_callback_update()

    asyncio.run(handlers.delete(update, SimpleNamespace(bot=FakeBot())))

    assert query.edits[0][0] == "Видалено\n=== 905 кк"


def test_delete_callback_keeps_messages_when_sheets_fail() -> None:
    class FailingService:
        def delete_message(self, *args):
            raise SheetsWriteError("failed")

    class FakeBot:
        def __init__(self):
            self.calls = 0

        async def delete_message(self, **kwargs):
            del kwargs
            self.calls += 1

    bot = FakeBot()
    handlers = TelegramHandlers(123, -1001, FailingService())
    update, query = make_callback_update()

    asyncio.run(handlers.delete(update, SimpleNamespace(bot=bot)))

    assert bot.calls == 0
    assert query.edits == []
    assert query.answers == [(DELETE_ERROR_TEXT, {"show_alert": True})]


def test_delete_callback_rejects_unauthorized_user() -> None:
    handlers = TelegramHandlers(123, -1001, SimpleNamespace())
    update, query = make_callback_update(user_id=999)

    asyncio.run(handlers.delete(update, SimpleNamespace()))

    assert query.answers == [("Недоступно.", {"show_alert": True})]
    assert query.edits == []


@pytest.mark.parametrize(
    "data", [None, "other:42:2026-08-02", "delete:0:2026-08-02", "delete:42:bad"]
)
def test_delete_callback_rejects_invalid_payload(data) -> None:
    handlers = TelegramHandlers(123, -1001, SimpleNamespace())
    update, query = make_callback_update(data)

    asyncio.run(handlers.delete(update, SimpleNamespace()))

    assert query.answers == [("Некоректна кнопка.", {"show_alert": True})]


def test_photo_handler_downloads_largest_photo_and_passes_caption() -> None:
    class FakeTelegramFile:
        async def download_as_bytearray(self):
            return bytearray(b"largest-photo")

    class FakePhoto:
        async def get_file(self):
            return FakeTelegramFile()

    class FakeService:
        def __init__(self):
            self.args = None

        def process_message(self, *args):
            self.args = args
            return MealReply("reply", 1, date(2026, 8, 2))

    service = FakeService()
    handlers = TelegramHandlers(123, -1001, service)
    update, message = make_update()
    message.caption = "200 г"
    message.photo = [SimpleNamespace(), FakePhoto()]

    asyncio.run(handlers.photo(update, None))

    assert service.args == ("200 г", 1, message.date, b"largest-photo")
    assert message.replies == ["reply"]


def test_photo_handler_ignores_media_groups() -> None:
    class FakeService:
        def __init__(self):
            self.calls = 0

        def process_message(self, *args):
            self.calls += 1
            return "reply"

    service = FakeService()
    handlers = TelegramHandlers(123, -1001, service)
    update, message = make_update()
    message.photo = [SimpleNamespace()]
    message.media_group_id = "album"

    asyncio.run(handlers.photo(update, None))

    assert service.calls == 0
    assert message.replies == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InputFormatError("bad"), FORMAT_ERROR_TEXT),
        (NotFoodError(), NOT_FOOD_TEXT),
        (SheetsReadError("bad"), READ_ERROR_TEXT),
        (SheetsWriteUncertainError("bad"), UNCERTAIN_WRITE_TEXT),
        (SheetsWriteError("bad"), WRITE_ERROR_TEXT),
        (AnalysisError("bad"), ANALYSIS_ERROR_TEXT),
        (RuntimeError("bad"), ANALYSIS_ERROR_TEXT),
    ],
)
def test_handler_maps_errors_to_user_messages(error, expected) -> None:
    class FailingService:
        def process_message(self, *args):
            raise error

    handlers = TelegramHandlers(123, -1001, FailingService())
    update, message = make_update()
    asyncio.run(handlers.text(update, None))
    assert message.replies == [expected]


def test_start_and_help_share_help_text() -> None:
    handlers = TelegramHandlers(123, -1001, SimpleNamespace())
    update, message = make_update()
    asyncio.run(handlers.start(update, None))
    asyncio.run(handlers.help(update, None))
    assert len(message.replies) == 2
    assert message.replies[0] == message.replies[1]
