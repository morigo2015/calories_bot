"""Fast tests that validate the probe itself without a Garmin account."""

from __future__ import annotations

from garmin_account_probe.audit import AuditConfig, Probe, run_audit


class FakeGarmin:
    def summary(self, day: str) -> dict[str, object]:
        return {"calendarDate": day, "steps": 1234, "privateValue": "not-in-report"}


def test_report_contains_shape_but_not_raw_values() -> None:
    fake = FakeGarmin()
    report = run_audit(
        fake,  # type: ignore[arg-type]
        AuditConfig(target_date="2026-01-02", stability_runs=2, delay_seconds=0),
        probes=(
            Probe("summary", "health", lambda api, day: api.summary(day), stability=True),
        ),
        sleep_fn=lambda _seconds: None,
    )

    assert report["summary"]["verdict"] == "working"
    assert report["summary"]["stability_success_rate"] == 1.0
    serialized = str(report)
    assert "privateValue" in serialized  # field names help integration design
    assert "not-in-report" not in serialized
    assert "1234" not in serialized


def test_empty_response_is_available_not_failed() -> None:
    report = run_audit(
        FakeGarmin(),  # type: ignore[arg-type]
        AuditConfig(target_date="2026-01-02", stability_runs=2, delay_seconds=0),
        probes=(Probe("optional", "health", lambda _api, _day: []),),
        sleep_fn=lambda _seconds: None,
    )

    probe = report["probes"][0]
    assert probe["statuses"] == {"available_empty": 1}
    assert probe["success_rate"] == 1.0

