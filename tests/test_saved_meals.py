from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from calories_bot.bot import CaloriesService, SavedMealNameError
from calories_bot.models import (
    FoodAnalysis,
    FoodItem,
    LLMMetadata,
    RecentMeal,
    SavedMeal,
    StoredMeal,
    calculate_meal,
    scale_meal,
)
from calories_bot.saved_meals import (
    SAVED_MEALS_HEADERS,
    GoogleSavedMealStore,
    SavedMealsSchemaError,
)
from calories_bot.sheets import MealDeletion, SheetState

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


def saved(saved_id: str, source_id: int, name: str = "сир") -> SavedMeal:
    return SavedMeal(
        saved_meal_id=saved_id,
        source_message_id=source_id,
        display_name=name,
        default_total_weight_g=50,
        base_meal=meal(name),
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

    def batch_update(self, updates, **kwargs):
        del kwargs
        for update in updates:
            column = 2 if update["range"].startswith("C") else 3
            row = int(update["range"][1:]) - 1
            self.rows[row][column] = update["values"][0][0]

    def delete_rows(self, row):
        self.rows.pop(row - 1)


def sheet_store(rows) -> GoogleSavedMealStore:
    store = GoogleSavedMealStore.__new__(GoogleSavedMealStore)
    store._worksheet = Worksheet(rows)
    return store


def test_saved_meal_store_crud_and_newest_first() -> None:
    store = sheet_store([SAVED_MEALS_HEADERS])

    store.append(saved("one", 1, "Перша"))
    store.append(saved("two", 2, "Друга"))

    assert [item.saved_meal_id for item in store.list_meals()] == ["two", "one"]
    assert store.find_by_source(1).display_name == "Перша"
    assert store.rename("one", "Нова назва").display_name == "Нова назва"
    assert store.set_default_weight("one", 175).default_total_weight_g == 175
    assert store.delete("two") is True
    assert store.delete("two") is False
    assert [item.saved_meal_id for item in store.list_meals()] == ["one"]


def test_saved_meal_store_rejects_unknown_schema() -> None:
    store = sheet_store([["name", "weight"]])

    with pytest.raises(SavedMealsSchemaError):
        store._ensure_headers()


def test_saved_meal_store_initializes_empty_worksheet() -> None:
    store = sheet_store([])

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

    def rename(self, saved_id, name):
        current = self.get(saved_id)
        if current is None:
            return None
        updated = current.model_copy(update={"display_name": name})
        self.items[self.items.index(current)] = updated
        return updated

    def set_default_weight(self, saved_id, weight):
        current = self.get(saved_id)
        if current is None:
            return None
        updated = current.model_copy(update={"default_total_weight_g": weight})
        self.items[self.items.index(current)] = updated
        return updated

    def delete(self, saved_id):
        current = self.get(saved_id)
        if current is None:
            return False
        self.items.remove(current)
        return True


class MemoryMealStore:
    def __init__(self):
        self.rows: list[tuple[date, int, StoredMeal]] = []

    def add_source(self, source_id, value=None, normalized="food"):
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
        del day
        return []

    def get_daily_totals(self, start, end):
        del start, end
        return {}

    def delete_meal(self, message_id, day):
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
