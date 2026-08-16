from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.eval_dashboard import (
    DEFAULT_HOST,
    DashboardApp,
    compare_configurations,
    load_report,
    render_compare,
    render_run,
)
from scripts.eval_storage import (
    DatasetValidationError,
    prepare_dataset_case,
    safe_image_path,
    save_dataset_case,
)


def _configuration(results: list[dict[str, object]]) -> dict[str, object]:
    passed = sum(result["passed"] is True for result in results)
    return {
        "model": "model<script>",
        "effort": "low",
        "pass_rate": passed / len(results),
        "hard_failures": 0,
        "average_latency_seconds": 1.5,
        "cost_usd": "0.01",
        "results": results,
    }


def _write_report(
    results_dir: Path,
    name: str,
    configuration: dict[str, object],
    *,
    dataset_hash: str = "same",
) -> Path:
    path = results_dir / "runs" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": name,
                "name": name,
                "started_at": "2026-08-08T00:00:00Z",
                "dataset_sha256": dataset_hash,
                "dataset_snapshot": [
                    {
                        "id": "case<script>",
                        "text": "<img src=x onerror=alert(1)>",
                        "expected": {"is_food": False},
                    }
                ],
                "configurations": [configuration],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_legacy_report_read_only(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text('{"passed":true,"runs":[]}', encoding="utf-8")

    report = load_report(path, tmp_path)

    assert report.legacy is True
    assert report.name == "legacy"
    assert report.timestamp == "Unknown"


def test_compare_tracks_regressions_improvements_and_missing() -> None:
    a = _configuration(
        [
            {"case_id": "regression", "repeat_index": 0, "passed": True},
            {"case_id": "improvement", "repeat_index": 0, "passed": False},
            {"case_id": "missing_b", "repeat_index": 0, "passed": True},
        ]
    )
    b = _configuration(
        [
            {"case_id": "regression", "repeat_index": 0, "passed": False},
            {"case_id": "improvement", "repeat_index": 0, "passed": True},
            {"case_id": "missing_a", "repeat_index": 0, "passed": True},
        ]
    )

    comparison = compare_configurations(a, b)
    statuses = {
        row["case_id"]: (row["status_a"], row["status_b"]) for row in comparison["rows"]
    }

    assert [row["case_id"] for row in comparison["regressions"]] == ["regression"]
    assert [row["case_id"] for row in comparison["improvements"]] == ["improvement"]
    assert statuses["missing_b"] == ("pass", "not_run")
    assert statuses["missing_a"] == ("not_run", "pass")


def test_dashboard_escapes_report_content_and_warns_on_dataset_change(
    tmp_path: Path,
) -> None:
    results_dir = tmp_path / "results"
    config_a = _configuration(
        [{"case_id": "case<script>", "repeat_index": 0, "passed": True}]
    )
    config_b = _configuration(
        [{"case_id": "case<script>", "repeat_index": 0, "passed": False}]
    )
    report_a = load_report(
        _write_report(results_dir, "A<script>", config_a, dataset_hash="a"),
        results_dir,
    )
    report_b = load_report(
        _write_report(results_dir, "B", config_b, dataset_hash="b"), results_dir
    )

    detail = render_run(report_a, 0)
    comparison = render_compare([report_a, report_b], report_a, 0, report_b, 0, False)

    assert "<script>" not in detail
    assert "&lt;script&gt;" in detail
    assert "Different datasets" in comparison
    assert "Regressions" in comparison


def _dataset(path: Path) -> None:
    path.write_text(
        "// keep this comment\n"
        '{"id":"one","text":"apple","expected":{"is_food":true,"weight_g":[1,2]}}\n'
        "\n"
        '{"id":"two","text":"hello","expected":{"is_food":false}}\n',
        encoding="utf-8",
    )


def test_dataset_edit_is_atomic_and_preserves_other_lines(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    _dataset(path)
    before = path.read_text(encoding="utf-8")

    save_dataset_case(
        path,
        "one",
        '{"id":"one","text":"pear","expected":{"is_food":true}}',
    )
    after = path.read_text(encoding="utf-8")

    assert "// keep this comment" in after
    assert '"id":"two"' in after
    assert "\n\n" in after
    assert "pear" in after
    assert before != after


@pytest.mark.parametrize(
    "raw, message",
    [
        ("not json", "invalid JSON"),
        ('{"id":"two","expected":{"is_food":true}}', "duplicate case id"),
        ('{"id":"new","expected":{"is_food":"yes"}}', "must be a boolean"),
        (
            '{"id":"new","expected":{"is_food":true,"weight_g":[2,1]}}',
            "min <= max",
        ),
    ],
)
def test_invalid_dataset_edit_does_not_change_file(
    tmp_path: Path, raw: str, message: str
) -> None:
    path = tmp_path / "cases.jsonl"
    _dataset(path)
    before = path.read_bytes()

    with pytest.raises(DatasetValidationError, match=message):
        save_dataset_case(path, None, raw)

    assert path.read_bytes() == before


def test_add_case_appends(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    _dataset(path)

    case, preview = prepare_dataset_case(
        path, None, '{"id":"three","text":"tea","expected":{"is_food":true}}'
    )
    save_dataset_case(
        path, None, '{"id":"three","text":"tea","expected":{"is_food":true}}'
    )

    assert case["id"] == "three"
    assert preview.rstrip().endswith(
        '{"id":"three","text":"tea","expected":{"is_food":true}}'
    )
    assert (
        path.read_text(encoding="utf-8")
        .rstrip()
        .endswith('{"id":"three","text":"tea","expected":{"is_food":true}}')
    )


def test_grouping_dataset_case_is_validated(tmp_path: Path) -> None:
    path = tmp_path / "grouping.jsonl"
    path.write_text(
        '{"id":"wine","names":["Сухе вино","Червоне вино"],'
        '"expected":{"group_expectations":[{"members":[0,1],'
        '"label_terms":["вино"]}],"max_group_count":1}}\n',
        encoding="utf-8",
    )

    case, _ = prepare_dataset_case(
        path,
        "wine",
        '{"id":"wine","names":["Вино","Wine"],'
        '"expected":{"group_expectations":[{"members":[0,1],'
        '"label_terms":["вино","wine"]}],"max_group_count":1}}',
    )

    assert case["names"] == ["Вино", "Wine"]


def test_image_paths_are_restricted_to_images_directory(tmp_path: Path) -> None:
    evals = tmp_path / "evals"
    (evals / "images").mkdir(parents=True)
    safe = evals / "images" / "safe.png"
    safe.write_bytes(b"png")

    assert safe_image_path(evals, "images/safe.png") == safe.resolve()
    for value in ("../secret", "/etc/passwd", "other/file.png"):
        with pytest.raises(DatasetValidationError):
            safe_image_path(evals, value)


def test_image_symlink_escape_is_blocked(tmp_path: Path) -> None:
    evals = tmp_path / "evals"
    images = evals / "images"
    images.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    (images / "link.png").symlink_to(outside)

    with pytest.raises(DatasetValidationError):
        safe_image_path(evals, "images/link.png")


def test_dashboard_defaults_to_loopback_and_app_uses_configured_paths(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "cases.jsonl"
    _dataset(dataset)
    app = DashboardApp(tmp_path / "results", dataset)

    assert DEFAULT_HOST == "127.0.0.1"
    assert "Ground truth dataset" in app.get_html("/dataset", {})
