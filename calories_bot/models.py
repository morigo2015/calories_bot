from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ValueOrigin = Literal[
    "user_text",
    "deterministic_reference",
    "image",
    "model_estimate",
]

MAX_FOOD_ITEMS = 20
MAX_ITEM_NAME_LENGTH = 120
MAX_MEAL_NAME_LENGTH = 160
MAX_PORTION_DISPLAY_LENGTH = 40
MAX_WEIGHT_G = 10_000
MAX_KCAL_PER_100G = 1_000


class FoodItem(BaseModel):
    name: str
    weight_g: float = Field(gt=0)
    weight_estimated: bool
    kcal_per_100g: float = Field(gt=0)
    kcal_estimated: bool
    weight_origin: ValueOrigin | None = None
    kcal_origin: ValueOrigin | None = None
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


class MealResult(BaseModel):
    meal_name: str
    items: list[CalculatedFoodItem]
    total_weight_g: float = Field(gt=0)
    kcal_per_100g: float = Field(gt=0)
    meal_kcal: float = Field(ge=0)
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


def round_whole(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_meal(analysis: FoodAnalysis) -> MealResult:
    if not analysis.is_food or not analysis.items:
        raise ValueError("Cannot calculate a non-food response")

    items: list[CalculatedFoodItem] = []
    total_weight = 0.0
    total_calories = 0.0

    for item in analysis.items:
        calories = item.weight_g * item.kcal_per_100g / 100
        total_weight += item.weight_g
        total_calories += calories
        items.append(CalculatedFoodItem(**item.model_dump(), calories=calories))

    return MealResult(
        meal_name=analysis.meal_name,
        items=items,
        total_weight_g=total_weight,
        kcal_per_100g=total_calories / total_weight * 100,
        meal_kcal=total_calories,
        estimated=any(
            item.weight_estimated or item.kcal_estimated for item in analysis.items
        ),
    )
