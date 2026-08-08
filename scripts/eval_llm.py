from __future__ import annotations

import argparse
import copy
import hashlib
import os
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from calories_bot.analyzer import (
    SYSTEM_PROMPT,
    AnalysisError,
    ModelPricing,
    NormalizedInput,
    OpenAIAnalyzer,
    normalize_input,
)
from calories_bot.models import FoodAnalysis, LLMMetadata, calculate_meal
from scripts.eval_storage import (
    DatasetValidationError,
    atomic_write_json,
    read_dataset,
    safe_image_path,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "cases.jsonl"
DEFAULT_REPORTS = ROOT / "eval-results" / "runs"
ALLOWED_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    actual: str
    expected: str


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    repeat_index: int
    passed: bool
    hard_failure: bool
    latency_seconds: float
    checks: list[CheckResult]
    normalized_input: dict[str, Any]
    actual: dict[str, Any] | None
    error: dict[str, Any] | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: str | None


def _optional_decimal(name: str) -> Decimal | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise SystemExit(f"{name} must be a decimal number") from exc
    if not value.is_finite() or value < 0:
        raise SystemExit(f"{name} must be a non-negative finite number")
    return value


def pricing_from_env() -> ModelPricing:
    return ModelPricing(
        input_per_1m=_optional_decimal("OPENAI_INPUT_COST_PER_1M"),
        cached_input_per_1m=_optional_decimal("OPENAI_CACHED_INPUT_COST_PER_1M"),
        output_per_1m=_optional_decimal("OPENAI_OUTPUT_COST_PER_1M"),
    )


def load_cases(path: Path, selected: set[str]) -> list[dict[str, Any]]:
    try:
        _, parsed = read_dataset(path)
    except DatasetValidationError as exc:
        raise SystemExit(str(exc)) from exc
    all_cases = [case for _, case in parsed]
    seen = {case["id"] for case in all_cases}
    cases = [case for case in all_cases if not selected or case["id"] in selected]
    missing = selected.difference(seen)
    if missing:
        raise SystemExit("Unknown case IDs: " + ", ".join(sorted(missing)))
    if not cases:
        raise SystemExit("No eval cases selected")
    return cases


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def create_run_identity(started_at: datetime | None = None) -> tuple[str, str, str]:
    started = (started_at or _utc_now()).astimezone(UTC)
    compact = started.strftime("%Y%m%dT%H%M%SZ")
    return (
        compact + "-" + secrets.token_hex(3),
        _iso_utc(started),
        started.strftime("Run %Y-%m-%d %H:%M UTC"),
    )


def _project_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def _git_metadata() -> dict[str, Any]:
    metadata: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if commit.returncode == 0:
            metadata["commit"] = commit.stdout.strip() or None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if status.returncode == 0:
            metadata["dirty"] = bool(status.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return metadata


def _normalized_input_dict(normalized: NormalizedInput) -> dict[str, Any]:
    return {
        "text": normalized.text,
        "original_text": normalized.original_text,
        "explicit_values": [asdict(value) for value in normalized.explicit_values],
        "household_portions": [
            asdict(portion) for portion in normalized.household_portions
        ],
    }


def serialize_analysis(analysis: FoodAnalysis) -> dict[str, Any]:
    """Serialize diagnostic fields, including Pydantic-excluded source IDs."""
    return {
        "is_food": analysis.is_food,
        "meal_name": analysis.meal_name,
        "items": [
            {
                "name": item.name,
                "weight_g": item.weight_g,
                "weight_estimated": item.weight_estimated,
                "weight_origin": item.weight_origin,
                "weight_source_id": item.weight_source_id,
                "kcal_per_100g": item.kcal_per_100g,
                "kcal_estimated": item.kcal_estimated,
                "kcal_origin": item.kcal_origin,
                "kcal_source_id": item.kcal_source_id,
                "portion_display": item.portion_display,
            }
            for item in analysis.items
        ],
    }


def build_dataset_snapshot(
    cases: list[dict[str, Any]], cases_path: Path
) -> list[dict[str, Any]]:
    snapshot = copy.deepcopy(cases)
    for case in snapshot:
        image_name = case.get("image")
        if not image_name:
            continue
        try:
            image_path = safe_image_path(cases_path.parent, image_name)
            case["image_sha256"] = _sha256(image_path.read_bytes())
        except (DatasetValidationError, OSError) as exc:
            raise SystemExit(f"Cannot read image for {case['id']}: {exc}") from exc
    return snapshot


def _in_range(value: float, bounds: list[float]) -> bool:
    return len(bounds) == 2 and bounds[0] <= value <= bounds[1]


def _check(
    checks: list[CheckResult], name: str, passed: bool, actual: object, expected: object
) -> None:
    checks.append(
        CheckResult(
            name=name,
            passed=passed,
            actual=str(actual),
            expected=str(expected),
        )
    )


def grade_analysis(
    analysis: FoodAnalysis, expected: dict[str, Any]
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    expected_food = bool(expected["is_food"])
    _check(
        checks,
        "is_food",
        analysis.is_food == expected_food,
        analysis.is_food,
        expected_food,
    )
    if not expected_food or not analysis.is_food:
        return checks

    item_bounds = expected.get("item_count")
    if item_bounds is not None:
        _check(
            checks,
            "item_count",
            _in_range(len(analysis.items), item_bounds),
            len(analysis.items),
            item_bounds,
        )

    searchable = " ".join(
        [analysis.meal_name, *(item.name for item in analysis.items)]
    ).casefold()
    for alternatives in expected.get("required_terms", []):
        found = any(str(term).casefold() in searchable for term in alternatives)
        _check(checks, "required_term", found, searchable, alternatives)

    portion_terms = expected.get("portion_terms")
    if portion_terms:
        portions = " ".join(
            item.portion_display or "" for item in analysis.items
        ).casefold()
        found = any(str(term).casefold() in portions for term in portion_terms)
        _check(checks, "portion", found, portions or "<empty>", portion_terms)

    if len(analysis.items) == 1:
        item = analysis.items[0]
        weight_bounds = expected.get("weight_g")
        if weight_bounds is not None:
            _check(
                checks,
                "weight_g",
                _in_range(item.weight_g, weight_bounds),
                item.weight_g,
                weight_bounds,
            )
        kcal_bounds = expected.get("kcal_per_100g")
        if kcal_bounds is not None:
            _check(
                checks,
                "kcal_per_100g",
                _in_range(item.kcal_per_100g, kcal_bounds),
                item.kcal_per_100g,
                kcal_bounds,
            )

    meal_bounds = expected.get("meal_kcal")
    if meal_bounds is not None:
        meal = calculate_meal(analysis)
        _check(
            checks,
            "meal_kcal",
            _in_range(meal.meal_kcal, meal_bounds),
            round(meal.meal_kcal, 2),
            meal_bounds,
        )
    return checks


def check_explicit_sources(analysis: FoodAnalysis, text: str) -> list[CheckResult]:
    normalized = normalize_input(text)
    checks: list[CheckResult] = []
    for source in normalized.explicit_values:
        if source.kind == "weight":
            matches = [
                item
                for item in analysis.items
                if item.weight_source_id == source.source_id
                and item.weight_g == source.value
                and not item.weight_estimated
                and item.weight_origin == "user_text"
            ]
        else:
            matches = [
                item
                for item in analysis.items
                if item.kcal_source_id == source.source_id
                and item.kcal_per_100g == source.value
                and not item.kcal_estimated
                and item.kcal_origin == "user_text"
            ]
        _check(
            checks,
            f"explicit_{source.source_id}",
            len(matches) == 1,
            len(matches),
            "exactly one authoritative assignment",
        )
    return checks


def _metadata_values(
    metadata: LLMMetadata,
) -> tuple[int | None, int | None, str | None]:
    cost = str(metadata.llm_cost_usd) if metadata.llm_cost_usd is not None else None
    return metadata.input_tokens, metadata.output_tokens, cost


def run_case(
    analyzer: OpenAIAnalyzer,
    case: dict[str, Any],
    cases_path: Path,
    repeat_index: int = 0,
) -> CaseResult:
    case_id = str(case["id"])
    text = str(case.get("text", ""))
    image_bytes: bytes | None = None
    image_name = case.get("image")
    if image_name:
        try:
            image_path = safe_image_path(cases_path.parent, image_name)
            image_bytes = image_path.read_bytes()
        except (DatasetValidationError, OSError) as exc:
            raise SystemExit(f"Cannot read image for {case_id}: {image_name}") from exc
    if text:
        normalized = normalize_input(text)
    else:
        normalized = NormalizedInput(
            text="",
            explicit_values=(),
            original_text="",
            household_portions=(),
        )

    started = time.perf_counter()
    try:
        result = analyzer.analyze(normalized, image_bytes)
        checks = grade_analysis(result.analysis, case["expected"])
        if text:
            checks.extend(check_explicit_sources(result.analysis, text))
        input_tokens, output_tokens, cost = _metadata_values(result.metadata)
        passed = all(check.passed for check in checks)
        return CaseResult(
            case_id=case_id,
            repeat_index=repeat_index,
            passed=passed,
            hard_failure=False,
            latency_seconds=time.perf_counter() - started,
            checks=checks,
            normalized_input=_normalized_input_dict(normalized),
            actual=serialize_analysis(result.analysis),
            error=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
    except AnalysisError as exc:
        return CaseResult(
            case_id=case_id,
            repeat_index=repeat_index,
            passed=False,
            hard_failure=True,
            latency_seconds=time.perf_counter() - started,
            checks=[],
            normalized_input=_normalized_input_dict(normalized),
            actual=None,
            error={"type": type(exc).__name__, "message": str(exc)},
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
        )


def parse_config(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        raise argparse.ArgumentTypeError("use MODEL:EFFORT")
    model, effort = raw.rsplit(":", 1)
    if not model or effort not in ALLOWED_EFFORTS:
        raise argparse.ArgumentTypeError(
            "effort must be one of: " + ", ".join(sorted(ALLOWED_EFFORTS))
        )
    return model, effort


def _print_case(result: CaseResult) -> None:
    marker = "PASS" if result.passed else "FAIL"
    print(f"  {marker} {result.case_id} ({result.latency_seconds:.1f}s)")
    if result.error:
        print(f"       {result.error['message']}")
    for check in result.checks:
        if not check.passed:
            print(f"       {check.name}: got {check.actual}; expected {check.expected}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the stable, paid LLM eval suite for calories-bot"
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case", action="append", default=[], dest="case_ids")
    parser.add_argument(
        "--config",
        action="append",
        type=parse_config,
        metavar="MODEL:EFFORT",
        help="repeat to compare model/effort configurations",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--min-pass-rate", type=float, default=0.90)
    parser.add_argument("--name", help="human-readable name for this eval run")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="confirm that real, billable OpenAI API requests may be sent",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.repeat < 1 or not 0 <= args.min_pass_rate <= 1:
        raise SystemExit("--repeat must be positive and pass rate must be from 0 to 1")
    if args.name is not None and not args.name.strip():
        raise SystemExit("--name must not be empty")

    load_dotenv(os.getenv("CALORIES_BOT_ENV_FILE") or None)
    cases_path = args.cases.resolve()
    cases = load_cases(cases_path, set(args.case_ids))
    configs = args.config or [
        (
            os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
            os.getenv("OPENAI_REASONING_EFFORT", "none"),
        )
    ]
    requests = len(cases) * len(configs) * args.repeat
    print(
        f"Plan: {len(cases)} cases × {len(configs)} configs × "
        f"{args.repeat} repeat(s) = {requests} paid request(s)."
    )
    if not args.confirm:
        print("No requests sent. Re-run with --confirm after reviewing the plan.")
        return 2

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is missing")
    run_id, started_at, automatic_name = create_run_identity()
    run_name = args.name.strip() if args.name is not None else automatic_name
    try:
        dataset_bytes = cases_path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"Cannot read eval dataset: {cases_path}") from exc
    dataset_snapshot = build_dataset_snapshot(cases, cases_path)
    configured_model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    configured_pricing = pricing_from_env()
    unknown_pricing = ModelPricing(None, None, None)
    report_configurations: list[dict[str, Any]] = []
    overall_passed = True

    for model, effort in configs:
        print(f"\n{model}:{effort}")
        pricing = configured_pricing if model == configured_model else unknown_pricing
        analyzer = OpenAIAnalyzer(api_key, model, effort, pricing)
        results: list[CaseResult] = []
        for repeat_index in range(args.repeat):
            for case in cases:
                result = run_case(analyzer, case, cases_path, repeat_index=repeat_index)
                results.append(result)
                _print_case(result)

        passed_count = sum(result.passed for result in results)
        pass_rate = passed_count / len(results)
        hard_failures = sum(result.hard_failure for result in results)
        total_input = sum(result.input_tokens or 0 for result in results)
        total_output = sum(result.output_tokens or 0 for result in results)
        known_costs = [
            Decimal(result.cost_usd) for result in results if result.cost_usd
        ]
        total_cost = sum(known_costs, Decimal()) if known_costs else None
        average_latency = sum(result.latency_seconds for result in results) / len(
            results
        )
        config_passed = pass_rate >= args.min_pass_rate and hard_failures == 0
        overall_passed = overall_passed and config_passed
        print(
            f"  Result: {passed_count}/{len(results)} ({pass_rate:.0%}), "
            f"hard failures={hard_failures}, avg={average_latency:.1f}s, "
            f"tokens={total_input}+{total_output}, "
            f"cost=${total_cost if total_cost is not None else 'unknown'}"
        )
        report_configurations.append(
            {
                "model": model,
                "effort": effort,
                "passed": config_passed,
                "pass_rate": pass_rate,
                "hard_failures": hard_failures,
                "average_latency_seconds": average_latency,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cost_usd": str(total_cost) if total_cost is not None else None,
                "results": [asdict(result) for result in results],
            }
        )

    report = {
        "schema_version": 2,
        "run_id": run_id,
        "name": run_name,
        "started_at": started_at,
        "finished_at": _iso_utc(_utc_now()),
        "git": _git_metadata(),
        "prompt_sha256": _sha256(SYSTEM_PROMPT.encode("utf-8")),
        "dataset_sha256": _sha256(dataset_bytes),
        "cases_path": _project_path(cases_path),
        "minimum_pass_rate": args.min_pass_rate,
        "passed": overall_passed,
        "dataset_snapshot": dataset_snapshot,
        "configurations": report_configurations,
    }
    canonical_report = DEFAULT_REPORTS / f"{run_id}.json"
    atomic_write_json(canonical_report, report)
    print(f"\nHistorical report: {canonical_report}")
    if args.report and args.report.resolve() != canonical_report.resolve():
        atomic_write_json(args.report, report)
        print(f"Report copy: {args.report}")
    return 0 if overall_passed else 1


if __name__ == "__main__":
    sys.exit(main())
