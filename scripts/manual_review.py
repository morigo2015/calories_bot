from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from calories_bot.quality import BatchKind, QualityStore
from scripts.eval_storage import atomic_write_json

SCHEMA_VERSION = 1
VERDICTS = {"correct", "minor_error", "major_error", "uncertain"}
ISSUE_TYPES = {
    "none",
    "explicit_value",
    "basis",
    "assignment",
    "components",
    "recognition",
    "other",
}
AUDIT_MARKER = re.compile(
    r"\d|ккал|калор|біл|бел|жир|вугл|углев|100\s*г|порц|ваг",
    re.IGNORECASE,
)


class ManualReviewError(ValueError):
    """Raised when a manual-review batch or response is invalid."""


def is_audit_candidate(case: dict[str, Any]) -> bool:
    normalized = case.get("normalized_input")
    explicit = (
        normalized.get("explicit_values", []) if isinstance(normalized, dict) else []
    )
    analysis = case.get("analysis")
    items = analysis.get("items", []) if isinstance(analysis, dict) else []
    return (
        case.get("input_kind") in {"photo", "voice"}
        or bool(explicit)
        or len(items) > 1
        or bool(AUDIT_MARKER.search(str(case.get("original_text", ""))))
    )


def _judge_case(case: dict[str, Any], kind: BatchKind) -> dict[str, Any]:
    value = {
        "case_id": case["case_id"],
        "input_kind": case["input_kind"],
        "input": case["original_text"],
        "normalized_input": case["normalized_input"],
        "analysis": case["analysis"],
        "calculated_meal": case["meal"],
        "reply_to_user": case["rendered_reply"],
    }
    if case.get("photo_path"):
        value["photo_note"] = (
            "The source includes a photo. Upload it to ChatGPT separately if visual "
            "recognition must be judged."
        )
    if kind == "reported":
        value["user_feedback"] = case.get("explanation") or (
            "The user marked the analysis as wrong without an explanation."
        )
    return value


def render_review_prompt(
    batch_id: str, kind: BatchKind, cases: list[dict[str, Any]]
) -> str:
    mode = (
        "These cases were reported by users. Treat feedback as evidence, not as "
        "automatic ground truth."
        if kind == "reported"
        else (
            "This is a blind audit. Model name, user identity, and user feedback "
            "are hidden."
        )
    )
    payload = [_judge_case(case, kind) for case in cases]
    return f"""You are reviewing food-analysis results for a home calorie bot.

{mode}

Important product rule: do not penalize reasonable estimates when the user did not
provide the information. Mark an error when explicit input was missed, assigned to
the wrong food, read with the wrong per-100-g/portion basis, components were parsed
incorrectly, or the food/photo was recognized incorrectly.

Use verdicts: correct, minor_error, major_error, uncertain.
Use issue_type: none, explicit_value, basis, assignment, components, recognition,
or other. For correct verdict use issue_type=none. Keep explanations concise.
suggested_eval_case may be null or a complete draft object with id, text and expected.

Return exactly one JSON object, without commentary or Markdown:
{{"schema_version":1,"batch_id":"{batch_id}","reviews":[
  {{"case_id":"...","verdict":"correct|minor_error|major_error|uncertain",
    "issue_type":"none|explicit_value|basis|assignment|components|recognition|other",
    "explanation":"...","suggested_eval_case":null}}
]}}

Cases:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def create_review_batch(
    store: QualityStore,
    kind: BatchKind,
    batch_size: int,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not 1 <= batch_size <= 10:
        raise ManualReviewError("Batch size must be from 1 to 10")
    candidates = store.list_cases(kind, limit=100)
    if kind == "audit":
        candidates = [case for case in candidates if is_audit_candidate(case)]
    cases = candidates[:batch_size]
    if not cases:
        raise ManualReviewError(f"No {kind} cases are waiting for review")
    created_at = now or datetime.now(UTC)
    batch_id = store.create_batch(
        kind, [str(case["case_id"]) for case in cases], created_at
    )
    prompt = render_review_prompt(batch_id, kind, cases)
    batch_dir = output_dir / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    (batch_dir / "request.md").write_text(prompt, encoding="utf-8")
    atomic_write_json(
        batch_dir / "cases.json",
        {
            "schema_version": SCHEMA_VERSION,
            "batch_id": batch_id,
            "kind": kind,
            "cases": [_judge_case(case, kind) for case in cases],
        },
    )
    return {
        "batch_id": batch_id,
        "kind": kind,
        "cases": cases,
        "prompt": prompt,
        "batch_dir": batch_dir,
    }


def _strip_outer_fence(raw: str) -> str:
    value = raw.strip()
    match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL | re.IGNORECASE
    )
    return match.group(1).strip() if match else value


def validate_review_result(
    raw: str, batch_id: str, expected_case_ids: list[str]
) -> dict[str, Any]:
    try:
        value = json.loads(_strip_outer_fence(raw))
    except json.JSONDecodeError as exc:
        raise ManualReviewError(f"Invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ManualReviewError("Result must be a JSON object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ManualReviewError("schema_version must be 1")
    if value.get("batch_id") != batch_id:
        raise ManualReviewError("batch_id does not match")
    reviews = value.get("reviews")
    if not isinstance(reviews, list):
        raise ManualReviewError("reviews must be a list")
    ids = [review.get("case_id") for review in reviews if isinstance(review, dict)]
    if ids != expected_case_ids:
        raise ManualReviewError(
            "Reviews must contain every batch case exactly once, in the original order"
        )
    for review in reviews:
        if not isinstance(review, dict):
            raise ManualReviewError("Every review must be an object")
        if review.get("verdict") not in VERDICTS:
            raise ManualReviewError(f"Invalid verdict for {review.get('case_id')}")
        if review.get("issue_type") not in ISSUE_TYPES:
            raise ManualReviewError(f"Invalid issue_type for {review.get('case_id')}")
        if review["verdict"] == "correct" and review["issue_type"] != "none":
            raise ManualReviewError("A correct review must use issue_type=none")
        explanation = review.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ManualReviewError("Every review needs an explanation")
        suggestion = review.get("suggested_eval_case")
        if suggestion is not None and not isinstance(suggestion, dict):
            raise ManualReviewError("suggested_eval_case must be an object or null")
    return value


def import_review_result(
    store: QualityStore,
    batch_id: str,
    raw: str,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise ManualReviewError("Unknown review batch")
    value = validate_review_result(raw, batch_id, list(batch["case_ids"]))
    imported_at = now or datetime.now(UTC)
    store.import_reviews(batch_id, value["reviews"], imported_at)
    batch_dir = output_dir / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(batch_dir / "result.json", value)
    return value


def review_to_eval_draft(review: dict[str, Any]) -> dict[str, Any]:
    suggestion = review.get("suggested_eval_case")
    if isinstance(suggestion, dict):
        return suggestion
    return {
        "id": f"quality-{str(review['case_id'])[:12]}",
        "text": str(review.get("original_text", "")),
        "expected": {"is_food": True},
    }
