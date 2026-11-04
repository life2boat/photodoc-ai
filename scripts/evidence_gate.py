#!/usr/bin/env python3
"""PhotoDoc AI — evidence gate (Stage 1).

Read-only. Answers one question: does this backup set represent exactly the
current persistent state of this database, under a verified, immutable manifest?

It writes nothing, starts nothing and changes no database. It is the Stage 1
building block; the deployment protocol that keeps writers excluded is a separate
piece of work, so this tool reports state and refuses on anything unproven rather
than claiming it can freeze the system.

Exit codes::

    0  pass
    2  a required input is missing (no set, no database)
    3  the set or the evidence is invalid
    4  the set is too old
    6  invalid configuration
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backup_format  # noqa: E402
import db_evidence  # noqa: E402

EXIT_OK = 0
EXIT_MISSING = 2
EXIT_INVALID = 3
EXIT_STALE = 4
EXIT_CONFIG = 6

MAX_AGE_HOURS_LIMIT = 24.0 * 365.0 * 10.0
POLICIES = ("normal", "pre-outbox-transition")


def parse_age_hours(raw: object) -> tuple[float | None, str | None]:
    """A NaN limit would silently disable freshness, so only finite positives pass."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, f"--max-age-hours is not a number: {raw!r}"
    if not math.isfinite(value):
        return None, f"--max-age-hours must be finite, got {raw!r}"
    if value <= 0:
        return None, f"--max-age-hours must be greater than zero, got {raw!r}"
    if value > MAX_AGE_HOURS_LIMIT:
        return None, "--max-age-hours is unreasonably large (over ten years)"
    return value, None


def evaluate(live_db: Path, backup_dir: Path, *, expected_application_id: int,
             max_age_hours: float, policy: str = "normal",
             require_verified: bool = True) -> tuple[int, dict]:
    report: dict = {
        "gate": "stage1-evidence",
        "policy": policy,
        "live_db": str(live_db),
        "backup_dir": str(backup_dir),
        "ok": False,
        "errors": [],
        "checks": {},
    }
    errors = report["errors"]

    if policy not in POLICIES:
        errors.append(f"unknown policy {policy!r}; expected one of {POLICIES}")
        return EXIT_CONFIG, report
    if not Path(live_db).is_file():
        errors.append(f"live database not found: {live_db}")
        return EXIT_MISSING, report
    if not Path(backup_dir).is_dir():
        errors.append(f"backup directory not found: {backup_dir}")
        return EXIT_MISSING, report

    format_ok, format_report = backup_format.verify_set(
        Path(backup_dir), expected_application_id=expected_application_id,
        require_verified=require_verified)
    report["checks"]["format"] = format_report
    if not format_ok:
        errors.extend(format_report.get("errors", []))
        return EXIT_INVALID, report

    created_at = format_report.get("created_at")
    age = backup_format.age_hours(created_at)
    report["checks"]["age_hours"] = round(age, 3)
    if age < 0:
        errors.append(f"the backup is timestamped in the future ({created_at}); "
                      "freshness cannot be established")
        return EXIT_INVALID, report
    if age > max_age_hours:
        errors.append(f"the backup is {age:.3f}h old, older than the {max_age_hours}h limit")
        return EXIT_STALE, report

    backup_db = Path(backup_dir) / backup_format.DB_FILE
    evidence = db_evidence.compare_databases(
        Path(live_db), backup_db, expected_application_id=expected_application_id,
        policy=policy)
    report["checks"]["evidence"] = evidence.as_dict()
    if not evidence.ok:
        errors.extend(evidence.reasons)
        return EXIT_INVALID, report

    # Bind the manifest's recorded evidence digests to the content just verified.
    manifest = format_report.get("checks", {}).get("manifest", {})
    if manifest.get("schema_digest") != evidence.schema_digest:
        errors.append(
            "the manifest's schema_digest does not match the backup database: the set was "
            "not produced from this state")
    if manifest.get("logical_digest") != evidence.backup_logical_digest:
        errors.append(
            "the manifest's logical_digest does not match the backup database: the set was "
            "not produced from this state")
    if manifest.get("application_id") != expected_application_id:
        errors.append("the manifest application_id does not match the expected application")

    report["ok"] = not errors
    return (EXIT_OK if report["ok"] else EXIT_INVALID), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PhotoDoc AI — Stage 1 evidence gate (read-only)")
    parser.add_argument("--live-db", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--application-id", type=int, default=1346650441,
                        help="expected SQLite application_id (default: PhotoDoc 'PDAI')")
    parser.add_argument("--max-age-hours", default="24")
    parser.add_argument("--policy", default="normal", choices=POLICIES)
    parser.add_argument("--allow-unverified", action="store_true",
                        help="accept a manifest that does not record a PASS verification")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    age, age_error = parse_age_hours(args.max_age_hours)
    if age_error:
        payload = {"gate": "stage1-evidence", "ok": False, "errors": [age_error],
                   "checks": {}}
        print(json.dumps(payload, indent=2) if args.json else f"CONFIG ERROR: {age_error}")
        return EXIT_CONFIG

    code, report = evaluate(args.live_db, args.backup_dir,
                            expected_application_id=args.application_id,
                            max_age_hours=age, policy=args.policy,
                            require_verified=not args.allow_unverified)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("==================================================")
        print("      PhotoDoc AI — Stage 1 Evidence Gate         ")
        print("==================================================")
        print(f"Live database:    {report['live_db']}")
        print(f"Backup set:       {report['backup_dir']}")
        print(f"Policy:           {report['policy']}")
        age_value = report.get("checks", {}).get("age_hours")
        print(f"Age (hours):      {age_value}")
        evidence = report.get("checks", {}).get("evidence") or {}
        for table, counts in (evidence.get("tables") or {}).items():
            print(f"Evidence {table:<18} live={counts['live_rows']} "
                  f"backup={counts['backup_rows']} compared={counts['compared']}")
        print("--------------------------------------------------")
        if report["ok"]:
            print("GATE: PASS — the set matches the live database exactly.")
        else:
            print(f"GATE: FAIL (exit {code})")
            for error in report["errors"]:
                print(f"  - {error}")
        print("==================================================")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
