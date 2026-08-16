from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

from openai import OpenAI
from openai.types.shared import ReasoningEffort
from pydantic import BaseModel, Field, model_validator

from .analyzer import ModelPricing, UsageRecorder, metadata_from_usage
from .models import LLMMetadata

MAX_WEEKLY_MEAL_GROUPS = 20

MEAL_GROUPING_SYSTEM_PROMPT = """Group meal names for a compact Ukrainian
weekly food summary. Return only the structured MealGrouping object.

Rules:
- Return exactly one assignment for every source_id and never invent an ID.
- Use a short natural Ukrainian group_name naming the base food or dish.
- Merge variants that an ordinary person would consider the same base category:
  all wine regardless of color, dryness, grape or brand -> "Вино"; all coffee
  drinks -> "Кава"; cheeses such as mozzarella, feta, Coburger and gouda ->
  "Сир"; spaghetti, carbonara and amatriciana -> "Паста"; spelling variants,
  singular/plural and word-order variants belong together.
- Ignore brand, variety, color and preparation descriptors for base products.
- Do not merge genuinely different foods into vague groups such as "Їжа".
  Preserve distinctive dishes when no useful common base category exists.
- Assign a composite name exactly once to its dominant food group. Never split
  it and never estimate proportions.
- Produce at most 20 distinct groups. If necessary, use meaningful broader food
  families for low-impact one-off items.
"""


class MealGroupingError(RuntimeError):
    """Raised when semantic meal grouping cannot be safely used."""


class MealGroupAssignment(BaseModel):
    source_id: int = Field(ge=0)
    group_name: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def normalize_name(self) -> MealGroupAssignment:
        self.group_name = " ".join(self.group_name.split())
        if not self.group_name:
            raise ValueError("Group name cannot be empty")
        return self


class MealGrouping(BaseModel):
    assignments: list[MealGroupAssignment]


@dataclass(frozen=True)
class MealGroupingResult:
    group_names: tuple[str, ...]
    metadata: LLMMetadata


class MealGrouper(Protocol):
    def group(self, meal_names: tuple[str, ...]) -> MealGroupingResult: ...


class OpenAIMealGrouper:
    def __init__(
        self,
        api_key: str,
        model: str,
        effort: str,
        timeout_seconds: float,
        pricing: ModelPricing,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds)
        self._model = model
        self._effort = effort
        self._pricing = pricing
        self._usage_recorder = usage_recorder

    def group(self, meal_names: tuple[str, ...]) -> MealGroupingResult:
        if not meal_names:
            return MealGroupingResult(
                (), LLMMetadata(model=self._model, effort=self._effort)
            )
        source = [
            {"source_id": source_id, "name": name}
            for source_id, name in enumerate(meal_names)
        ]
        try:
            response = self._client.responses.parse(
                model=self._model,
                reasoning={"effort": cast(ReasoningEffort, self._effort)},
                store=False,
                input=cast(
                    Any,
                    [
                        {"role": "system", "content": MEAL_GROUPING_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(source, ensure_ascii=False),
                        },
                    ],
                ),
                text_format=MealGrouping,
            )
        except Exception as exc:
            raise MealGroupingError("OpenAI meal-grouping request failed") from exc

        metadata = metadata_from_usage(
            getattr(response, "usage", None),
            self._model,
            self._effort,
            self._pricing,
            self._usage_recorder,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise MealGroupingError("OpenAI returned no structured meal grouping")
        by_id: dict[int, str] = {}
        for assignment in parsed.assignments:
            if assignment.source_id in by_id:
                raise MealGroupingError("OpenAI returned a duplicate meal source ID")
            by_id[assignment.source_id] = assignment.group_name
        expected_ids = set(range(len(meal_names)))
        if set(by_id) != expected_ids:
            raise MealGroupingError("OpenAI meal source IDs do not match the request")
        group_names = tuple(by_id[source_id] for source_id in range(len(meal_names)))
        return MealGroupingResult(group_names, metadata)
