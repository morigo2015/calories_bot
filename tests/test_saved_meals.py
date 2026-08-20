from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from calories_bot.bot import (
    CaloriesService,
    CompositeMealWeightError,
    MealWeightUnchangedError,
    SavedMealNameError,
)
from calories_bot.models import (
    FoodAnalysis,
    FoodItem,
    LLMMetadata,
    MealIconSuggestion,
    RecentMeal,
    SavedMeal,
    StoredMeal,
    calculate_meal,
    format_simple_meal_request,
    nutrition_summary,
    scale_meal,
)
from calories_bot.saved_meals import (
    LEGACY_SAVED_MEALS_HEADERS,
    PREVIOUS_SAVED_MEALS_HEADERS,
    SAVED_MEALS_HEADERS,
    GoogleSavedMealStore,
    SavedMealsSchemaError,
)
from calories_bot.sheets import DayMeal, MealDeletion, MealUpdate, SheetState

TZ = ZoneInfo("Europe/Kyiv")
DAY = date(2026, 8, 9)


def meal(name: str = "сир"):
    return calculate_meal(
        FoodAnalysis(
            is_food=True,
            meal_name=name,
            items=[
                FoodItem(
                    name="сир",
                    weight_g=50,
                    weight_estimated=False,
                    kcal_per_100g=120,
                    kcal_estimated=False,
                )
            ],
        )
    )


def composite_meal():
    return calculate_meal(
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


def saved(
    saved_id: str,
    source_id: int,
    name: str = "сир",
    *,
    value=None,
    icon: str | None = None,
) -> SavedMeal:
    return SavedMeal(
        saved_meal_id=saved_id,
        source_message_id=source_id,
        display_name=name,
        default_total_weight_g=50,
        base_meal=value or meal(name),
        icon=icon,
    )


class Worksheet:
    def __init__(self, rows):
        self.rows = rows

    def get_all_values(self, **kwargs):
        del kwargs
        return self.rows

    def append_row(self, row, **kwargs):
        del kwargs
        self.rows.append(list(row))

    def clear(self):
        self.rows.clear()

    def batch_update(self, updates, **kwargs):
        del kwargs
        for update in updates:
            column = 2 if update["range"].startswith("C") else 3
            row = int(update["range"][1:]) - 1
            self.rows[row][column] = update["values"][0][0]

    def add_cols(self, cols):
        for row in self.rows:
            row.extend([""] * cols)

    def update(self, values, range_name, **kwargs):
        del kwargs
        assert range_name == "F1:F1"
        self.rows[0][5] = values[0][0]

    def delete_rows(self, row):
        self.rows.pop(row - 1)


def sheet_store(rows) -> GoogleSavedMealStore:
    store = GoogleSavedMealStore.__new__(GoogleSavedMealStore)
    store._worksheet = Worksheet(rows)
    return store


def test_saved_meal_store_append_delete_and_newest_first() -> None:
    store = sheet_store([SAVED_MEALS_HEADERS])

    store.append(saved("one", 1, "Перша"))
    store.append(saved("two", 2, "Друга"))

    assert [item.saved_meal_id for item in store.list_meals()] == ["two", "one"]
    assert store.find_by_source(1).display_name == "Перша"
    assert store.delete("two") is True
    assert store.delete("two") is False
    assert [item.saved_meal_id for item in store.list_meals()] == ["one"]


def test_saved_meal_store_persists_icon() -> None:
    store = sheet_store([SAVED_MEALS_HEADERS])

    stored = store.append(saved("one", 1, icon="🧀"))

    assert stored.icon == "🧀"
    assert store._worksheet.rows[1][-1] == "🧀"


def test_saved_meal_store_rejects_unknown_schema() -> None:
    store = sheet_store([["name", "weight"]])

    with pytest.raises(SavedMealsSchemaError):
        store._ensure_headers()


def test_saved_meal_store_initializes_empty_worksheet() -> None:
    store = sheet_store([])

    store._ensure_headers()

    assert store._worksheet.rows == [SAVED_MEALS_HEADERS]


def test_saved_meal_store_clears_previous_schema_once() -> None:
    existing = saved("one", 1)
    store = sheet_store(
        [
            list(LEGACY_SAVED_MEALS_HEADERS),
            [
                existing.saved_meal_id,
                existing.source_message_id,
                existing.display_name,
                existing.default_total_weight_g,
                existing.base_meal.model_dump_json(),
            ],
        ]
    )

    store._ensure_headers()

    assert store._worksheet.rows == [SAVED_MEALS_HEADERS]


def test_saved_meal_store_clears_composite_capable_schema_once() -> None:
    existing = saved("one", 1)
    store = sheet_store(
        [
            list(PREVIOUS_SAVED_MEALS_HEADERS),
            [
                existing.saved_meal_id,
                existing.source_message_id,
                existing.display_name,
                existing.default_total_weight_g,
                existing.base_meal.model_dump_json(),
                "",
            ],
        ]
    )

    store._ensure_headers()
    store._ensure_headers()

    assert store._worksheet.rows == [SAVED_MEALS_HEADERS]


def test_scale_meal_changes_all_components_without_changing_origins() -> None:
    base = calculate_meal(
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
                    portion_display="1 шт.",
                ),
                FoodItem(
                    name="рис",
                    weight_g=200,
                    weight_estimated=True,
                    kcal_per_100g=100,
                    kcal_estimated=True,
                ),
            ],
        )
    )

    result = scale_meal(base, 600, meal_name="Великий обід")

    assert result.meal_name == "Великий обід"
    assert [item.weight_g for item in result.items] == [200, 400]
    assert [item.calories for item in result.items] == [400, 400]
    assert result.meal_kcal == 800
    assert result.items[0].weight_origin == "user_text"
    assert result.items[1].weight_origin == "model_estimate"
    assert result.items[0].portion_display is None


class MemorySavedStore:
    def __init__(self):
        self.items: list[SavedMeal] = []

    def list_meals(self):
        return list(reversed(self.items))

    def get(self, saved_id):
        return next(
            (item for item in self.items if item.saved_meal_id == saved_id), None
        )

    def find_by_source(self, source_id):
        return next(
            (item for item in self.items if item.source_message_id == source_id), None
        )

    def append(self, item):
        self.items.append(item)
        return item

    def delete(self, saved_id):
        current = self.get(saved_id)
        if current is None:
            return False
        self.items.remove(current)
        return True


class MemoryMealStore:
    def __init__(self):
        self.rows: list[tuple[date, int, StoredMeal]] = []

    def add_source(self, source_id, value=None, normalized=None):
        normalized = normalized or format_simple_meal_request(
            source_id, 0, 1, "analysis", "food"
        )
        self.rows.append(
            (
                DAY,
                source_id,
                StoredMeal(
                    normalized_request=normalized,
                    meal=value or meal(),
                    metadata=LLMMetadata(model="test", effort="none"),
                ),
            )
        )

    def get_state(self, day, message_id):
        existing = next(
            (
                stored
                for row_day, row_id, stored in self.rows
                if row_id == message_id and (message_id < 0 or row_day == day)
            ),
            None,
        )
        total = sum(
            stored.meal.meal_kcal for row_day, _, stored in self.rows if row_day == day
        )
        return SheetState(today_total=total, existing=existing)

    def get_meal(self, day, message_id):
        return next(
            (
                stored
                for row_day, row_id, stored in self.rows
                if row_day == day and row_id == message_id
            ),
            None,
        )

    def get_latest_meal(self):
        if not self.rows:
            return None
        day, message_id, stored = self.rows[-1]
        return RecentMeal(
            telegram_message_id=message_id,
            day=day,
            meal=stored.meal,
            normalized_request=stored.normalized_request,
        )

    def get_recent_meals(self, limit=8):
        return [self.get_latest_meal()][:limit] if self.rows else []

    def update_meal(self, day, message_id, value):
        for index, (row_day, row_id, stored) in enumerate(self.rows):
            if row_id == message_id and (message_id < 0 or row_day == day):
                updated = stored.model_copy(update={"meal": value})
                self.rows[index] = (row_day, row_id, updated)
                total = sum(
                    row.meal.meal_kcal
                    for stored_day, _, row in self.rows
                    if stored_day == row_day
                )
                return MealUpdate(row_day, total, updated)
        return None

    def append_meal(
        self,
        timestamp,
        message_id,
        request,
        normalized,
        photo_path,
        value,
        metadata,
    ):
        del request, photo_path
        stored = StoredMeal(
            normalized_request=normalized, meal=value, metadata=metadata
        )
        self.rows.append((timestamp.astimezone(TZ).date(), message_id, stored))
        return stored

    def get_day_meals(self, day):
        return [
            DayMeal(
                stored.meal.meal_name,
                stored.meal.meal_kcal,
                nutrition_summary(stored.meal),
            )
            for row_day, _, stored in self.rows
            if row_day == day
        ]

    def get_daily_totals(self, start, end):
        del start, end
        return {}

    def delete_meal(self, message_id, day):
        for index, (row_day, row_id, stored) in enumerate(self.rows):
            if row_day == day and row_id == message_id:
                del self.rows[index]
                total = sum(
                    row.meal.meal_kcal
                    for stored_day, _, row in self.rows
                    if stored_day == day
                )
                return MealDeletion(
                    day,
                    total,
                    None,
                    True,
                    stored.meal.meal_name,
                )
        return MealDeletion(day, 0, None, False)


def service(tmp_path, meals, saved_meals):
    analyzer = SimpleNamespace(
        analyze=lambda *args: (_ for _ in ()).throw(
            AssertionError("saved meals must not call the analyzer")
        )
    )
    return CaloriesService(
        analyzer,
        meals,
        TZ,
        time(1),
        tmp_path / "photos",
        saved_store=saved_meals,
    )


def test_service_save_is_idempotent_and_auto_suffixes_names(
    monkeypatch, tmp_path
) -> None:
    meals = MemoryMealStore()
    meals.add_source(1)
    meals.add_source(2)
    meals.add_source(3)
    saved_meals = MemorySavedStore()
    app = service(tmp_path, meals, saved_meals)
    ids = iter(["first", "second"])
    monkeypatch.setattr(
        "calories_bot.bot.secrets.token_urlsafe", lambda size: next(ids)
    )

    first, first_created = app.save_source_meal(1, DAY)
    repeated, repeated_created = app.save_source_meal(1, DAY)
    second, second_created = app.save_source_meal(2, DAY)

    assert (first.display_name, first_created) == ("сир", True)
    assert repeated.saved_meal_id == first.saved_meal_id
    assert repeated_created is False
    assert (second.display_name, second_created) == ("сир (2)", True)
    with pytest.raises(SavedMealNameError, match="вже є"):
        app.save_source_meal(3, DAY, "сир")


def test_deleting_consumed_meal_keeps_saved_meal(monkeypatch, tmp_path) -> None:
    meals = MemoryMealStore()
    meals.add_source(1)
    saved_meals = MemorySavedStore()
    app = service(tmp_path, meals, saved_meals)
    monkeypatch.setattr(
        "calories_bot.bot.secrets.token_urlsafe", lambda size: "template"
    )
    stored, created = app.save_source_meal(1, DAY)

    deletion = app.delete_message(1, DAY)

    assert created is True
    assert deletion.deleted is True
    assert meals.get_meal(DAY, 1) is None
    assert saved_meals.get(stored.saved_meal_id) == stored


def test_reused_saved_meal_scales_without_llm_and_retry_is_idempotent(tmp_path) -> None:
    meals = MemoryMealStore()
    saved_meals = MemorySavedStore()
    saved_meals.append(saved("template", 1, "Улюблений сир"))
    app = service(tmp_path, meals, saved_meals)
    timestamp = datetime(2026, 8, 9, 12, tzinfo=UTC)

    first = app.add_saved_meal("template", 100, -123, timestamp)
    repeated = app.add_saved_meal("template", 100, -123, timestamp)

    assert first is not None and repeated is not None
    assert first.can_save is False
    assert first.telegram_message_id == -123
    assert len(meals.rows) == 1
    assert meals.rows[0][2].meal.meal_name == "Улюблений сир"
    assert meals.rows[0][2].meal.total_weight_g == 100
    assert meals.rows[0][2].meal.meal_kcal == 120
    assert meals.rows[0][2].metadata.model == "saved_meal"


@pytest.mark.parametrize(("confidence", "expected"), [(0.8, "🧀"), (0.79, None)])
def test_saved_meal_uses_only_confident_llm_icon(
    monkeypatch, tmp_path, confidence, expected
) -> None:
    meals = MemoryMealStore()
    meals.add_source(1)
    saved_meals = MemorySavedStore()
    analyzer = SimpleNamespace(
        suggest_meal_icon=lambda value: MealIconSuggestion(
            emoji="🧀", confidence=confidence
        )
    )
    app = CaloriesService(
        analyzer,
        meals,
        TZ,
        time(1),
        tmp_path / "photos",
        saved_store=saved_meals,
    )
    monkeypatch.setattr(
        "calories_bot.bot.secrets.token_urlsafe", lambda size: "template"
    )

    result, created = app.save_source_meal(1, DAY)

    assert created is True
    assert result.icon == expected


def test_icon_failure_does_not_block_saving(monkeypatch, tmp_path) -> None:
    meals = MemoryMealStore()
    meals.add_source(1)
    saved_meals = MemorySavedStore()
    analyzer = SimpleNamespace(
        suggest_meal_icon=lambda value: (_ for _ in ()).throw(TimeoutError())
    )
    app = CaloriesService(
        analyzer,
        meals,
        TZ,
        time(1),
        tmp_path / "photos",
        saved_store=saved_meals,
    )
    monkeypatch.setattr(
        "calories_bot.bot.secrets.token_urlsafe", lambda size: "template"
    )

    result, created = app.save_source_meal(1, DAY)

    assert created is True
    assert result.icon is None


def test_change_weight_updates_existing_single_item_and_total(tmp_path) -> None:
    meals = MemoryMealStore()
    meals.add_source(1)
    saved_meals = MemorySavedStore()
    app = service(tmp_path, meals, saved_meals)

    result = app.change_meal_weight(1, DAY, 100)

    assert result is not None
    assert len(meals.rows) == 1
    assert meals.rows[0][2].meal.total_weight_g == 100
    assert meals.rows[0][2].meal.meal_kcal == 120
    assert result.daily_total_text == app.get_day_summary(
        datetime(2026, 8, 9, 12, tzinfo=TZ)
    )
    assert "<summary>🔥 К <b><u>120 кк</u></b></summary>" in result.daily_total_text


def test_change_weight_rejects_unchanged_weight_without_updating_store(
    tmp_path,
) -> None:
    meals = MemoryMealStore()
    meals.add_source(1)
    app = service(tmp_path, meals, MemorySavedStore())

    with pytest.raises(MealWeightUnchangedError, match="50 grams"):
        app.change_meal_weight(1, DAY, 50)

    assert meals.rows[0][2].meal.total_weight_g == 50


def test_weight_changes_are_rejected_for_composite_meals(tmp_path) -> None:
    meals = MemoryMealStore()
    meals.add_source(1, composite_meal())
    saved_meals = MemorySavedStore()
    app = service(tmp_path, meals, saved_meals)

    with pytest.raises(CompositeMealWeightError):
        app.change_meal_weight(1, DAY, 500)
