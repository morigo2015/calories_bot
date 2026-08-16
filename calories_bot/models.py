from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ValueOrigin = Literal[
    "user_text",
    "deterministic_reference",
    "image",
    "model_estimate",
]
NutrientBasis = Literal["per_100g", "portion"]

MAX_FOOD_ITEMS = 20
MAX_ITEM_NAME_LENGTH = 120
MAX_MEAL_NAME_LENGTH = 160
MAX_SAVED_MEAL_NAME_LENGTH = 80
MAX_PORTION_DISPLAY_LENGTH = 40
MAX_WEIGHT_G = 10_000
MAX_KCAL_PER_100G = 1_000
SIMPLE_MEAL_REQUEST_PREFIX = "simple_meal:v1:"


@dataclass(frozen=True)
class SimpleMealRequest:
    source_message_id: int
    component_index: int
    component_count: int
    kind: str
    payload: str


def format_simple_meal_request(
    source_message_id: int,
    component_index: int,
    component_count: int,
    kind: str,
    payload: str,
) -> str:
    if component_count < 1 or not 0 <= component_index < component_count:
        raise ValueError("Invalid simple-meal component position")
    if not kind or ":" in kind:
        raise ValueError("Invalid simple-meal request kind")
    return (
        f"{SIMPLE_MEAL_REQUEST_PREFIX}{source_message_id}:{component_index}:"
        f"{component_count}:{kind}:{payload}"
    )


def parse_simple_meal_request(value: str) -> SimpleMealRequest | None:
    if not value.startswith(SIMPLE_MEAL_REQUEST_PREFIX):
        return None
    try:
        source_raw, index_raw, count_raw, kind, payload = value.removeprefix(
            SIMPLE_MEAL_REQUEST_PREFIX
        ).split(":", maxsplit=4)
        parsed = SimpleMealRequest(
            source_message_id=int(source_raw),
            component_index=int(index_raw),
            component_count=int(count_raw),
            kind=kind,
            payload=payload,
        )
    except (TypeError, ValueError):
        return None
    if (
        parsed.component_count < 1
        or not 0 <= parsed.component_index < parsed.component_count
        or not parsed.kind
    ):
        return None
    return parsed


class FoodItem(BaseModel):
    name: str
    weight_g: float = Field(gt=0)
    weight_estimated: bool
    kcal_per_100g: float = Field(ge=0)
    kcal_estimated: bool
    weight_origin: ValueOrigin | None = None
    kcal_origin: ValueOrigin | None = None
    protein_per_100g: float | None = Field(default=None, ge=0)
    fat_per_100g: float | None = Field(default=None, ge=0)
    carbs_per_100g: float | None = Field(default=None, ge=0)
    protein_estimated: bool = True
    fat_estimated: bool = True
    carbs_estimated: bool = True
    protein_origin: ValueOrigin | None = None
    fat_origin: ValueOrigin | None = None
    carbs_origin: ValueOrigin | None = None
    protein_source_basis: NutrientBasis | None = None
    fat_source_basis: NutrientBasis | None = None
    carbs_source_basis: NutrientBasis | None = None
    kcal_source_basis: NutrientBasis | None = None
    portion_display: str | None = None
    weight_source_id: str | None = Field(
        default=None,
        exclude=True,
        description="ID of the authoritative weight token, or null if estimated",
    )
    kcal_source_id: str | None = Field(
        default=None,
        exclude=True,
        description="ID of the authoritative kcal/100g token, or null if estimated",
    )
    protein_source_id: str | None = Field(default=None, exclude=True)
    fat_source_id: str | None = Field(default=None, exclude=True)
    carbs_source_id: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def fill_legacy_origins(cls, data: object) -> object:
        """Give historical items safe origins without changing stored rows."""
        if not isinstance(data, dict):
            return data
        values = dict(data)
        if values.get("weight_origin") is None:
            values["weight_origin"] = (
                "model_estimate"
                if values.get("weight_estimated", True)
                else "user_text"
            )
        if values.get("kcal_origin") is None:
            values["kcal_origin"] = (
                "model_estimate" if values.get("kcal_estimated", True) else "user_text"
            )
        for nutrient in ("protein", "fat", "carbs"):
            value_key = f"{nutrient}_per_100g"
            origin_key = f"{nutrient}_origin"
            if values.get(value_key) is not None and values.get(origin_key) is None:
                values[origin_key] = (
                    "model_estimate"
                    if values.get(f"{nutrient}_estimated", True)
                    else "user_text"
                )
        return values

    @model_validator(mode="after")
    def validate_name(self) -> FoodItem:
        self.name = " ".join(self.name.split())
        if not self.name:
            raise ValueError("Food item name cannot be empty")
        if len(self.name) > MAX_ITEM_NAME_LENGTH:
            raise ValueError("Food item name is too long")
        if self.weight_g > MAX_WEIGHT_G:
            raise ValueError("Food item weight is implausibly large")
        if self.kcal_per_100g > MAX_KCAL_PER_100G:
            raise ValueError("Food item kcal/100g is implausibly large")
        if self.portion_display is not None:
            self.portion_display = " ".join(self.portion_display.split())
            if not self.portion_display:
                self.portion_display = None
            elif len(self.portion_display) > MAX_PORTION_DISPLAY_LENGTH:
                raise ValueError("Food item portion display is too long")
        return self


class FoodAnalysis(BaseModel):
    is_food: bool
    meal_name: str = Field(
        description=(
            "Concise non-empty Ukrainian meal name when is_food is true; "
            "empty string when is_food is false"
        )
    )
    items: list[FoodItem]

    @model_validator(mode="after")
    def validate_shape(self) -> FoodAnalysis:
        self.meal_name = " ".join(self.meal_name.split())
        if self.is_food:
            if not self.items:
                raise ValueError("A food response must contain items")
            if len(self.items) > MAX_FOOD_ITEMS:
                raise ValueError("A food response contains too many items")
            normalized_names = [item.name.casefold() for item in self.items]
            if len(normalized_names) != len(set(normalized_names)):
                raise ValueError("A food response contains duplicate items")
            if not self.meal_name:
                # A missing display label must not discard an otherwise valid,
                # source-checked food analysis. Item names are already validated.
                self.meal_name = ", ".join(
                    dict.fromkeys(item.name for item in self.items)
                )
            if len(self.meal_name) > MAX_MEAL_NAME_LENGTH:
                raise ValueError("Meal name is too long")
        else:
            if self.items:
                raise ValueError("A non-food response must not contain items")
            self.meal_name = ""
        return self


class CalculatedFoodItem(FoodItem):
    calories: float = Field(ge=0)
    protein_g: float | None = Field(default=None, ge=0)
    fat_g: float | None = Field(default=None, ge=0)
    carbs_g: float | None = Field(default=None, ge=0)


class MealResult(BaseModel):
    meal_name: str
    items: list[CalculatedFoodItem]
    total_weight_g: float = Field(gt=0)
    kcal_per_100g: float = Field(ge=0)
    meal_kcal: float = Field(ge=0)
    protein_per_100g: float | None = Field(default=None, ge=0)
    fat_per_100g: float | None = Field(default=None, ge=0)
    carbs_per_100g: float | None = Field(default=None, ge=0)
    protein_g: float | None = Field(default=None, ge=0)
    fat_g: float | None = Field(default=None, ge=0)
    carbs_g: float | None = Field(default=None, ge=0)
    estimated: bool


class LLMMetadata(BaseModel):
    model: str
    effort: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0, exclude=True)
    llm_cost_usd: Decimal | None = Field(default=None, ge=0)


class StoredMeal(BaseModel):
    normalized_request: str
    meal: MealResult
    metadata: LLMMetadata
    photo_path: str | None = None


class SavedMeal(BaseModel):
    saved_meal_id: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")
    source_message_id: int
    display_name: str
    default_total_weight_g: int = Field(ge=1, le=MAX_WEIGHT_G)
    base_meal: MealResult
    icon: str | None = None

    @model_validator(mode="after")
    def validate_display_name(self) -> SavedMeal:
        if len(self.base_meal.items) != 1:
            raise ValueError("A saved meal must contain exactly one item")
        self.display_name = " ".join(self.display_name.split())
        if not self.display_name:
            raise ValueError("Saved meal name cannot be empty")
        if len(self.display_name) > MAX_SAVED_MEAL_NAME_LENGTH:
            raise ValueError("Saved meal name is too long")
        if self.icon is not None:
            self.icon = self.icon.strip()
            if (
                not self.icon
                or len(self.icon) > 8
                or any(char.isalnum() or char.isspace() for char in self.icon)
            ):
                raise ValueError("Saved meal icon must be one emoji")
        return self


class MealIconSuggestion(BaseModel):
    emoji: str = Field(min_length=1, max_length=8)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_emoji(self) -> MealIconSuggestion:
        self.emoji = self.emoji.strip()
        if not self.emoji or any(
            char.isalnum() or char.isspace() for char in self.emoji
        ):
            raise ValueError("Icon suggestion must contain one emoji")
        return self


class RecentMeal(BaseModel):
    telegram_message_id: int
    day: date
    meal: MealResult
    normalized_request: str


@dataclass(frozen=True)
class NutritionSummary:
    kcal: float = 0.0
    protein_g: float | None = 0.0
    fat_g: float | None = 0.0
    carbs_g: float | None = 0.0
    kcal_estimated: bool = False
    protein_estimated: bool = False
    fat_estimated: bool = False
    carbs_estimated: bool = False

    @classmethod
    def unknown_macros(
        cls, kcal: float, *, kcal_estimated: bool = False
    ) -> NutritionSummary:
        return cls(
            kcal=kcal,
            protein_g=None,
            fat_g=None,
            carbs_g=None,
            kcal_estimated=kcal_estimated,
        )

    def __add__(self, other: NutritionSummary) -> NutritionSummary:
        def add_optional(left: float | None, right: float | None) -> float | None:
            return None if left is None or right is None else left + right

        return NutritionSummary(
            kcal=self.kcal + other.kcal,
            protein_g=add_optional(self.protein_g, other.protein_g),
            fat_g=add_optional(self.fat_g, other.fat_g),
            carbs_g=add_optional(self.carbs_g, other.carbs_g),
            kcal_estimated=self.kcal_estimated or other.kcal_estimated,
            protein_estimated=self.protein_estimated or other.protein_estimated,
            fat_estimated=self.fat_estimated or other.fat_estimated,
            carbs_estimated=self.carbs_estimated or other.carbs_estimated,
        )


def item_nutrient_total_estimated(item: CalculatedFoodItem, nutrient: str) -> bool:
    if getattr(item, f"{nutrient}_g") is None:
        return False
    if (
        getattr(item, f"{nutrient}_origin") == "user_text"
        and getattr(item, f"{nutrient}_source_basis") == "portion"
    ):
        return False
    return item.weight_origin != "user_text" or getattr(item, f"{nutrient}_estimated")


def item_calorie_total_estimated(item: CalculatedFoodItem) -> bool:
    if item.kcal_origin == "user_text" and item.kcal_source_basis == "portion":
        return False
    return item.weight_origin != "user_text" or item.kcal_estimated


def nutrition_summary(meal: MealResult) -> NutritionSummary:
    return NutritionSummary(
        kcal=meal.meal_kcal,
        protein_g=meal.protein_g,
        fat_g=meal.fat_g,
        carbs_g=meal.carbs_g,
        kcal_estimated=any(item_calorie_total_estimated(item) for item in meal.items),
        protein_estimated=any(
            item_nutrient_total_estimated(item, "protein") for item in meal.items
        ),
        fat_estimated=any(
            item_nutrient_total_estimated(item, "fat") for item in meal.items
        ),
        carbs_estimated=any(
            item_nutrient_total_estimated(item, "carbs") for item in meal.items
        ),
    )


def round_whole(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_meal(analysis: FoodAnalysis) -> MealResult:
    if not analysis.is_food or not analysis.items:
        raise ValueError("Cannot calculate a non-food response")

    items: list[CalculatedFoodItem] = []
    total_weight = 0.0
    total_calories = 0.0
    macro_totals: dict[str, float | None] = {
        "protein": 0.0,
        "fat": 0.0,
        "carbs": 0.0,
    }

    for item in analysis.items:
        calories = item.weight_g * item.kcal_per_100g / 100
        total_weight += item.weight_g
        total_calories += calories
        item_macros: dict[str, float | None] = {}
        for nutrient in macro_totals:
            density = getattr(item, f"{nutrient}_per_100g")
            total = None if density is None else item.weight_g * density / 100
            item_macros[f"{nutrient}_g"] = total
            if total is None or macro_totals[nutrient] is None:
                macro_totals[nutrient] = None
            else:
                macro_totals[nutrient] += total
        items.append(
            CalculatedFoodItem.model_validate(
                {**item.model_dump(), "calories": calories, **item_macros}
            )
        )

    def per_100g(nutrient: str) -> float | None:
        total = macro_totals[nutrient]
        return None if total is None else total / total_weight * 100

    return MealResult(
        meal_name=analysis.meal_name,
        items=items,
        total_weight_g=total_weight,
        kcal_per_100g=total_calories / total_weight * 100,
        meal_kcal=total_calories,
        protein_per_100g=per_100g("protein"),
        fat_per_100g=per_100g("fat"),
        carbs_per_100g=per_100g("carbs"),
        protein_g=macro_totals["protein"],
        fat_g=macro_totals["fat"],
        carbs_g=macro_totals["carbs"],
        estimated=any(
            item.weight_estimated
            or item.kcal_estimated
            or item.protein_estimated
            or item.fat_estimated
            or item.carbs_estimated
            for item in analysis.items
        ),
    )


def scale_meal(
    meal: MealResult,
    target_weight_g: int,
    *,
    meal_name: str | None = None,
) -> MealResult:
    """Scale a calculated meal while preserving its nutritional assumptions."""
    if not 1 <= target_weight_g <= MAX_WEIGHT_G:
        raise ValueError("Meal weight must be between 1 and 10000 grams")
    ratio = target_weight_g / meal.total_weight_g
    changed_weight = target_weight_g != round_whole(meal.total_weight_g)
    items: list[CalculatedFoodItem] = []
    for item in meal.items:
        weight_g = item.weight_g * ratio
        macro_values: dict[str, float | None] = {}
        item_data = item.model_dump(
            exclude={
                "weight_g",
                "calories",
                "protein_g",
                "fat_g",
                "carbs_g",
                "portion_display",
            }
        )
        if changed_weight and len(meal.items) == 1:
            item_data["weight_estimated"] = False
            item_data["weight_origin"] = "user_text"
        for nutrient in ("protein", "fat", "carbs"):
            density = getattr(item, f"{nutrient}_per_100g")
            macro_values[f"{nutrient}_g"] = (
                None if density is None else weight_g * density / 100
            )
            basis_key = f"{nutrient}_source_basis"
            if changed_weight and item_data.get(basis_key) == "portion":
                item_data[basis_key] = "per_100g"
        if changed_weight and item_data.get("kcal_source_basis") == "portion":
            item_data["kcal_source_basis"] = "per_100g"
        items.append(
            CalculatedFoodItem.model_validate(
                {
                    **item_data,
                    "weight_g": weight_g,
                    "calories": weight_g * item.kcal_per_100g / 100,
                    **macro_values,
                    "portion_display": (
                        None if changed_weight else item.portion_display
                    ),
                }
            )
        )
    meal_kcal = sum(item.calories for item in items)

    def scaled_total(nutrient: str) -> float | None:
        values = [getattr(item, f"{nutrient}_g") for item in items]
        return (
            None
            if any(value is None for value in values)
            else sum(value for value in values if value is not None)
        )

    protein_g = scaled_total("protein")
    fat_g = scaled_total("fat")
    carbs_g = scaled_total("carbs")
    return MealResult(
        meal_name=meal_name or meal.meal_name,
        items=items,
        total_weight_g=float(target_weight_g),
        kcal_per_100g=meal_kcal / target_weight_g * 100,
        meal_kcal=meal_kcal,
        protein_per_100g=(
            None if protein_g is None else protein_g / target_weight_g * 100
        ),
        fat_per_100g=None if fat_g is None else fat_g / target_weight_g * 100,
        carbs_per_100g=(None if carbs_g is None else carbs_g / target_weight_g * 100),
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        estimated=meal.estimated,
    )
