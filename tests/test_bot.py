import asyncio
from datetime import UTC, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telegram.constants import ChatType

from calories_bot.analyzer import AnalysisError, AnalysisResult, InputFormatError
from calories_bot.bot import (
    ANALYSIS_ERROR_TEXT,
    FORMAT_ERROR_TEXT,
    NOT_FOOD_TEXT,
    READ_ERROR_TEXT,
    UNCERTAIN_WRITE_TEXT,
    WRITE_ERROR_TEXT,
    CaloriesService,
    NotFoodError,
    TelegramHandlers,
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
    def __init__(self, state: SheetState) -> None:
        self.state = state
        self.calls = 0
        self.appended = []

    def get_state(self, day, telegram_message_id):
        self.calls += 1
        self.day = day
        return self.state

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


def test_service_appends_normalized_request_and_adds_daily_total(tmp_path) -> None:
    analyzer = FakeAnalyzer(food_analysis())
    store = FakeStore(SheetState(today_total=300, existing=None))
    service = build_service(analyzer, store, tmp_path)

    reply = service.process_message("сир 50", 42, datetime(2026, 8, 2, 12, tzinfo=TZ))

    assert reply.startswith("сир 50 гр\n")
    assert "Калорії: 60 ккал" in reply
    assert "За сьогодні: 360 ккал" in reply
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
    assert reply.startswith("сир 50 гр")
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
    assert reply.startswith("сир\n")
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

    assert reply.startswith("сир\n")
    assert analyzer.normalized.text == ""
    assert store.appended[0][2] == ""


def test_estimate_is_marked_in_reply() -> None:
    meal = calculate_meal(food_analysis(estimated=True))
    reply = format_reply(meal, 60, "сир")
    assert "Калорії: ≈60 ккал" in reply
    assert "Оцінена вага: ≈50 г" in reply


def test_composite_reply_lists_estimated_weights_and_items() -> None:
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
    assert "Оцінена вага: яблуко — ≈100 г" in reply
    assert "Склад:" in reply
    assert "• яблуко: ≈50 ккал" in reply
    assert "• сир: 60 ккал" in reply


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

        async def reply_text(self, text):
            self.replies.append(text)

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
            return "reply"

    service = FakeService()
    handlers = TelegramHandlers(123, -1001, service)
    update, message = make_update()
    asyncio.run(handlers.text(update, None))
    assert service.args[2] == message.date
    assert message.replies == ["reply"]


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
            return "reply"

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
