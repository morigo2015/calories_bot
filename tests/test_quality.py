from __future__ import annotations

from datetime import UTC, date, datetime

from calories_bot.analyzer import NormalizedInput
from calories_bot.models import FoodAnalysis, FoodItem, calculate_meal
from calories_bot.quality import QualityStore

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
DAY = date(2026, 9, 25)


def _analysis() -> FoodAnalysis:
    return FoodAnalysis(
        is_food=True,
        meal_name="Сир",
        items=[
            FoodItem(
                name="Сир",
                weight_g=150,
                weight_estimated=False,
                weight_origin="user_text",
                weight_source_id="W1",
                kcal_per_100g=120,
                kcal_estimated=True,
                kcal_origin="model_estimate",
            )
        ],
    )


def _record(store: QualityStore, *, user_id: int = 123, message_id: int = 10) -> str:
    analysis = _analysis()
    return store.record_case(
        recorded_at=NOW,
        telegram_user_id=user_id,
        source_message_id=message_id,
        accounting_day=DAY,
        input_kind="text",
        original_text="сир 150 г",
        normalized=NormalizedInput(text="сир 150 гр", explicit_values=()),
        analysis=analysis,
        meal=calculate_meal(analysis),
        rendered_reply="Сир — 180 кк",
        photo_path=None,
        image_bytes=None,
        model="test-model",
        effort="low",
        prompt_sha256="abc",
        app_version="test",
    )


def test_quality_case_is_immutable_and_idempotent(tmp_path) -> None:
    store = QualityStore(tmp_path / "statistics.sqlite3")

    first = _record(store)
    second = _record(store)
    case = store.get_case(123, 10, DAY)

    assert second == first
    assert case is not None
    assert case["original_text"] == "сир 150 г"
    assert case["analysis"]["items"][0]["weight_source_id"] == "W1"
    assert case["meal"]["meal_kcal"] == 180


def test_feedback_is_one_click_idempotent_and_explanation_matches_prompt(
    tmp_path,
) -> None:
    store = QualityStore(tmp_path / "statistics.sqlite3")
    case_id = _record(store)

    first = store.report_error(123, 10, DAY, NOW)
    second = store.report_error(123, 10, DAY, NOW)
    assert first is not None and first.created is True
    assert second is not None and second.created is False
    assert second.case_id == case_id
    assert store.can_report_case(123, 10, DAY) is False

    store.set_feedback_prompt(case_id, 500)
    assert store.save_explanation_by_prompt(999, 500, "wrong", NOW) is False
    assert store.save_explanation_by_prompt(123, 500, "  Вагу не помічено.  ", NOW)

    reported = store.list_cases("reported")
    assert reported[0]["explanation"] == "Вагу не помічено."
    assert store.list_cases("audit") == []


def test_delete_user_removes_quality_feedback_and_reviews(tmp_path) -> None:
    store = QualityStore(tmp_path / "statistics.sqlite3")
    case_id = _record(store)
    store.report_error(123, 10, DAY, NOW)
    batch_id = store.create_batch("reported", [case_id], NOW)
    store.import_reviews(
        batch_id,
        [
            {
                "case_id": case_id,
                "verdict": "major_error",
                "issue_type": "explicit_value",
                "explanation": "Missed weight",
                "suggested_eval_case": None,
            }
        ],
        NOW,
    )

    store.delete_user(123)

    assert store.get_case(123, 10, DAY) is None
    assert store.list_reviews() == []


def test_review_import_is_idempotent(tmp_path) -> None:
    store = QualityStore(tmp_path / "statistics.sqlite3")
    case_id = _record(store)
    batch_id = store.create_batch("audit", [case_id], NOW)
    reviews = [
        {
            "case_id": case_id,
            "verdict": "correct",
            "issue_type": "none",
            "explanation": "OK",
            "suggested_eval_case": None,
        }
    ]

    store.import_reviews(batch_id, reviews, NOW)
    store.import_reviews(batch_id, reviews, NOW)

    assert len(store.list_reviews()) == 1
