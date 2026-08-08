# ruff: noqa: E501
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from scripts.eval_storage import (
    DatasetValidationError,
    prepare_dataset_case,
    read_dataset,
    safe_image_path,
    save_dataset_case,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "eval-results"
DEFAULT_DATASET = ROOT / "evals" / "cases.jsonl"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_FORM_BYTES = 2_000_000


@dataclass(frozen=True)
class LoadedReport:
    key: str
    path: Path
    data: dict[str, Any]
    legacy: bool

    @property
    def name(self) -> str:
        value = self.data.get("name")
        return str(value) if isinstance(value, str) and value else self.path.stem

    @property
    def timestamp(self) -> str:
        value = self.data.get("started_at")
        return str(value) if isinstance(value, str) and value else "Unknown"


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json(value: object) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False, indent=2), quote=True)


def load_report(path: Path, results_dir: Path) -> LoadedReport:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load report {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Report {path.name} must contain a JSON object")
    legacy = value.get("schema_version") != 2
    configurations = value.get("runs" if legacy else "configurations")
    if not isinstance(configurations, list):
        raise ValueError(f"Report {path.name} has no configurations")
    try:
        key = path.resolve().relative_to(results_dir.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("Report is outside the configured results directory") from exc
    return LoadedReport(key=key, path=path, data=value, legacy=legacy)


def load_reports(results_dir: Path) -> list[LoadedReport]:
    paths = list((results_dir / "runs").glob("*.json"))
    paths.extend(results_dir.glob("*.json"))
    reports: list[LoadedReport] = []
    for path in paths:
        try:
            reports.append(load_report(path, results_dir))
        except ValueError:
            continue
    reports.sort(
        key=lambda report: (
            report.data.get("started_at") or "",
            report.path.stat().st_mtime_ns,
        ),
        reverse=True,
    )
    return reports


def _configurations(report: LoadedReport) -> list[dict[str, Any]]:
    key = "runs" if report.legacy else "configurations"
    values = report.data.get(key, [])
    return [value for value in values if isinstance(value, dict)]


def _result_rows(configuration: dict[str, Any]) -> list[dict[str, Any]]:
    values = configuration.get("results", [])
    if not isinstance(values, list):
        return []
    occurrences: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        row = dict(value)
        case_id = str(row.get("case_id", "Unknown"))
        if not isinstance(row.get("repeat_index"), int):
            row["repeat_index"] = occurrences.get(case_id, 0)
        occurrences[case_id] = occurrences.get(case_id, 0) + 1
        rows.append(row)
    return rows


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _money(value: object) -> str:
    if value is None:
        return "Unknown"
    try:
        return f"${Decimal(str(value)):.6f}"
    except (InvalidOperation, ValueError):
        return "Unknown"


def configuration_summary(configuration: dict[str, Any]) -> dict[str, Any]:
    rows = _result_rows(configuration)
    total = len(rows)
    passed_count = sum(row.get("passed") is True for row in rows)
    pass_rate = _number(configuration.get("pass_rate"))
    if pass_rate is None and total:
        pass_rate = passed_count / total
    hard_failures = configuration.get("hard_failures")
    if not isinstance(hard_failures, int):
        hard_failures = sum(row.get("hard_failure") is True for row in rows)
    return {
        "passed": passed_count,
        "total": total,
        "pass_rate": pass_rate,
        "hard_failures": hard_failures,
        "cost": configuration.get("cost_usd"),
        "latency": _number(configuration.get("average_latency_seconds")),
        "model": configuration.get("model", "Unknown"),
        "effort": configuration.get("effort", "Unknown"),
    }


def compare_configurations(
    configuration_a: dict[str, Any], configuration_b: dict[str, Any]
) -> dict[str, Any]:
    rows_a = _result_rows(configuration_a)
    rows_b = _result_rows(configuration_b)
    by_a = {
        (str(row.get("case_id", "Unknown")), int(row["repeat_index"])): row
        for row in rows_a
    }
    by_b = {
        (str(row.get("case_id", "Unknown")), int(row["repeat_index"])): row
        for row in rows_b
    }
    ordered_keys = list(by_a)
    ordered_keys.extend(key for key in by_b if key not in by_a)
    rows: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    for key in ordered_keys:
        a = by_a.get(key)
        b = by_b.get(key)
        status_a = "pass" if a and a.get("passed") is True else "fail"
        status_b = "pass" if b and b.get("passed") is True else "fail"
        if a is None:
            status_a = "not_run"
        if b is None:
            status_b = "not_run"
        row = {
            "case_id": key[0],
            "repeat_index": key[1],
            "status_a": status_a,
            "status_b": status_b,
            "changed": status_a != status_b,
        }
        rows.append(row)
        if status_a == "pass" and status_b == "fail":
            regressions.append(row)
        elif status_a == "fail" and status_b == "pass":
            improvements.append(row)
    return {
        "rows": rows,
        "regressions": regressions,
        "improvements": improvements,
    }


STYLE = """
:root{color-scheme:light;--bg:#f5f3ee;--card:#fff;--ink:#24231f;--muted:#6d6a61;
--line:#ded9ce;--ok:#18794e;--bad:#b42318;--warn:#9a6700;--accent:#315c45}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 ui-sans-serif,system-ui,sans-serif}main{max-width:1180px;margin:0 auto;padding:28px}
nav{display:flex;gap:18px;align-items:center;margin-bottom:24px}nav a{color:var(--accent);
font-weight:700;text-decoration:none}.brand{font-size:20px;margin-right:auto}h1,h2,h3{line-height:1.2}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:17px;
margin:12px 0;box-shadow:0 1px 2px #00000008}.meta,.muted{color:var(--muted);font-size:13px}
.ok{color:var(--ok);font-weight:700}.bad{color:var(--bad);font-weight:700}.warn{color:var(--warn);
font-weight:700}.pill{display:inline-block;border-radius:999px;padding:2px 9px;background:#eeece6;
font-size:12px;margin-right:5px}.actions{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
a.button,button{display:inline-block;border:0;border-radius:7px;padding:8px 12px;background:var(--accent);
color:white;text-decoration:none;font:inherit;cursor:pointer}button.secondary,a.secondary{background:#69665e}
table{border-collapse:collapse;width:100%;overflow:auto;display:block}th,td{border-bottom:1px solid var(--line);
padding:9px;text-align:left;vertical-align:top}th{font-size:12px;text-transform:uppercase;color:var(--muted)}
pre{white-space:pre-wrap;word-break:break-word;background:#f4f2ed;padding:12px;border-radius:8px}
textarea{width:100%;min-height:430px;padding:12px;font:13px/1.45 ui-monospace,monospace}
select,input{padding:8px;border:1px solid var(--line);border-radius:6px;background:white}
.notice{padding:12px;border-left:4px solid var(--warn);background:#fff7d6;margin:14px 0}
.failure{border-left:4px solid var(--bad)}.passed{border-left:4px solid var(--ok)}
img.preview{max-width:360px;max-height:280px;border-radius:8px;border:1px solid var(--line)}
details{margin:10px 0}code{word-break:break-word}@media(max-width:650px){main{padding:16px}}
"""


def _page(title: str, content: str) -> str:
    return (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_escape(title)}</title><style>{STYLE}</style></head><body><main>"
        "<nav><a class='brand' href='/'>Eval dashboard</a>"
        "<a href='/'>Runs</a><a href='/dataset'>Dataset</a></nav>"
        f"{content}</main></body></html>"
    )


def _status(value: str) -> str:
    labels = {"pass": "Pass", "fail": "Fail", "not_run": "Not run"}
    css = "ok" if value == "pass" else "bad" if value == "fail" else "muted"
    return f"<span class='{css}'>{labels[value]}</span>"


def _summary_card(label: str, summary: dict[str, Any]) -> str:
    rate = summary["pass_rate"]
    rate_text = f"{rate:.1%}" if rate is not None else "Unknown"
    latency = summary["latency"]
    latency_text = f"{latency:.2f}s" if latency is not None else "Unknown"
    return (
        f"<div class='card'><h3>{_escape(label)}</h3>"
        f"<div><strong>{summary['passed']}/{summary['total']}</strong> ({rate_text})</div>"
        f"<div>Hard failures: {_escape(summary['hard_failures'])}</div>"
        f"<div>Cost: {_money(summary['cost'])}</div><div>Avg latency: {latency_text}</div>"
        f"<div class='meta'>{_escape(summary['model'])} · {_escape(summary['effort'])}</div></div>"
    )


def render_index(reports: list[LoadedReport]) -> str:
    options: list[str] = []
    cards: list[str] = []
    for report in reports:
        config_parts: list[str] = []
        for index, configuration in enumerate(_configurations(report)):
            summary = configuration_summary(configuration)
            label = f"{report.name} — {summary['model']}:{summary['effort']}"
            option_value = f"{report.key}|{index}"
            options.append(
                f"<option value='{_escape(option_value)}'>{_escape(label)}</option>"
            )
            query = urlencode({"run": report.key, "config": index})
            compare = urlencode({"a": report.key, "ac": index})
            rate = summary["pass_rate"]
            rate_text = f"{rate:.1%}" if rate is not None else "Unknown"
            latency = summary["latency"]
            latency_text = f"{latency:.2f}s" if latency is not None else "Unknown"
            config_parts.append(
                "<div class='card'>"
                f"<strong>{summary['passed']}/{summary['total']} · {rate_text}</strong> "
                f"<span class='meta'>{_escape(summary['model'])} · {_escape(summary['effort'])}</span>"
                f"<div class='meta'>hard failures={summary['hard_failures']} · "
                f"cost={_money(summary['cost'])} · avg latency={latency_text}</div>"
            )
            config_parts[-1] += (
                f"<div class='actions'><a class='button' href='/run?{query}'>Details</a>"
                f"<a class='button secondary' href='/compare?{compare}'>Compare</a></div></div>"
            )
        legacy = (
            " <span class='pill warn'>Legacy report</span>" if report.legacy else ""
        )
        cards.append(
            f"<section class='card'><h2>{_escape(report.name)}{legacy}</h2>"
            f"<div class='meta'>{_escape(report.timestamp)}</div>{''.join(config_parts)}</section>"
        )
    compare_form = ""
    if len(options) >= 2:
        compare_form = (
            "<section class='card'><h2>Compare two configurations</h2>"
            "<form action='/compare' method='get' class='actions'>"
            f"<label>A <select name='a_choice'>{''.join(options)}</select></label>"
            f"<label>B <select name='b_choice'>{''.join(options)}</select></label>"
            "<button type='submit'>Compare</button></form></section>"
        )
    empty = "<div class='card'>No eval reports found.</div>" if not cards else ""
    return _page(
        "Eval runs",
        f"<h1>LLM eval runs</h1>{compare_form}{empty}{''.join(cards)}",
    )


def _snapshot_by_id(report: LoadedReport) -> dict[str, dict[str, Any]]:
    values = report.data.get("dataset_snapshot", [])
    if not isinstance(values, list):
        return {}
    return {
        str(value["id"]): value
        for value in values
        if isinstance(value, dict) and "id" in value
    }


def _render_checks(checks: object) -> str:
    if not isinstance(checks, list) or not checks:
        return "<p class='muted'>No checks recorded.</p>"
    rows: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        passed = check.get("passed") is True
        rows.append(
            f"<tr><td class='{'ok' if passed else 'bad'}'>"
            f"{'Pass' if passed else 'Fail'}</td><td>{_escape(check.get('name', ''))}</td>"
            f"<td>{_escape(check.get('actual', ''))}</td>"
            f"<td>{_escape(check.get('expected', ''))}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Status</th><th>Check</th><th>Actual</th>"
        f"<th>Expected</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _render_actual(actual: object) -> str:
    if not isinstance(actual, dict):
        return "<p class='muted'>Actual analysis was not recorded.</p>"
    items = actual.get("items", [])
    rows: list[str] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append(
                "<tr>"
                f"<td>{_escape(item.get('name', ''))}</td>"
                f"<td>{_escape(item.get('weight_g', ''))}</td>"
                f"<td>{_escape(item.get('weight_source_id'))}</td>"
                f"<td>{_escape(item.get('weight_origin'))}</td>"
                f"<td>{_escape(item.get('weight_estimated'))}</td>"
                f"<td>{_escape(item.get('kcal_per_100g', ''))}</td>"
                f"<td>{_escape(item.get('kcal_source_id'))}</td>"
                f"<td>{_escape(item.get('kcal_origin'))}</td>"
                f"<td>{_escape(item.get('kcal_estimated'))}</td>"
                f"<td>{_escape(item.get('portion_display'))}</td></tr>"
            )
    header = (
        f"<p><strong>is_food:</strong> {_escape(actual.get('is_food'))} · "
        f"<strong>meal:</strong> {_escape(actual.get('meal_name', ''))}</p>"
    )
    table = (
        "<table><thead><tr><th>Item</th><th>Weight</th><th>Weight source</th>"
        "<th>Weight origin</th><th>Weight estimated</th><th>Kcal/100g</th>"
        "<th>Kcal source</th><th>Kcal origin</th><th>Kcal estimated</th>"
        f"<th>Portion</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )
    return header + table


def _render_result(result: dict[str, Any], case: dict[str, Any] | None) -> str:
    passed = result.get("passed") is True
    case_id = str(result.get("case_id", "Unknown"))
    repeat_index = result.get("repeat_index", 0)
    text = case.get("text", "") if case else ""
    expected = case.get("expected") if case else None
    normalized = result.get("normalized_input")
    normalized_text = normalized.get("text") if isinstance(normalized, dict) else None
    image = ""
    if case and case.get("image"):
        image_query = urlencode({"path": str(case["image"])})
        image = (
            f"<p><img class='preview' src='/image?{image_query}' "
            f"alt='Image for {_escape(case_id)}'></p>"
        )
    error = result.get("error")
    if isinstance(error, dict):
        error_text = f"{error.get('type', 'Error')}: {error.get('message', '')}"
    else:
        error_text = str(error) if error else ""
    error_html = (
        f"<div class='notice bad'>{_escape(error_text)}</div>" if error_text else ""
    )
    input_tokens = result.get("input_tokens")
    output_tokens = result.get("output_tokens")
    diagnostics = (
        f"latency={_escape(result.get('latency_seconds', 'Unknown'))}s · "
        f"tokens={_escape(input_tokens)}+{_escape(output_tokens)} · "
        f"cost={_money(result.get('cost_usd'))}"
    )
    return (
        f"<article class='card {'passed' if passed else 'failure'}'>"
        f"<h3>{_escape(case_id)} <span class='pill'>repeat {repeat_index}</span></h3>"
        f"<div class='meta'>{diagnostics}</div>{error_html}"
        f"<h4>Original input</h4><pre>{_escape(text) if case else 'Not recorded'}</pre>{image}"
        f"<h4>Normalized text</h4><pre>{_escape(normalized_text) if normalized_text is not None else 'Not recorded'}</pre>"
        f"<details><summary>Expected snapshot</summary><pre>{_json(expected) if expected is not None else 'Not recorded'}</pre></details>"
        f"<h4>Actual FoodAnalysis</h4>{_render_actual(result.get('actual'))}"
        f"<h4>Checks</h4>{_render_checks(result.get('checks'))}</article>"
    )


def render_run(report: LoadedReport, config_index: int) -> str:
    configurations = _configurations(report)
    if not 0 <= config_index < len(configurations):
        raise KeyError("Configuration not found")
    configuration = configurations[config_index]
    summary = configuration_summary(configuration)
    snapshot = _snapshot_by_id(report)
    results = _result_rows(configuration)
    failures = [result for result in results if result.get("passed") is not True]
    passed = [result for result in results if result.get("passed") is True]
    rendered = [
        _render_result(result, snapshot.get(str(result.get("case_id"))))
        for result in failures + passed
    ]
    legacy = (
        "<div class='notice'>Legacy report: some diagnostic fields are unavailable.</div>"
        if report.legacy
        else ""
    )
    compare = urlencode({"a": report.key, "ac": config_index})
    return _page(
        report.name,
        f"<h1>{_escape(report.name)}</h1><p class='meta'>{_escape(report.timestamp)}</p>"
        f"{legacy}<div class='grid'>{_summary_card('Summary', summary)}</div>"
        f"<p><a class='button secondary' href='/compare?{compare}'>Compare</a></p>"
        f"<h2>Failures ({len(failures)})</h2>"
        f"{''.join(rendered[: len(failures)]) or '<p class=muted>None.</p>'}"
        f"<h2>Passed ({len(passed)})</h2>{''.join(rendered[len(failures) :])}",
    )


def _split_choice(value: str) -> tuple[str, int]:
    key, separator, index = value.rpartition("|")
    if not separator:
        raise ValueError("Invalid configuration choice")
    return key, int(index)


def _delta(a: object, b: object, *, percent: bool = False) -> str:
    a_number = _number(a)
    b_number = _number(b)
    if a_number is None or b_number is None:
        return "Unknown"
    difference = b_number - a_number
    return f"{difference:+.1%}" if percent else f"{difference:+.4f}"


def render_compare(
    reports: list[LoadedReport],
    report_a: LoadedReport,
    config_a: int,
    report_b: LoadedReport | None,
    config_b: int | None,
    changed_only: bool,
) -> str:
    configurations_a = _configurations(report_a)
    if not 0 <= config_a < len(configurations_a):
        raise KeyError("Configuration A not found")
    options: list[str] = []
    for report in reports:
        for index, configuration in enumerate(_configurations(report)):
            summary = configuration_summary(configuration)
            value = f"{report.key}|{index}"
            options.append(
                f"<option value='{_escape(value)}'>{_escape(report.name)} — "
                f"{_escape(summary['model'])}:{_escape(summary['effort'])}</option>"
            )
    if report_b is None or config_b is None:
        hidden = urlencode({"a": report_a.key, "ac": config_a})
        return _page(
            "Compare eval runs",
            f"<h1>Compare with {_escape(report_a.name)}</h1>"
            f"<form action='/compare?{hidden}' method='get' class='card'>"
            f"<input type='hidden' name='a' value='{_escape(report_a.key)}'>"
            f"<input type='hidden' name='ac' value='{config_a}'>"
            f"<label>Configuration B <select name='b_choice'>{''.join(options)}</select></label> "
            "<button type='submit'>Compare</button></form>",
        )
    configurations_b = _configurations(report_b)
    if not 0 <= config_b < len(configurations_b):
        raise KeyError("Configuration B not found")
    configuration_a = configurations_a[config_a]
    configuration_b = configurations_b[config_b]
    summary_a = configuration_summary(configuration_a)
    summary_b = configuration_summary(configuration_b)
    comparison = compare_configurations(configuration_a, configuration_b)
    rows = comparison["rows"]
    if changed_only:
        rows = [row for row in rows if row["changed"]]
    dataset_warning = ""
    hash_a = report_a.data.get("dataset_sha256")
    hash_b = report_b.data.get("dataset_sha256")
    if hash_a and hash_b and hash_a != hash_b:
        dataset_warning = (
            "<div class='notice'><strong>Different datasets.</strong> Results may "
            "differ because ground truth or dataset composition changed.</div>"
        )
    row_html = "".join(
        f"<tr><td>{_escape(row['case_id'])}</td><td>{row['repeat_index']}</td>"
        f"<td>{_status(row['status_a'])}</td><td>{_status(row['status_b'])}</td></tr>"
        for row in rows
    )
    query = {
        "a": report_a.key,
        "ac": config_a,
        "b": report_b.key,
        "bc": config_b,
    }
    toggle_query = dict(query)
    if not changed_only:
        toggle_query["changed"] = "1"
    toggle_label = "Show all" if changed_only else "Changed only"
    regression_items = (
        "".join(
            f"<li>{_escape(row['case_id'])} · repeat {row['repeat_index']}</li>"
            for row in comparison["regressions"]
        )
        or "<li>None</li>"
    )
    improvement_items = (
        "".join(
            f"<li>{_escape(row['case_id'])} · repeat {row['repeat_index']}</li>"
            for row in comparison["improvements"]
        )
        or "<li>None</li>"
    )
    deltas = (
        "<div class='card'><h3>B − A</h3>"
        f"<div>Pass rate: {_delta(summary_a['pass_rate'], summary_b['pass_rate'], percent=True)}</div>"
        f"<div>Cost: {_delta(summary_a['cost'], summary_b['cost'])}</div>"
        f"<div>Average latency: {_delta(summary_a['latency'], summary_b['latency'])}s</div></div>"
    )
    return _page(
        "Compare eval runs",
        f"<h1>Compare configurations</h1>{dataset_warning}"
        f"<div class='grid'>{_summary_card('A · ' + report_a.name, summary_a)}"
        f"{_summary_card('B · ' + report_b.name, summary_b)}{deltas}</div>"
        "<div class='grid'><div class='card'><h2>Regressions</h2>"
        f"<ul>{regression_items}</ul></div><div class='card'><h2>Improvements</h2>"
        f"<ul>{improvement_items}</ul></div></div>"
        f"<p><a class='button secondary' href='/compare?{urlencode(toggle_query)}'>{toggle_label}</a></p>"
        "<table><thead><tr><th>Case ID</th><th>Repeat</th><th>A</th><th>B</th>"
        f"</tr></thead><tbody>{row_html}</tbody></table>",
    )


def _expected_summary(expected: object) -> str:
    if not isinstance(expected, dict):
        return "Invalid expected object"
    parts = [f"is_food={expected.get('is_food')}"]
    parts.extend(
        f"{key}={value}" for key, value in expected.items() if key != "is_food"
    )
    return " · ".join(parts)


def render_dataset(dataset_path: Path) -> str:
    _, parsed = read_dataset(dataset_path)
    cards: list[str] = []
    for _, case in parsed:
        case_id = str(case["id"])
        input_html = f"<p>{_escape(case.get('text', ''))}</p>"
        if case.get("image"):
            query = urlencode({"path": str(case["image"])})
            input_html += (
                f"<img class='preview' src='/image?{query}' "
                f"alt='Image for {_escape(case_id)}'>"
            )
        edit = urlencode({"id": case_id})
        cards.append(
            f"<article class='card'><h2>{_escape(case_id)}</h2>{input_html}"
            f"<p class='meta'>{_escape(_expected_summary(case.get('expected')))}</p>"
            f"<a class='button' href='/dataset/edit?{edit}'>View/Edit</a></article>"
        )
    return _page(
        "Eval dataset",
        "<div class='actions'><h1 style='margin-right:auto'>Ground truth dataset</h1>"
        "<a class='button' href='/dataset/edit'>Add case</a></div>"
        f"<p class='meta'>{_escape(dataset_path)}</p>{''.join(cards)}",
    )


def render_dataset_editor(
    dataset_path: Path,
    case_id: str | None,
    *,
    raw_json: str | None = None,
    message: str | None = None,
    error: bool = False,
) -> str:
    case: dict[str, Any] | None = None
    if case_id is not None:
        _, parsed = read_dataset(dataset_path)
        case = next((item for _, item in parsed if item["id"] == case_id), None)
        if case is None:
            raise KeyError("Dataset case not found")
    if raw_json is None:
        value: object = (
            case
            if case is not None
            else {
                "id": "",
                "text": "",
                "expected": {"is_food": True},
            }
        )
        raw_json = json.dumps(value, ensure_ascii=False, indent=2)
    message_html = ""
    if message:
        message_html = (
            f"<div class='notice {'bad' if error else 'ok'}'>{_escape(message)}</div>"
        )
    original = case_id or ""
    preview = ""
    if case is not None:
        preview = (
            f"<div class='card'><h2>Preview</h2><p>{_escape(case.get('text', ''))}</p>"
            f"<pre>{_json(case.get('expected'))}</pre></div>"
        )
    return _page(
        "Edit eval case",
        f"<h1>{'Edit ' + _escape(case_id) if case_id else 'Add case'}</h1>{message_html}{preview}"
        "<form method='post' class='card'>"
        f"<input type='hidden' name='original_id' value='{_escape(original)}'>"
        f"<textarea name='case_json' spellcheck='false'>{_escape(raw_json)}</textarea>"
        "<div class='actions'><button formaction='/dataset/validate'>Validate</button>"
        "<button formaction='/dataset/save'>Save</button>"
        "<a class='button secondary' href='/dataset'>Cancel</a></div></form>",
    )


class DashboardApp:
    def __init__(self, results_dir: Path, dataset_path: Path) -> None:
        self.results_dir = results_dir
        self.dataset_path = dataset_path

    def reports(self) -> list[LoadedReport]:
        return load_reports(self.results_dir)

    def find_report(self, key: str) -> LoadedReport:
        report = next((item for item in self.reports() if item.key == key), None)
        if report is None:
            raise KeyError("Report not found")
        return report

    def get_html(self, path: str, query: dict[str, list[str]]) -> str:
        reports = self.reports()
        if path == "/":
            return render_index(reports)
        if path == "/run":
            report = self.find_report(_required(query, "run"))
            return render_run(report, int(_first(query, "config", "0")))
        if path == "/compare":
            if "a_choice" in query:
                a_key, a_config = _split_choice(_required(query, "a_choice"))
            else:
                a_key = _required(query, "a")
                a_config = int(_first(query, "ac", "0"))
            if "b_choice" in query:
                b_key, b_config = _split_choice(_required(query, "b_choice"))
            elif "b" in query:
                b_key = _required(query, "b")
                b_config = int(_first(query, "bc", "0"))
            else:
                b_key = None
                b_config = None
            return render_compare(
                reports,
                self.find_report(a_key),
                a_config,
                self.find_report(b_key) if b_key is not None else None,
                b_config,
                _first(query, "changed", "0") == "1",
            )
        if path == "/dataset":
            return render_dataset(self.dataset_path)
        if path == "/dataset/edit":
            return render_dataset_editor(self.dataset_path, _optional(query, "id"))
        raise KeyError("Page not found")

    def post_html(self, path: str, form: dict[str, list[str]]) -> str:
        original_id = _first(form, "original_id", "") or None
        raw_json = _required(form, "case_json")
        if path == "/dataset/validate":
            try:
                case, _ = prepare_dataset_case(self.dataset_path, original_id, raw_json)
            except DatasetValidationError as exc:
                return render_dataset_editor(
                    self.dataset_path,
                    original_id,
                    raw_json=raw_json,
                    message=str(exc),
                    error=True,
                )
            return render_dataset_editor(
                self.dataset_path,
                original_id,
                raw_json=raw_json,
                message=f"Case {case['id']} is valid. No changes saved.",
            )
        if path == "/dataset/save":
            try:
                case = save_dataset_case(self.dataset_path, original_id, raw_json)
            except DatasetValidationError as exc:
                return render_dataset_editor(
                    self.dataset_path,
                    original_id,
                    raw_json=raw_json,
                    message=str(exc),
                    error=True,
                )
            return render_dataset_editor(
                self.dataset_path,
                str(case["id"]),
                message=f"Saved {case['id']}.",
            )
        raise KeyError("Page not found")

    def image_path(self, value: str) -> Path:
        return safe_image_path(self.dataset_path.parent, value)


def _first(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    return values[0] if values else default


def _required(query: dict[str, list[str]], key: str) -> str:
    value = _first(query, key, "")
    if not value:
        raise ValueError(f"Missing parameter: {key}")
    return value


def _optional(query: dict[str, list[str]], key: str) -> str | None:
    value = _first(query, key, "")
    return value or None


def make_handler(app: DashboardApp) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._send_html(
                _page(
                    status.phrase,
                    f"<h1>{status.value} {status.phrase}</h1><p>{_escape(message)}</p>",
                ),
                status,
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/image":
                self._serve_image(_first(query, "path", ""))
                return
            try:
                body = app.get_html(parsed.path, query)
            except (KeyError, ValueError, DatasetValidationError) as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc))
                return
            self._send_html(body)

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
                return
            if length > MAX_FORM_BYTES:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Form is too large")
                return
            raw = self.rfile.read(length).decode("utf-8", errors="strict")
            form = parse_qs(raw, keep_blank_values=True)
            try:
                body = app.post_html(urlparse(self.path).path, form)
            except (KeyError, ValueError, DatasetValidationError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_html(body)

        def _serve_image(self, value: str) -> None:
            try:
                path = app.image_path(value)
                data = path.read_bytes()
            except (DatasetValidationError, OSError):
                self._error(HTTPStatus.NOT_FOUND, "Image not found")
                return
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.address_string()} - {format % args}", file=sys.stderr)

    return DashboardHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local LLM eval dashboard")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 0 <= args.port <= 65535:
        raise SystemExit("--port must be from 0 to 65535")
    app = DashboardApp(args.results_dir.resolve(), args.dataset.resolve())
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    host, port = server.server_address[:2]
    print(f"Eval dashboard: http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping eval dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
