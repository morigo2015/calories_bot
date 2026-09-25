from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from .analyzer import NormalizedInput
from .models import FoodAnalysis, MealResult

InputKind = Literal["text", "photo", "voice"]
BatchKind = Literal["reported", "audit"]
Verdict = Literal["correct", "minor_error", "major_error", "uncertain"]
IssueType = Literal[
    "none",
    "explicit_value",
    "basis",
    "assignment",
    "components",
    "recognition",
    "other",
]


class QualityStoreError(RuntimeError):
    """Raised when durable quality data cannot be read or written."""


@dataclass(frozen=True)
class FeedbackResult:
    case_id: str
    created: bool
    feedback_prompt_message_id: int | None


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def serialize_normalized_input(normalized: NormalizedInput) -> dict[str, Any]:
    return {
        "text": normalized.text,
        "original_text": normalized.original_text,
        "explicit_values": [asdict(value) for value in normalized.explicit_values],
        "household_portions": [
            asdict(portion) for portion in normalized.household_portions
        ],
    }


def serialize_food_analysis(analysis: FoodAnalysis) -> dict[str, Any]:
    """Keep all diagnostic fields, including Pydantic-excluded source IDs."""
    nutrient_fields = ("protein", "fat", "carbs")
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
                "kcal_source_basis": item.kcal_source_basis,
                **{
                    f"{name}_per_100g": getattr(item, f"{name}_per_100g")
                    for name in nutrient_fields
                },
                **{
                    f"{name}_estimated": getattr(item, f"{name}_estimated")
                    for name in nutrient_fields
                },
                **{
                    f"{name}_origin": getattr(item, f"{name}_origin")
                    for name in nutrient_fields
                },
                **{
                    f"{name}_source_id": getattr(item, f"{name}_source_id")
                    for name in nutrient_fields
                },
                **{
                    f"{name}_source_basis": getattr(item, f"{name}_source_basis")
                    for name in nutrient_fields
                },
                "portion_display": item.portion_display,
            }
            for item in analysis.items
        ],
        "portion_nutrition": (
            None
            if analysis.portion_nutrition is None
            else {
                "kcal": analysis.portion_nutrition.kcal,
                "protein_g": analysis.portion_nutrition.protein_g,
                "fat_g": analysis.portion_nutrition.fat_g,
                "carbs_g": analysis.portion_nutrition.carbs_g,
                "kcal_source_id": analysis.portion_nutrition.kcal_source_id,
                "protein_source_id": analysis.portion_nutrition.protein_source_id,
                "fat_source_id": analysis.portion_nutrition.fat_source_id,
                "carbs_source_id": analysis.portion_nutrition.carbs_source_id,
            }
        ),
    }


class QualityStore:
    """Immutable food-analysis cases plus user feedback and manual reviews."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS quality_cases (
                        case_id TEXT PRIMARY KEY,
                        recorded_at TEXT NOT NULL,
                        telegram_user_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        accounting_day TEXT NOT NULL,
                        operation TEXT NOT NULL CHECK (operation = 'food_analysis'),
                        input_kind TEXT NOT NULL
                            CHECK (input_kind IN ('text', 'photo', 'voice')),
                        original_text TEXT NOT NULL,
                        normalized_input_json TEXT NOT NULL,
                        analysis_json TEXT NOT NULL,
                        meal_json TEXT NOT NULL,
                        rendered_reply TEXT NOT NULL,
                        photo_path TEXT,
                        photo_sha256 TEXT,
                        model TEXT NOT NULL,
                        effort TEXT NOT NULL,
                        prompt_sha256 TEXT NOT NULL,
                        app_version TEXT NOT NULL,
                        UNIQUE (
                            telegram_user_id, source_message_id,
                            accounting_day, operation
                        )
                    );
                    CREATE INDEX IF NOT EXISTS quality_cases_recorded_at
                        ON quality_cases(recorded_at);
                    CREATE INDEX IF NOT EXISTS quality_cases_user_message
                        ON quality_cases(
                            telegram_user_id, source_message_id, accounting_day
                        );

                    CREATE TABLE IF NOT EXISTS analysis_feedback (
                        case_id TEXT PRIMARY KEY
                            REFERENCES quality_cases(case_id) ON DELETE CASCADE,
                        reported_at TEXT NOT NULL,
                        feedback_prompt_message_id INTEGER,
                        explanation TEXT,
                        explanation_received_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS analysis_feedback_prompt
                        ON analysis_feedback(feedback_prompt_message_id);

                    CREATE TABLE IF NOT EXISTS manual_review_batches (
                        batch_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL CHECK (kind IN ('reported', 'audit')),
                        created_at TEXT NOT NULL,
                        case_ids_json TEXT NOT NULL,
                        imported_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS manual_reviews (
                        batch_id TEXT NOT NULL
                            REFERENCES manual_review_batches(batch_id)
                            ON DELETE CASCADE,
                        case_id TEXT NOT NULL
                            REFERENCES quality_cases(case_id) ON DELETE CASCADE,
                        verdict TEXT NOT NULL CHECK (
                            verdict IN (
                                'correct', 'minor_error', 'major_error', 'uncertain'
                            )
                        ),
                        issue_type TEXT NOT NULL CHECK (
                            issue_type IN (
                                'none', 'explicit_value', 'basis', 'assignment',
                                'components', 'recognition', 'other'
                            )
                        ),
                        explanation TEXT NOT NULL,
                        suggested_eval_case_json TEXT,
                        imported_at TEXT NOT NULL,
                        PRIMARY KEY (batch_id, case_id)
                    );
                    CREATE INDEX IF NOT EXISTS manual_reviews_case
                        ON manual_reviews(case_id);
                    """
                )
        except sqlite3.Error as exc:
            raise QualityStoreError("Could not initialize quality storage") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _case_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("normalized_input_json", "analysis_json", "meal_json"):
            if field in result:
                result[field.removesuffix("_json")] = json.loads(result.pop(field))
        if "suggested_eval_case_json" in result:
            raw = result.pop("suggested_eval_case_json")
            result["suggested_eval_case"] = json.loads(raw) if raw else None
        return result

    def record_case(
        self,
        *,
        recorded_at: datetime,
        telegram_user_id: int,
        source_message_id: int,
        accounting_day: date,
        input_kind: InputKind,
        original_text: str,
        normalized: NormalizedInput,
        analysis: FoodAnalysis,
        meal: MealResult,
        rendered_reply: str,
        photo_path: str | None,
        image_bytes: bytes | None,
        model: str,
        effort: str,
        prompt_sha256: str,
        app_version: str,
    ) -> str:
        case_id = secrets.token_hex(12)
        values = (
            case_id,
            _utc_iso(recorded_at),
            telegram_user_id,
            source_message_id,
            accounting_day.isoformat(),
            "food_analysis",
            input_kind,
            original_text,
            _json(serialize_normalized_input(normalized)),
            _json(serialize_food_analysis(analysis)),
            _json(meal.model_dump(mode="json")),
            rendered_reply,
            photo_path,
            hashlib.sha256(image_bytes).hexdigest()
            if image_bytes is not None
            else None,
            model,
            effort,
            prompt_sha256,
            app_version,
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO quality_cases (
                        case_id, recorded_at, telegram_user_id, source_message_id,
                        accounting_day, operation, input_kind, original_text,
                        normalized_input_json, analysis_json, meal_json,
                        rendered_reply, photo_path, photo_sha256, model, effort,
                        prompt_sha256, app_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                row = connection.execute(
                    """
                    SELECT case_id FROM quality_cases
                    WHERE telegram_user_id = ? AND source_message_id = ?
                      AND accounting_day = ? AND operation = 'food_analysis'
                    """,
                    (telegram_user_id, source_message_id, accounting_day.isoformat()),
                ).fetchone()
                if row is None:
                    raise QualityStoreError("Could not resolve stored quality case")
                return str(row["case_id"])
        except sqlite3.Error as exc:
            raise QualityStoreError("Could not record quality case") from exc

    def has_case(
        self, telegram_user_id: int, source_message_id: int, accounting_day: date
    ) -> bool:
        return (
            self.get_case(telegram_user_id, source_message_id, accounting_day)
            is not None
        )

    def can_report_case(
        self, telegram_user_id: int, source_message_id: int, accounting_day: date
    ) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT 1
                    FROM quality_cases q
                    LEFT JOIN analysis_feedback f ON f.case_id = q.case_id
                    WHERE q.telegram_user_id = ? AND q.source_message_id = ?
                      AND q.accounting_day = ? AND q.operation = 'food_analysis'
                      AND f.case_id IS NULL
                    """,
                    (telegram_user_id, source_message_id, accounting_day.isoformat()),
                ).fetchone()
                return row is not None
        except sqlite3.Error as exc:
            raise QualityStoreError("Could not check quality feedback state") from exc

    def get_case(
        self, telegram_user_id: int, source_message_id: int, accounting_day: date
    ) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM quality_cases
                    WHERE telegram_user_id = ? AND source_message_id = ?
                      AND accounting_day = ? AND operation = 'food_analysis'
                    """,
                    (telegram_user_id, source_message_id, accounting_day.isoformat()),
                ).fetchone()
                return None if row is None else self._case_from_row(row)
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise QualityStoreError("Could not read quality case") from exc

    def report_error(
        self,
        telegram_user_id: int,
        source_message_id: int,
        accounting_day: date,
        reported_at: datetime,
    ) -> FeedbackResult | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT case_id FROM quality_cases
                    WHERE telegram_user_id = ? AND source_message_id = ?
                      AND accounting_day = ? AND operation = 'food_analysis'
                    """,
                    (telegram_user_id, source_message_id, accounting_day.isoformat()),
                ).fetchone()
                if row is None:
                    return None
                case_id = str(row["case_id"])
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO analysis_feedback (case_id, reported_at)
                    VALUES (?, ?)
                    """,
                    (case_id, _utc_iso(reported_at)),
                )
                feedback = connection.execute(
                    """
                    SELECT feedback_prompt_message_id FROM analysis_feedback
                    WHERE case_id = ?
                    """,
                    (case_id,),
                ).fetchone()
                assert feedback is not None
                prompt_id = feedback["feedback_prompt_message_id"]
                return FeedbackResult(
                    case_id=case_id,
                    created=cursor.rowcount == 1,
                    feedback_prompt_message_id=(
                        int(prompt_id) if prompt_id is not None else None
                    ),
                )
        except sqlite3.Error as exc:
            raise QualityStoreError("Could not record analysis feedback") from exc

    def set_feedback_prompt(self, case_id: str, prompt_message_id: int) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE analysis_feedback
                    SET feedback_prompt_message_id = COALESCE(
                        feedback_prompt_message_id, ?
                    )
                    WHERE case_id = ?
                    """,
                    (prompt_message_id, case_id),
                )
        except sqlite3.Error as exc:
            raise QualityStoreError("Could not link feedback prompt") from exc

    def save_explanation_by_prompt(
        self,
        telegram_user_id: int,
        prompt_message_id: int,
        explanation: str,
        received_at: datetime,
    ) -> bool:
        explanation = explanation.strip()
        if not explanation:
            return False
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE analysis_feedback
                    SET explanation = ?, explanation_received_at = ?
                    WHERE feedback_prompt_message_id = ?
                      AND case_id IN (
                          SELECT case_id FROM quality_cases
                          WHERE telegram_user_id = ?
                      )
                    """,
                    (
                        explanation[:4000],
                        _utc_iso(received_at),
                        prompt_message_id,
                        telegram_user_id,
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise QualityStoreError("Could not save feedback explanation") from exc

    def list_cases(self, kind: BatchKind, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be from 1 to 1000")
        feedback_join = (
            "JOIN analysis_feedback f ON f.case_id = q.case_id"
            if kind == "reported"
            else "LEFT JOIN analysis_feedback f ON f.case_id = q.case_id"
        )
        # Keep reported and blind audit queues separate. Audit never includes a
        # user-reported case and never exposes identity or feedback to the judge.
        where = "" if kind == "reported" else "AND f.case_id IS NULL"
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT q.*, f.reported_at, f.explanation,
                           EXISTS(
                               SELECT 1 FROM manual_reviews r
                               WHERE r.case_id = q.case_id
                           ) AS reviewed
                    FROM quality_cases q
                    {feedback_join}
                    WHERE NOT EXISTS (
                        SELECT 1 FROM manual_reviews r WHERE r.case_id = q.case_id
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM manual_review_batches b, json_each(b.case_ids_json) j
                        WHERE j.value = q.case_id
                    ) {where}
                    ORDER BY q.recorded_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [self._case_from_row(row) for row in rows]
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise QualityStoreError("Could not list quality cases") from exc

    def create_batch(
        self, kind: BatchKind, case_ids: list[str], created_at: datetime
    ) -> str:
        if not case_ids or len(case_ids) != len(set(case_ids)):
            raise ValueError("A review batch needs unique case IDs")
        batch_id = (
            f"{kind}-{created_at.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        )
        try:
            with self._connect() as connection:
                placeholders = ",".join("?" for _ in case_ids)
                rows = connection.execute(
                    "SELECT case_id FROM quality_cases "
                    f"WHERE case_id IN ({placeholders})",
                    case_ids,
                ).fetchall()
                if {str(row[0]) for row in rows} != set(case_ids):
                    raise ValueError("Unknown quality case ID")
                connection.execute(
                    """
                    INSERT INTO manual_review_batches (
                        batch_id, kind, created_at, case_ids_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (batch_id, kind, _utc_iso(created_at), _json(case_ids)),
                )
                return batch_id
        except sqlite3.Error as exc:
            raise QualityStoreError("Could not create review batch") from exc

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM manual_review_batches WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                if row is None:
                    return None
                value = dict(row)
                value["case_ids"] = json.loads(value.pop("case_ids_json"))
                return value
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise QualityStoreError("Could not read review batch") from exc

    def import_reviews(
        self, batch_id: str, reviews: list[dict[str, Any]], imported_at: datetime
    ) -> None:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise ValueError("Unknown review batch")
        expected = list(batch["case_ids"])
        if [str(review.get("case_id", "")) for review in reviews] != expected:
            raise ValueError(
                "Reviews must contain every batch case exactly once, in order"
            )
        now = _utc_iso(imported_at)
        try:
            with self._connect() as connection:
                for review in reviews:
                    connection.execute(
                        """
                        INSERT INTO manual_reviews (
                            batch_id, case_id, verdict, issue_type, explanation,
                            suggested_eval_case_json, imported_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(batch_id, case_id) DO UPDATE SET
                            verdict = excluded.verdict,
                            issue_type = excluded.issue_type,
                            explanation = excluded.explanation,
                            suggested_eval_case_json =
                                excluded.suggested_eval_case_json,
                            imported_at = excluded.imported_at
                        """,
                        (
                            batch_id,
                            review["case_id"],
                            review["verdict"],
                            review["issue_type"],
                            review["explanation"],
                            _json(review["suggested_eval_case"])
                            if review.get("suggested_eval_case") is not None
                            else None,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE manual_review_batches SET imported_at = ?
                    WHERE batch_id = ?
                    """,
                    (now, batch_id),
                )
        except sqlite3.Error as exc:
            raise QualityStoreError("Could not import manual reviews") from exc

    def list_reviews(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT r.*, b.kind, q.original_text, q.input_kind,
                           q.rendered_reply, q.model, q.effort
                    FROM manual_reviews r
                    JOIN quality_cases q ON q.case_id = r.case_id
                    JOIN manual_review_batches b ON b.batch_id = r.batch_id
                    ORDER BY r.imported_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [self._case_from_row(row) for row in rows]
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise QualityStoreError("Could not list manual reviews") from exc

    def get_review(self, batch_id: str, case_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT r.*, b.kind, q.original_text, q.input_kind,
                           q.rendered_reply, q.model, q.effort
                    FROM manual_reviews r
                    JOIN quality_cases q ON q.case_id = r.case_id
                    JOIN manual_review_batches b ON b.batch_id = r.batch_id
                    WHERE r.batch_id = ? AND r.case_id = ?
                    """,
                    (batch_id, case_id),
                ).fetchone()
                return None if row is None else self._case_from_row(row)
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise QualityStoreError("Could not read manual review") from exc

    def delete_user(self, telegram_user_id: int) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM quality_cases WHERE telegram_user_id = ?",
                    (telegram_user_id,),
                )
        except sqlite3.Error as exc:
            raise QualityStoreError("Could not delete user quality data") from exc
