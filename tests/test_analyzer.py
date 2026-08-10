import base64
from decimal import Decimal
from types import SimpleNamespace

import pytest

from calories_bot.analyzer import (
    AnalysisError,
    InputFormatError,
    ModelPricing,
    NormalizedInput,
    OpenAIAnalyzer,
    apply_household_portions,
    calculate_llm_cost,
    enforce_explicit_values,
    normalize_input,
)
from calories_bot.models import (
    FoodAnalysis,
    FoodItem,
    MealIconSuggestion,
    calculate_meal,
    round_whole,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("сир 50", "сир 50 гр"),
        ("сир 50г 120#", "сир 50 гр 120 ккал/100г"),
        ("#120 сир 50 г", "120 ккал/100г сир 50 гр"),
        ("сир 50; яблуко 120", "сир 50 гр; яблуко 120 гр"),
        ("сир 50, яблуко", "сир 50 гр, яблуко"),
        ("чай 250\nпечиво 30", "чай 250 гр\nпечиво 30 гр"),
        ("2 яйця", "2 яйця"),
        ("2 яйця, хліб 50", "2 яйця, хліб 50 гр"),
        ("йогурт 200г 65 кк/100гр", "йогурт 200 гр 65 ккал/100г"),
        ("батончик 45g 380kcal/100g", "батончик 45 гр 380 ккал/100г"),
        ("сир 120 г 30 ккал на 100 грам", "сир 120 гр 30 ккал/100г"),
        ("сир 120 г 30 калорій за 100 грамів", "сир 120 гр 30 ккал/100г"),
        (
            "Каша с мясом, калорийностью 150 ккал/100 г. 500 г",
            "Каша с мясом, калорийностью 150 ккал/100г. 500 гр",
        ),
        ("кава без цукру", "кава без цукру"),
        ("піца 30 см", "піца 30 см"),
        ("піца 30", "піца 30 гр"),
        ("яблуко 100.", "яблуко 100 гр."),
        ("яблуко 100!", "яблуко 100 гр!"),
    ],
)
def test_normative_normalization_examples(source: str, expected: str) -> None:
    assert normalize_input(source).text == expected


@pytest.mark.parametrize(
    "unit",
    [
        "г",
        "гр",
        "грам",
        "грама",
        "грами",
        "грамів",
        "грамами",
        "грамм",
        "грамма",
        "граммы",
        "граммов",
        "граммів",
        "g",
        "gr",
        "gram",
        "grams",
        "GRAMS",
    ],
)
def test_spoken_weight_units_are_normalized(unit: str) -> None:
    normalized = normalize_input(f"сир 50 {unit}")
    assert normalized.text == "сир 50 гр"
    assert [(value.kind, value.value) for value in normalized.explicit_values] == [
        ("weight", 50)
    ]


@pytest.mark.parametrize("unit", ["грамів", "граммів", "gram", "grams"])
def test_spoken_weight_units_work_in_kcal_per_100g(unit: str) -> None:
    normalized = normalize_input(f"сир 50 грамів 120 кк/100 {unit}")
    assert normalized.text == "сир 50 гр 120 ккал/100г"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "сир сто двадцять грам, тридцять ккал на сто грам",
            "сир 120 гр, 30 ккал/100г",
        ),
        (
            "творог сто двадцать граммов, тридцать килокалорий на сто граммов",
            "творог 120 гр, 30 ккал/100г",
        ),
        ("два яйця, хліб пʼятдесят", "2 яйця, хліб 50 гр"),
        ("кавун одна тисяча двісті грамів", "кавун 1200 гр"),
    ],
)
def test_ukrainian_and_russian_spoken_numbers_are_normalized(
    source: str, expected: str
) -> None:
    normalized = normalize_input(source)
    assert normalized.text == expected


def test_spoken_calorie_and_weight_values_keep_explicit_sources() -> None:
    normalized = normalize_input("сир сто двадцять грам триста ккал на сто грам")
    assert [(value.kind, value.value) for value in normalized.explicit_values] == [
        ("weight", 120),
        ("kcal", 300),
    ]


def test_household_egg_portion_is_preserved_and_uses_reference_weight() -> None:
    normalized = normalize_input("два яйця")
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="яйця",
        items=[
            FoodItem(
                name="яйця",
                weight_g=140,
                weight_estimated=True,
                kcal_per_100g=140,
                kcal_estimated=True,
            )
        ],
    )

    applied = apply_household_portions(analysis, normalized.household_portions)

    assert applied.items[0].weight_g == 100
    assert applied.items[0].weight_origin == "deterministic_reference"
    assert applied.items[0].portion_display == "2 шт."


def test_weight_units_are_not_matched_inside_words() -> None:
    assert normalize_input("телеграма").text == "телеграма"


@pytest.mark.parametrize(
    "unit",
    [
        "кк",
        "kk",
        "кk",
        "kк",
        "ккал",
        "kkал",
        "ккaл",
        "kкaл",
        "kcal",
        "KCAL",
    ],
)
def test_mixed_script_calorie_units(unit: str) -> None:
    normalized = normalize_input(f"сир 50 гр 120 {unit} / 100 gr")
    assert normalized.text == "сир 50 гр 120 ккал/100г"
    assert [(value.kind, value.value) for value in normalized.explicit_values] == [
        ("weight", 50),
        ("kcal", 120),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "сир # 120",
        "сир 120 #",
        "сир #120#",
        "сир #",
        "сир 50.5",
        "сир 50,5",
        "сир 0",
        "сир 50 гр 0#",
        "",
    ],
)
def test_invalid_input_is_rejected(source: str) -> None:
    with pytest.raises(InputFormatError):
        normalize_input(source)


def test_explicit_sources_override_model_values_and_flags() -> None:
    normalized = normalize_input("сир 50, яблуко 100#")
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="перекус",
        items=[
            FoodItem(
                name="сир",
                weight_g=90,
                weight_estimated=True,
                kcal_per_100g=300,
                kcal_estimated=False,
                weight_source_id="W1",
            ),
            FoodItem(
                name="яблуко",
                weight_g=150,
                weight_estimated=False,
                kcal_per_100g=50,
                kcal_estimated=True,
                kcal_source_id="K1",
            ),
        ],
    )
    enforced = enforce_explicit_values(analysis, normalized.explicit_values)

    assert enforced.items[0].weight_g == 50
    assert enforced.items[0].weight_estimated is False
    assert enforced.items[0].weight_origin == "user_text"
    assert enforced.items[0].kcal_estimated is True
    assert enforced.items[1].kcal_per_100g == 100
    assert enforced.items[1].kcal_estimated is False
    assert enforced.items[1].kcal_origin == "user_text"
    assert enforced.items[1].weight_estimated is True


@pytest.mark.parametrize(
    "items",
    [
        [
            FoodItem(
                name="сир",
                weight_g=50,
                weight_estimated=False,
                kcal_per_100g=120,
                kcal_estimated=True,
            )
        ],
        [
            FoodItem(
                name="сир",
                weight_g=50,
                weight_estimated=False,
                kcal_per_100g=120,
                kcal_estimated=True,
                weight_source_id="K1",
            )
        ],
    ],
)
def test_missing_or_wrong_explicit_source_is_rejected(items: list[FoodItem]) -> None:
    normalized = normalize_input("сир 50")
    analysis = FoodAnalysis(is_food=True, meal_name="сир", items=items)
    with pytest.raises(AnalysisError):
        enforce_explicit_values(analysis, normalized.explicit_values)


def test_duplicate_explicit_source_is_rejected() -> None:
    normalized = normalize_input("сир 50")
    item = dict(
        weight_g=50,
        weight_estimated=False,
        kcal_per_100g=120,
        kcal_estimated=True,
        weight_source_id="W1",
    )
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="сир",
        items=[FoodItem(name="сир", **item), FoodItem(name="сир 2", **item)],
    )
    with pytest.raises(AnalysisError):
        enforce_explicit_values(analysis, normalized.explicit_values)


def test_exact_arithmetic_for_both_hash_positions() -> None:
    for source in ("#120 сир 50", "сир 50 гр 120#"):
        normalized = normalize_input(source)
        weight_id = next(
            v.source_id for v in normalized.explicit_values if v.kind == "weight"
        )
        kcal_id = next(
            v.source_id for v in normalized.explicit_values if v.kind == "kcal"
        )
        analysis = FoodAnalysis(
            is_food=True,
            meal_name="сир",
            items=[
                FoodItem(
                    name="сир",
                    weight_g=1,
                    weight_estimated=True,
                    kcal_per_100g=1,
                    kcal_estimated=True,
                    weight_source_id=weight_id,
                    kcal_source_id=kcal_id,
                )
            ],
        )
        assert (
            round_whole(
                calculate_meal(
                    enforce_explicit_values(analysis, normalized.explicit_values)
                ).meal_kcal
            )
            == 60
        )


def test_llm_cost_separates_cached_tokens() -> None:
    pricing = ModelPricing(Decimal("2"), Decimal("0.5"), Decimal("8"))
    cost = calculate_llm_cost(1000, 400, 200, pricing)
    assert cost == Decimal("0.003")


def test_food_analysis_derives_missing_meal_name_from_items() -> None:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="  ",
        items=[
            FoodItem(
                name="сир",
                weight_g=50,
                weight_estimated=False,
                kcal_per_100g=120,
                kcal_estimated=False,
            ),
            FoodItem(
                name="хліб",
                weight_g=30,
                weight_estimated=True,
                kcal_per_100g=250,
                kcal_estimated=True,
            ),
        ],
    )

    assert analysis.meal_name == "сир, хліб"


def test_legacy_food_item_derives_safe_origins() -> None:
    item = FoodItem.model_validate(
        {
            "name": "сир",
            "weight_g": 50,
            "weight_estimated": False,
            "kcal_per_100g": 120,
            "kcal_estimated": True,
        }
    )
    assert item.weight_origin == "user_text"
    assert item.kcal_origin == "model_estimate"


@pytest.mark.parametrize(
    ("field", "value"),
    [("weight_g", 10_001), ("kcal_per_100g", 1_001)],
)
def test_implausible_analysis_values_are_rejected(field: str, value: int) -> None:
    values = {
        "name": "сир",
        "weight_g": 50,
        "weight_estimated": True,
        "kcal_per_100g": 120,
        "kcal_estimated": True,
    }
    values[field] = value
    with pytest.raises(ValueError, match="implausibly"):
        FoodItem.model_validate(values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("школад #300 100", "школад 300 ккал/100г 100 гр"),
        ("сир #120 50 г", "сир 120 ккал/100г 50 гр"),
    ],
)
def test_normalizes_live_telegram_examples(raw: str, expected: str) -> None:
    assert normalize_input(raw).text == expected


def test_llm_cost_is_blank_for_incomplete_pricing() -> None:
    pricing = ModelPricing(None, Decimal("0.5"), Decimal("8"))
    assert calculate_llm_cost(1000, 0, 200, pricing) is None


def test_llm_cost_rejects_impossible_usage() -> None:
    pricing = ModelPricing(Decimal("2"), Decimal("0.5"), Decimal("8"))
    with pytest.raises(AnalysisError):
        calculate_llm_cost(10, 11, 1, pricing)


def test_openai_analyzer_returns_usage_and_sends_normalized_text() -> None:
    normalized = normalize_input("сир 50 гр 120#")
    parsed = FoodAnalysis(
        is_food=True,
        meal_name="сир",
        items=[
            FoodItem(
                name="сир",
                weight_g=1,
                weight_estimated=True,
                kcal_per_100g=1,
                kcal_estimated=True,
                weight_source_id="W1",
                kcal_source_id="K1",
            )
        ],
    )
    response = SimpleNamespace(
        output_parsed=parsed,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            input_tokens_details=SimpleNamespace(cached_tokens=40),
        ),
    )

    class FakeResponses:
        def __init__(self) -> None:
            self.kwargs = None

        def parse(self, **kwargs):
            self.kwargs = kwargs
            return response

    fake_responses = FakeResponses()
    analyzer = OpenAIAnalyzer.__new__(OpenAIAnalyzer)
    analyzer._client = SimpleNamespace(responses=fake_responses)
    analyzer._model = "test-model"
    analyzer._effort = "none"
    analyzer._pricing = ModelPricing(Decimal("2"), Decimal("1"), Decimal("8"))

    result = analyzer.analyze(normalized)

    assert result.analysis.items[0].weight_g == 50
    assert result.analysis.items[0].kcal_per_100g == 120
    assert result.metadata.input_tokens == 100
    assert result.metadata.output_tokens == 20
    assert result.metadata.llm_cost_usd == Decimal("0.00032")
    assert fake_responses.kwargs["input"][-1]["content"] == normalized.text


def test_openai_analyzer_allows_missing_usage() -> None:
    response = SimpleNamespace(
        output_parsed=FoodAnalysis(is_food=False, meal_name="", items=[]),
        usage=None,
    )
    analyzer = OpenAIAnalyzer.__new__(OpenAIAnalyzer)
    analyzer._client = SimpleNamespace(
        responses=SimpleNamespace(parse=lambda **kwargs: response)
    )
    analyzer._model = "test-model"
    analyzer._effort = "none"
    analyzer._pricing = ModelPricing(None, None, None)

    result = analyzer.analyze(NormalizedInput("привіт", ()))
    assert result.metadata.input_tokens is None
    assert result.metadata.llm_cost_usd is None


def test_openai_analyzer_sends_text_and_base64_photo() -> None:
    normalized = normalize_input("200 г")
    response = SimpleNamespace(
        output_parsed=FoodAnalysis(
            is_food=True,
            meal_name="паста",
            items=[
                FoodItem(
                    name="паста",
                    weight_g=1,
                    weight_estimated=True,
                    kcal_per_100g=150,
                    kcal_estimated=True,
                    weight_source_id="W1",
                )
            ],
        ),
        usage=None,
    )

    class FakeResponses:
        def __init__(self) -> None:
            self.kwargs = None

        def parse(self, **kwargs):
            self.kwargs = kwargs
            return response

    fake_responses = FakeResponses()
    analyzer = OpenAIAnalyzer.__new__(OpenAIAnalyzer)
    analyzer._client = SimpleNamespace(responses=fake_responses)
    analyzer._model = "test-model"
    analyzer._effort = "none"
    analyzer._pricing = ModelPricing(None, None, None)

    result = analyzer.analyze(normalized, b"jpeg-data")

    content = fake_responses.kwargs["input"][-1]["content"]
    assert result.analysis.items[0].weight_g == 200
    assert content[0] == {"type": "input_text", "text": "200 гр"}
    assert content[1] == {
        "type": "input_image",
        "image_url": (
            "data:image/jpeg;base64," + base64.b64encode(b"jpeg-data").decode("ascii")
        ),
        "detail": "auto",
    }


def test_openai_analyzer_uses_png_media_type() -> None:
    response = SimpleNamespace(
        output_parsed=FoodAnalysis(is_food=False, meal_name="", items=[]),
        usage=None,
    )
    fake_responses = SimpleNamespace(kwargs=None)

    def parse(**kwargs):
        fake_responses.kwargs = kwargs
        return response

    fake_responses.parse = parse
    analyzer = OpenAIAnalyzer.__new__(OpenAIAnalyzer)
    analyzer._client = SimpleNamespace(responses=fake_responses)
    analyzer._model = "test-model"
    analyzer._effort = "none"
    analyzer._pricing = ModelPricing(None, None, None)

    analyzer.analyze(NormalizedInput("", ()), b"\x89PNG\r\n\x1a\ncontent")

    content = fake_responses.kwargs["input"][-1]["content"]
    assert content[1]["image_url"].startswith("data:image/png;base64,")


def test_openai_errors_are_wrapped() -> None:
    analyzer = OpenAIAnalyzer.__new__(OpenAIAnalyzer)
    analyzer._client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=lambda **kwargs: (_ for _ in ()).throw(TimeoutError("failed"))
        )
    )
    analyzer._model = "test-model"
    analyzer._effort = "none"
    analyzer._pricing = ModelPricing(None, None, None)
    with pytest.raises(AnalysisError):
        analyzer.analyze(NormalizedInput("сир", ()))


def test_empty_structured_output_is_rejected() -> None:
    analyzer = OpenAIAnalyzer.__new__(OpenAIAnalyzer)
    analyzer._client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=lambda **kwargs: SimpleNamespace(output_parsed=None, usage=None)
        )
    )
    analyzer._model = "test-model"
    analyzer._effort = "none"
    analyzer._pricing = ModelPricing(None, None, None)
    with pytest.raises(AnalysisError):
        analyzer.analyze(NormalizedInput("сир", ()))


def test_openai_analyzer_requests_structured_meal_icon() -> None:
    response = SimpleNamespace(
        output_parsed=MealIconSuggestion(emoji="🧀", confidence=0.91)
    )
    fake_responses = SimpleNamespace(kwargs=None)

    def parse(**kwargs):
        fake_responses.kwargs = kwargs
        return response

    fake_responses.parse = parse
    analyzer = OpenAIAnalyzer.__new__(OpenAIAnalyzer)
    analyzer._client = SimpleNamespace(responses=fake_responses)
    analyzer._model = "test-model"
    analyzer._effort = "none"
    analyzer._pricing = ModelPricing(None, None, None)

    suggestion = analyzer.suggest_meal_icon(
        calculate_meal(
            FoodAnalysis(
                is_food=True,
                meal_name="сир",
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
    )

    assert suggestion == MealIconSuggestion(emoji="🧀", confidence=0.91)
    assert fake_responses.kwargs["text_format"] is MealIconSuggestion
    assert fake_responses.kwargs["store"] is False


def test_openai_analyzer_configures_explicit_timeout(monkeypatch) -> None:
    created = {}

    def build_client(**kwargs):
        created.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("calories_bot.analyzer.OpenAI", build_client)

    OpenAIAnalyzer(
        "key",
        "test-model",
        "low",
        45,
        ModelPricing(None, None, None),
    )

    assert created == {"api_key": "key", "timeout": 45}
