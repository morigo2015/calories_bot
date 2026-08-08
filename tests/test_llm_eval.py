from __future__ import annotations

from calories_bot.models import FoodAnalysis, FoodItem
from scripts.eval_llm import grade_analysis, load_cases, parse_config


def food_analysis() -> FoodAnalysis:
    return FoodAnalysis(
        is_food=True,
        meal_name="Два яйця",
        items=[
            FoodItem(
                name="Яйця",
                weight_g=100,
                weight_estimated=True,
                weight_origin="deterministic_reference",
                kcal_per_100g=155,
                kcal_estimated=True,
                kcal_origin="model_estimate",
                portion_display="2 шт.",
            )
        ],
    )


def test_grade_analysis_accepts_invariants() -> None:
    checks = grade_analysis(
        food_analysis(),
        {
            "is_food": True,
            "item_count": [1, 1],
            "required_terms": [["яйц"]],
            "portion_terms": ["2 шт"],
            "weight_g": [100, 100],
            "kcal_per_100g": [140, 170],
            "meal_kcal": [150, 160],
        },
    )

    assert checks
    assert all(check.passed for check in checks)


def test_grade_analysis_reports_failed_invariant() -> None:
    checks = grade_analysis(
        food_analysis(),
        {"is_food": True, "required_terms": [["борщ"]]},
    )

    assert any(check.name == "required_term" and not check.passed for check in checks)


def test_grade_non_food_does_not_calculate_meal() -> None:
    analysis = FoodAnalysis(is_food=False, meal_name="", items=[])

    checks = grade_analysis(analysis, {"is_food": False, "meal_kcal": [0, 0]})

    assert len(checks) == 1
    assert checks[0].passed


def test_load_cases_supports_selection(tmp_path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"id":"one","text":"яблуко","expected":{"is_food":true}}\n'
        '{"id":"two","text":"привіт","expected":{"is_food":false}}\n',
        encoding="utf-8",
    )

    cases = load_cases(path, {"two"})

    assert [case["id"] for case in cases] == ["two"]


def test_parse_config_uses_last_colon() -> None:
    assert parse_config("gpt-5.6-luna:low") == ("gpt-5.6-luna", "low")
