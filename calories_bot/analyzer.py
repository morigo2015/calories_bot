from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol, cast

from openai import OpenAI
from openai.types.shared import ReasoningEffort

from .models import FoodAnalysis, LLMMetadata

LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = """You analyze short Ukrainian-language descriptions and optional
photos of food that a user has eaten.
Return only the structured FoodAnalysis object.

Rules:
- Use both the text and the photo when a photo is provided. The photo identifies
  the food, while explicit values in the text remain authoritative.
- A photo without text is valid. Identify the meal and estimate its portion.
- If neither the text nor the photo describes consumed food, set is_food=false,
  meal_name="", items=[].
- If the message describes consumed food, set is_food=true, provide a concise
  non-empty meal_name, and provide at least one item.
- Keep food items in the same order as they appear in the message.
- Explicit source IDs are authoritative. Assign each ID to the nearest food
  item and to the matching field.
- Every explicit source ID must be used exactly once.
- A plain number without an explicit source ID is part of the description,
  for example "2 яйця" or "піца 30 см".
- If weight is absent, estimate a typical consumed portion and set
  weight_estimated=true and weight_source_id=null.
- If kcal per 100 g is absent, estimate it and set kcal_estimated=true and
  kcal_source_id=null.
- For supplied values, set the matching estimated flag to false and return
  its source ID.
- Split composite meals into useful ingredients only when the user names
  multiple components. Do not invent unnecessary ingredients.
- Use concise Ukrainian names. Do not calculate totals; the application does that.
"""


class AnalysisError(RuntimeError):
    """Raised when a model response cannot be safely used."""


class InputFormatError(ValueError):
    """Raised when deterministic input normalization rejects a message."""


@dataclass(frozen=True)
class ExplicitValue:
    source_id: str
    kind: Literal["weight", "kcal"]
    value: int
    start: int
    end: int


@dataclass(frozen=True)
class NormalizedInput:
    text: str
    explicit_values: tuple[ExplicitValue, ...]


@dataclass(frozen=True)
class ModelPricing:
    input_per_1m: Decimal | None
    cached_input_per_1m: Decimal | None
    output_per_1m: Decimal | None

    @property
    def complete(self) -> bool:
        return (
            self.input_per_1m is not None
            and self.cached_input_per_1m is not None
            and self.output_per_1m is not None
        )


@dataclass(frozen=True)
class AnalysisResult:
    analysis: FoodAnalysis
    metadata: LLMMetadata


class Analyzer(Protocol):
    def analyze(
        self, normalized: NormalizedInput, image_bytes: bytes | None = None
    ) -> AnalysisResult: ...


_DECIMAL = re.compile(r"\d+[.,]\d+")
_HASH_MARKER = re.compile(r"(?<![\w#])(?:#(?P<prefix>\d+)|(?P<suffix>\d+)#)(?![\w#])")
_KCAL_UNIT = r"(?:[кk][кk]|[кk][кk][аa]л|kcal)"
_WEIGHT_UNIT = (
    r"(?:граммів|граммов|граммами|граммы|грамми|грамма|грамм|"
    r"грамів|грамами|грамом|грами|грама|граму|грам|гр|г|grams|gram|gr|g)"
)
_TEXT_KCAL = re.compile(
    rf"(?<!\w)(?P<value>\d+)\s*{_KCAL_UNIT}\s*/\s*100\s*{_WEIGHT_UNIT}?(?!\w)",
    re.IGNORECASE,
)
_CANONICAL_KCAL = re.compile(r"(?<!\w)(?P<value>\d+) ккал/100г(?!\w)")
_EXPLICIT_WEIGHT = re.compile(
    rf"(?<!\w)(?P<value>\d+)\s*{_WEIGHT_UNIT}(?!\w)", re.IGNORECASE
)
_CANONICAL_WEIGHT = re.compile(r"(?<!\w)(?P<value>\d+) гр(?!\w)")
_BARE_AT_BOUNDARY = re.compile(
    r"(?<!\w)(?P<value>\d+)(?=[ \t]*(?:[,;]|\r?\n|[.!?]*[ \t]*$))",
    re.MULTILINE,
)


def _positive_integer(value: str, label: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise InputFormatError(f"{label} must be a positive integer")
    return parsed


def _placeholder(index: int) -> str:
    # Private-use characters keep calorie expressions away from the weight pass.
    letters = ""
    number = index
    while True:
        number, remainder = divmod(number, 26)
        letters = chr(ord("A") + remainder) + letters
        if number == 0:
            break
        number -= 1
    return f"\ue000{letters}\ue001"


def normalize_input(text: str) -> NormalizedInput:
    normalized = text.strip()
    if not normalized:
        raise InputFormatError("Message is empty")
    if _DECIMAL.search(normalized):
        raise InputFormatError("Decimal values are not supported; use whole numbers")

    def replace_hash(match: re.Match[str]) -> str:
        value = _positive_integer(
            match.group("prefix") or match.group("suffix"), "Calories"
        )
        return f"{value} ккал/100г"

    normalized = _HASH_MARKER.sub(replace_hash, normalized)
    if "#" in normalized:
        raise InputFormatError(
            "Use # directly before or after calories, for example #120"
        )

    def replace_text_kcal(match: re.Match[str]) -> str:
        value = _positive_integer(match.group("value"), "Calories")
        return f"{value} ккал/100г"

    normalized = _TEXT_KCAL.sub(replace_text_kcal, normalized)

    calorie_expressions: list[str] = []

    def protect_calories(match: re.Match[str]) -> str:
        calorie_expressions.append(match.group(0))
        return _placeholder(len(calorie_expressions) - 1)

    normalized = _CANONICAL_KCAL.sub(protect_calories, normalized)

    def replace_weight(match: re.Match[str]) -> str:
        value = _positive_integer(match.group("value"), "Weight")
        return f"{value} гр"

    normalized = _EXPLICIT_WEIGHT.sub(replace_weight, normalized)
    normalized = _BARE_AT_BOUNDARY.sub(replace_weight, normalized)

    for index, expression in enumerate(calorie_expressions):
        normalized = normalized.replace(_placeholder(index), expression)

    normalized = re.sub(r"[ \t]+", " ", normalized).strip()

    explicit: list[ExplicitValue] = []
    weight_index = 0
    kcal_index = 0
    matches: list[tuple[int, int, Literal["weight", "kcal"], int]] = []
    for match in _CANONICAL_WEIGHT.finditer(normalized):
        matches.append(
            (match.start(), match.end(), "weight", int(match.group("value")))
        )
    for match in _CANONICAL_KCAL.finditer(normalized):
        matches.append((match.start(), match.end(), "kcal", int(match.group("value"))))

    for start, end, kind, value in sorted(matches):
        if kind == "weight":
            weight_index += 1
            source_id = f"W{weight_index}"
        else:
            kcal_index += 1
            source_id = f"K{kcal_index}"
        explicit.append(
            ExplicitValue(
                source_id=source_id,
                kind=kind,
                value=value,
                start=start,
                end=end,
            )
        )
    return NormalizedInput(text=normalized, explicit_values=tuple(explicit))


def enforce_explicit_values(
    analysis: FoodAnalysis, explicit_values: tuple[ExplicitValue, ...]
) -> FoodAnalysis:
    if not analysis.is_food:
        return analysis

    sources = {source.source_id: source for source in explicit_values}
    used: set[str] = set()
    items = [item.model_copy(deep=True) for item in analysis.items]

    for item in items:
        assignments = (
            ("weight", item.weight_source_id),
            ("kcal", item.kcal_source_id),
        )
        for kind, source_id in assignments:
            if source_id is None:
                if kind == "weight":
                    item.weight_estimated = True
                else:
                    item.kcal_estimated = True
                continue
            source = sources.get(source_id)
            if source is None or source.kind != kind:
                raise AnalysisError(f"Invalid explicit source ID: {source_id}")
            if source_id in used:
                raise AnalysisError(
                    f"Explicit source ID was used more than once: {source_id}"
                )
            used.add(source_id)
            if kind == "weight":
                item.weight_g = source.value
                item.weight_estimated = False
            else:
                item.kcal_per_100g = source.value
                item.kcal_estimated = False

    missing = set(sources) - used
    if missing:
        raise AnalysisError(
            "The model did not assign every explicit source ID: "
            + ", ".join(sorted(missing))
        )
    return FoodAnalysis(is_food=True, meal_name=analysis.meal_name, items=items)


def calculate_llm_cost(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    pricing: ModelPricing,
) -> Decimal | None:
    if not pricing.complete:
        return None
    if cached_input_tokens > input_tokens:
        raise AnalysisError("Cached input token count exceeds total input tokens")
    uncached_input = input_tokens - cached_input_tokens
    assert pricing.input_per_1m is not None
    assert pricing.cached_input_per_1m is not None
    assert pricing.output_per_1m is not None
    return (
        Decimal(uncached_input) * pricing.input_per_1m
        + Decimal(cached_input_tokens) * pricing.cached_input_per_1m
        + Decimal(output_tokens) * pricing.output_per_1m
    ) / Decimal(1_000_000)


class OpenAIAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str,
        effort: str,
        pricing: ModelPricing,
    ) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._effort = effort
        self._pricing = pricing

    def analyze(
        self, normalized: NormalizedInput, image_bytes: bytes | None = None
    ) -> AnalysisResult:
        constraints = [
            {
                "id": source.source_id,
                "kind": source.kind,
                "value": source.value,
                "start": source.start,
                "end": source.end,
                "text": normalized.text[source.start : source.end],
            }
            for source in normalized.explicit_values
        ]
        user_content: str | list[dict[str, str]] = normalized.text
        if image_bytes is not None:
            encoded_image = base64.b64encode(image_bytes).decode("ascii")
            user_content = [
                {
                    "type": "input_text",
                    "text": normalized.text or "Розпізнай страву на фото.",
                },
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{encoded_image}",
                    "detail": "auto",
                },
            ]

        try:
            response = self._client.responses.parse(
                model=self._model,
                reasoning={"effort": cast(ReasoningEffort, self._effort)},
                store=False,
                input=cast(
                    Any,
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "developer",
                            "content": (
                                "Authoritative source values with character positions: "
                                f"{constraints}"
                            ),
                        },
                        {"role": "user", "content": user_content},
                    ],
                ),
                text_format=FoodAnalysis,
            )
        except Exception as exc:
            raise AnalysisError("OpenAI request failed") from exc

        parsed = response.output_parsed
        if parsed is None:
            raise AnalysisError("OpenAI returned no structured analysis")
        analysis = enforce_explicit_values(parsed, normalized.explicit_values)

        usage = response.usage
        if usage is None:
            LOGGER.warning("OpenAI returned no token usage")
            metadata = LLMMetadata(model=self._model, effort=self._effort)
        else:
            cached_tokens = usage.input_tokens_details.cached_tokens
            metadata = LLMMetadata(
                model=self._model,
                effort=self._effort,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=cached_tokens,
                llm_cost_usd=calculate_llm_cost(
                    usage.input_tokens,
                    cached_tokens,
                    usage.output_tokens,
                    self._pricing,
                ),
            )
        return AnalysisResult(analysis=analysis, metadata=metadata)
