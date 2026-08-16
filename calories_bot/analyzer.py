from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, cast

from openai import OpenAI
from openai.types.shared import ReasoningEffort

from .models import FoodAnalysis, LLMMetadata, MealIconSuggestion, MealResult

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
- A message containing only authoritative nutrition sources is valid food:
  use the Ukrainian name "Продукт" and a 100 g portion when no name or weight
  is supplied.
- Keep food items in the same order as they appear in the message.
- Explicit source IDs are authoritative. Assign each ID to the nearest food
  item and to the matching field.
- Every explicit source ID must be used exactly once.
- A plain number without an explicit source ID is part of the description,
  for example "2 яйця" or "піца 30 см".
- If weight is absent, estimate a typical consumed portion and set
  weight_estimated=true, weight_origin=model_estimate and weight_source_id=null.
- If kcal per 100 g is absent, estimate it and set kcal_estimated=true and
  kcal_origin=model_estimate and kcal_source_id=null.
- Always provide protein, fat, and carbohydrate grams per 100 g when food is
  present. Estimate every missing nutrient separately. Set its estimated flag,
  origin, and source ID by the same rules as kcal.
- Compact К/Б/Ж/В source values and natural values "на 100 г" are per 100 g.
  Values explicitly described as being for a portion or package apply to that
  consumed portion; the application converts them to per-100-g density.
- For supplied values, set the matching estimated flag to false and return
  its source ID. The application will set the origin to user_text.
- On a nutrition-label photo, try to read kcal, protein, fat, and carbohydrates,
  preferring the per-100-g column. Values read or estimated from a photo use
  origin=image and are approximate.
- For a natural portion such as "2 яйця", "1 тарілка", "жменя" or "половина",
  return a concise Ukrainian portion_display such as "2 шт." or "1 тарілка".
- Split composite meals into useful ingredients when the user names multiple
  standalone foods. Each explicitly named standalone food must be a separate
  item even when punctuation is missing, grammar is broken, or a word is
  repeated. Do not merge a sequence such as "рис курка салат" into one item.
  Keep an established mixed-dish name such as "салат Цезар" as one item unless
  the user separately lists its ingredients. Do not invent ingredients.
- Use concise Ukrainian names. Do not calculate totals; the application does that.

Examples:
- "сир 150 г" -> one item; assign the weight source and estimate kcal/100g.
- "сир 150 г, 120 ккал/100 г" -> assign both authoritative sources.
- "два яйця і бутерброд" -> two named items with estimated missing nutrition.
- "зїв рис курку і салат салата небагато" -> three items: rice, chicken,
  and salad; the repeated word does not create a fourth item.
- "тарілка борщу" -> one item with portion_display="1 тарілка".
- A meal photo with caption "250 г" -> use the caption weight and recognize food.
- A meal photo without caption -> recognize and estimate the portion.
- A label photo -> read the product name and all available nutrition values;
  estimate missing nutrition and portion if absent.
- "як справи?" -> is_food=false.
"""

ICON_SYSTEM_PROMPT = """Choose exactly one emoji that visually represents the
given meal. Return the emoji and a confidence from 0 to 1 that an ordinary user
would immediately associate it with the meal. Use a food emoji, not a decorative
symbol. If no specific emoji fits, choose the closest food emoji and give low
confidence."""
TRANSCRIPTION_MODEL = "gpt-transcribe"


class AnalysisError(RuntimeError):
    """Raised when a model response cannot be safely used."""


class InputFormatError(ValueError):
    """Raised when deterministic input normalization rejects a message."""


class TranscriptionError(RuntimeError):
    """Raised when a voice message cannot be transcribed into usable text."""


class UsageRecorder(Protocol):
    def record_llm_usage(
        self,
        recorded_at: datetime,
        model: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: Decimal | None,
    ) -> None: ...


@dataclass(frozen=True)
class ExplicitValue:
    source_id: str
    kind: Literal["weight", "kcal", "protein", "fat", "carbs"]
    value: int
    start: int
    end: int
    basis: Literal["per_100g", "portion"] | None = None


@dataclass(frozen=True)
class HouseholdPortion:
    count: int
    display: str
    item_alias: str
    reference_weight_g: int | None


@dataclass(frozen=True)
class NormalizedInput:
    text: str
    explicit_values: tuple[ExplicitValue, ...]
    original_text: str = ""
    household_portions: tuple[HouseholdPortion, ...] = ()


@dataclass(frozen=True)
class ModelPricing:
    input_per_1m: Decimal | None
    cached_input_per_1m: Decimal | None
    output_per_1m: Decimal | None

    @property
    def complete(self) -> bool:
        """Whether ordinary input and output token prices are configured."""
        return self.input_per_1m is not None and self.output_per_1m is not None


@dataclass(frozen=True)
class AnalysisResult:
    analysis: FoodAnalysis
    metadata: LLMMetadata


class Analyzer(Protocol):
    def analyze(
        self, normalized: NormalizedInput, image_bytes: bytes | None = None
    ) -> AnalysisResult: ...

    def suggest_meal_icon(self, meal: MealResult) -> MealIconSuggestion: ...


class Transcriber(Protocol):
    def transcribe(self, audio_bytes: bytes) -> str: ...


class OpenAITranscriber:
    def __init__(self, api_key: str, timeout_seconds: float) -> None:
        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds)

    def transcribe(self, audio_bytes: bytes) -> str:
        if not audio_bytes:
            raise TranscriptionError("Voice message is empty")
        try:
            transcription = self._client.audio.transcriptions.create(
                model=TRANSCRIPTION_MODEL,
                file=("voice.ogg", audio_bytes, "audio/ogg"),
            )
        except Exception as exc:
            raise TranscriptionError("OpenAI could not transcribe voice") from exc
        text = str(getattr(transcription, "text", "")).strip()
        if not text:
            raise TranscriptionError("OpenAI returned an empty transcript")
        return text


_DECIMAL = re.compile(r"\d+[.,]\d+")
_HASH_MARKER = re.compile(r"(?<![\w#])(?:#(?P<prefix>\d+)|(?P<suffix>\d+)#)(?![\w#])")
_COMPACT_NUTRIENT = re.compile(
    r"(?<!\w)(?P<marker>[кkбжвb])(?P<separator>[:_ -]?)(?P<value>\d+)(?!\w)",
    re.IGNORECASE,
)
_KCAL_UNIT = (
    r"(?:[кk][кk]|[кk][кk][аa]л|kcal|"
    r"кілокалорій|кілокалорії|кілокалорія|"
    r"килокалорий|килокалории|килокалория|"
    r"калорій|калорії|калорія|"
    r"калорий|калории|калория)"
)
_WEIGHT_UNIT = (
    r"(?:граммів|граммов|граммами|граммах|граммы|грамми|грамма|грамме|грамм|"
    r"грамів|грамами|грамом|грами|грама|граму|грам|гр|г|grams|gram|gr|g)"
)
_TEXT_KCAL = re.compile(
    rf"(?<!\w)(?P<value>\d+)\s*{_KCAL_UNIT}\s*"
    rf"(?:/\s*(?:100|сто)|(?:на|за)\s*(?:100|сто))\s*"
    rf"{_WEIGHT_UNIT}?(?!\w)",
    re.IGNORECASE,
)
_CANONICAL_KCAL = re.compile(r"(?<!\w)(?P<value>\d+) ккал/100г(?!\w)")
_CANONICAL_PORTION_KCAL = re.compile(r"(?<!\w)(?P<value>\d+) ккал/порцію(?!\w)")
_PLAIN_KCAL = re.compile(
    rf"(?<!\w)(?P<value>\d+)\s*{_KCAL_UNIT}(?!\w)"
    rf"(?!\s*(?:/|на|за)\s*(?:100|сто))",
    re.IGNORECASE,
)
_MACRO_LABELS = {
    "protein": "білків",
    "fat": "жирів",
    "carbs": "вуглеводів",
}
_CANONICAL_MACROS = {
    kind: re.compile(rf"(?<!\w)(?P<value>\d+) г {label}/(?P<basis>100г|порцію)(?!\w)")
    for kind, label in _MACRO_LABELS.items()
}
_NATURAL_MACROS = {
    "protein": re.compile(
        rf"(?<!\w)(?:білк\w*|белк\w*|protein)\s*[:=_-]?\s*"
        rf"(?P<value>\d+)(?:\s*{_WEIGHT_UNIT})?",
        re.IGNORECASE,
    ),
    "fat": re.compile(
        rf"(?<!\w)(?:жир\w*|fat)\s*[:=_-]?\s*"
        rf"(?P<value>\d+)(?:\s*{_WEIGHT_UNIT})?",
        re.IGNORECASE,
    ),
    "carbs": re.compile(
        rf"(?<!\w)(?:вуглевод\w*|углевод\w*|carb\w*)\s*[:=_-]?\s*"
        rf"(?P<value>\d+)(?:\s*{_WEIGHT_UNIT})?",
        re.IGNORECASE,
    ),
}
_PER_100_PREFIX = re.compile(
    rf"(?<!\w)(?:на|за)\s*(?:100|сто)\s*{_WEIGHT_UNIT}(?!\w)",
    re.IGNORECASE,
)
_PORTION_CONTEXT = re.compile(
    r"(?:у|в|на)\s+(?:цій\s+|одній\s+)?(?:порці\w*|упаковц\w*)",
    re.IGNORECASE,
)


def _nearby_basis_context(text: str, start: int) -> str:
    before = text[:start]
    return re.split(r"[,;\n]", before)[-1]


_EXPLICIT_WEIGHT = re.compile(
    rf"(?<!\w)(?P<value>\d+)\s*{_WEIGHT_UNIT}(?!\w)", re.IGNORECASE
)
_CANONICAL_WEIGHT = re.compile(r"(?<!\w)(?P<value>\d+) гр(?!\w)")
_BARE_AT_BOUNDARY = re.compile(
    r"(?<!\w)(?P<value>\d+)(?=[ \t]*(?:[,;]|\r?\n|[.!?]*[ \t]*$))",
    re.MULTILINE,
)
_EGG_PORTION = re.compile(
    r"(?<!\w)(?P<count>\d+)\s*"
    r"(?P<unit>яйц(?:е|я|ь)|яєць|яйця|яйцо|яйца|яиц)(?!\w)",
    re.IGNORECASE,
)
_EGG_ITEM_ALIASES = ("яйц", "яєч", "омлет")
_EGG_REFERENCE_WEIGHT_G = 50

_NUMBER_WORD_VALUES = {
    "нуль": 0,
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "одне": 1,
    "одно": 1,
    "одну": 1,
    "два": 2,
    "дві": 2,
    "две": 2,
    "три": 3,
    "чотири": 4,
    "четыре": 4,
    "п'ять": 5,
    "пять": 5,
    "шість": 6,
    "шесть": 6,
    "сім": 7,
    "семь": 7,
    "вісім": 8,
    "восемь": 8,
    "дев'ять": 9,
    "девять": 9,
    "десять": 10,
    "одинадцять": 11,
    "одиннадцать": 11,
    "дванадцять": 12,
    "двенадцать": 12,
    "тринадцять": 13,
    "тринадцать": 13,
    "чотирнадцять": 14,
    "четырнадцать": 14,
    "п'ятнадцять": 15,
    "пятнадцать": 15,
    "шістнадцять": 16,
    "шестнадцать": 16,
    "сімнадцять": 17,
    "семнадцать": 17,
    "вісімнадцять": 18,
    "восемнадцать": 18,
    "дев'ятнадцять": 19,
    "девятнадцать": 19,
    "двадцять": 20,
    "двадцать": 20,
    "тридцять": 30,
    "тридцать": 30,
    "сорок": 40,
    "п'ятдесят": 50,
    "пятьдесят": 50,
    "шістдесят": 60,
    "шестьдесят": 60,
    "сімдесят": 70,
    "семьдесят": 70,
    "вісімдесят": 80,
    "восемьдесят": 80,
    "дев'яносто": 90,
    "девяносто": 90,
    "сто": 100,
    "двісті": 200,
    "двести": 200,
    "триста": 300,
    "чотириста": 400,
    "четыреста": 400,
    "п'ятсот": 500,
    "пятьсот": 500,
    "шістсот": 600,
    "шестьсот": 600,
    "сімсот": 700,
    "семьсот": 700,
    "вісімсот": 800,
    "восемьсот": 800,
    "дев'ятсот": 900,
    "девятьсот": 900,
}
_THOUSAND_WORDS = {
    "тисяча",
    "тисячі",
    "тисяч",
    "тысяча",
    "тысячи",
    "тысяч",
}
_ALL_NUMBER_WORDS = set(_NUMBER_WORD_VALUES) | _THOUSAND_WORDS


def _number_word_regex(word: str) -> str:
    return re.escape(word).replace("'", "['’ʼ]")


_NUMBER_WORD_ALTERNATION = "|".join(
    _number_word_regex(word)
    for word in sorted(_ALL_NUMBER_WORDS, key=len, reverse=True)
)
_SPOKEN_NUMBER = re.compile(
    rf"(?<!\w)(?:{_NUMBER_WORD_ALTERNATION})"
    rf"(?:[ \t]+(?:{_NUMBER_WORD_ALTERNATION}))*(?!\w)",
    re.IGNORECASE,
)


def _number_word_key(word: str) -> str:
    return word.casefold().replace("’", "'").replace("ʼ", "'")


def _replace_spoken_number(match: re.Match[str]) -> str:
    total = 0
    current = 0
    for raw_word in re.split(r"[ \t]+", match.group(0)):
        word = _number_word_key(raw_word)
        if word in _THOUSAND_WORDS:
            total += (current or 1) * 1000
            current = 0
        else:
            current += _NUMBER_WORD_VALUES[word]
    return str(total + current)


def _nonnegative_integer(value: str, label: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise InputFormatError(f"{label} must be a non-negative integer")
    return parsed


def _positive_integer(value: str, label: str) -> int:
    parsed = _nonnegative_integer(value, label)
    if parsed == 0:
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
    original_text = text
    normalized = text.strip()
    if not normalized:
        raise InputFormatError("Message is empty")
    normalized = _SPOKEN_NUMBER.sub(_replace_spoken_number, normalized)
    if _DECIMAL.search(normalized):
        raise InputFormatError("Decimal values are not supported; use whole numbers")

    def replace_hash(match: re.Match[str]) -> str:
        value = _nonnegative_integer(
            match.group("prefix") or match.group("suffix"), "Calories"
        )
        return f"{value} ккал/100г"

    normalized = _HASH_MARKER.sub(replace_hash, normalized)
    if "#" in normalized:
        raise InputFormatError(
            "Use # directly before or after calories, for example #120"
        )

    compact_labels = {
        "к": ("kcal", "ккал"),
        "k": ("kcal", "ккал"),
        "б": ("protein", "г білків"),
        "ж": ("fat", "г жирів"),
        "в": ("carbs", "г вуглеводів"),
        "b": ("carbs", "г вуглеводів"),
    }

    def replace_compact(match: re.Match[str]) -> str:
        marker = match.group("marker").casefold()
        _kind, label = compact_labels[marker]
        value = _nonnegative_integer(match.group("value"), label)
        return f"{value} {label}/100г"

    normalized = _COMPACT_NUTRIENT.sub(replace_compact, normalized)

    def replace_text_kcal(match: re.Match[str]) -> str:
        value = _nonnegative_integer(match.group("value"), "Calories")
        return f"{value} ккал/100г"

    normalized = _TEXT_KCAL.sub(replace_text_kcal, normalized)

    def replace_portion_kcal(match: re.Match[str]) -> str:
        context = _nearby_basis_context(normalized, match.start())
        if not _PORTION_CONTEXT.search(context):
            return match.group(0)
        value = _nonnegative_integer(match.group("value"), "Calories")
        return f"{value} ккал/порцію"

    normalized = _PLAIN_KCAL.sub(replace_portion_kcal, normalized)

    per_100_markers: list[str] = []

    def protect_per_100(match: re.Match[str]) -> str:
        per_100_markers.append("на 100 г")
        return _placeholder(10_000 + len(per_100_markers) - 1)

    normalized = _PER_100_PREFIX.sub(protect_per_100, normalized)

    for kind, pattern in _NATURAL_MACROS.items():
        label = _MACRO_LABELS[kind]

        def replace_natural_macro(
            match: re.Match[str],
            *,
            nutrient_label: str = label,
            source_text: str = normalized,
        ) -> str:
            value = _nonnegative_integer(match.group("value"), nutrient_label)
            context = _nearby_basis_context(source_text, match.start())
            basis = "порцію" if _PORTION_CONTEXT.search(context) else "100г"
            return f"{value} г {nutrient_label}/{basis}"

        normalized = pattern.sub(replace_natural_macro, normalized)

    nutrition_expressions: list[str] = []

    def protect_calories(match: re.Match[str]) -> str:
        nutrition_expressions.append(match.group(0))
        return _placeholder(len(nutrition_expressions) - 1)

    normalized = _CANONICAL_KCAL.sub(protect_calories, normalized)
    normalized = _CANONICAL_PORTION_KCAL.sub(protect_calories, normalized)
    for pattern in _CANONICAL_MACROS.values():
        normalized = pattern.sub(protect_calories, normalized)

    def replace_weight(match: re.Match[str]) -> str:
        value = _positive_integer(match.group("value"), "Weight")
        return f"{value} гр"

    normalized = _EXPLICIT_WEIGHT.sub(replace_weight, normalized)
    normalized = _BARE_AT_BOUNDARY.sub(replace_weight, normalized)

    for index, expression in enumerate(nutrition_expressions):
        normalized = normalized.replace(_placeholder(index), expression)
    for index, expression in enumerate(per_100_markers):
        normalized = normalized.replace(_placeholder(10_000 + index), expression)

    normalized = re.sub(r"[ \t]+", " ", normalized).strip()

    explicit: list[ExplicitValue] = []
    source_indexes = {kind: 0 for kind in ("weight", "kcal", "protein", "fat", "carbs")}
    matches: list[
        tuple[
            int,
            int,
            Literal["weight", "kcal", "protein", "fat", "carbs"],
            int,
            Literal["per_100g", "portion"] | None,
        ]
    ] = []
    for match in _CANONICAL_WEIGHT.finditer(normalized):
        matches.append(
            (match.start(), match.end(), "weight", int(match.group("value")), None)
        )
    for match in _CANONICAL_KCAL.finditer(normalized):
        matches.append(
            (match.start(), match.end(), "kcal", int(match.group("value")), "per_100g")
        )
    for match in _CANONICAL_PORTION_KCAL.finditer(normalized):
        matches.append(
            (match.start(), match.end(), "kcal", int(match.group("value")), "portion")
        )
    for kind, pattern in _CANONICAL_MACROS.items():
        for match in pattern.finditer(normalized):
            matches.append(
                (
                    match.start(),
                    match.end(),
                    cast(Literal["protein", "fat", "carbs"], kind),
                    int(match.group("value")),
                    "per_100g" if match.group("basis") == "100г" else "portion",
                )
            )

    prefixes = {"weight": "W", "kcal": "K", "protein": "P", "fat": "F", "carbs": "C"}
    for start, end, kind, value, basis in sorted(matches):
        source_indexes[kind] += 1
        source_id = f"{prefixes[kind]}{source_indexes[kind]}"
        explicit.append(
            ExplicitValue(
                source_id=source_id,
                kind=kind,
                value=value,
                start=start,
                end=end,
                basis=basis,
            )
        )
    portions = tuple(
        HouseholdPortion(
            count=int(match.group("count")),
            display=f"{int(match.group('count'))} шт.",
            item_alias="egg",
            reference_weight_g=int(match.group("count")) * _EGG_REFERENCE_WEIGHT_G,
        )
        for match in _EGG_PORTION.finditer(normalized)
        if int(match.group("count")) > 0
    )
    return NormalizedInput(
        text=normalized,
        explicit_values=tuple(explicit),
        original_text=original_text,
        household_portions=portions,
    )


def enforce_explicit_values(
    analysis: FoodAnalysis,
    explicit_values: tuple[ExplicitValue, ...],
    *,
    image_present: bool = False,
) -> FoodAnalysis:
    if not analysis.is_food:
        if explicit_values:
            raise AnalysisError("A non-food response omitted explicit source IDs")
        return analysis

    sources = {source.source_id: source for source in explicit_values}
    used: set[str] = set()
    items = [item.model_copy(deep=True) for item in analysis.items]

    for item in items:
        assignments = (
            ("weight", item.weight_source_id),
            ("kcal", item.kcal_source_id),
            ("protein", item.protein_source_id),
            ("fat", item.fat_source_id),
            ("carbs", item.carbs_source_id),
        )
        for kind, source_id in assignments:
            if source_id is None:
                if kind == "weight":
                    item.weight_estimated = True
                    item.weight_origin = (
                        "image"
                        if image_present and item.weight_origin == "image"
                        else "model_estimate"
                    )
                elif kind == "kcal":
                    item.kcal_estimated = True
                    item.kcal_origin = (
                        "image"
                        if image_present and item.kcal_origin == "image"
                        else "model_estimate"
                    )
                else:
                    setattr(item, f"{kind}_estimated", True)
                    current_origin = getattr(item, f"{kind}_origin")
                    setattr(
                        item,
                        f"{kind}_origin",
                        "image"
                        if image_present and current_origin == "image"
                        else "model_estimate",
                    )
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
                item.weight_origin = "user_text"
            elif kind == "kcal":
                item.kcal_per_100g = (
                    source.value / item.weight_g * 100
                    if source.basis == "portion"
                    else source.value
                )
                item.kcal_estimated = (
                    source.basis == "portion" and item.weight_origin != "user_text"
                )
                item.kcal_origin = "user_text"
                item.kcal_source_basis = source.basis or "per_100g"
            else:
                density = float(source.value)
                if source.basis == "portion":
                    density = source.value / item.weight_g * 100
                setattr(item, f"{kind}_per_100g", density)
                setattr(
                    item,
                    f"{kind}_estimated",
                    source.basis == "portion" and item.weight_origin != "user_text",
                )
                setattr(item, f"{kind}_origin", "user_text")
                setattr(item, f"{kind}_source_basis", source.basis or "per_100g")

    missing = set(sources) - used
    if missing:
        raise AnalysisError(
            "The model did not assign every explicit source ID: "
            + ", ".join(sorted(missing))
        )
    return FoodAnalysis(is_food=True, meal_name=analysis.meal_name, items=items)


def apply_household_portions(
    analysis: FoodAnalysis, portions: tuple[HouseholdPortion, ...]
) -> FoodAnalysis:
    if not analysis.is_food or not portions:
        return analysis
    items = [item.model_copy(deep=True) for item in analysis.items]
    available = list(range(len(items)))
    for portion in portions:
        if portion.item_alias != "egg":
            continue
        match_index = next(
            (
                index
                for index in available
                if any(
                    alias in items[index].name.casefold() for alias in _EGG_ITEM_ALIASES
                )
            ),
            None,
        )
        if match_index is None:
            continue
        item = items[match_index]
        item.portion_display = portion.display
        if item.weight_source_id is None and portion.reference_weight_g is not None:
            item.weight_g = portion.reference_weight_g
            item.weight_estimated = True
            item.weight_origin = "deterministic_reference"
        available.remove(match_index)
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
    assert pricing.output_per_1m is not None
    cached_input_price = pricing.cached_input_per_1m or pricing.input_per_1m
    return (
        Decimal(uncached_input) * pricing.input_per_1m
        + Decimal(cached_input_tokens) * cached_input_price
        + Decimal(output_tokens) * pricing.output_per_1m
    ) / Decimal(1_000_000)


def metadata_from_usage(
    usage: Any | None,
    model: str,
    effort: str,
    pricing: ModelPricing,
    usage_recorder: UsageRecorder | None = None,
) -> LLMMetadata:
    """Convert OpenAI usage and persist it in the common bot statistics."""
    if usage is None:
        LOGGER.warning("OpenAI returned no token usage")
        return LLMMetadata(model=model, effort=effort)
    input_tokens = int(usage.input_tokens)
    output_tokens = int(usage.output_tokens)
    details = getattr(usage, "input_tokens_details", None)
    cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
    estimated_cost = calculate_llm_cost(
        input_tokens,
        cached_tokens,
        output_tokens,
        pricing,
    )
    if usage_recorder is not None:
        try:
            usage_recorder.record_llm_usage(
                datetime.now(UTC),
                model,
                input_tokens,
                cached_tokens,
                output_tokens,
                estimated_cost,
            )
        except Exception:
            LOGGER.exception("Could not persist OpenAI token usage")
    return LLMMetadata(
        model=model,
        effort=effort,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_tokens,
        llm_cost_usd=estimated_cost,
    )


class OpenAIAnalyzer:
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

    def _metadata_from_usage(self, usage: Any | None) -> LLMMetadata:
        return metadata_from_usage(
            usage,
            self._model,
            self._effort,
            self._pricing,
            getattr(self, "_usage_recorder", None),
        )

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
                "basis": source.basis,
                "text": normalized.text[source.start : source.end],
            }
            for source in normalized.explicit_values
        ]
        portion_hints = [
            {
                "count": portion.count,
                "display": portion.display,
                "item_alias": portion.item_alias,
                "reference_weight_g": portion.reference_weight_g,
            }
            for portion in normalized.household_portions
        ]
        user_content: str | list[dict[str, str]] = normalized.text
        if image_bytes is not None:
            encoded_image = base64.b64encode(image_bytes).decode("ascii")
            image_mime_type = (
                "image/png"
                if image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
                else "image/jpeg"
            )
            user_content = [
                {
                    "type": "input_text",
                    "text": normalized.text or "Розпізнай страву на фото.",
                },
                {
                    "type": "input_image",
                    "image_url": f"data:{image_mime_type};base64,{encoded_image}",
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
                                f"{constraints}. Deterministic portion hints: "
                                f"{portion_hints}. Original user text: "
                                f"{normalized.original_text!r}"
                            ),
                        },
                        {"role": "user", "content": user_content},
                    ],
                ),
                text_format=FoodAnalysis,
            )
        except Exception as exc:
            raise AnalysisError("OpenAI request failed") from exc

        metadata = self._metadata_from_usage(getattr(response, "usage", None))

        parsed = response.output_parsed
        if parsed is None:
            raise AnalysisError("OpenAI returned no structured analysis")
        analysis = enforce_explicit_values(
            parsed,
            normalized.explicit_values,
            image_present=image_bytes is not None,
        )
        analysis = apply_household_portions(analysis, normalized.household_portions)

        return AnalysisResult(analysis=analysis, metadata=metadata)

    def suggest_meal_icon(self, meal: MealResult) -> MealIconSuggestion:
        try:
            response = self._client.responses.parse(
                model=self._model,
                reasoning={"effort": cast(ReasoningEffort, self._effort)},
                store=False,
                input=cast(
                    Any,
                    [
                        {"role": "system", "content": ICON_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"Meal: {meal.meal_name}. Components: "
                                + ", ".join(item.name for item in meal.items)
                            ),
                        },
                    ],
                ),
                text_format=MealIconSuggestion,
            )
        except Exception as exc:
            raise AnalysisError("OpenAI icon request failed") from exc
        self._metadata_from_usage(getattr(response, "usage", None))
        if response.output_parsed is None:
            raise AnalysisError("OpenAI returned no icon suggestion")
        return response.output_parsed
