import asyncio
import threading
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telegram.constants import ChatType, ParseMode

from calories_bot import bot as bot_module
from calories_bot.analyzer import AnalysisError, AnalysisResult, InputFormatError
from calories_bot.bot import (
    ANALYSIS_ERROR_TEXT,
    DELETE_ERROR_TEXT,
    FORMAT_ERROR_TEXT,
    LLM_OPERATION_TEXT,
    LONG_OPERATION_TEXT,
    NOT_FOOD_TEXT,
    READ_ERROR_TEXT,
    RECENT_MEALS_LIMIT,
    UNCERTAIN_WRITE_TEXT,
    VOICE_ERROR_TEXT,
    WRITE_ERROR_TEXT,
    CaloriesService,
    MealReply,
    MealWeightUnchangedError,
    NotFoodError,
    TelegramHandlers,
    format_daily_total,
    format_day_reply,
    format_reply,
    format_users_reply,
    format_weekly_calories_reply,
    format_weekly_meals_reply,
    format_weekly_reply,
    load_help_text,
    load_start_text,
)
from calories_bot.meal_grouping import MealGroupingError, MealGroupingResult
from calories_bot.models import (
    FoodAnalysis,
    FoodItem,
    LLMMetadata,
    NutritionSummary,
    RecentMeal,
    SavedMeal,
    StoredMeal,
    calculate_meal,
    nutrition_summary,
    parse_simple_meal_request,
    round_meal_nutrition,
)
from calories_bot.sheets import (
    DayMeal,
    MealDeletion,
    PeriodMeal,
    SheetsReadError,
    SheetState,
    SheetsWriteError,
    SheetsWriteUncertainError,
)
from calories_bot.users import UserRecord

TZ = ZoneInfo("Europe/Kyiv")
METADATA = LLMMetadata(model="test", effort="none", input_tokens=10, output_tokens=5)


@pytest.fixture(autouse=True)
def run_to_thread_inline(monkeypatch):
    async def inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("calories_bot.bot.asyncio.to_thread", inline)


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
                protein_per_100g=20,
                fat_per_100g=10,
                carbs_per_100g=0,
                protein_estimated=estimated,
                fat_estimated=estimated,
                carbs_estimated=estimated,
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
        if day_meals is not None:
            self.day_meals = day_meals
        elif state.today_total or state.today_nutrition is not None:
            nutrition = (
                state.today_nutrition
                if isinstance(state.today_nutrition, NutritionSummary)
                else NutritionSummary.unknown_macros(state.today_total)
            )
            self.day_meals = [DayMeal("попередні страви", state.today_total, nutrition)]
        else:
            self.day_meals = []
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

    def get_daily_totals(self, start_day, end_day):
        self.range = (start_day, end_day)
        return getattr(self, "daily_totals", {})

    def get_period_meals(self, start_day, end_day):
        self.range = (start_day, end_day)
        return getattr(self, "period_meals", [])

    def get_first_meal_day(self):
        return getattr(self, "first_meal_day", None)

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
        self.day_meals.append(
            DayMeal(
                meal.meal_name,
                meal.meal_kcal,
                nutrition_summary(meal),
                meal.total_weight_g,
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
            DayMeal(
                meal_name="вівсянка з бананом",
                meal_kcal=320,
                total_weight_g=250,
            ),
            DayMeal(
                meal_name="  Вівсянка   з бананом ",
                meal_kcal=30,
                total_weight_g=50,
            ),
            DayMeal(
                meal_name="курка з рисом",
                meal_kcal=460,
                total_weight_g=400,
            ),
        ]
    )

    assert reply == (
        "<h3>За сьогодні:</h3>"
        "<details><summary>🔥 К <b><u>810</u></b> кк</summary>"
        "<ul><li>курка з рисом, 400 г  🔥 К 460</li>"
        "<li>вівсянка з бананом, 300 г ×2  🔥 К 350</li></ul></details>"
        "<details><summary>🥩 Б <b><u>—</u></b> г</summary>"
        "<p>Немає внесків</p></details>"
        "<details><summary>🥑 Ж <b><u>—</u></b> г</summary>"
        "<p>Немає внесків</p></details>"
        "<details><summary>🍞 В <b><u>—</u></b> г</summary>"
        "<p>Немає внесків</p></details>"
    )


def test_format_day_reply_handles_empty_day() -> None:
    reply = format_day_reply([])

    assert reply.count("<details>") == 4
    assert "<summary>🔥 К <b><u>0</u></b> кк</summary>" in reply
    assert "<summary>🥩 Б <b><u>0</u></b> г</summary>" in reply
    assert "<summary>🥑 Ж <b><u>0</u></b> г</summary>" in reply
    assert "<summary>🍞 В <b><u>0</u></b> г</summary>" in reply


def test_day_reply_sums_kbjv_but_keeps_rows_calorie_only() -> None:
    reply = format_day_reply(
        [
            DayMeal(
                "сир",
                375,
                NutritionSummary(kcal=375, protein_g=30, fat_g=15, carbs_g=0),
            ),
            DayMeal(
                "хліб",
                200,
                NutritionSummary(kcal=200, protein_g=6, fat_g=2, carbs_g=40),
            ),
        ]
    )

    assert "<summary>🔥 К <b><u>575</u></b> кк</summary>" in reply
    assert "<summary>🥩 Б <b><u>36</u></b> г</summary>" in reply
    assert "<li>сир  🥩 Б 30</li>" in reply
    assert "<li>хліб  🥩 Б 6</li>" in reply
    assert "сир  🍞 В" not in reply
    assert reply.count("<details>") == 4


def test_day_reply_sums_whole_values_saved_for_each_meal() -> None:
    meals = [
        DayMeal(
            "спагеті",
            420,
            NutritionSummary(kcal=420, protein_g=14, fat_g=11, carbs_g=72),
        ),
        DayMeal(
            "сир",
            106,
            NutritionSummary(kcal=106, protein_g=6, fat_g=9, carbs_g=1),
        ),
        DayMeal(
            "вино",
            350,
            NutritionSummary(kcal=350, protein_g=0, fat_g=0, carbs_g=13),
        ),
        *[
            DayMeal(
                "кава",
                2,
                NutritionSummary(kcal=2, protein_g=0, fat_g=0, carbs_g=0),
            )
            for _ in range(5)
        ],
    ]

    reply = format_day_reply(meals)

    assert "<summary>🔥 К <b><u>886</u></b> кк</summary>" in reply
    assert "<li>спагеті  🔥 К 420</li><li>вино  🔥 К 350</li>" in reply
    assert "<li>сир  🔥 К 106</li><li>кава ×5  🔥 К 10</li>" in reply
    assert "<summary>🥩 Б <b><u>20</u></b> г</summary>" in reply
    assert "<li>спагеті  🥩 Б 14</li>" in reply
    assert "<li>сир  🥩 Б 6</li>" in reply
    assert "кава ×5  🥩 Б" not in reply
    assert "вино  🥩 Б" not in reply
    assert "<summary>🥑 Ж <b><u>20</u></b> г</summary>" in reply
    assert "<li>спагеті  🥑 Ж 11</li><li>сир  🥑 Ж 9</li>" in reply
    assert "<summary>🍞 В <b><u>86</u></b> г</summary>" in reply
    assert (
        "<li>спагеті  🍞 В 72</li><li>вино  🍞 В 13</li><li>сир  🍞 В 1</li>" in reply
    )

    daily_total = format_daily_total(
        NutritionSummary(kcal=886, protein_g=20, fat_g=20, carbs_g=86),
        None,
    )
    assert daily_total == (
        "🔥 К <b><u>886</u></b> кк\n🥩 Б <b><u>20</u></b> г\n"
        "🥑 Ж <b><u>20</u></b> г\n🍞 В <b><u>86</u></b> г"
    )


def test_daily_views_do_not_round_values_while_formatting() -> None:
    nutrition = NutritionSummary(kcal=10.5, protein_g=2.5, fat_g=1.5, carbs_g=3.5)

    assert format_daily_total(nutrition, 20, 5) == (
        "🔥 К <b><u>10.5</u></b> / 20 → −9.5 кк\n"
        "<code>██████░░░░░░│░░░</code>\n"
        "🥩 Б <b><u>2.5</u></b> / 5 → −2.5 г\n"
        "<code>██████░░░░░░│░░░</code>\n"
        "🥑 Ж <b><u>1.5</u></b> г\n🍞 В <b><u>3.5</u></b> г"
    )
    day_reply = format_day_reply([DayMeal("тест", 10.5, nutrition)])
    assert (
        "<summary>🔥 К <b><u>10.5</u></b> кк</summary><ul><li>тест  🔥 К 10.5</li>"
    ) in day_reply
    assert (
        "<summary>🥩 Б <b><u>2.5</u></b> г</summary><ul><li>тест  🥩 Б 2.5</li>"
    ) in day_reply


def test_format_day_reply_escapes_html_in_meal_names() -> None:
    reply = format_day_reply([DayMeal(meal_name="<сир & хліб>", meal_kcal=100)])

    assert "<li>&lt;сир &amp; хліб&gt;  🔥 К 100</li>" in reply


def test_format_day_reply_shows_goal_for_empty_day() -> None:
    reply = format_day_reply([], 1500)

    assert (
        "<summary>🔥 К <b><u>0</u></b> / 1500 → −1500 кк<br/>"
        "<code>░░░░░░░░░░░░│░░░</code></summary>"
    ) in reply
    assert reply.count("<details>") == 4


def test_format_day_reply_includes_daily_goal_and_remaining_calories() -> None:
    reply = format_day_reply(
        [DayMeal(meal_name="сир", meal_kcal=360)],
        daily_kcal_goal=1500,
    )

    assert (
        "<summary>🔥 К <b><u>360</u></b> / 1500 → −1140 кк<br/>"
        "<code>██░░░░░░░░░░│░░░</code></summary>"
    ) in reply
    assert "<ul><li>сир  🔥 К 360</li></ul>" in reply
    assert reply.count("<details>") == 4


def test_format_day_reply_shows_goal_overage_without_negative_remaining() -> None:
    reply = format_day_reply(
        [DayMeal(meal_name="піца", meal_kcal=1600)],
        daily_kcal_goal=1500,
    )

    assert (
        "<summary>🔥 К <b><u>1600</u></b> / 1500 → +100 кк<br/>"
        "<code>████████████│█░░</code></summary>"
    ) in reply
    assert "<ul><li>піца  🔥 К 1600</li></ul>" in reply


def test_service_day_summary_uses_shifted_accounting_date(tmp_path) -> None:
    store = FakeStore(
        SheetState(today_total=0, existing=None),
        [DayMeal(meal_name="сир", meal_kcal=60)],
    )
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    reply = service.get_day_summary(datetime(2026, 8, 2, 0, 30, tzinfo=TZ))

    assert store.day.isoformat() == "2026-08-01"
    assert "<summary>🔥 К <b><u>60</u></b> кк</summary>" in reply
    assert "<li>сир  🔥 К 60</li>" in reply


def test_service_day_summary_uses_daily_goal(tmp_path) -> None:
    store = FakeStore(
        SheetState(today_total=0, existing=None),
        [DayMeal(meal_name="сир", meal_kcal=60)],
    )
    service = CaloriesService(
        FakeAnalyzer(food_analysis()),
        store,
        TZ,
        time(1),
        tmp_path / "photos",
        daily_kcal_goal=1500,
    )

    reply = service.get_day_summary(datetime(2026, 8, 2, 12, tzinfo=TZ))

    assert (
        "<summary>🔥 К <b><u>60</u></b> / 1500 → −1440 кк<br/>"
        "<code>░░░░░░░░░░░░│░░░</code></summary>"
    ) in reply
    assert "<li>сир  🔥 К 60</li>" in reply


def test_service_appends_normalized_request_and_adds_daily_total(tmp_path) -> None:
    analyzer = FakeAnalyzer(food_analysis())
    store = FakeStore(SheetState(today_total=300, existing=None))
    service = build_service(analyzer, store, tmp_path)

    timestamp = datetime(2026, 8, 2, 12, tzinfo=TZ)
    reply = service.process_message("сир 50", 42, timestamp)

    assert reply.text.startswith("<h3>Сир 50 г</h3>")
    assert "🔥 К 60&nbsp;&nbsp;&nbsp;🥩 Б 10&nbsp;&nbsp;&nbsp;🥑 Ж 5" in reply.text
    assert "<sub>На 100 г:</sub><br/>🔥 К 120" in reply.text
    assert reply.daily_total_text == service.get_day_summary(timestamp)
    assert "<summary>🔥 К <b><u>360</u></b> кк</summary>" in reply.daily_total_text
    assert reply.accounting_day == date(2026, 8, 2)
    marker = parse_simple_meal_request(store.appended[0][3])
    assert marker is not None
    assert marker.payload == "сир 50 гр"
    assert analyzer.normalized.text == "сир 50 гр"


def test_composite_input_is_stored_and_replied_to_per_component(tmp_path) -> None:
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
    store = FakeStore(SheetState(today_total=300, existing=None))
    service = build_service(FakeAnalyzer(analysis), store, tmp_path)

    timestamp = datetime(2026, 8, 2, 12, tzinfo=TZ)
    replies = service.process_message("яблуко та сир", 42, timestamp)

    assert isinstance(replies, list)
    assert len(replies) == 2
    assert [entry[5].meal_name for entry in store.appended] == ["яблуко", "сир"]
    assert all(len(entry[5].items) == 1 for entry in store.appended)
    assert store.appended[0][6].input_tokens == 10
    assert store.appended[1][6].input_tokens is None
    assert replies[0].telegram_message_id == 42
    assert replies[1].telegram_message_id < 0
    assert replies[0].daily_total_text is None
    assert replies[1].daily_total_text == service.get_day_summary(timestamp)
    assert "<summary>🔥 К <b><u>410</u></b> кк</summary>" in replies[1].daily_total_text
    markers = [parse_simple_meal_request(entry[3]) for entry in store.appended]
    assert [marker.component_index for marker in markers if marker] == [0, 1]
    assert all(marker and marker.component_count == 2 for marker in markers)


def test_handler_sends_each_component_with_the_same_standard_buttons() -> None:
    class Service:
        def process_message(self, *args):
            del args
            return [
                MealReply("Яблуко", 1, date(2026, 8, 2)),
                MealReply(
                    "Сир",
                    -2,
                    date(2026, 8, 2),
                    daily_total_text="За день: <b><u>110 кк</u></b>",
                ),
            ]

    handlers = TelegramHandlers(
        999, FakeManager({123: user_record()}, {123: Service()})
    )
    update, message = make_update()

    asyncio.run(handlers.text(update, SimpleNamespace(user_data={})))

    assert message.replies == [
        "Яблуко",
        "Сир",
        "За день: <b><u>110 кк</u></b>",
    ]
    for kwargs in message.reply_kwargs[:2]:
        assert [
            [button.text for button in row]
            for row in kwargs["reply_markup"].inline_keyboard
        ] == [
            ["⭐ Зберегти", "⚖️ Змінити вагу"],
            ["🗑 Видалити"],
        ]
    assert "reply_markup" not in message.reply_kwargs[2]


def test_handler_sends_meal_reply_as_rich_message_with_buttons() -> None:
    class Bot:
        def __init__(self):
            self.calls = []

        async def do_api_request(self, endpoint, **kwargs):
            self.calls.append((endpoint, kwargs))
            return SimpleNamespace()

    handlers = TelegramHandlers(999, FakeManager())
    _, message = make_update()
    bot = Bot()
    result = MealReply(
        "<h3>Сир 50 г</h3><p>🔥60&nbsp;&nbsp;&nbsp;🥩 10</p>",
        42,
        date(2026, 8, 2),
    )

    asyncio.run(handlers._send_meal_replies(message, result, SimpleNamespace(bot=bot)))

    assert message.replies == []
    assert bot.calls[0][0] == "sendRichMessage"
    api_kwargs = bot.calls[0][1]["api_kwargs"]
    assert api_kwargs["rich_message"] == {"html": result.text}
    assert api_kwargs["reply_markup"]["inline_keyboard"][0][0]["text"] == (
        "⭐ Зберегти"
    )


def test_handler_remembers_the_separate_daily_total_message() -> None:
    class Statistics:
        def __init__(self):
            self.recorded = []

        def record_daily_total_message(self, *args):
            self.recorded.append(args)

    class ReplyingMessage:
        def __init__(self):
            self.calls = []

        async def reply_text(self, text, **kwargs):
            self.calls.append((text, kwargs))
            return SimpleNamespace(
                chat_id=123,
                message_id=500 + len(self.calls),
                date=datetime(2026, 8, 2, 9, tzinfo=UTC),
            )

    statistics = Statistics()
    handlers = TelegramHandlers(
        999,
        FakeManager(),
        statistics=statistics,
    )
    message = ReplyingMessage()
    result = MealReply(
        "Сир",
        42,
        date(2026, 8, 2),
        daily_total_text="За день: 60 кк",
    )

    asyncio.run(handlers._send_meal_replies(message, result))

    assert [call[0] for call in message.calls] == ["Сир", "За день: 60 кк"]
    assert statistics.recorded == [(123, 502, datetime(2026, 8, 2, 9, tzinfo=UTC))]


def test_meal_reply_replaces_previous_summary_from_same_accounting_day() -> None:
    period = (
        datetime(2026, 8, 2, 4, 30, tzinfo=TZ),
        datetime(2026, 8, 3, 4, 30, tzinfo=TZ),
    )

    class Service:
        def accounting_day_bounds(self, day):
            assert day == date(2026, 8, 2)
            return period

    class Statistics:
        def __init__(self):
            self.periods = []
            self.forgotten = []
            self.recorded = []

        def daily_total_message_ids_between(self, *args):
            self.periods.append(args)
            return (480, 490)

        def forget_daily_total_messages(self, *args):
            self.forgotten.append(args)

        def record_daily_total_message(self, *args):
            self.recorded.append(args)

    class Bot:
        def __init__(self):
            self.sent = 0
            self.deleted = []

        async def do_api_request(self, endpoint, **kwargs):
            del endpoint, kwargs
            self.sent += 1
            return SimpleNamespace(
                chat_id=123,
                message_id=500 + self.sent,
                date=datetime(2026, 8, 2, 9, tzinfo=UTC),
            )

        async def delete_messages(self, **kwargs):
            self.deleted.append(kwargs)
            return True

    statistics = Statistics()
    handlers = TelegramHandlers(999, FakeManager(), statistics=statistics)
    _, message = make_update()
    bot = Bot()
    result = MealReply(
        "Сир",
        42,
        date(2026, 8, 2),
        daily_total_text="<h3>За сьогодні:</h3>",
    )

    asyncio.run(
        handlers._send_meal_replies(
            message,
            result,
            SimpleNamespace(bot=bot),
            Service(),
        )
    )

    assert statistics.periods == [(123, *period)]
    assert bot.deleted == [{"chat_id": 123, "message_ids": (480, 490)}]
    assert statistics.forgotten == [(123, (480, 490))]
    assert statistics.recorded == [(123, 502, datetime(2026, 8, 2, 9, tzinfo=UTC))]


def test_service_refreshes_total_after_deletion_during_analysis(tmp_path) -> None:
    class BlockingAnalyzer(FakeAnalyzer):
        def __init__(self) -> None:
            super().__init__(food_analysis())
            self.started = threading.Event()
            self.resume = threading.Event()

        def analyze(self, normalized, image_bytes=None):
            self.started.set()
            assert self.resume.wait(timeout=1)
            return super().analyze(normalized, image_bytes)

    class DeletionAwareStore(FakeStore):
        def __init__(self) -> None:
            super().__init__(SheetState(today_total=570, existing=None))
            self.total = 570

        def get_state(self, day, telegram_message_id):
            del day, telegram_message_id
            return SheetState(today_total=self.total, existing=None)

        def delete_meal(self, telegram_message_id, fallback_day):
            del telegram_message_id
            self.total = 274
            self.day_meals = [
                DayMeal(
                    "попередні страви",
                    self.total,
                    NutritionSummary.unknown_macros(self.total),
                )
            ]
            return MealDeletion(fallback_day, self.total, None, True)

    analyzer = BlockingAnalyzer()
    store = DeletionAwareStore()
    service = build_service(analyzer, store, tmp_path)
    result: list[MealReply] = []

    worker = threading.Thread(
        target=lambda: result.append(
            service.process_message("сир 50", 42, datetime(2026, 8, 2, 12, tzinfo=TZ))
        )
    )
    worker.start()
    assert analyzer.started.wait(timeout=1)

    service.delete_message(7, date(2026, 8, 2))
    analyzer.resume.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert "<summary>🔥 К <b><u>334</u></b> кк</summary>" in result[0].daily_total_text


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
    assert "<h3>Сир 50 г</h3>" in reply.text
    assert "🔥 К 60&nbsp;&nbsp;&nbsp;🥩 Б 10&nbsp;&nbsp;&nbsp;🥑 Ж 5" in reply.text
    assert "<sub>На 100 г:</sub><br/>🔥 К 120" in reply.text
    assert "<summary>🔥 К <b><u>360</u></b> кк</summary>" in reply.daily_total_text
    assert analyzer.calls == 0
    assert store.appended == []


def test_invalid_format_checks_duplicate_but_does_not_call_analyzer(tmp_path) -> None:
    analyzer = FakeAnalyzer(food_analysis())
    store = FakeStore(SheetState(today_total=0, existing=None))
    service = build_service(analyzer, store, tmp_path)
    with pytest.raises(ValueError):
        service.process_message("сир 50.5", 1, datetime(2026, 8, 2, 12, tzinfo=TZ))
    assert analyzer.calls == 0
    assert store.calls == 1


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
    assert "<h3>Сир 50 г</h3>" in reply.text
    assert "🔥 К 60&nbsp;&nbsp;&nbsp;🥩 Б 10&nbsp;&nbsp;&nbsp;🥑 Ж 5" in reply.text
    assert "<summary>🔥 К <b><u>360</u></b> кк</summary>" in reply.daily_total_text
    assert analyzer.normalized.text == "50 гр"
    assert analyzer.image_bytes == b"jpeg-data"
    expected_photo = tmp_path / "photos" / "2026-08-02-42.jpg"
    assert photo_path == str(expected_photo.resolve())
    assert expected_photo.read_bytes() == b"jpeg-data"


def test_service_accepts_photo_without_caption(tmp_path) -> None:
    analyzer = FakeAnalyzer(food_analysis(estimated=True))
    store = FakeStore(SheetState(today_total=0, existing=None))
    service = build_service(analyzer, store, tmp_path)

    reply = service.process_message(
        "", 43, datetime(2026, 8, 2, 12, tzinfo=TZ), b"jpeg-data"
    )

    assert reply.text.startswith("<h3>Сир ≈50 г</h3>")
    assert "<details><summary>🔥 К 60&nbsp;&nbsp;&nbsp;🥩 Б 10" in reply.text
    assert "<sub>На 100 г:</sub><br/>🔥 К 120" in reply.text
    assert "<summary>🔥 К <b><u>60</u></b> кк</summary>" in reply.daily_total_text
    assert analyzer.normalized.text == ""
    assert store.appended[0][2] == ""


def test_estimated_weight_is_marked_but_nutrition_stays_compact() -> None:
    meal = calculate_meal(food_analysis(estimated=True))
    reply = format_reply(meal)
    assert reply.startswith("<h3>Сир ≈50 г</h3>")
    assert "🔥 К 60" in reply
    assert "К:" not in reply
    assert "≈60" not in reply


def test_reply_uses_compact_requested_format() -> None:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="яблучні оладки",
        items=[
            FoodItem(
                name="яблучні оладки",
                weight_g=50,
                weight_estimated=False,
                kcal_per_100g=220,
                kcal_estimated=True,
            )
        ],
    )

    reply = format_reply(calculate_meal(analysis))

    assert reply == (
        "<h3>Яблучні оладки 50 г</h3>"
        "<details><summary>🔥 К 110&nbsp;&nbsp;&nbsp;🥩 Б -&nbsp;&nbsp;&nbsp;"
        "🥑 Ж -&nbsp;&nbsp;&nbsp;🍞 В -</summary>"
        "<p><sub>На 100 г:</sub><br/>"
        "🔥 К 220&nbsp;&nbsp;&nbsp;🥩 Б -&nbsp;&nbsp;&nbsp;🥑 Ж -&nbsp;&nbsp;&nbsp;"
        "🍞 В -</p></details>"
    )


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
    reply = format_reply(calculate_meal(analysis))
    assert reply.startswith("<h3>Перекус 150 г</h3><p>🔥 К 110")
    assert "<h4>Яблуко ≈100 г</h4>" in reply
    assert "<h4>Сир 50 г</h4>" in reply
    assert reply.count("На 100 г:") == 2


def test_reply_escapes_html_in_item_name() -> None:
    analysis = food_analysis()
    analysis.items[0].name = "сир <міцний>"
    meal = calculate_meal(analysis)
    reply = format_reply(meal)
    assert reply.startswith("<h3>Сир &lt;міцний&gt; 50 г</h3>")


def test_reply_uses_portion_display() -> None:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="яйця",
        items=[
            FoodItem(
                name="яйця",
                weight_g=100,
                weight_estimated=True,
                weight_origin="deterministic_reference",
                kcal_per_100g=140,
                kcal_estimated=True,
                kcal_origin="model_estimate",
                portion_display="2 шт.",
            )
        ],
    )

    reply = format_reply(calculate_meal(analysis))

    assert reply.startswith("<h3>Яйця 2 шт.&nbsp;&nbsp;≈100 г</h3>")
    assert "<details><summary>🔥 К 140&nbsp;&nbsp;&nbsp;🥩 Б -" in reply


def test_reply_explains_estimated_weight_per_piece() -> None:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="сирники",
        items=[
            FoodItem(
                name="сирники",
                weight_g=250,
                weight_estimated=True,
                kcal_per_100g=25,
                kcal_estimated=False,
                portion_display="5 шт.",
            )
        ],
    )

    reply = format_reply(round_meal_nutrition(calculate_meal(analysis)))

    assert reply.startswith("<h3>Сирники 5 шт.&nbsp;&nbsp;≈250 г</h3>")
    assert "<details><summary>🔥 К 63&nbsp;&nbsp;&nbsp;🥩 Б -" in reply
    assert "<sub>На 100 г:</sub><br/>🔥 К 25" in reply


def test_reply_explains_non_count_portion_weight() -> None:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="борщ",
        items=[
            FoodItem(
                name="борщ",
                weight_g=300,
                weight_estimated=True,
                kcal_per_100g=40,
                kcal_estimated=True,
                portion_display="1 тарілка",
            )
        ],
    )

    reply = format_reply(calculate_meal(analysis))

    assert reply.startswith("<h3>Борщ 1 тарілка&nbsp;&nbsp;≈300 г</h3>")
    assert "<details><summary>🔥 К 120&nbsp;&nbsp;&nbsp;🥩 Б -" in reply


def test_reply_does_not_repeat_weight_already_shown_as_portion() -> None:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="ковбаски з яловичини та свинини",
        items=[
            FoodItem(
                name="ковбаски з яловичини та свинини",
                weight_g=150,
                weight_estimated=False,
                kcal_per_100g=300,
                kcal_estimated=True,
                portion_display="150 г",
            )
        ],
    )

    reply = format_reply(calculate_meal(analysis))

    assert reply.startswith("<h3>Ковбаски з яловичини та свинини 150 г</h3>")
    assert "<details><summary>🔥 К 450&nbsp;&nbsp;&nbsp;🥩 Б -" in reply
    assert "150 г (150 г)" not in reply


def test_daily_goal_shows_overage_with_unit_and_reserved_bar() -> None:
    reply = format_daily_total(2130, 2000)
    assert reply == (
        "🔥 К <b><u>2130</u></b> / 2000 → +130 кк\n"
        "<code>████████████│█░░</code>\n"
        "🥩 Б <b><u>—</u></b> г\n🥑 Ж <b><u>—</u></b> г\n"
        "🍞 В <b><u>—</u></b> г"
    )


def test_daily_total_shows_calorie_and_protein_goal_progress() -> None:
    reply = format_daily_total(
        NutritionSummary(kcal=905, protein_g=122, fat_g=19, carbs_g=92),
        1500,
        100,
    )

    assert reply == (
        "🔥 К <b><u>905</u></b> / 1500 → −595 кк\n"
        "<code>███████░░░░░│░░░</code>\n"
        "🥩 Б <b><u>122</u></b> / 100 → +22 г\n"
        "<code>████████████│██░</code>\n"
        "🥑 Ж <b><u>19</u></b> г\n🍞 В <b><u>92</u></b> г"
    )


@pytest.mark.parametrize(
    ("kcal", "protein", "calorie_bar", "protein_bar"),
    [
        (1050, 86, "████████░░░░│░░░", "██████████░░│░░░"),
        (1500, 100, "████████████│░░░", "████████████│░░░"),
        (1650, 110, "████████████│█░░", "████████████│█░░"),
        (2000, 130, "████████████│███", "████████████│███"),
    ],
)
def test_calorie_and_protein_bars_share_one_fixed_scale(
    kcal, protein, calorie_bar, protein_bar
) -> None:
    reply = format_daily_total(
        NutritionSummary(kcal=kcal, protein_g=protein),
        1500,
        100,
    )
    bars = [
        line.removeprefix("<code>").removesuffix("</code>")
        for line in reply.splitlines()
        if line.startswith("<code>")
    ]

    assert bars == [calorie_bar, protein_bar]
    assert all(len(bar) == 16 and bar.index("│") == 12 for bar in bars)


def test_weekly_calories_includes_seven_days_admin_balance_and_averages() -> None:
    burned = {date(2026, 8, day): 2000 for day in range(2, 9)}
    reply = format_weekly_calories_reply(
        date(2026, 8, 8),
        {
            date(2026, 8, 2): 1640.4,
            date(2026, 8, 3): 1799.6,
            date(2026, 8, 5): 1870,
            date(2026, 8, 6): 1730,
            date(2026, 8, 8): 2310,
        },
        burned,
    )

    assert reply.startswith("<h3>Різниця калорій за тиждень</h3>")
    assert "Спожито: 9350 ккал" in reply
    assert "Витрачено: 14000 ккал" in reply
    assert "Різниця: <b><u>-4650 ккал</u></b>" in reply
    assert "<h4>В середньому за день:</h4>" in reply
    assert "Спожито: 1336 ккал" in reply
    assert "Різниця: <b><u>-664 ккал</u></b>" in reply
    assert "<details><summary>По дням</summary>" in reply
    assert "+ спожито ккал, - витрачено, = різниця" in reply
    assert reply.count("<li>") == 7
    assert "<b>сб 08.08</b>: + 2310" in reply
    assert "= <b><u>+310</u></b>" in reply
    daily_details = reply.split("<details><summary>По дням</summary>", maxsplit=1)[1]
    assert "2310 ккал" not in daily_details
    assert "2000 ккал" not in daily_details
    assert "+310 ккал" not in daily_details


def test_weekly_calories_for_regular_user_hides_unavailable_burned_values() -> None:
    reply = format_weekly_calories_reply(date(2026, 8, 8), {date(2026, 8, 8): 700})

    assert "Спожито: 700 ккал" in reply
    assert "Витрачено" not in reply
    assert "Різниця:" not in reply
    assert "- " not in reply
    assert "= " not in reply
    assert "Спожито: 100 ккал" in reply
    assert "<b>сб 08.08</b>: + 700" in reply


def test_weekly_calories_uses_only_calories_from_nutrition_summaries() -> None:
    reply = format_weekly_calories_reply(
        date(2026, 8, 8),
        {
            date(2026, 8, 7): NutritionSummary(
                kcal=700, protein_g=50, fat_g=20, carbs_g=80
            ),
            date(2026, 8, 8): NutritionSummary(
                kcal=800, protein_g=60, fat_g=25, carbs_g=90
            ),
        },
    )

    assert "Спожито: 1500 ккал" in reply
    assert "Б:" not in reply
    assert "Ж:" not in reply
    assert "В:" not in reply


def test_weekly_meals_aggregates_names_weights_and_sorts_by_calories() -> None:
    reply = format_weekly_meals_reply(
        [
            PeriodMeal("вино сухе", 200, 120),
            PeriodMeal("  Вино   сухе ", 150, 80),
            PeriodMeal("сир", 100, 360),
        ]
    )

    assert reply.startswith("<h3>КБЖВ за тиждень</h3>")
    assert "<h4>В середньому в день:</h4>" in reply
    assert "🔥 К <b><u>80</u></b> кк" in reply
    assert "<details><summary>Деталі по стравам</summary>" in reply
    cheese = "<li><b><u>Сир</u></b> 100 г — 🔥 К 360"
    wine = "<li><b><u>Вино сухе</u></b> 350 г — 🔥 К 200"
    assert cheese in reply
    assert wine in reply
    assert reply.index(cheese) < reply.index(wine)


def test_weekly_meals_handles_no_records() -> None:
    assert format_weekly_meals_reply([]) == (
        "<h3>КБЖВ за тиждень</h3>"
        "<h4>В середньому в день:</h4>"
        "<p>🔥 К <b><u>0</u></b> кк<br/>🥩 Б <b><u>0</u></b> г<br/>"
        "🥑 Ж <b><u>0</u></b> г<br/>🍞 В <b><u>0</u></b> г</p>"
        "<p>Записів немає.</p>"
    )


def test_weekly_meals_shows_full_nutrition_after_each_meal() -> None:
    reply = format_weekly_meals_reply(
        [
            PeriodMeal(
                "сир",
                150,
                375,
                NutritionSummary(kcal=375, protein_g=30, fat_g=15, carbs_g=0),
            )
        ]
    )

    assert (
        "<li><b><u>Сир</u></b> 150 г — "
        "🔥 К 375&nbsp;&nbsp;&nbsp;🥩 Б 30&nbsp;&nbsp;&nbsp;🥑 Ж 15"
    ) in reply
    assert (
        "🔥 К <b><u>54</u></b> кк<br/>🥩 Б <b><u>4</u></b> г<br/>"
        "🥑 Ж <b><u>2</u></b> г<br/>🍞 В <b><u>0</u></b> г"
    ) in reply


def test_weekly_meals_average_uses_daily_calorie_and_protein_goals() -> None:
    meals = [
        PeriodMeal(
            f"страва {index}",
            100,
            913,
            NutritionSummary(kcal=913, protein_g=21, fat_g=20, carbs_g=93),
        )
        for index in range(7)
    ]

    reply = format_weekly_meals_reply(meals, None, 1500, 145)

    assert (
        "🔥 К <b><u>913</u></b> / 1500 → −587 кк<br/>"
        "<code>███████░░░░░│░░░</code><br/>"
        "🥩 Б <b><u>21</u></b> / 145 → −124 г<br/>"
        "<code>█░░░░░░░░░░░│░░░</code><br/>"
        "🥑 Ж <b><u>20</u></b> г<br/>🍞 В <b><u>93</u></b> г"
    ) in reply


def test_weekly_reply_combines_meals_macros_and_consumed_calories() -> None:
    day = date(2026, 8, 8)
    nutrition = NutritionSummary(kcal=700, protein_g=56, fat_g=21, carbs_g=84)
    reply = format_weekly_reply(
        day,
        {day: nutrition},
        [PeriodMeal("сир", 200, 700, nutrition, day)],
    )

    assert reply.startswith("<h3>Попередні 7 днів (без сьогодні):</h3>")
    assert "<summary>🔥 К <b><u>100</u></b> кк</summary>" in reply
    assert "<li>сир, 200 г  🔥 К 100</li>" in reply
    assert "<details><summary>КБЖВ по дням</summary>" in reply
    assert "<b>сб 08.08</b>: 🔥 К 700" in reply
    assert "Витрачено" not in reply
    assert "Баланс калорій" not in reply


def test_weekly_service_groups_meals_and_shows_their_total_weight(tmp_path) -> None:
    class Grouper:
        def group(self, names):
            assert set(names) == {"Кава", "Кава чорна"}
            return MealGroupingResult(("Кава", "Кава"), METADATA)

    first_day = date(2026, 8, 2)
    store = FakeStore(SheetState(today_total=0, existing=None))
    store.first_meal_day = first_day
    store.period_meals = [
        PeriodMeal(
            "Кава",
            300,
            700,
            NutritionSummary.unknown_macros(700),
            first_day,
        ),
        PeriodMeal(
            "Кава чорна",
            200,
            350,
            NutritionSummary.unknown_macros(350),
            date(2026, 8, 3),
        ),
    ]
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    reply = service.get_weekly(
        datetime(2026, 8, 9, 12, tzinfo=TZ), meal_grouper=Grouper()
    )

    assert "<li>Кава, 500 г  🔥 К 150</li>" in reply


def test_weekly_reply_uses_actual_days_for_averages_and_garmin_details() -> None:
    first_day = date(2026, 8, 22)
    last_day = date(2026, 8, 23)
    first = NutritionSummary(kcal=600, protein_g=40, fat_g=20, carbs_g=80)
    last = NutritionSummary(kcal=1000, protein_g=60, fat_g=30, carbs_g=120)

    reply = format_weekly_reply(
        last_day,
        {first_day: first, last_day: last},
        [
            PeriodMeal("сир", 100, 600, first, first_day),
            PeriodMeal("рис", 200, 1000, last, last_day),
        ],
        {first_day: 2000, last_day: 2100},
        period_days=2,
    )

    assert reply.startswith("<h3>Попередні 2 дні (без сьогодні):</h3>")
    assert "<sub>історія повного тижня ще не накопичена</sub>" in reply
    assert "<summary>🔥 К <b><u>800</u></b> кк</summary>" in reply
    assert "<summary>🥩 Б <b><u>50</u></b> г</summary>" in reply
    assert "<summary>🥑 Ж <b><u>25</u></b> г</summary>" in reply
    assert "<summary>🍞 В <b><u>100</u></b> г</summary>" in reply
    assert "<details><summary>КБЖВ по дням</summary>" in reply
    assert "<b>сб 22.08</b>: 🔥 К 600" in reply
    assert (
        "<details><summary>Баланс калорій за добу: <br/>"
        "<b><u>Дефіцит 1250 ккал</u></b><br/>"
        "+ 800  - 2050<br/></summary>"
    ) in reply
    assert "<b>нд 23.08</b>: + 1000&nbsp;&nbsp;- 2100" in reply
    assert "= <b><u>-1100</u></b>" in reply


def test_weekly_reply_uses_same_calorie_and_protein_progress_bars() -> None:
    day = date(2026, 8, 23)
    nutrition = NutritionSummary(kcal=1650, protein_g=110, fat_g=40, carbs_g=150)

    reply = format_weekly_reply(
        day,
        {day: nutrition},
        [PeriodMeal("вечеря", 500, 1650, nutrition, day)],
        daily_kcal_goal=1500,
        daily_protein_goal=100,
        period_days=1,
    )

    assert (
        "<summary>🔥 К <b><u>1650</u></b> / 1500 → +150 кк<br/>"
        "<code>████████████│█░░</code></summary>"
    ) in reply
    assert (
        "<summary>🥩 Б <b><u>110</u></b> / 100 → +10 г<br/>"
        "<code>████████████│█░░</code></summary>"
    ) in reply


def test_weekly_reply_labels_average_calorie_surplus() -> None:
    day = date(2026, 8, 23)
    nutrition = NutritionSummary(kcal=2500, protein_g=100, fat_g=70, carbs_g=300)

    reply = format_weekly_reply(
        day,
        {day: nutrition},
        [PeriodMeal("вечеря", 500, 2500, nutrition, day)],
        {day: 2000},
        period_days=1,
    )

    assert (
        "Баланс калорій за добу: <br/><b><u>Профіцит 500 ккал</u></b><br/>"
        "+ 2500  - 2000"
    ) in reply


def test_weekly_macros_are_unavailable_before_tracking_start_date() -> None:
    end_day = date(2026, 8, 16)
    old_meal = PeriodMeal(
        "сир",
        100,
        300,
        NutritionSummary.unknown_macros(300),
        date(2026, 8, 15),
    )

    reply = format_weekly_reply(
        end_day,
        {date(2026, 8, 15): NutritionSummary.unknown_macros(300)},
        [old_meal],
        period_days=3,
    )

    assert "<summary>🔥 К <b><u>100</u></b> кк</summary>" in reply
    assert "<summary>🥩 Б <b><u>—</u></b> г</summary>" in reply


def test_weekly_macros_after_tracking_start_use_completed_macro_days() -> None:
    meals = [
        PeriodMeal(
            f"страва {day}",
            100,
            300,
            NutritionSummary(kcal=300, protein_g=30, fat_g=15, carbs_g=45),
            date(2026, 8, day),
        )
        for day in range(17, 20)
    ]

    reply = format_weekly_reply(
        date(2026, 8, 19),
        {
            meal.accounting_day: meal.nutrition
            for meal in meals
            if meal.accounting_day is not None and meal.nutrition is not None
        },
        meals,
        period_days=3,
    )

    assert "<summary>🔥 К <b><u>300</u></b> кк</summary>" in reply
    assert "<summary>🥩 Б <b><u>30</u></b> г</summary>" in reply
    assert "<summary>🥑 Ж <b><u>15</u></b> г</summary>" in reply
    assert "<summary>🍞 В <b><u>45</u></b> г</summary>" in reply


def test_weekly_period_crossing_tracking_start_ignores_old_missing_macros() -> None:
    meals = [
        PeriodMeal(
            f"стара страва {day}",
            100,
            700,
            NutritionSummary.unknown_macros(700),
            date(2026, 8, day),
        )
        for day in range(14, 17)
    ] + [
        PeriodMeal(
            f"нова страва {day}",
            100,
            700,
            NutritionSummary(kcal=700, protein_g=40, fat_g=20, carbs_g=80),
            date(2026, 8, day),
        )
        for day in range(17, 21)
    ]

    reply = format_weekly_reply(
        date(2026, 8, 20),
        {
            meal.accounting_day: meal.nutrition
            for meal in meals
            if meal.accounting_day is not None and meal.nutrition is not None
        },
        meals,
    )

    assert "<summary>🔥 К <b><u>700</u></b> кк</summary>" in reply
    assert "<summary>🥩 Б <b><u>40</u></b> г</summary>" in reply
    assert "<summary>🥑 Ж <b><u>20</u></b> г</summary>" in reply
    assert "<summary>🍞 В <b><u>80</u></b> г</summary>" in reply


def test_weekly_service_uses_short_history_for_heading_and_averages(tmp_path) -> None:
    store = FakeStore(SheetState(today_total=0, existing=None))
    store.first_meal_day = date(2026, 8, 6)
    store.period_meals = [
        PeriodMeal(
            "сир",
            100,
            300,
            NutritionSummary(kcal=300, protein_g=30, fat_g=15, carbs_g=3),
            date(2026, 8, 6),
        ),
        PeriodMeal(
            "рис",
            200,
            600,
            NutritionSummary(kcal=600, protein_g=15, fat_g=3, carbs_g=120),
            date(2026, 8, 8),
        ),
    ]
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    reply = service.get_weekly(datetime(2026, 8, 9, 12, tzinfo=TZ))

    assert store.range == (date(2026, 8, 6), date(2026, 8, 8))
    assert reply.startswith("<h3>Попередні 3 дні (без сьогодні):</h3>")
    assert "<sub>історія повного тижня ще не накопичена</sub>" in reply
    assert "<summary>🔥 К <b><u>300</u></b> кк</summary>" in reply
    assert "<summary>🥩 Б <b><u>—</u></b> г</summary>" in reply
    daily_details = reply.split("<details><summary>КБЖВ по дням</summary>", maxsplit=1)[
        1
    ]
    assert daily_details.count("<li><b>") == 3


def test_weekly_service_uses_one_completed_day_when_history_is_empty(tmp_path) -> None:
    store = FakeStore(SheetState(today_total=0, existing=None))
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    reply = service.get_weekly(datetime(2026, 8, 9, 12, tzinfo=TZ))

    assert store.range == (date(2026, 8, 8), date(2026, 8, 8))
    assert reply.startswith("<h3>Попередні 1 день (без сьогодні):</h3>")


def test_weekly_meals_never_exceeds_twenty_rows() -> None:
    meals = [PeriodMeal(f"Страва {index}", 100, 1_000 - index) for index in range(25)]

    reply = format_weekly_meals_reply(meals)

    assert reply.count("<li>") == 20
    assert "<li><b><u>Інше (6 категорій)</u></b>" in reply


def test_weekly_meals_semantically_groups_names_and_sums_locally(tmp_path) -> None:
    class Grouper:
        names = None

        def group(self, names):
            self.names = names
            return MealGroupingResult(("Вино", "Вино", "Кава", "Кава"), METADATA)

    store = FakeStore(SheetState(today_total=0, existing=None))
    store.period_meals = [
        PeriodMeal("Сухе вино", 200, 160),
        PeriodMeal("Sauvignon Blanc", 100, 80),
        PeriodMeal("Кава", 300, 4),
        PeriodMeal("Кава чорна", 200, 2),
    ]
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)
    grouper = Grouper()

    reply = service.get_weekly_meals(datetime(2026, 8, 2, tzinfo=UTC), grouper)

    assert set(grouper.names) == {
        "Сухе вино",
        "Sauvignon Blanc",
        "Кава",
        "Кава чорна",
    }
    wine = "<li><b><u>Вино</u></b> 300 г — 🔥 К 240"
    coffee = "<li><b><u>Кава</u></b> 500 г — 🔥 К 6"
    assert wine in reply
    assert coffee in reply
    assert reply.index(wine) < reply.index(coffee)


def test_weekly_meals_falls_back_when_llm_grouping_fails(tmp_path) -> None:
    class Grouper:
        def group(self, names):
            raise MealGroupingError("controlled")

    store = FakeStore(SheetState(today_total=0, existing=None))
    store.period_meals = [PeriodMeal("Сухе вино", 200, 160)]
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    reply = service.get_weekly_meals(datetime(2026, 8, 2, tzinfo=UTC), Grouper())

    assert "<li><b><u>Сухе вино</u></b> 200 г — 🔥 К 160" in reply
    assert "🔥 К <b><u>23</u></b> кк" in reply


def test_user_list_contains_status_ids_and_pending_invites() -> None:
    users = [
        user_record(123, status="active"),
        user_record(456, status="blocked"),
        UserRecord(
            row_number=4,
            telegram_user_id=None,
            display_name="Новий користувач",
            telegram_username="",
            status="invited",
            invite_token="token",
            spreadsheet_id="",
            day_start=time(1),
        ),
    ]

    assert format_users_reply(users) == (
        "Користувачі (3):\n"
        "• User — активний — ID 123 (@user)\n"
        "• User — заблокований — ID 456 (@user)\n"
        "• Новий користувач — запрошений — ще не активований"
    )


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


def test_service_formats_deletion_confirmation(tmp_path) -> None:
    store = FakeStore(SheetState(today_total=0, existing=None))
    service = CaloriesService(
        FakeAnalyzer(food_analysis()),
        store,
        TZ,
        time(1),
        tmp_path / "photos",
        daily_kcal_goal=1500,
    )

    deleted = service.format_deletion_reply(
        MealDeletion(date(2026, 8, 2), 905, None, True, "сир <міцний>")
    )
    repeated = service.format_deletion_reply(
        MealDeletion(date(2026, 8, 2), 905, None, False)
    )

    assert deleted == "Видалено Сир &lt;міцний&gt;"
    assert repeated == "Цей запис уже видалено"


def test_service_uses_personal_day_start_for_summary_cleanup_bounds(tmp_path) -> None:
    service = CaloriesService(
        FakeAnalyzer(food_analysis()),
        FakeStore(SheetState(today_total=0, existing=None)),
        TZ,
        time(4, 30),
        tmp_path / "photos",
    )

    assert service.accounting_day_bounds(date(2026, 8, 2)) == (
        datetime(2026, 8, 2, 4, 30, tzinfo=TZ),
        datetime(2026, 8, 3, 4, 30, tzinfo=TZ),
    )


def test_google_write_failure_is_propagated_without_changing_state(tmp_path) -> None:
    class FailingStore(FakeStore):
        def append_meal(self, *args):
            raise SheetsWriteError("failed")

    store = FailingStore(SheetState(today_total=300, existing=None))
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)
    with pytest.raises(SheetsWriteError):
        service.process_message(
            "сир 50",
            42,
            datetime(2026, 8, 2, 12, tzinfo=TZ),
            image_bytes=b"photo",
        )
    assert store.state.today_total == 300
    assert not (tmp_path / "photos" / "2026-08-02-42.jpg").exists()


def test_uncertain_google_write_keeps_photo_for_a_possibly_stored_row(tmp_path) -> None:
    class FailingStore(FakeStore):
        def append_meal(self, *args):
            raise SheetsWriteUncertainError("unknown")

    store = FailingStore(SheetState(today_total=300, existing=None))
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    with pytest.raises(SheetsWriteUncertainError):
        service.process_message(
            "сир 50",
            42,
            datetime(2026, 8, 2, 12, tzinfo=TZ),
            image_bytes=b"photo",
        )

    assert (tmp_path / "photos" / "2026-08-02-42.jpg").read_bytes() == b"photo"


def test_partial_component_write_is_reported_as_uncertain(tmp_path) -> None:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="перекус",
        items=[
            FoodItem(
                name="яблуко",
                weight_g=100,
                weight_estimated=False,
                kcal_per_100g=50,
                kcal_estimated=False,
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

    class PartialStore(FakeStore):
        def append_meal(self, *args):
            if self.appended:
                raise SheetsWriteError("second component failed")
            return super().append_meal(*args)

    store = PartialStore(SheetState(today_total=0, existing=None))
    service = build_service(FakeAnalyzer(analysis), store, tmp_path)

    with pytest.raises(SheetsWriteUncertainError, match="part"):
        service.process_message(
            "яблуко та сир", 42, datetime(2026, 8, 2, 12, tzinfo=TZ)
        )

    assert len(store.appended) == 1


def user_record(
    user_id=123,
    *,
    status="active",
    sheet="sheet-123",
    cutoff=time(1),
    goal=None,
    protein_goal=None,
):
    from calories_bot.users import UserRecord

    return UserRecord(
        row_number=2,
        telegram_user_id=user_id,
        display_name="User",
        telegram_username="user",
        status=status,
        invite_token="",
        spreadsheet_id=sheet,
        day_start=cutoff,
        daily_kcal_goal=goal,
        daily_protein_goal=protein_goal,
    )


class FakeManager:
    def __init__(self, users=None, services=None):
        self.users = users or {}
        self.services = services or {}
        self.activation = None
        self.invites = []
        self.status_changes = []
        self.deleted_users = []

    def get_user(self, user_id):
        return self.users.get(user_id)

    def service_for(self, user):
        return self.services[user.telegram_user_id]

    def activate(self, *args):
        self.activation = args
        record = user_record(args[1])
        self.users[args[1]] = record
        return record

    def create_invite(self, name):
        self.invites.append(name)
        return "secure-token"

    def list_users(self):
        return list(self.users.values())

    def set_status(self, user_id, status):
        self.status_changes.append((user_id, status))
        current = self.users[user_id]
        updated = user_record(
            user_id,
            status=status,
            sheet=current.spreadsheet_id,
            cutoff=current.day_start,
        )
        self.users[user_id] = updated
        return updated

    def set_daily_kcal_goal(self, user_id, goal):
        current = self.users[user_id]
        updated = user_record(
            user_id,
            status=current.status,
            sheet=current.spreadsheet_id,
            cutoff=current.day_start,
            goal=goal,
            protein_goal=current.daily_protein_goal,
        )
        self.users[user_id] = updated
        service = self.services.get(user_id)
        if service is not None and hasattr(service, "set_daily_kcal_goal"):
            service.set_daily_kcal_goal(goal)
        return updated

    def set_daily_protein_goal(self, user_id, goal):
        current = self.users[user_id]
        updated = user_record(
            user_id,
            status=current.status,
            sheet=current.spreadsheet_id,
            cutoff=current.day_start,
            goal=current.daily_kcal_goal,
            protein_goal=goal,
        )
        self.users[user_id] = updated
        service = self.services.get(user_id)
        if service is not None and hasattr(service, "set_daily_protein_goal"):
            service.set_daily_protein_goal(goal)
        return updated

    def delete_user(self, user_id):
        self.deleted_users.append(user_id)
        self.users.pop(user_id, None)


def make_update(*, user_id=123, chat_id=None, chat_type=ChatType.PRIVATE):
    class FakeSentMessage:
        def __init__(self, parent, text, message_id):
            self._parent = parent
            self.text = text
            self.message_id = message_id
            self.chat_id = user_id if chat_id is None else chat_id
            self.date = datetime(2026, 8, 2, 9, tzinfo=UTC)

        async def delete(self):
            index = self._parent.replies.index(self.text)
            self._parent.replies.pop(index)
            self._parent.reply_kwargs.pop(index)
            self._parent.deleted_replies.append(self.text)

    class FakeMessage:
        text = "сир 50"
        caption = None
        photo = []
        voice = None
        media_group_id = None
        message_id = 1
        date = datetime(2026, 8, 2, 9, tzinfo=UTC)

        def __init__(self):
            self.replies = []
            self.reply_kwargs = []
            self.deleted_replies = []
            self.chat_id = user_id if chat_id is None else chat_id

        async def reply_text(self, text, **kwargs):
            self.replies.append(text)
            self.reply_kwargs.append(kwargs)
            if text in {LONG_OPERATION_TEXT, LLM_OPERATION_TEXT}:
                return FakeSentMessage(self, text, 100)
            return None

    message = FakeMessage()
    return (
        SimpleNamespace(
            update_id=77,
            message=message,
            effective_user=SimpleNamespace(
                id=user_id,
                username=f"user{user_id}",
                full_name=f"User {user_id}",
            ),
            effective_chat=SimpleNamespace(
                id=user_id if chat_id is None else chat_id, type=chat_type
            ),
            effective_message=message,
        ),
        message,
    )


@pytest.mark.parametrize(
    "chat_type", [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL]
)
def test_non_private_sources_are_ignored(chat_type) -> None:
    class FakeService:
        def __init__(self):
            self.calls = 0

        def process_message(self, *args):
            self.calls += 1
            return "reply"

    service = FakeService()
    manager = FakeManager({123: user_record()}, {123: service})
    handlers = TelegramHandlers(999, manager)
    update, message = make_update(chat_type=chat_type)
    asyncio.run(handlers.text(update, None))
    assert service.calls == 0
    assert message.replies == []


def test_handler_resolves_user_then_passes_message_to_personal_service() -> None:
    class FakeService:
        def __init__(self):
            self.args = None

        def get_existing_reply(self, *args):
            return None

        def process_message(self, *args):
            self.args = args
            return MealReply(
                "reply",
                1,
                date(2026, 8, 2),
                daily_total_text="За день: 60 кк",
            )

    service = FakeService()
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()
    asyncio.run(handlers.text(update, None))
    assert service.args[2] == message.date
    assert message.replies == ["reply", "За день: 60 кк"]
    assert message.reply_kwargs[0]["parse_mode"] == ParseMode.HTML
    assert message.reply_kwargs[0]["do_quote"] is False
    assert message.reply_kwargs[0]["reply_markup"] is not None
    assert "reply_markup" not in message.reply_kwargs[1]
    assert message.deleted_replies == [LLM_OPERATION_TEXT]


def test_voice_is_transcribed_and_processed_as_food_text() -> None:
    class VoiceFile:
        async def download_as_bytearray(self):
            return bytearray(b"ogg-audio")

    class Voice:
        async def get_file(self):
            return VoiceFile()

    class Transcriber:
        def __init__(self):
            self.audio = None

        def transcribe(self, audio):
            self.audio = audio
            return "Сливи двісті грамів калорійність сорок калорій на сто грамів"

    class Service:
        def __init__(self):
            self.args = None

        def get_existing_reply(self, *args):
            return None

        def process_message(self, *args):
            self.args = args
            return MealReply("Сливи", 1, date(2026, 8, 2))

    transcriber = Transcriber()
    service = Service()
    handlers = TelegramHandlers(
        999,
        FakeManager({123: user_record()}, {123: service}),
        transcriber=transcriber,
    )
    update, message = make_update()
    message.voice = Voice()

    asyncio.run(handlers.voice(update, SimpleNamespace(user_data={})))

    assert transcriber.audio == b"ogg-audio"
    assert service.args[0] == (
        "Сливи двісті грамів калорійність сорок калорій на сто грамів"
    )
    assert message.replies == ["Сливи"]
    assert message.deleted_replies == [LLM_OPERATION_TEXT]


def test_temporary_status_is_deleted_when_operation_fails() -> None:
    handlers = TelegramHandlers(999, FakeManager())
    _, message = make_update()

    async def fail_after_status():
        with pytest.raises(RuntimeError, match="controlled"):
            async with handlers._temporary_status(message, llm=True):
                assert message.replies == [LLM_OPERATION_TEXT]
                raise RuntimeError("controlled")

    asyncio.run(fail_after_status())

    assert message.replies == []
    assert message.deleted_replies == [LLM_OPERATION_TEXT]


def test_voice_without_transcriber_returns_clear_error() -> None:
    handlers = TelegramHandlers(
        999, FakeManager({123: user_record()}, {123: SimpleNamespace()})
    )
    update, message = make_update()
    message.voice = SimpleNamespace()

    asyncio.run(handlers.voice(update, SimpleNamespace(user_data={})))

    assert message.replies == [VOICE_ERROR_TEXT]


def test_duplicate_photo_is_checked_before_download() -> None:
    class Photo:
        async def get_file(self):
            raise AssertionError("duplicate photo must not be downloaded")

    class Service:
        def get_existing_reply(self, message_id, timestamp):
            del timestamp
            return MealReply("existing", message_id, date(2026, 8, 2))

    service = Service()
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()
    message.photo = [Photo()]

    asyncio.run(handlers.photo(update, None))

    assert message.replies == ["existing"]
    assert message.reply_kwargs[0]["reply_markup"] is not None
    assert message.reply_kwargs[0]["parse_mode"] == ParseMode.HTML
    assert message.reply_kwargs[0]["do_quote"] is False
    buttons = message.reply_kwargs[0]["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == [
        "⭐ Зберегти",
        "⚖️ Змінити вагу",
    ]
    assert buttons[0].callback_data == "save:1:2026-08-02"
    assert buttons[1].callback_data == "meal-weight:1:2026-08-02"
    delete_button = message.reply_kwargs[0]["reply_markup"].inline_keyboard[1][0]
    assert delete_button.text == "🗑 Видалити"
    assert delete_button.callback_data == "delete:1:2026-08-02"


def test_day_handler_passes_telegram_message_date() -> None:
    rich_reply = (
        "<details><summary>🔥 60 ккал</summary><ul><li>сир  🔥60</li></ul></details>"
    )

    class FakeService:
        def __init__(self):
            self.timestamp = None

        def get_day_summary(self, timestamp):
            self.timestamp = timestamp
            return rich_reply

        def accounting_day(self, timestamp):
            assert timestamp == self.timestamp
            return date(2026, 8, 2)

        def accounting_day_bounds(self, day):
            assert day == date(2026, 8, 2)
            return (
                datetime(2026, 8, 2, 1, tzinfo=TZ),
                datetime(2026, 8, 3, 1, tzinfo=TZ),
            )

    class Statistics:
        def __init__(self):
            self.recorded = []
            self.periods = []
            self.forgotten = []

        def daily_total_message_ids_between(self, *args):
            self.periods.append(args)
            return (590,)

        def forget_daily_total_messages(self, *args):
            self.forgotten.append(args)

        def record_daily_total_message(self, *args):
            self.recorded.append(args)

    service = FakeService()
    statistics = Statistics()
    handlers = TelegramHandlers(
        999,
        FakeManager({123: user_record()}, {123: service}),
        statistics=statistics,
    )
    update, message = make_update()

    class SentMessage:
        chat_id = 123
        message_id = 600
        date = datetime(2026, 8, 2, 9, tzinfo=UTC)

        def __init__(self, text):
            self.text = text

        async def delete(self):
            index = message.replies.index(self.text)
            message.replies.pop(index)
            message.reply_kwargs.pop(index)

    class Bot:
        def __init__(self):
            self.calls = []
            self.deleted = []

        async def do_api_request(self, endpoint, **kwargs):
            self.calls.append((endpoint, kwargs))
            return SentMessage(rich_reply)

        async def delete_messages(self, **kwargs):
            self.deleted.append(kwargs)
            return True

    bot = Bot()
    asyncio.run(handlers.day(update, SimpleNamespace(bot=bot)))

    assert service.timestamp == message.date
    assert message.replies == []
    assert bot.calls == [
        (
            "sendRichMessage",
            {
                "api_kwargs": {
                    "chat_id": 123,
                    "rich_message": {"html": rich_reply},
                },
                "return_type": bot_module.Message,
            },
        )
    ]
    assert statistics.periods == [
        (
            123,
            datetime(2026, 8, 2, 1, tzinfo=TZ),
            datetime(2026, 8, 3, 1, tzinfo=TZ),
        )
    ]
    assert bot.deleted == [{"chat_id": 123, "message_ids": (590,)}]
    assert statistics.forgotten == [(123, (590,))]
    assert statistics.recorded == [(123, 600, datetime(2026, 8, 2, 9, tzinfo=UTC))]


def test_day_handler_maps_read_error_to_user_message() -> None:
    class FailingService:
        def get_day_summary(self, timestamp):
            del timestamp
            raise SheetsReadError("failed")

    handlers = TelegramHandlers(
        999, FakeManager({123: user_record()}, {123: FailingService()})
    )
    update, message = make_update()

    asyncio.run(handlers.day(update, None))

    assert message.replies == [READ_ERROR_TEXT]


def test_weekly_calories_excludes_current_accounting_day_for_user(tmp_path) -> None:
    store = FakeStore(SheetState(today_total=0, existing=None))
    store.daily_totals = {date(2026, 8, 1): 500}
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()
    message.date = datetime(2026, 8, 2, 0, 30, tzinfo=TZ)

    class Bot:
        def __init__(self):
            self.calls = []

        async def do_api_request(self, endpoint, **kwargs):
            self.calls.append((endpoint, kwargs))
            return SimpleNamespace()

    bot = Bot()
    asyncio.run(
        handlers.weekly_calories(update, SimpleNamespace(user_data={}, bot=bot))
    )

    assert store.range == (date(2026, 7, 25), date(2026, 7, 31))
    assert bot.calls[0][0] == "sendRichMessage"
    rich_html = bot.calls[0][1]["api_kwargs"]["rich_message"]["html"]
    assert rich_html.startswith("<h3>Різниця калорій за тиждень</h3>")
    assert "<details><summary>По дням</summary>" in rich_html
    assert message.deleted_replies == [LONG_OPERATION_TEXT]


def test_admin_weekly_calories_includes_garmin_balance(tmp_path) -> None:
    store = FakeStore(SheetState(today_total=0, existing=None))
    store.daily_totals = {date(2026, 8, 1): 2300}
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)
    burned = {date(2026, 7, 26) + timedelta(days=offset): 2500 for offset in range(7)}
    garmin = SimpleNamespace(get_daily_calories=lambda: burned)
    handlers = TelegramHandlers(
        999,
        FakeManager({999: user_record(999)}, {999: service}),
        garmin_calories=garmin,
    )
    update, message = make_update(user_id=999)

    asyncio.run(handlers.weekly_calories(update, SimpleNamespace(user_data={})))

    assert "<b>сб 01.08</b>: + 2300" in message.replies[0]
    assert "= <b><u>-200</u></b>" in message.replies[0]


def test_weekly_admin_falls_back_to_consumed_when_garmin_is_unavailable() -> None:
    class Service:
        burned_totals = "unset"

        def get_weekly(self, timestamp, burned_totals, meal_grouper):
            del timestamp, meal_grouper
            self.burned_totals = burned_totals
            return "<h3>Статистика за тиждень</h3><p>Спожито: 700 ккал</p>"

    service = Service()

    def fail_garmin():
        raise RuntimeError("cache missing")

    handlers = TelegramHandlers(
        999,
        FakeManager({999: user_record(999)}, {999: service}),
        garmin_calories=SimpleNamespace(get_daily_calories=fail_garmin),
    )
    update, message = make_update(user_id=999)

    asyncio.run(handlers.weekly(update, SimpleNamespace(user_data={})))

    assert service.burned_totals is None
    assert message.replies[0].startswith("<b>Статистика за тиждень</b>")
    assert "Спожито: 700 ккал" in message.replies[0]


def test_weekly_meals_uses_same_completed_week(tmp_path) -> None:
    store = FakeStore(SheetState(today_total=0, existing=None))
    store.period_meals = [PeriodMeal("сир", 100, 360)]
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)
    grouper = SimpleNamespace(
        group=lambda names: MealGroupingResult(tuple("Сир" for _ in names), METADATA)
    )
    handlers = TelegramHandlers(
        999,
        FakeManager({123: user_record()}, {123: service}),
        meal_grouper=grouper,
    )
    update, message = make_update()

    asyncio.run(handlers.weekly_meals(update, SimpleNamespace(user_data={})))

    assert store.range == (date(2026, 7, 26), date(2026, 8, 1))
    assert len(message.replies) == 1
    assert message.replies[0].startswith("<b>КБЖВ за тиждень</b>")
    assert "<b>Деталі по стравам</b>" in message.replies[0]
    assert "<b><u>Сир</u></b> 100 г — 🔥 К 360" in message.replies[0]
    assert message.deleted_replies == [LLM_OPERATION_TEXT]


def test_goal_waiting_state_consumes_text_without_food_analysis() -> None:
    class GoalService:
        def __init__(self):
            self.food_calls = 0

        def process_message(self, *args):
            self.food_calls += 1

    service = GoalService()
    manager = FakeManager({123: user_record()}, {123: service})
    handlers = TelegramHandlers(999, manager)
    update, message = make_update()
    context = SimpleNamespace(args=[], user_data={})

    asyncio.run(handlers.goal(update, context))
    assert context.user_data == {"awaiting_daily_kcal_goal": {"kind": "daily_goal"}}
    goal_buttons = message.reply_kwargs[0]["reply_markup"].inline_keyboard
    assert goal_buttons[0][0].text == "❌ Скасувати"
    message.text = "2000"
    asyncio.run(handlers.text(update, context))

    assert manager.users[123].daily_kcal_goal == 2000
    assert service.food_calls == 0
    assert context.user_data == {}
    assert message.replies[-1] == "Денну ціль встановлено: 2000 кк ✓"


def test_protein_goal_waiting_state_saves_grams_without_food_analysis() -> None:
    class GoalService:
        def __init__(self):
            self.food_calls = 0
            self.protein_goal = None

        def process_message(self, *args):
            self.food_calls += 1

        def set_daily_protein_goal(self, goal):
            self.protein_goal = goal

    service = GoalService()
    manager = FakeManager({123: user_record()}, {123: service})
    handlers = TelegramHandlers(999, manager)
    update, message = make_update()
    context = SimpleNamespace(args=[], user_data={})

    asyncio.run(handlers.protein_goal(update, context))
    assert context.user_data == {"awaiting_daily_kcal_goal": {"kind": "protein_goal"}}
    message.text = "100"
    asyncio.run(handlers.text(update, context))

    assert manager.users[123].daily_protein_goal == 100
    assert service.protein_goal == 100
    assert service.food_calls == 0
    assert context.user_data == {}
    assert message.replies[-1] == "Ціль по білку встановлено: 100 г ✓"


def test_meal_weight_state_updates_existing_reply_and_sends_full_result() -> None:
    class WeightService:
        def __init__(self):
            self.args = None

        def change_meal_weight(self, *args):
            self.args = args
            return MealReply(
                "Сир 120 кк",
                42,
                date(2026, 8, 2),
                daily_total_text="За день: 120 кк",
            )

    class Bot:
        def __init__(self):
            self.edits = []

        async def edit_message_text(self, **kwargs):
            self.edits.append(kwargs)

    service = WeightService()
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()
    message.text = "100"
    bot = Bot()
    context = SimpleNamespace(
        user_data={
            "awaiting_meal_weight": {
                "kind": "meal_weight",
                "message_id": 42,
                "day": "2026-08-02",
                "result_chat_id": 123,
                "result_message_id": 777,
                "prompt_chat_id": 123,
                "prompt_message_id": 778,
                "accepts_text": True,
            }
        },
        bot=bot,
    )

    asyncio.run(handlers.text(update, context))

    assert service.args == (42, date(2026, 8, 2), 100)
    assert context.user_data == {}
    assert bot.edits[0]["message_id"] == 777
    assert bot.edits[0]["text"] == "Розрахунок оновлено нижче ↓"
    assert bot.edits[0]["reply_markup"] is None
    assert bot.edits[1] == {
        "chat_id": 123,
        "message_id": 778,
        "text": "✅ Вагу змінено на 100 г",
        "reply_markup": None,
    }
    assert message.replies == ["Сир 120 кк", "За день: 120 кк"]
    assert message.reply_kwargs[0]["parse_mode"] == ParseMode.HTML
    assert message.reply_kwargs[0]["reply_markup"] is not None
    assert message.reply_kwargs[0]["do_quote"] is False
    assert "reply_markup" not in message.reply_kwargs[1]


def test_meal_weight_state_closes_without_reply_when_weight_is_unchanged() -> None:
    class WeightService:
        def change_meal_weight(self, *args):
            raise MealWeightUnchangedError(args[2])

    class Bot:
        def __init__(self):
            self.edits = []

        async def edit_message_text(self, **kwargs):
            self.edits.append(kwargs)

    handlers = TelegramHandlers(
        999, FakeManager({123: user_record()}, {123: WeightService()})
    )
    update, message = make_update()
    message.text = "50 г"
    bot = Bot()
    context = SimpleNamespace(
        user_data={
            "awaiting_meal_weight": {
                "kind": "meal_weight",
                "message_id": 42,
                "day": "2026-08-02",
                "result_chat_id": 123,
                "result_message_id": 777,
                "prompt_chat_id": 123,
                "prompt_message_id": 778,
                "accepts_text": True,
            }
        },
        bot=bot,
    )

    asyncio.run(handlers.text(update, context))

    assert context.user_data == {}
    assert message.replies == []
    assert bot.edits == [
        {
            "chat_id": 123,
            "message_id": 778,
            "text": "Вага вже становить 50 г.",
            "reply_markup": None,
        }
    ]


@pytest.mark.parametrize("raw", ["50", "50 г", "50г", "50 гр", "50 грамів"])
def test_weight_parser_accepts_natural_gram_formats(raw: str) -> None:
    assert TelegramHandlers._parse_weight(raw) == 50


def test_weight_choice_buttons_use_configured_presets() -> None:
    handlers = TelegramHandlers(999, FakeManager(), (75, 125, 250))

    keyboard = handlers._weight_choice_markup(42, date(2026, 8, 2)).inline_keyboard

    assert [button.text for button in keyboard[0]] == ["75г", "125г", "250г"]
    assert keyboard[0][1].callback_data == "meal-weight-set:42:2026-08-02:125"
    assert keyboard[1][0].text == "Інша вага"
    assert keyboard[1][0].callback_data == "meal-weight-other:42:2026-08-02"


def test_weight_text_is_ignored_until_other_weight_is_selected() -> None:
    class WeightService:
        def __init__(self):
            self.calls = 0

        def change_meal_weight(self, *args):
            self.calls += 1

    service = WeightService()
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()
    message.text = "125"
    context = SimpleNamespace(
        user_data={
            "awaiting_meal_weight": {
                "kind": "meal_weight",
                "message_id": 42,
                "day": "2026-08-02",
                "accepts_text": False,
            }
        }
    )

    asyncio.run(handlers.text(update, context))

    assert service.calls == 0
    assert message.replies == ["Спочатку обери вагу кнопкою або натисни «Інша вага»."]


def test_composite_meal_reply_hides_weight_button() -> None:
    result = MealReply(
        "Обід",
        42,
        date(2026, 8, 2),
        can_save=True,
        can_change_weight=False,
    )

    keyboard = TelegramHandlers._meal_reply_markup(result).inline_keyboard

    assert [[button.text for button in row] for row in keyboard] == [
        ["⭐ Зберегти"],
        ["🗑 Видалити"],
    ]


def test_goal_with_existing_value_can_be_disabled() -> None:
    service = SimpleNamespace(set_daily_kcal_goal=lambda goal: None)
    manager = FakeManager(
        {123: user_record(goal=2000)},
        {123: service},
    )
    handlers = TelegramHandlers(999, manager)
    update, message = make_update()
    context = SimpleNamespace(args=[], user_data={})

    asyncio.run(handlers.goal(update, context))
    button = message.reply_kwargs[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "goal-disable:123"
    cancel = message.reply_kwargs[-1]["reply_markup"].inline_keyboard[1][0]
    assert cancel.callback_data == "wait-cancel"

    callback_update, query = make_callback_update("goal-disable:123")
    asyncio.run(handlers.goal_disable_callback(callback_update, context))

    assert manager.users[123].daily_kcal_goal is None
    assert query.edits == [("Денну ціль вимкнено ✓", {"reply_markup": None})]


def test_protein_goal_with_existing_value_can_be_disabled() -> None:
    service = SimpleNamespace(set_daily_protein_goal=lambda goal: None)
    manager = FakeManager(
        {123: user_record(protein_goal=100)},
        {123: service},
    )
    handlers = TelegramHandlers(999, manager)
    update, message = make_update()
    context = SimpleNamespace(args=[], user_data={})

    asyncio.run(handlers.protein_goal(update, context))
    button = message.reply_kwargs[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "protein-goal-disable:123"

    callback_update, query = make_callback_update("protein-goal-disable:123")
    asyncio.run(handlers.protein_goal_disable_callback(callback_update, context))

    assert manager.users[123].daily_protein_goal is None
    assert query.edits == [("Ціль по білку вимкнено ✓", {"reply_markup": None})]


def make_callback_update(data="delete:42:2026-08-02", *, user_id=123):
    class FakeQuery:
        def __init__(self):
            self.id = "callback-1"
            self.data = data
            self.message = None
            self.answers = []
            self.edits = []
            self.markup_edits = []

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

        async def edit_message_reply_markup(self, **kwargs):
            self.markup_edits.append(kwargs)

    query = FakeQuery()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username=f"user{user_id}"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        effective_message=SimpleNamespace(),
        callback_query=query,
    )
    return update, query


def test_delete_callback_cleans_stale_totals_and_sends_updated_total() -> None:
    rich_summary = "<h3>За сьогодні:</h3><details><summary>905 кк</summary></details>"

    class FakeService:
        def __init__(self):
            self.args = None

        def delete_message(self, *args):
            self.args = args
            return MealDeletion(date(2026, 8, 2), 905, None, True, "сир")

        def format_deletion_reply(self, deletion):
            assert deletion.deleted is True
            return "Видалено Сир"

        def get_day_summary(self, timestamp):
            assert timestamp.tzinfo == UTC
            return rich_summary

        def accounting_day(self, timestamp):
            assert timestamp.tzinfo == UTC
            return date(2026, 8, 2)

        def accounting_day_bounds(self, day):
            assert day == date(2026, 8, 2)
            return (
                datetime(2026, 8, 2, 1, tzinfo=TZ),
                datetime(2026, 8, 3, 1, tzinfo=TZ),
            )

    class Statistics:
        def __init__(self):
            self.period_calls = []
            self.forgotten = []
            self.recorded = []

        def daily_total_message_ids_between(self, *args):
            self.period_calls.append(args)
            return (701, 705)

        def forget_daily_total_messages(self, *args):
            self.forgotten.append(args)

        def record_daily_total_message(self, *args):
            self.recorded.append(args)

    class Bot:
        def __init__(self):
            self.deleted = []
            self.calls = []

        async def delete_messages(self, **kwargs):
            self.deleted.append(kwargs)
            return True

        async def do_api_request(self, endpoint, **kwargs):
            self.calls.append((endpoint, kwargs))
            return SimpleNamespace(
                chat_id=kwargs["api_kwargs"]["chat_id"],
                message_id=710,
                date=datetime(2026, 8, 2, 9, tzinfo=UTC),
            )

    service = FakeService()
    statistics = Statistics()
    handlers = TelegramHandlers(
        999,
        FakeManager({123: user_record()}, {123: service}),
        statistics=statistics,
    )
    update, query = make_callback_update()
    query.message = SimpleNamespace(message_id=700, chat_id=123)
    bot = Bot()

    asyncio.run(handlers.delete(update, SimpleNamespace(bot=bot)))

    assert service.args == (42, date(2026, 8, 2))
    assert query.answers == [(None, {})]
    assert query.edits == [
        (
            "Видалено Сир",
            {"parse_mode": ParseMode.HTML, "reply_markup": None},
        )
    ]
    assert statistics.period_calls == [
        (
            123,
            datetime(2026, 8, 2, 1, tzinfo=TZ),
            datetime(2026, 8, 3, 1, tzinfo=TZ),
        )
    ]
    assert bot.deleted == [{"chat_id": 123, "message_ids": (701, 705)}]
    assert statistics.forgotten == [(123, (701, 705))]
    assert bot.calls == [
        (
            "sendRichMessage",
            {
                "api_kwargs": {
                    "chat_id": 123,
                    "rich_message": {"html": rich_summary},
                },
                "return_type": bot_module.Message,
            },
        )
    ]
    assert statistics.recorded == [(123, 710, datetime(2026, 8, 2, 9, tzinfo=UTC))]


def test_saved_add_callback_uses_stable_negative_event_id() -> None:
    class Service:
        def __init__(self):
            self.calls = []

        def add_saved_meal(self, *args):
            self.calls.append(args)
            return MealReply("Додано", args[2], date(2026, 8, 2), can_save=False)

    service = Service()
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, query = make_callback_update("saved-add:meal1:350")
    context = SimpleNamespace(user_data={})

    asyncio.run(handlers.library_callback(update, context))
    asyncio.run(handlers.library_callback(update, context))

    assert len(service.calls) == 2
    assert service.calls[0][0:2] == ("meal1", 350)
    assert service.calls[0][2] < 0
    assert service.calls[0][2] == service.calls[1][2]
    assert query.answers == [("Додано", {}), ("Додано", {})]


def test_recent_add_callback_uses_weight_printed_on_button() -> None:
    class Service:
        def __init__(self):
            self.args = None

        def add_recent_meal(self, *args):
            self.args = args
            return MealReply("Додано", args[3], date(2026, 8, 2))

    service = Service()
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, query = make_callback_update("recent-add:42:2026-08-02:175")

    asyncio.run(handlers.library_callback(update, SimpleNamespace(user_data={})))

    assert service.args[0:3] == (42, date(2026, 8, 2), 175)
    assert service.args[3] < 0
    assert query.answers == [("Додано", {})]


@pytest.mark.parametrize(
    ("callback_data", "service_method"),
    [
        ("saved-add:meal1:350", "add_saved_meal"),
        ("recent-add:42:2026-08-02:175", "add_recent_meal"),
    ],
)
def test_selecting_saved_or_recent_meal_deletes_the_used_menu(
    callback_data, service_method
) -> None:
    class Service:
        def add_saved_meal(self, *args):
            return MealReply("Додано", args[2], date(2026, 8, 2))

        def add_recent_meal(self, *args):
            return MealReply("Додано", args[3], date(2026, 8, 2))

    class MenuMessage:
        chat_id = 123
        message_id = 600

        def __init__(self):
            self.deleted = False
            self.replies = []

        async def delete(self):
            self.deleted = True

        async def reply_text(self, text, **kwargs):
            self.replies.append((text, kwargs))

    service = Service()
    assert callable(getattr(service, service_method))
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, query = make_callback_update(callback_data)
    menu = MenuMessage()
    query.message = menu

    asyncio.run(
        handlers.library_callback(
            update,
            SimpleNamespace(user_data={}),
            show_status=False,
        )
    )

    assert menu.deleted is True
    assert [reply[0] for reply in menu.replies] == ["Додано"]
    assert query.answers == [("Додано", {})]
    assert query.markup_edits == []


def test_save_callback_hides_save_button_but_keeps_delete() -> None:
    value = calculate_meal(food_analysis())
    template = SavedMeal(
        saved_meal_id="meal1",
        source_message_id=42,
        display_name="сир",
        default_total_weight_g=50,
        base_meal=value,
    )
    service = SimpleNamespace(save_source_meal=lambda *args: (template, True))
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, query = make_callback_update("save:42:2026-08-02")

    asyncio.run(handlers.save_callback(update, SimpleNamespace()))

    keyboard = query.markup_edits[0]["reply_markup"].inline_keyboard
    assert query.answers == [("Збережено: сир", {})]
    assert keyboard[0][0].text == "⚖️ Змінити вагу"
    assert keyboard[0][0].callback_data == "meal-weight:42:2026-08-02"
    assert keyboard[1][0].text == "🗑 Видалити"
    assert keyboard[1][0].callback_data == "delete:42:2026-08-02"


def test_change_weight_callback_rejects_composite_meal() -> None:
    composite = calculate_meal(
        FoodAnalysis(
            is_food=True,
            meal_name="обід",
            items=[
                FoodItem(
                    name="курка",
                    weight_g=100,
                    weight_estimated=False,
                    kcal_per_100g=200,
                    kcal_estimated=False,
                ),
                FoodItem(
                    name="рис",
                    weight_g=200,
                    weight_estimated=False,
                    kcal_per_100g=100,
                    kcal_estimated=False,
                ),
            ],
        )
    )
    recent = RecentMeal(
        telegram_message_id=42,
        day=date(2026, 8, 2),
        meal=composite,
        normalized_request="обід",
    )
    service = SimpleNamespace(get_recent_meal=lambda *args: recent)
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, query = make_callback_update("meal-weight:42:2026-08-02")
    context = SimpleNamespace(user_data={})

    asyncio.run(handlers.meal_weight_callback(update, context))

    assert context.user_data == {}
    assert query.answers == [
        (
            "Змінити вагу можна лише для страви з одного компонента.",
            {"show_alert": True},
        )
    ]


def test_repeated_delete_callback_uses_idempotent_result() -> None:
    class FakeService:
        def delete_message(self, *args):
            return MealDeletion(date(2026, 8, 2), 905, None, False)

        def format_deletion_reply(self, deletion):
            assert deletion.deleted is False
            return "Цей запис уже видалено"

    handlers = TelegramHandlers(
        999, FakeManager({123: user_record()}, {123: FakeService()})
    )
    update, query = make_callback_update()

    asyncio.run(handlers.delete(update, SimpleNamespace()))

    assert query.edits[0][0] == "Цей запис уже видалено"


def test_delete_callback_keeps_messages_when_sheets_fail() -> None:
    class FailingService:
        def delete_message(self, *args):
            raise SheetsWriteError("failed")

    handlers = TelegramHandlers(
        999, FakeManager({123: user_record()}, {123: FailingService()})
    )
    update, query = make_callback_update()

    asyncio.run(handlers.delete(update, SimpleNamespace()))

    assert query.edits == []
    assert query.answers == [(DELETE_ERROR_TEXT, {"show_alert": True})]


def test_delete_callback_rejects_unknown_user_without_touching_store() -> None:
    handlers = TelegramHandlers(999, FakeManager())
    update, query = make_callback_update(user_id=999)

    asyncio.run(handlers.delete(update, SimpleNamespace()))

    assert query.answers == [("Доступ лише за запрошенням.", {"show_alert": True})]
    assert query.edits == []


@pytest.mark.parametrize(
    "data", [None, "other:42:2026-08-02", "delete:0:2026-08-02", "delete:42:bad"]
)
def test_delete_callback_rejects_invalid_payload(data) -> None:
    service = SimpleNamespace()
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
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

        def get_existing_reply(self, *args):
            return None

        def process_message(self, *args):
            self.args = args
            return MealReply("reply", 1, date(2026, 8, 2))

    service = FakeService()
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
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
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
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

    handlers = TelegramHandlers(
        999, FakeManager({123: user_record()}, {123: FailingService()})
    )
    update, message = make_update()
    asyncio.run(handlers.text(update, None))
    assert message.replies == [expected]


def test_start_and_help_use_separate_content_for_active_user() -> None:
    service = SimpleNamespace()
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()
    asyncio.run(handlers.start(update, SimpleNamespace(args=[])))
    asyncio.run(handlers.help(update, SimpleNamespace()))
    assert len(message.replies) == 2
    assert message.replies == [load_start_text(), load_help_text()]
    assert message.reply_kwargs == [
        {"do_quote": False},
        {"do_quote": False},
    ]


def test_help_text_is_loaded_from_editable_files(monkeypatch, tmp_path) -> None:
    help_file = tmp_path / "help.txt"
    admin_help_file = tmp_path / "admin_help.txt"
    help_file.write_text("Перша довідка\n", encoding="utf-8")
    admin_help_file.write_text("Команди адміністратора\n", encoding="utf-8")
    monkeypatch.setattr(bot_module, "HELP_TEXT_FILE", help_file)
    monkeypatch.setattr(bot_module, "ADMIN_HELP_TEXT_FILE", admin_help_file)

    assert load_help_text(admin=True) == "Перша довідка\n\nКоманди адміністратора"

    help_file.write_text("Оновлена довідка\n", encoding="utf-8")
    assert load_help_text() == "Оновлена довідка"


def test_unknown_and_blocked_users_never_get_a_service() -> None:
    manager = FakeManager({124: user_record(124, status="blocked")})
    handlers = TelegramHandlers(999, manager)

    unknown_update, unknown_message = make_update(user_id=123)
    blocked_update, blocked_message = make_update(user_id=124)
    asyncio.run(handlers.text(unknown_update, SimpleNamespace()))
    asyncio.run(handlers.text(blocked_update, SimpleNamespace()))

    assert unknown_message.replies == ["Доступ лише за запрошенням."]
    assert blocked_message.replies == ["Доступ до бота вимкнено."]


def test_blocked_photo_is_not_downloaded() -> None:
    class Photo:
        async def get_file(self):
            raise AssertionError("blocked photo must not be downloaded")

    handlers = TelegramHandlers(999, FakeManager({123: user_record(status="blocked")}))
    update, message = make_update()
    message.photo = [Photo()]

    asyncio.run(handlers.photo(update, SimpleNamespace()))

    assert message.replies == ["Доступ до бота вимкнено."]


def test_two_users_are_routed_to_different_services() -> None:
    class Service:
        def __init__(self, label):
            self.label = label
            self.calls = []

        def process_message(self, *args):
            self.calls.append(args)
            return MealReply(self.label, args[1], date(2026, 8, 2))

    first = Service("first")
    second = Service("second")
    manager = FakeManager(
        {123: user_record(123), 124: user_record(124, sheet="sheet-124")},
        {123: first, 124: second},
    )
    handlers = TelegramHandlers(999, manager)
    first_update, first_message = make_update(user_id=123)
    second_update, second_message = make_update(user_id=124)

    asyncio.run(handlers.text(first_update, SimpleNamespace()))
    asyncio.run(handlers.text(second_update, SimpleNamespace()))

    assert len(first.calls) == len(second.calls) == 1
    assert first_message.replies == ["first"]
    assert second_message.replies == ["second"]


def test_start_activates_invite_and_repeat_start_does_not_reactivate() -> None:
    manager = FakeManager()
    handlers = TelegramHandlers(999, manager)
    update, message = make_update()
    context = SimpleNamespace(args=["token"])

    asyncio.run(handlers.start(update, context))
    asyncio.run(handlers.start(update, context))

    assert manager.activation == ("token", 123, "user123")
    assert message.replies == [load_start_text(), load_start_text()]


def test_start_without_invite_and_blocked_start_show_access_messages() -> None:
    manager = FakeManager({124: user_record(124, status="blocked")})
    handlers = TelegramHandlers(999, manager)
    unknown_update, unknown_message = make_update(user_id=123)
    blocked_update, blocked_message = make_update(user_id=124)

    asyncio.run(handlers.start(unknown_update, SimpleNamespace(args=[])))
    asyncio.run(handlers.start(blocked_update, SimpleNamespace(args=[])))

    assert unknown_message.replies == ["Доступ лише за запрошенням."]
    assert blocked_message.replies == ["Доступ до бота вимкнено."]


def test_admin_invite_returns_deep_link() -> None:
    class Bot:
        async def get_me(self):
            return SimpleNamespace(username="calorie_bot")

    manager = FakeManager()
    handlers = TelegramHandlers(999, manager)
    update, message = make_update(user_id=999)

    asyncio.run(handlers.invite(update, SimpleNamespace(args=["Вася"], bot=Bot())))

    assert manager.invites == ["Вася"]
    assert message.replies == ["https://t.me/calorie_bot?start=secure-token"]


def test_admin_invite_from_menu_consumes_next_text_as_name() -> None:
    class Bot:
        async def get_me(self):
            return SimpleNamespace(username="calorie_bot")

    manager = FakeManager()
    handlers = TelegramHandlers(999, manager)
    update, message = make_update(user_id=999)
    context = SimpleNamespace(args=[], user_data={}, bot=Bot())

    asyncio.run(handlers.invite(update, context))
    message.text = "  Нова   людина "
    asyncio.run(handlers.text(update, context))

    assert manager.invites == ["Нова людина"]
    assert message.replies == [
        "Введи ім’я нового користувача.",
        "https://t.me/calorie_bot?start=secure-token",
    ]
    assert context.user_data == {}


def test_meals_command_shows_all_saved_meals_without_pagination() -> None:
    value = calculate_meal(food_analysis())
    saved_meals = [
        SavedMeal(
            saved_meal_id=f"meal{i}",
            source_message_id=i,
            display_name=f"Страва {i}",
            default_total_weight_g=50,
            base_meal=value,
        )
        for i in range(25)
    ]
    service = SimpleNamespace(list_saved_meals=lambda: saved_meals)
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()

    asyncio.run(handlers.meals(update, SimpleNamespace(user_data={})))

    keyboard = message.reply_kwargs[0]["reply_markup"].inline_keyboard
    assert len(keyboard) == 26
    assert keyboard[0][0].text == "➕ Страва 0 · 50 г"
    assert [button.text for button in keyboard[-1]] == ["🗑 Видалити із збережених"]
    assert keyboard[-1][0].callback_data == "meals-manage"
    assert not any(
        "наступ" in button.text.casefold() for row in keyboard for button in row
    )


def test_empty_saved_meals_explains_how_to_save_without_delete_button() -> None:
    service = SimpleNamespace(list_saved_meals=lambda: [])
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()

    asyncio.run(handlers.meals(update, SimpleNamespace(user_data={})))

    assert message.replies == [
        "Збережених страв ще немає.\n"
        "Після розрахунку натисни «⭐ Зберегти», і страва з’явиться тут."
    ]
    assert message.reply_kwargs[0]["reply_markup"] is None


def test_saved_meal_management_only_offers_deletion() -> None:
    saved = SavedMeal(
        saved_meal_id="cheese",
        source_message_id=1,
        display_name="Сир",
        default_total_weight_g=50,
        base_meal=calculate_meal(food_analysis()),
        icon="🧀",
    )
    service = SimpleNamespace(list_saved_meals=lambda: [saved])
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))

    menu_update, menu_query = make_callback_update("meals-manage")
    asyncio.run(handlers.library_callback(menu_update, SimpleNamespace(user_data={})))

    assert menu_query.edits[0][0] == "Яку страву видалити?"
    menu_keyboard = menu_query.edits[0][1]["reply_markup"].inline_keyboard
    assert menu_keyboard[0][0].text == "🧀 Сир"
    assert menu_keyboard[0][0].callback_data == "manage-delete:cheese"
    assert menu_keyboard[-1][0].callback_data == "meals-back"

    delete_update, delete_query = make_callback_update("manage-delete:cheese")
    asyncio.run(handlers.library_callback(delete_update, SimpleNamespace(user_data={})))

    assert delete_query.edits[0][0] == "Видалити «Сир» із збережених?"
    delete_keyboard = delete_query.edits[0][1]["reply_markup"].inline_keyboard
    assert [button.text for button in delete_keyboard[0]] == [
        "🗑 Видалити",
        "❌ Скасувати",
    ]
    assert delete_keyboard[0][0].callback_data == "manage-delete-do:cheese"
    assert delete_keyboard[0][1].callback_data == "meals-manage"


def test_saved_meal_button_uses_confident_semantic_icon() -> None:
    value = calculate_meal(food_analysis())
    service = SimpleNamespace(
        list_saved_meals=lambda: [
            SavedMeal(
                saved_meal_id="cheese",
                source_message_id=1,
                display_name="Сир",
                default_total_weight_g=50,
                base_meal=value,
                icon="🧀",
            )
        ]
    )
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()

    asyncio.run(handlers.meals(update, SimpleNamespace(user_data={})))

    button = message.reply_kwargs[0]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "➕ 🧀 Сир · 50 г"


def test_recent_command_is_separate_direct_add_list() -> None:
    value = calculate_meal(food_analysis())
    recent = [
        RecentMeal(
            telegram_message_id=42,
            day=date(2026, 8, 2),
            meal=value,
            normalized_request="сир",
        )
    ]
    service = SimpleNamespace(list_recent_meals=lambda: recent)
    handlers = TelegramHandlers(999, FakeManager({123: user_record()}, {123: service}))
    update, message = make_update()

    asyncio.run(handlers.recent(update, SimpleNamespace(user_data={})))

    button = message.reply_kwargs[0]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "➕ сир · 50 г"
    assert button.callback_data == "recent-add:42:2026-08-02:50"


def test_recent_meal_list_limit_is_doubled_to_sixteen(tmp_path) -> None:
    class RecentStore(FakeStore):
        requested_limit = None

        def get_recent_meals(self, limit):
            self.requested_limit = limit
            return []

    store = RecentStore(SheetState(today_total=0, existing=None))
    service = build_service(FakeAnalyzer(food_analysis()), store, tmp_path)

    assert service.list_recent_meals() == []
    assert RECENT_MEALS_LIMIT == 16
    assert store.requested_limit == 16


def test_admin_help_and_user_list_are_available_without_personal_account() -> None:
    manager = FakeManager({123: user_record()})
    handlers = TelegramHandlers(999, manager)
    update, message = make_update(user_id=999)

    asyncio.run(handlers.help(update, SimpleNamespace()))
    asyncio.run(handlers.users(update, SimpleNamespace()))

    assert message.replies == [
        load_help_text(admin=True),
        "Користувачі (1):\n• User — активний — ID 123 (@user)",
    ]


def test_info_shows_release_to_admin_only() -> None:
    handlers = TelegramHandlers(999, FakeManager())
    admin_update, admin_message = make_update(user_id=999)
    user_update, user_message = make_update(user_id=123)

    asyncio.run(handlers.info(admin_update, SimpleNamespace(user_data={})))
    asyncio.run(handlers.info(user_update, SimpleNamespace(user_data={})))

    assert admin_message.replies == ["Версія: 1.9.0"]
    assert user_message.replies == ["Недоступно."]


def test_tracking_records_incoming_interaction_and_extended_info() -> None:
    class Statistics:
        def __init__(self):
            self.recorded = None

        def record_message(self, *args):
            self.recorded = args

        def format_info(self, version):
            return f"Версія: {version}\nЗапити за 24 години:\n• разом: 7"

    statistics = Statistics()
    handlers = TelegramHandlers(999, FakeManager(), statistics=statistics)
    update, message = make_update(user_id=999)

    asyncio.run(handlers.track_interaction(update, SimpleNamespace()))
    asyncio.run(handlers.info(update, SimpleNamespace(user_data={})))

    assert statistics.recorded == (
        77,
        message.date,
        999,
        "User 999",
        "user999",
    )
    assert message.replies == ["Версія: 1.9.0\nЗапити за 24 години:\n• разом: 7"]


def test_only_admin_can_read_cached_garmin_calories() -> None:
    garmin = SimpleNamespace(format_weekly_report=lambda: "Garmin report")
    handlers = TelegramHandlers(999, FakeManager(), garmin_calories=garmin)
    admin_update, admin_message = make_update(user_id=999)
    user_update, user_message = make_update(user_id=123)

    asyncio.run(handlers.burned(admin_update, SimpleNamespace(user_data={})))
    asyncio.run(handlers.burned(user_update, SimpleNamespace(user_data={})))

    assert admin_message.replies == ["Garmin report"]
    assert user_message.replies == ["Недоступно."]


def test_tracking_records_inline_interactions_for_the_clicking_user() -> None:
    class Statistics:
        def __init__(self):
            self.recorded = None

        def record_message(self, *args):
            self.recorded = args

    statistics = Statistics()
    handlers = TelegramHandlers(999, FakeManager(), statistics=statistics)
    update = SimpleNamespace(
        update_id=88,
        message=None,
        callback_query=SimpleNamespace(),
        effective_user=SimpleNamespace(
            id=123,
            full_name="Юля",
            username="yulia",
        ),
    )

    asyncio.run(handlers.track_interaction(update, SimpleNamespace()))

    assert statistics.recorded[0] == 88
    assert statistics.recorded[2:] == (123, "Юля", "yulia")
    assert datetime.now(UTC) - statistics.recorded[1] < timedelta(seconds=1)


def test_non_admin_cannot_execute_admin_command() -> None:
    manager = FakeManager({123: user_record()})
    handlers = TelegramHandlers(999, manager)
    update, message = make_update()

    asyncio.run(handlers.block(update, SimpleNamespace(args=["123"])))

    assert manager.status_changes == []
    assert message.replies == ["Недоступно."]


def test_admin_block_unblock_and_confirmed_delete() -> None:
    manager = FakeManager({123: user_record()})
    handlers = TelegramHandlers(999, manager)
    admin_update, admin_message = make_update(user_id=999)

    asyncio.run(handlers.block(admin_update, SimpleNamespace(args=["123"])))
    asyncio.run(handlers.unblock(admin_update, SimpleNamespace(args=["123"])))
    asyncio.run(
        handlers.delete_user_command(admin_update, SimpleNamespace(args=["123"]))
    )
    callback_update, query = make_callback_update("admin-delete:123", user_id=999)
    asyncio.run(handlers.admin_delete_callback(callback_update, SimpleNamespace()))

    assert manager.status_changes == [(123, "blocked"), (123, "active")]
    assert manager.deleted_users == [123]
    assert admin_message.replies[:2] == ["Заблоковано: 123", "Розблоковано: 123"]
    assert query.edits == [("Користувача 123 видалено.", {"reply_markup": None})]


def test_admin_can_cancel_delete() -> None:
    handlers = TelegramHandlers(999, FakeManager())
    update, query = make_callback_update("admin-cancel:123", user_id=999)

    asyncio.run(handlers.admin_delete_callback(update, SimpleNamespace()))

    assert query.answers == [(None, {})]
    assert query.edits == [("Видалення скасовано.", {"reply_markup": None})]


def test_admin_commands_validate_arguments() -> None:
    manager = FakeManager()
    handlers = TelegramHandlers(999, manager)
    update, message = make_update(user_id=999)

    asyncio.run(handlers.invite(update, SimpleNamespace(args=[], bot=None)))
    asyncio.run(handlers.block(update, SimpleNamespace(args=["bad"])))
    asyncio.run(handlers.delete_user_command(update, SimpleNamespace(args=[])))

    assert message.replies == [
        "Введи ім’я нового користувача.",
        "Формат: /block <telegram_user_id>",
        "Формат: /delete <telegram_user_id>",
    ]
