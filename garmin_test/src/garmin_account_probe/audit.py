"""Read-only account coverage and stability audit."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)


@dataclass(frozen=True)
class AuditConfig:
    """Settings chosen to be useful without aggressively polling Garmin."""

    target_date: str = (date.today() - timedelta(days=1)).isoformat()
    stability_runs: int = 3
    delay_seconds: float = 2.0
    timeout_warning_seconds: float = 15.0

    def __post_init__(self) -> None:
        date.fromisoformat(self.target_date)
        if not 2 <= self.stability_runs <= 20:
            raise ValueError("stability_runs must be between 2 and 20")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")


@dataclass(frozen=True)
class Probe:
    name: str
    category: str
    call: Callable[[Garmin, str], Any]
    stability: bool = False


def default_probes() -> tuple[Probe, ...]:
    """Representative read-only endpoints; no account data is modified."""

    return (
        Probe(
            "daily_summary",
            "daily_health",
            lambda api, day: api.get_user_summary(day),
            True,
        ),
        Probe(
            "heart_rate",
            "daily_health",
            lambda api, day: api.get_heart_rates(day),
            True,
        ),
        Probe("sleep", "daily_health", lambda api, day: api.get_sleep_data(day)),
        Probe("steps", "daily_health", lambda api, day: api.get_steps_data(day)),
        Probe("stress", "daily_health", lambda api, day: api.get_all_day_stress(day)),
        Probe(
            "body_battery",
            "advanced_health",
            lambda api, day: api.get_body_battery(day, day),
        ),
        Probe("hrv", "advanced_health", lambda api, day: api.get_hrv_data(day)),
        Probe("pulse_ox", "advanced_health", lambda api, day: api.get_spo2_data(day)),
        Probe(
            "respiration",
            "advanced_health",
            lambda api, day: api.get_respiration_data(day),
        ),
        Probe(
            "intensity_minutes",
            "daily_health",
            lambda api, day: api.get_intensity_minutes_data(day),
        ),
        Probe("hydration", "wellness", lambda api, day: api.get_hydration_data(day)),
        Probe(
            "training_readiness",
            "training",
            lambda api, day: api.get_training_readiness(day),
        ),
        Probe("devices", "device", lambda api, _day: api.get_devices(), True),
        Probe("recent_activities", "activities", lambda api, _day: api.get_activities(0, 10)),
        Probe("personal_records", "activities", lambda api, _day: api.get_personal_record()),
    )


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    for candidate in (400, 401, 403, 404, 408, 429, 500, 502, 503, 504):
        if str(candidate) in str(exc):
            return candidate
    return None


def _shape(value: Any) -> dict[str, Any]:
    """Describe a response without storing health values or identity data."""

    if value is None:
        return {"response_type": "null", "empty": True}
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value)[:100]
        return {
            "response_type": "object",
            "empty": not value,
            "top_level_fields": keys,
            "field_count": len(value),
        }
    if isinstance(value, list):
        item_fields: list[str] = []
        if value and isinstance(value[0], dict):
            item_fields = sorted(str(key) for key in value[0])[:100]
        return {
            "response_type": "array",
            "empty": not value,
            "item_count": len(value),
            "first_item_fields": item_fields,
        }
    return {"response_type": type(value).__name__, "empty": False}


def _shape_hash(shape: dict[str, Any]) -> str:
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:12]


def _single_call(probe: Probe, api: Garmin, target_date: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        value = probe.call(api, target_date)
        shape = _shape(value)
        return {
            "status": "available_empty" if shape["empty"] else "available",
            "latency_seconds": round(time.perf_counter() - started, 3),
            "shape": shape,
            "shape_hash": _shape_hash(shape),
        }
    except GarminConnectNotFoundError as exc:
        status = "not_supported_or_not_found"
        error_type = type(exc).__name__
        http_status = _http_status(exc) or 404
    except GarminConnectTooManyRequestsError as exc:
        status = "rate_limited"
        error_type = type(exc).__name__
        http_status = _http_status(exc) or 429
    except GarminConnectAuthenticationError as exc:
        status = "authentication_failed"
        error_type = type(exc).__name__
        http_status = _http_status(exc)
    except GarminConnectConnectionError as exc:
        http_status = _http_status(exc)
        status = (
            "not_supported_or_not_found"
            if http_status in {400, 403, 404}
            else "connection_failed"
        )
        error_type = type(exc).__name__
    except Exception as exc:  # Keep the audit running when one optional endpoint changes.
        status = "unexpected_error"
        error_type = type(exc).__name__
        http_status = _http_status(exc)
    return {
        "status": status,
        "latency_seconds": round(time.perf_counter() - started, 3),
        "error_type": error_type,
        "http_status": http_status,
    }


def _summarize(probe: Probe, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    successes = [item for item in attempts if item["status"].startswith("available")]
    latencies = [item["latency_seconds"] for item in attempts]
    statuses: dict[str, int] = {}
    for item in attempts:
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
    hashes = {item["shape_hash"] for item in successes}
    result: dict[str, Any] = {
        "name": probe.name,
        "category": probe.category,
        "attempts": len(attempts),
        "successes": len(successes),
        "success_rate": round(len(successes) / len(attempts), 3),
        "statuses": statuses,
        "latency_seconds": {
            "min": min(latencies),
            "median": round(statistics.median(latencies), 3),
            "max": max(latencies),
        },
        "shape_stable": len(hashes) <= 1,
    }
    if successes:
        result["response_shape"] = successes[0]["shape"]
    return result


def run_audit(
    api: Garmin,
    config: AuditConfig,
    *,
    probes: tuple[Probe, ...] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Probe data coverage once and repeat a small stability subset."""

    selected = probes or default_probes()
    results: list[dict[str, Any]] = []
    stopped_early = False
    for probe in selected:
        run_count = config.stability_runs if probe.stability else 1
        attempts: list[dict[str, Any]] = []
        for run_index in range(run_count):
            attempt = _single_call(probe, api, config.target_date)
            attempts.append(attempt)
            if attempt["status"] in {"authentication_failed", "rate_limited"}:
                stopped_early = True
                break
            if run_index + 1 < run_count:
                sleep_fn(config.delay_seconds)
        results.append(_summarize(probe, attempts))
        if stopped_early:
            break

    stable_results = [item for item in results if item["attempts"] > 1]
    successful_attempts = sum(item["successes"] for item in stable_results)
    total_attempts = sum(item["attempts"] for item in stable_results)
    available = sum(item["successes"] > 0 for item in results)
    rate_limited = any("rate_limited" in item["statuses"] for item in results)
    auth_failed = any("authentication_failed" in item["statuses"] for item in results)
    endpoint_errors = sum(
        count
        for item in results
        for status, count in item["statuses"].items()
        if status not in {"available", "available_empty", "not_supported_or_not_found"}
    )
    slow = any(
        item["latency_seconds"]["max"] > config.timeout_warning_seconds
        for item in stable_results
    )

    if auth_failed:
        verdict = "failed_authentication"
    elif rate_limited:
        verdict = "unstable_rate_limited"
    elif total_attempts and successful_attempts != total_attempts:
        verdict = "unstable_request_failures"
    elif slow:
        verdict = "working_but_slow"
    else:
        verdict = "working"

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_date": config.target_date,
        "privacy": "No raw Garmin values, credentials, tokens, names, or email addresses are stored.",
        "environment": {
            "python": platform.python_version(),
            "garminconnect": importlib.metadata.version("garminconnect"),
        },
        "configuration": asdict(config),
        "summary": {
            "verdict": verdict,
            "available_data_types": available,
            "probed_data_types": len(results),
            "requested_data_types": len(selected),
            "stability_success_rate": round(successful_attempts / total_attempts, 3)
            if total_attempts
            else None,
            "rate_limited": rate_limited,
            "endpoint_errors": endpoint_errors,
            "stopped_early": stopped_early,
        },
        "probes": results,
    }


def write_report(report: dict[str, Any], output: Path) -> Path:
    resolved = output.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return resolved
