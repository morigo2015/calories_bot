"""Tests that intentionally call the user's real Garmin Connect account."""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from garmin_account_probe.audit import AuditConfig, Probe, run_audit
from garmin_account_probe.auth import DEFAULT_TOKENSTORE, connect_with_saved_tokens

pytestmark = pytest.mark.integration


def _tokenstore() -> Path:
    return Path(os.getenv("GARMINTOKENS", str(DEFAULT_TOKENSTORE)))


def test_real_account_authentication_and_stability() -> None:
    """Saved tokens work and a completed daily summary remains reachable."""

    try:
        session = connect_with_saved_tokens(_tokenstore())
    except RuntimeError as exc:
        pytest.fail(str(exc), pytrace=False)

    runs = int(os.getenv("GARMIN_STABILITY_RUNS", "3"))
    delay = float(os.getenv("GARMIN_STABILITY_DELAY", "2"))
    max_latency = float(os.getenv("GARMIN_MAX_LATENCY_SECONDS", "15"))
    target_date = os.getenv(
        "GARMIN_TEST_DATE", (date.today() - timedelta(days=1)).isoformat()
    )
    report = run_audit(
        session.client,
        AuditConfig(
            target_date=target_date,
            stability_runs=runs,
            delay_seconds=delay,
            timeout_warning_seconds=max_latency,
        ),
        probes=(
            Probe(
                "daily_summary",
                "daily_health",
                lambda api, day: api.get_user_summary(day),
                stability=True,
            ),
        ),
        sleep_fn=time.sleep,
    )
    result = report["probes"][0]
    assert result["successes"] == runs, report
    assert result["shape_stable"], report
    assert result["latency_seconds"]["max"] <= max_latency, report

