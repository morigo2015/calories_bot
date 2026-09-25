from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from calories_bot.analyzer import NormalizedInput
from calories_bot.models import FoodAnalysis, FoodItem, calculate_meal
from calories_bot.quality import QualityStore
from scripts.manual_review import (
    ManualReviewError,
    create_review_batch,
    import_review_result,
    review_to_eval_draft,
    validate_review_result,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
DAY = date(2026, 9, 25)


def _record(store: QualityStore, message_id: int = 10) -> str:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="Сир",
        items=[
            FoodItem(
                name="Сир",
                weight_g=150,
                weight_estimated=False,
                kcal_per_100g=120,
                kcal_estimated=True,
            )
        ],
    )
    return store.record_case(
        recorded_at=NOW,
        telegram_user_id=123,
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
        model="secret-model",
        effort="high",
        prompt_sha256="prompt",
        app_version="test",
    )


def _result(batch_id: str, case_id: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "batch_id": batch_id,
            "reviews": [
                {
                    "case_id": case_id,
                    "verdict": "major_error",
                    "issue_type": "explicit_value",
                    "explanation": "Explicit weight was missed.",
                    "suggested_eval_case": {
                        "id": "explicit-weight",
                        "text": "сир 150 г",
                        "expected": {"is_food": True, "weight_g": [150, 150]},
                    },
                }
            ],
        },
        ensure_ascii=False,
    )


def test_blind_audit_batch_hides_identity_model_and_feedback(tmp_path) -> None:
    store = QualityStore(tmp_path / "stats.sqlite3")
    case_id = _record(store)

    batch = create_review_batch(store, "audit", 10, tmp_path / "reviews", now=NOW)

    assert batch["cases"][0]["case_id"] == case_id
    assert "secret-model" not in batch["prompt"]
    assert "telegram_user_id" not in batch["prompt"]
    assert "user_feedback" not in batch["prompt"]
    assert (batch["batch_dir"] / "request.md").exists()
    assert (batch["batch_dir"] / "cases.json").exists()


def test_reported_batch_contains_optional_user_explanation(tmp_path) -> None:
    store = QualityStore(tmp_path / "stats.sqlite3")
    _record(store)
    feedback = store.report_error(123, 10, DAY, NOW)
    assert feedback is not None
    store.set_feedback_prompt(feedback.case_id, 500)
    store.save_explanation_by_prompt(123, 500, "Не помічено вагу", NOW)

    batch = create_review_batch(store, "reported", 10, tmp_path / "reviews", now=NOW)

    assert "Не помічено вагу" in batch["prompt"]


def test_import_accepts_outer_json_fence_and_is_idempotent(tmp_path) -> None:
    store = QualityStore(tmp_path / "stats.sqlite3")
    case_id = _record(store)
    batch = create_review_batch(store, "audit", 1, tmp_path / "reviews", now=NOW)
    raw = "```json\n" + _result(batch["batch_id"], case_id) + "\n```"

    first = import_review_result(
        store, batch["batch_id"], raw, tmp_path / "reviews", now=NOW
    )
    second = import_review_result(
        store, batch["batch_id"], raw, tmp_path / "reviews", now=NOW
    )

    assert first == second
    assert len(store.list_reviews()) == 1
    assert (batch["batch_dir"] / "result.json").exists()
    assert review_to_eval_draft(store.list_reviews()[0])["id"] == "explicit-weight"


def test_review_validation_requires_every_case_exactly_once() -> None:
    raw = json.dumps({"schema_version": 1, "batch_id": "batch", "reviews": []})

    with pytest.raises(ManualReviewError, match="every batch case"):
        validate_review_result(raw, "batch", ["case-1"])
