#!/usr/bin/env python3
"""PhotoDoc AI — deployment backup gate.

Answers one question before a cutover is allowed to proceed: *is there a
complete, checksum-verified, fresh backup set that a rollback could actually
restore from?*

Exit code 0 means yes. Any other exit code means the deployment must stop. The
gate never creates a backup and never touches live data — it only reads.

Usage::

    python scripts/backup_gate.py --backup-dir /srv/backups/20261104T030000Z
    python scripts/backup_gate.py --latest-of /srv/backups --require-outbox
    python scripts/backup_gate.py --backup-dir ... --db /data/orders.db --require-quiescent

``--json`` emits a machine-readable report for the deployment script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backup_safety import (  # noqa: E402
    backup_timestamp,
    check_quiescence,
    verify_backup_set,
)

#: Exit codes are distinct so a caller can tell "no backup" from "bad backup".
EXIT_OK = 0
EXIT_MISSING = 2
EXIT_UNVERIFIED = 3
EXIT_STALE = 4
EXIT_NOT_QUIESCENT = 5


def latest_backup_set(root: Path, *, require_outbox: bool = False,
                      require_verified: bool = True) -> Path | None:
    """Newest directory under ``root`` that looks like a complete backup set.

    A set whose manifest does not record a PASS verification is never selected:
    auto-picking an unverified backup for a cutover is exactly the failure this
    gate exists to prevent.
    """
    root = Path(root)
    if not root.is_dir():
        return None
    candidates = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name.startswith("."):
            continue
        ok, _ = verify_backup_set(entry, require_outbox=require_outbox,
                                  require_verified=require_verified)
        if ok:
            candidates.append(entry)
    if not candidates:
        return None

    # Order by the manifest timestamp, not the directory mtime. A set that was
    # copied, restored or extracted into the backup root carries an mtime from the
    # copy, not from when the backup was taken, so mtime ordering can promote an
    # older backup. The manifest is the artifact the verification path trusts.
    def sort_key(entry: Path) -> tuple:
        stamp = backup_timestamp(entry)
        return (stamp is not None, stamp.timestamp() if stamp else 0.0,
                entry.stat().st_mtime)

    candidates.sort(key=sort_key, reverse=True)
    return candidates[0]


def _classify(report: dict) -> int:
    errors = report.get("errors", [])
    if not errors:
        return EXIT_OK
    joined = " | ".join(errors)
    if "does not exist" in joined or "not a directory" in joined:
        return EXIT_MISSING
    if "too old" in joined:
        return EXIT_STALE
    if "does not record a PASS" in joined:
        return EXIT_UNVERIFIED
    return EXIT_UNVERIFIED


def _live_db_checks(backup_dir: Path, live_db: Path) -> tuple[list[str], dict]:
    """Prove the backup set is not older than the data it is supposed to protect.

    Completeness is not enough: a perfectly valid backup taken before the newest
    order still loses that order. The decisive test is whether the backup's own
    timestamp is later than the newest order in the live database.
    """
    import sqlite3
    from datetime import datetime, timezone

    errors: list[str] = []
    info: dict = {"live_db": str(live_db), "live_orders": None, "newest_order": None,
                  "backup_created": None}
    if not Path(live_db).is_file():
        errors.append(f"live database not found: {live_db}")
        return errors, info

    try:
        conn = sqlite3.connect(f"file:{Path(live_db).resolve().as_posix()}?mode=ro", uri=True, timeout=10.0)
        try:
            info["live_orders"] = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            row = conn.execute("SELECT MAX(created_at) FROM orders").fetchone()
            info["newest_order"] = row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        errors.append(f"cannot read live database: {exc}")
        return errors, info

    manifest = json.loads((Path(backup_dir) / "backup_manifest.json").read_text(encoding="utf-8"))
    created_raw = manifest.get("timestamp")
    if not created_raw:
        errors.append("backup manifest has no timestamp; cannot prove it covers current data")
        return errors, info
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError:
        errors.append(f"unparseable backup timestamp: {created_raw!r}")
        return errors, info
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    info["backup_created"] = created.isoformat()

    newest = info["newest_order"]
    if newest:
        try:
            newest_dt = datetime.fromisoformat(str(newest))
            if newest_dt.tzinfo is None:
                newest_dt = newest_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            errors.append(f"unparseable orders.created_at value: {newest!r}")
            return errors, info
        if newest_dt > created:
            errors.append(
                "backup predates the newest order: it would lose data created after "
                f"{created.isoformat()} (newest order at {newest_dt.isoformat()})")
    return errors, info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PhotoDoc AI — deployment backup gate")
    parser.add_argument("--backup-dir", type=Path, help="backup set directory to verify")
    parser.add_argument("--latest-of", type=Path, help="directory containing backup sets")
    parser.add_argument("--require-outbox", action="store_true",
                        help="require the paid-email outbox table (required when running PR #26+)")
    parser.add_argument("--max-age-hours", type=float, default=24.0,
                        help="maximum acceptable backup age in hours (default 24)")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="do not require the manifest to record a PASS verification")
    parser.add_argument("--deep", action="store_true",
                        help="also extract and re-verify the upload archive")
    parser.add_argument("--db", type=Path, help="live database to check for quiescence")
    parser.add_argument("--live-db", type=Path,
                        help="live database the backup must cover (rejects a backup older than the newest order)")
    parser.add_argument("--require-quiescent", action="store_true",
                        help="fail unless no cleanup/delivery writer is in flight")
    parser.add_argument("--worker-enabled", choices=("yes", "no", "unknown"), default="unknown")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.backup_dir and not args.latest_of:
        parser.error("provide --backup-dir or --latest-of")

    report: dict = {"gate": "backup", "ok": False, "errors": [], "checks": {}}

    target = args.backup_dir
    if target is None:
        target = latest_backup_set(args.latest_of, require_outbox=args.require_outbox,
                                   require_verified=not args.allow_unverified)
        report["selected"] = str(target) if target else None
        if target is None:
            report["errors"].append(f"no complete backup set found under {args.latest_of}")
            report["ok"] = False
            _emit(report, args.json, EXIT_MISSING)
            return EXIT_MISSING

    ok, set_report = verify_backup_set(
        target,
        require_outbox=args.require_outbox,
        max_age_hours=args.max_age_hours,
        require_verified=not args.allow_unverified,
        check_upload_archive=args.deep,
    )
    report["checks"]["backup_set"] = set_report
    report["errors"].extend(set_report.get("errors", []))
    report["ok"] = ok and not report["errors"]
    code = EXIT_OK if report["ok"] else _classify(set_report)

    if args.live_db is not None:
        live_errors, live_info = _live_db_checks(target, args.live_db)
        report["checks"]["live_coverage"] = live_info
        report["checks"]["live_coverage"]["errors"] = live_errors
        if live_errors:
            report["ok"] = False
            code = EXIT_STALE
            report["errors"].extend(live_errors)

    if args.db is not None and args.require_quiescent:
        worker_enabled = {"yes": True, "no": False}.get(args.worker_enabled)
        quiet, quiet_report = check_quiescence(args.db, worker_enabled=worker_enabled)
        report["checks"]["quiescence"] = quiet_report
        if not quiet:
            report["ok"] = False
            code = EXIT_NOT_QUIESCENT
            report["errors"].append(
                "not quiescent: " + "; ".join(quiet_report.get("reasons", [])))

    _emit(report, args.json, code)
    return code


def _emit(report: dict, as_json: bool, code: int) -> None:
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return
    print("==================================================")
    print("      PhotoDoc AI — Deployment Backup Gate        ")
    print("==================================================")
    checks = report.get("checks", {}).get("backup_set", {})
    if checks:
        print(f"Backup directory:         {checks.get('backup_dir')}")
        print(f"SQLite integrity:         {checks.get('sqlite_integrity')}")
        print(f"Application identity:     {checks.get('application_id')}")
        print(f"Tables:                   {', '.join(checks.get('tables') or [])}")
        print(f"Orders:                   {checks.get('orders_count')}")
        print(f"Paid-email outbox:        {'present ' + str(checks.get('outbox_states')) if checks.get('outbox_present') else 'absent'}")
        print(f"Age (hours):              {checks.get('age_hours')}")
        print(f"Recovery verification:    {checks.get('verified')}")
    quiet = report.get("checks", {}).get("quiescence")
    if quiet:
        print(f"Quiescent:                {quiet.get('quiescent')} {quiet.get('reasons') or ''}")
    print("--------------------------------------------------")
    if code == EXIT_OK:
        print("GATE: PASS — a verified backup set is available for rollback.")
    else:
        print(f"GATE: FAIL (exit {code}) — deployment must not proceed.")
        for error in report.get("errors", []):
            print(f"  - {error}")
    print("==================================================")


if __name__ == "__main__":
    raise SystemExit(main())
