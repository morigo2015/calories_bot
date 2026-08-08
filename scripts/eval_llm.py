from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from calories_bot.analyzer import (
    AnalysisError,
    ModelPricing,
    NormalizedInput,
    OpenAIAnalyzer,
    normalize_input,
)
from calories_bot.models import FoodAnalysis, LLMMetadata, calculate_meal

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "cases.jsonl"
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
    passed: bool
    hard_failure: bool
    latency_seconds: float
    checks: list[CheckResult]
    error: str | None
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
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"Invalid JSON at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(case, dict) or not isinstance(case.get("id"), str):
                raise SystemExit(f"Case at {path}:{line_number} needs a string id")
            case_id = case["id"]
            if case_id in seen:
                raise SystemExit(f"Duplicate case id: {case_id}")
            seen.add(case_id)
            if not isinstance(case.get("text", ""), str):
                raise SystemExit(f"Case {case_id} needs a string text field")
            if (
                not isinstance(case.get("expected"), dict)
                or "is_food" not in case["expected"]
            ):
                raise SystemExit(f"Case {case_id} needs an expected object")
            if not selected or case_id in selected:
                cases.append(case)
    missing = selected - seen
    if missing:
        raise SystemExit("Unknown case IDs: " + ", ".join(sorted(missing)))
    if not cases:
        raise SystemExit("No eval cases selected")
    return cases


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
    analyzer: OpenAIAnalyzer, case: dict[str, Any], cases_path: Path
) -> CaseResult:
    case_id = str(case["id"])
    text = str(case.get("text", ""))
    image_bytes: bytes | None = None
    image_name = case.get("image")
    if image_name:
        image_path = (cases_path.parent / str(image_name)).resolve()
        if not image_path.is_relative_to(cases_path.parent.resolve()):
            raise SystemExit(f"Case {case_id} image escapes the eval directory")
        try:
            image_bytes = image_path.read_bytes()
        except OSError as exc:
            raise SystemExit(f"Cannot read image for {case_id}: {image_path}") from exc
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
            passed=passed,
            hard_failure=False,
            latency_seconds=time.perf_counter() - started,
            checks=checks,
            error=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
    except AnalysisError as exc:
        return CaseResult(
            case_id=case_id,
            passed=False,
            hard_failure=True,
            latency_seconds=time.perf_counter() - started,
            checks=[],
            error=str(exc),
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
        print(f"       {result.error}")
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
    configured_model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    configured_pricing = pricing_from_env()
    unknown_pricing = ModelPricing(None, None, None)
    report_runs: list[dict[str, Any]] = []
    overall_passed = True

    for model, effort in configs:
        print(f"\n{model}:{effort}")
        pricing = configured_pricing if model == configured_model else unknown_pricing
        analyzer = OpenAIAnalyzer(api_key, model, effort, pricing)
        results: list[CaseResult] = []
        for _ in range(args.repeat):
            for case in cases:
                result = run_case(analyzer, case, cases_path)
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
        report_runs.append(
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

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "cases": str(cases_path),
                    "minimum_pass_rate": args.min_pass_rate,
                    "passed": overall_passed,
                    "runs": report_runs,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nReport: {args.report}")
    return 0 if overall_passed else 1


if __name__ == "__main__":
    sys.exit(main())
