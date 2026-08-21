"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .audit import AuditConfig, run_audit, write_report
from .auth import DEFAULT_TOKENSTORE, connect_with_saved_tokens, interactive_login


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin-probe",
        description="Read-only Garmin Connect account coverage and stability check.",
    )
    parser.add_argument(
        "--tokenstore",
        type=Path,
        default=DEFAULT_TOKENSTORE,
        help="OAuth token directory (default: ~/.garminconnect)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("login", help="Interactive first login and token creation")
    audit = subparsers.add_parser("audit", help="Run read-only coverage/stability audit")
    audit.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="Completed day to inspect, YYYY-MM-DD (default: yesterday)",
    )
    audit.add_argument("--runs", type=int, default=3, help="Stability calls per core endpoint")
    audit.add_argument("--delay", type=float, default=2.0, help="Seconds between repeat calls")
    audit.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/garmin_probe_report.json"),
        help="Privacy-safe JSON report path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "login":
            session = interactive_login(args.tokenstore)
            print(f"Login successful. Renewable tokens saved in {session.tokenstore}.")
            print("The Garmin password was not saved by this project.")
            return 0

        session = connect_with_saved_tokens(args.tokenstore)
        config = AuditConfig(
            target_date=args.date,
            stability_runs=args.runs,
            delay_seconds=args.delay,
        )
        report = run_audit(session.client, config)
        output = write_report(report, args.output)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"Privacy-safe detailed report: {output}")
        verdict = report["summary"]["verdict"]
        return 0 if verdict in {"working", "working_but_slow"} else 1
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as exc:
        print(f"Garmin connection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    except GarminConnectTooManyRequestsError as exc:
        print(f"Garmin rate limit reached; stop and retry later: {exc}", file=sys.stderr)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

