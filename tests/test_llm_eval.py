from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from calories_bot.analyzer import AnalysisError, AnalysisResult
from calories_bot.models import FoodAnalysis, FoodItem, LLMMetadata
from scripts import eval_llm
from scripts.eval_llm import (
    build_dataset_snapshot,
    create_run_identity,
    grade_analysis,
    load_cases,
    parse_config,
    serialize_analysis,
)


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


def test_grade_analysis_checks_named_composite_components_and_total_weight() -> None:
    analysis = FoodAnalysis(
        is_food=True,
        meal_name="Обід",
        items=[
            FoodItem(
                name="Куряче філе",
                weight_g=120,
                weight_estimated=False,
                kcal_per_100g=165,
                kcal_estimated=True,
            ),
            FoodItem(
                name="Гречка",
                weight_g=180,
                weight_estimated=False,
                kcal_per_100g=110,
                kcal_estimated=True,
            ),
        ],
    )

    checks = grade_analysis(
        analysis,
        {
            "is_food": True,
            "item_count": [2, 2],
            "total_weight_g": [300, 300],
            "item_expectations": [
                {"required_terms": ["греч"], "weight_g": [180, 180]},
                {"required_terms": ["кур"], "weight_g": [120, 120]},
            ],
        },
    )

    assert checks
    assert all(check.passed for check in checks)


def test_grade_analysis_does_not_reuse_item_for_components() -> None:
    checks = grade_analysis(
        food_analysis(),
        {
            "is_food": True,
            "item_expectations": [
                {"required_terms": ["яйц"]},
                {"required_terms": ["яйц"]},
            ],
        },
    )

    assert any(check.name == "item_2_match" and not check.passed for check in checks)


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


def test_run_identity_has_utc_name_and_unique_id() -> None:
    started = datetime(2026, 8, 8, 15, 42, tzinfo=UTC)

    first_id, timestamp, name = create_run_identity(started)
    second_id, _, _ = create_run_identity(started)

    assert first_id.startswith("20260808T154200Z-")
    assert second_id != first_id
    assert timestamp == "2026-08-08T15:42:00Z"
    assert name == "Run 2026-08-08 15:42 UTC"


def test_serialize_analysis_keeps_explicit_source_ids() -> None:
    analysis = FoodAnalysis(
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
                kcal_estimated=False,
                kcal_origin="user_text",
                kcal_source_id="K1",
            )
        ],
    )

    serialized = serialize_analysis(analysis)

    assert serialized["items"][0]["weight_source_id"] == "W1"
    assert serialized["items"][0]["kcal_source_id"] == "K1"


def test_dataset_snapshot_hashes_images_and_is_independent(tmp_path: Path) -> None:
    evals = tmp_path / "evals"
    images = evals / "images"
    images.mkdir(parents=True)
    image = images / "meal.png"
    image.write_bytes(b"synthetic image")
    cases_path = evals / "cases.jsonl"
    case = {
        "id": "photo",
        "image": "images/meal.png",
        "expected": {"is_food": True},
    }

    snapshot = build_dataset_snapshot([case], cases_path)
    case["expected"]["is_food"] = False
    image.write_bytes(b"changed")

    assert snapshot[0]["expected"]["is_food"] is True
    assert snapshot[0]["image_sha256"] == hashlib.sha256(b"synthetic image").hexdigest()


class _PassingAnalyzer:
    def __init__(self, *args: object) -> None:
        pass

    def analyze(self, *args: object) -> AnalysisResult:
        return AnalysisResult(
            analysis=FoodAnalysis(is_food=False, meal_name="", items=[]),
            metadata=LLMMetadata(
                model="test-model", effort="none", input_tokens=2, output_tokens=1
            ),
        )


class _FailingAnalyzer(_PassingAnalyzer):
    def analyze(self, *args: object) -> AnalysisResult:
        raise AnalysisError("controlled failure")


class _CapturingAnalyzer(_PassingAnalyzer):
    init_args: tuple[object, ...] = ()

    def __init__(self, *args: object) -> None:
        type(self).init_args = args


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    analyzer: type[_PassingAnalyzer],
    *extra: str,
) -> tuple[int, list[Path], Path]:
    cases_path = tmp_path / "cases.jsonl"
    dataset = '{"id":"not_food","text":"hello","expected":{"is_food":false}}\n'
    cases_path.write_text(dataset, encoding="utf-8")
    reports = tmp_path / "eval-results" / "runs"
    monkeypatch.setattr(eval_llm, "DEFAULT_REPORTS", reports)
    monkeypatch.setattr(eval_llm, "OpenAIAnalyzer", analyzer)
    monkeypatch.setattr(eval_llm, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval_llm", "--cases", str(cases_path), "--confirm", *extra],
    )
    result = eval_llm.main()
    return result, sorted(reports.glob("*.json")), cases_path


def test_confirmed_eval_writes_schema_v2_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    explicit_report = tmp_path / "explicit" / "report.json"
    result, reports, cases_path = _run_main(
        monkeypatch,
        tmp_path,
        _PassingAnalyzer,
        "--name",
        "Після prompt",
        "--report",
        str(explicit_report),
    )

    report = json.loads(reports[0].read_text(encoding="utf-8"))
    serialized = json.dumps(report)
    assert result == 0
    assert len(reports) == 1
    assert report["schema_version"] == 2
    assert report["name"] == "Після prompt"
    assert (
        report["dataset_sha256"] == hashlib.sha256(cases_path.read_bytes()).hexdigest()
    )
    assert (
        report["prompt_sha256"]
        == hashlib.sha256(eval_llm.SYSTEM_PROMPT.encode()).hexdigest()
    )
    assert report["dataset_snapshot"][0]["id"] == "not_food"
    assert report["configurations"][0]["results"][0]["repeat_index"] == 0
    assert report["configurations"][0]["results"][0]["actual"]["is_food"] is False
    assert json.loads(explicit_report.read_text(encoding="utf-8")) == report
    assert "test-only-key" not in serialized
    assert "base64" not in serialized


def test_confirmed_eval_passes_configured_timeout_to_analyzer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "45")

    result, _, _ = _run_main(monkeypatch, tmp_path, _CapturingAnalyzer)

    assert result == 0
    assert _CapturingAnalyzer.init_args[3] == 45


def test_failed_eval_still_writes_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, reports, _ = _run_main(monkeypatch, tmp_path, _FailingAnalyzer)

    report = json.loads(reports[0].read_text(encoding="utf-8"))
    failure = report["configurations"][0]["results"][0]
    assert result == 1
    assert report["passed"] is False
    assert failure["error"] == {
        "type": "AnalysisError",
        "message": "controlled failure",
    }


def test_unconfirmed_eval_does_not_write_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        '{"id":"one","text":"hello","expected":{"is_food":false}}\n',
        encoding="utf-8",
    )
    reports = tmp_path / "runs"
    monkeypatch.setattr(eval_llm, "DEFAULT_REPORTS", reports)
    monkeypatch.setattr(eval_llm, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["eval_llm", "--cases", str(cases_path)])

    assert eval_llm.main() == 2
    assert not reports.exists()
