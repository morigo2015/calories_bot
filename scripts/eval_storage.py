from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

RANGE_FIELDS = {"item_count", "weight_g", "kcal_per_100g", "meal_kcal"}


class DatasetValidationError(ValueError):
    """Raised when an eval dataset cannot be safely loaded or saved."""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_image_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DatasetValidationError("image must be a non-empty string")
    if "\\" in value:
        raise DatasetValidationError("image must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("images",):
        raise DatasetValidationError("image must be a safe path inside images/")
    return value


def safe_image_path(evals_dir: Path, value: object) -> Path:
    relative = validate_image_relative_path(value)
    images_dir = (evals_dir / "images").resolve()
    candidate = (evals_dir / relative).resolve()
    if not candidate.is_relative_to(images_dir):
        raise DatasetValidationError("image path escapes evals/images/")
    return candidate


def validate_case(case: object) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise DatasetValidationError("case JSON must be an object")
    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise DatasetValidationError("id must be a non-empty string")
    if "text" in case and not isinstance(case["text"], str):
        raise DatasetValidationError(f"case {case_id}: text must be a string")
    if "image" in case:
        try:
            validate_image_relative_path(case["image"])
        except DatasetValidationError as exc:
            raise DatasetValidationError(f"case {case_id}: {exc}") from exc
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise DatasetValidationError(f"case {case_id}: expected must be an object")
    if not isinstance(expected.get("is_food"), bool):
        raise DatasetValidationError(
            f"case {case_id}: expected.is_food must be a boolean"
        )
    for name in RANGE_FIELDS:
        if name not in expected:
            continue
        bounds = expected[name]
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or not all(_is_number(item) for item in bounds)
            or bounds[0] > bounds[1]
        ):
            raise DatasetValidationError(
                f"case {case_id}: expected.{name} must be [min, max] with min <= max"
            )
    return case


def parse_dataset_lines(lines: list[str]) -> list[tuple[int, dict[str, Any]]]:
    parsed: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetValidationError(
                f"invalid JSON on line {index + 1}: {exc.msg}"
            ) from exc
        case = validate_case(value)
        case_id = case["id"]
        if case_id in seen:
            raise DatasetValidationError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        parsed.append((index, case))
    if not parsed:
        raise DatasetValidationError("dataset must contain at least one case")
    return parsed


def read_dataset(path: Path) -> tuple[list[str], list[tuple[int, dict[str, Any]]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetValidationError(f"cannot read dataset: {exc}") from exc
    lines = text.splitlines(keepends=True)
    return lines, parse_dataset_lines(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current_mode = stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR
    except FileNotFoundError:
        current_mode = 0o644
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, current_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: object) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare_dataset_case(
    path: Path, original_id: str | None, raw_json: str
) -> tuple[dict[str, Any], str]:
    try:
        value = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise DatasetValidationError(f"invalid JSON: {exc.msg}") from exc
    case = validate_case(value)
    lines, parsed = read_dataset(path)
    ids = {item["id"] for _, item in parsed}

    serialized = json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n"
    if original_id is None:
        if case["id"] in ids:
            raise DatasetValidationError(f"duplicate case id: {case['id']}")
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.append(serialized)
    else:
        matches = [(index, item) for index, item in parsed if item["id"] == original_id]
        if not matches:
            raise DatasetValidationError(f"case not found: {original_id}")
        if case["id"] != original_id and case["id"] in ids:
            raise DatasetValidationError(f"duplicate case id: {case['id']}")
        lines[matches[0][0]] = serialized

    parse_dataset_lines(lines)
    return case, "".join(lines)


def save_dataset_case(
    path: Path, original_id: str | None, raw_json: str
) -> dict[str, Any]:
    case, text = prepare_dataset_case(path, original_id, raw_json)
    _atomic_write(path, text)
    return case
