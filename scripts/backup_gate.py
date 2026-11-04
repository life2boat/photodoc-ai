#!/usr/bin/env python3
"""PhotoDoc AI — deployment backup gate.

Answers one question before a cutover is allowed to proceed: *is there a complete,
checksum-verified, current backup set that a rollback could actually restore from,
and does it preserve the live data exactly?*

Exit code 0 means yes. Any other exit code means the deployment must stop. The
gate never creates a backup and never writes to either database — every read is a
single ``mode=ro`` transaction.

Exit codes::

    0  pass
    2  no complete backup set found
    3  a set exists but is not verified / evidence comparison failed
    4  too old, or older than the live data
    5  writers are in flight (quiescence could not be proven)
    6  invalid configuration (bad age value, contradictory flags, missing input)
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backup_safety import (  # noqa: E402
    APPLICATION_ID,
    CORE_TABLES,
    OUTBOX_TABLE,
    backup_timestamp,
    readonly_uri,
    verify_backup_set,
)

EXIT_OK = 0
EXIT_MISSING = 2
EXIT_UNVERIFIED = 3
EXIT_STALE = 4
EXIT_NOT_QUIESCENT = 5
EXIT_CONFIG = 6

#: A freshness limit beyond ten years is a configuration mistake, not a policy.
MAX_AGE_HOURS_LIMIT = 24.0 * 365.0 * 10.0


# --------------------------------------------------------------------------- inputs
def parse_age_hours(raw: object) -> tuple[float | None, str | None]:
    """Validate ``--max-age-hours``.

    ``float("nan")`` is a real hazard: every comparison with NaN is False, so a
    stray ``--max-age-hours nan`` would silently disable the freshness check
    entirely. Only a finite, positive, sane number is accepted.
    """
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, f"--max-age-hours is not a number: {raw!r}"
    if not math.isfinite(value):
        return None, f"--max-age-hours must be a finite number, got {raw!r}"
    if value <= 0:
        return None, f"--max-age-hours must be greater than zero, got {raw!r}"
    if value > MAX_AGE_HOURS_LIMIT:
        return None, (f"--max-age-hours is unreasonably large ({value}); "
                      "a freshness limit above ten years is a configuration error")
    return value, None


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(readonly_uri(path), uri=True, timeout=10.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ------------------------------------------------------------------ live snapshot
def _live_snapshot(live_db: Path) -> tuple[dict, list[str]]:
    """Read everything the gate needs from the live database in ONE transaction.

    Reading counts, schema and rows through separate statements lets the database
    change between them: an outbox table created after the first read, an order
    paid between two queries. A single ``BEGIN`` read transaction fixes the
    snapshot; the quiescence requirement is what keeps it valid up to cutover.
    """
    errors: list[str] = []
    info: dict = {
        "live_db": str(live_db),
        "integrity": None,
        "application_id": None,
        "tables": [],
        "orders": {},
        "jobs": {},
        "outbox": None,
        "processing_jobs": None,
        "cleanup_in_flight": None,
        "snapshot": "single-read-transaction",
    }
    if not Path(live_db).is_file():
        errors.append(f"live database not found: {live_db}")
        return info, errors

    try:
        conn = _open_ro(live_db)
    except sqlite3.Error as exc:
        errors.append(f"cannot open the live database: {exc}")
        return info, errors

    try:
        conn.execute("BEGIN")  # consistent snapshot for every read below
        info["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        info["application_id"] = conn.execute("PRAGMA application_id").fetchone()[0]
        tables = _table_names(conn)
        info["tables"] = sorted(tables)

        if "orders" in tables:
            info["orders"] = {
                row[0]: row[1:] for row in conn.execute(
                    "SELECT id, payment_status, payment_amount, paid_at, payment_email_sent_at, "
                    "created_at FROM orders")
            }
        if "delivery_jobs" in tables:
            info["jobs"] = {
                row[0]: row[1:] for row in conn.execute(
                    "SELECT id, order_id, kind, state, attempts, cleanup_state, cleanup_phase, "
                    "local_deleted_at, content_sha256 FROM delivery_jobs")
            }
            now = datetime.now(timezone.utc).isoformat()
            info["processing_jobs"] = conn.execute(
                "SELECT COUNT(*) FROM delivery_jobs WHERE state='processing'", ).fetchone()[0]
            info["cleanup_in_flight"] = conn.execute(
                "SELECT COUNT(*) FROM delivery_jobs WHERE cleanup_phase='deleting' OR "
                "(cleanup_state='processing' AND "
                "(cleanup_lease_until IS NULL OR cleanup_lease_until > ?))", (now,)).fetchone()[0]
        if OUTBOX_TABLE in tables:
            info["outbox"] = {
                row[0]: row[1:] for row in conn.execute(
                    f"SELECT order_id, state, attempts, accepted_at FROM {OUTBOX_TABLE}")
            }
        conn.rollback()
    except sqlite3.Error as exc:
        errors.append(f"cannot read the live database: {exc}")
    finally:
        conn.close()

    if info["integrity"] not in (None, "ok"):
        errors.append(f"live database integrity_check failed: {info['integrity']}")
    if info["application_id"] is not None and info["application_id"] != APPLICATION_ID:
        errors.append(
            f"live database application_id is {info['application_id']}, "
            f"expected {APPLICATION_ID} — refusing to compare against a different database")
    for table in CORE_TABLES:
        if table not in info["tables"]:
            errors.append(f"live database is missing required table: {table}")
    return info, errors


# --------------------------------------------------------------------- comparison
def _compare_evidence(live: dict, backup: dict) -> tuple[list[str], dict]:
    """Prove the backup preserves the live rows, field by field.

    Equal *counts* are not evidence: a backup can hold five orders with the same
    ids in a different payment state, or the same number of jobs in different
    states, and restoring it would silently roll back payments or lose the record
    that an email attempt was ambiguous. This compares identity and the material
    fields of every row the live database has.
    """
    errors: list[str] = []
    info: dict = {"orders_compared": 0, "jobs_compared": 0, "outbox_compared": 0,
                  "missing_orders": [], "changed_orders": [], "missing_jobs": [],
                  "changed_jobs": [], "changed_outbox": []}
    order_fields = ("payment_status", "payment_amount", "paid_at", "payment_email_sent_at")
    job_fields = ("order_id", "kind", "state", "attempts", "cleanup_state",
                  "cleanup_phase", "local_deleted_at", "content_sha256")

    live_orders = live.get("orders") or {}
    backup_orders = backup.get("orders") or {}
    for order_id, live_row in live_orders.items():
        backup_row = backup_orders.get(order_id)
        if backup_row is None:
            info["missing_orders"].append(order_id)
            errors.append(
                f"the backup does not contain order #{order_id}: restoring it would delete "
                "that order")
            continue
        info["orders_compared"] += 1
        for name, live_value, backup_value in zip(order_fields, live_row, backup_row):
            if live_value != backup_value:
                info["changed_orders"].append({"order_id": order_id, "field": name,
                                               "live": live_value, "backup": backup_value})
                errors.append(
                    f"order #{order_id} field {name} differs between live and backup "
                    f"(live={live_value!r}, backup={backup_value!r}): restoring the backup "
                    "would roll payment or email evidence back")
                break

    live_jobs = live.get("jobs") or {}
    backup_jobs = backup.get("jobs") or {}
    for job_id, live_row in live_jobs.items():
        backup_row = backup_jobs.get(job_id)
        if backup_row is None:
            info["missing_jobs"].append(job_id)
            errors.append(
                f"the backup does not contain delivery job {job_id}: restoring it would lose "
                "the record of that delivery or email attempt")
            continue
        info["jobs_compared"] += 1
        for name, live_value, backup_value in zip(job_fields, live_row, backup_row):
            if live_value != backup_value:
                info["changed_jobs"].append({"job_id": job_id, "field": name,
                                             "live": live_value, "backup": backup_value})
                errors.append(
                    f"delivery job {job_id} field {name} differs between live and backup "
                    f"(live={live_value!r}, backup={backup_value!r}): restoring the backup "
                    "would revert delivery or email-attempt state")
                break

    live_outbox = live.get("outbox")
    backup_outbox = backup.get("outbox")
    if live_outbox is not None:
        if backup_outbox is None:
            errors.append(
                "the live database has paid_email_outbox but the backup does not: restoring it "
                "would lose at-most-once email evidence")
        else:
            for order_id, live_row in live_outbox.items():
                backup_row = backup_outbox.get(order_id)
                if backup_row is None:
                    info["changed_outbox"].append({"order_id": order_id, "state": "missing"})
                    errors.append(
                        f"the backup has no paid_email_outbox row for order #{order_id}: "
                        "restoring it would lose the at-most-once attempt record")
                    continue
                info["outbox_compared"] += 1
                if tuple(live_row) != tuple(backup_row):
                    info["changed_outbox"].append({"order_id": order_id,
                                                   "live": list(live_row),
                                                   "backup": list(backup_row)})
                    errors.append(
                        f"paid_email_outbox row for order #{order_id} differs between live and "
                        f"backup (live={tuple(live_row)!r}, backup={tuple(backup_row)!r}): "
                        "restoring the backup could resend an ambiguous email")
    elif backup_outbox is not None:
        errors.append(
            "the backup contains paid_email_outbox but the live database does not: this is a "
            "pre-outbox transition in the wrong direction")

    return errors, info


def _backup_snapshot(backup_dir: Path) -> tuple[dict, list[str]]:
    """Read the same shapes from the backup database, in one transaction."""
    errors: list[str] = []
    info: dict = {"orders": {}, "jobs": {}, "outbox": None, "tables": [], "application_id": None}
    db = Path(backup_dir) / "orders.db"
    if not db.is_file():
        return info, [f"backup database not found: {db}"]
    try:
        conn = _open_ro(db)
    except sqlite3.Error as exc:
        return info, [f"cannot open the backup database: {exc}"]
    try:
        conn.execute("BEGIN")
        info["application_id"] = conn.execute("PRAGMA application_id").fetchone()[0]
        tables = _table_names(conn)
        info["tables"] = sorted(tables)
        if "orders" in tables:
            info["orders"] = {
                row[0]: row[1:] for row in conn.execute(
                    "SELECT id, payment_status, payment_amount, paid_at, payment_email_sent_at "
                    "FROM orders")}
        if "delivery_jobs" in tables:
            info["jobs"] = {
                row[0]: row[1:] for row in conn.execute(
                    "SELECT id, order_id, kind, state, attempts, cleanup_state, cleanup_phase, "
                    "local_deleted_at, content_sha256 FROM delivery_jobs")}
        if OUTBOX_TABLE in tables:
            info["outbox"] = {
                row[0]: row[1:] for row in conn.execute(
                    f"SELECT order_id, state, attempts, accepted_at FROM {OUTBOX_TABLE}")}
        conn.rollback()
    except sqlite3.Error as exc:
        errors.append(f"cannot read the backup database: {exc}")
    finally:
        conn.close()
    return info, errors


# ------------------------------------------------------------------- backup choice
def latest_backup_set(root: Path, *, require_outbox: bool = False,
                      require_verified: bool = True) -> Path | None:
    """Newest set under ``root`` that verifies, ordered by manifest timestamp."""
    root = Path(root)
    if not root.is_dir():
        return None
    candidates = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or entry.name.startswith("."):
            continue
        ok, _ = verify_backup_set(entry, require_outbox=require_outbox,
                                  require_verified=require_verified)
        if ok:
            candidates.append(entry)
    if not candidates:
        return None

    def sort_key(entry: Path) -> tuple:
        stamp = backup_timestamp(entry)
        return (stamp is not None, stamp.timestamp() if stamp else 0.0, entry.stat().st_mtime)

    candidates.sort(key=sort_key, reverse=True)
    return candidates[0]


# --------------------------------------------------------------------- transition
def _pre_outbox_transition_checks(backup_dir: Path, live_db: Path) -> tuple[list[str], dict]:
    """Authorise a backup that lacks ``paid_email_outbox`` — once, and narrowly.

    Only for the first deployment onto a database older than PR #26. It is
    refused once either side has the table, and it still requires the full
    evidence comparison (:func:`_compare_evidence`), so it cannot discard rows.
    """
    errors: list[str] = []
    info: dict = {"requested": True, "applicable": False,
                  "live_has_outbox": None, "backup_has_outbox": None}
    backup_dir, live_db = Path(backup_dir), Path(live_db)

    if not live_db.is_file():
        return ([f"live database not found: {live_db}; the transition needs it to prove the "
                 "outbox is genuinely absent"], info, None)

    live_snap, live_errors = _live_snapshot(live_db)
    if live_errors:
        return live_errors, info, None
    backup_snap, backup_errors = _backup_snapshot(backup_dir)
    if backup_errors:
        return backup_errors, info, None

    info["live_has_outbox"] = OUTBOX_TABLE in live_snap["tables"]
    info["backup_has_outbox"] = OUTBOX_TABLE in backup_snap["tables"]

    if info["live_has_outbox"]:
        errors.append(
            f"the live database already contains {OUTBOX_TABLE}: the pre-outbox transition is "
            "no longer applicable. Re-run without --allow-pre-outbox-transition so the table is "
            "required.")
    if info["backup_has_outbox"]:
        errors.append(
            f"the backup already contains {OUTBOX_TABLE}: it is not a pre-outbox backup, so the "
            "transition flag must not be used. Re-run without it.")
    if errors:
        return errors, info, live_snap

    compare_errors, compare_info = _compare_evidence(live_snap, backup_snap)
    info["evidence"] = compare_info
    errors.extend(compare_errors)
    info["applicable"] = not errors
    return errors, info, live_snap


# ----------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PhotoDoc AI — deployment backup gate")
    parser.add_argument("--backup-dir", type=Path, help="backup set directory to verify")
    parser.add_argument("--latest-of", type=Path, help="directory containing backup sets")
    parser.add_argument("--require-outbox", action="store_true",
                        help="require the paid-email outbox table (required when running PR #26+)")
    parser.add_argument("--allow-pre-outbox-transition", action="store_true",
                        help="ONE-TIME pre-PR#26 transition: allow a backup without "
                             "paid_email_outbox, but only when the live database also lacks it "
                             "and no evidence would be lost. Requires --live-db.")
    parser.add_argument("--max-age-hours", default="24",
                        help="maximum acceptable backup age in hours (default 24)")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="do not require the manifest to record a PASS verification")
    parser.add_argument("--deep", action="store_true",
                        help="extract and re-verify the upload archive (mandatory for deployments)")
    parser.add_argument("--db", type=Path, help="database to inspect for writer quiescence")
    parser.add_argument("--live-db", type=Path,
                        help="live database the backup must match row for row")
    parser.add_argument("--require-quiescent", action="store_true",
                        help="fail unless no delivery/cleanup writer is in flight")
    parser.add_argument("--worker-enabled", choices=("yes", "no", "unknown"), default="unknown")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report: dict = {"gate": "backup", "ok": False, "errors": [], "checks": {}}

    age, age_error = parse_age_hours(args.max_age_hours)
    if age_error:
        report["errors"].append(age_error)
        _emit(report, args.json, EXIT_CONFIG)
        return EXIT_CONFIG

    if not args.backup_dir and not args.latest_of:
        report["errors"].append("provide --backup-dir or --latest-of")
        _emit(report, args.json, EXIT_CONFIG)
        return EXIT_CONFIG

    if args.allow_pre_outbox_transition and args.require_outbox:
        report["errors"].append(
            "--require-outbox and --allow-pre-outbox-transition are mutually exclusive: the "
            "transition is for backups without the table, the requirement is for backups with it")
        _emit(report, args.json, EXIT_CONFIG)
        return EXIT_CONFIG

    if args.allow_pre_outbox_transition and args.live_db is None:
        report["errors"].append(
            "--allow-pre-outbox-transition requires --live-db so the gate can prove the live "
            "database genuinely lacks paid_email_outbox")
        _emit(report, args.json, EXIT_CONFIG)
        return EXIT_CONFIG

    # A requested quiescence check that cannot actually run must not silently pass.
    if args.require_quiescent and args.db is None and args.live_db is None:
        report["errors"].append(
            "--require-quiescent needs --db or --live-db: without a database the gate cannot "
            "prove quiescence, and pretending it did would defeat the check")
        _emit(report, args.json, EXIT_CONFIG)
        return EXIT_CONFIG

    require_outbox = args.require_outbox and not args.allow_pre_outbox_transition

    target = args.backup_dir
    if target is None:
        target = latest_backup_set(args.latest_of, require_outbox=require_outbox,
                                   require_verified=not args.allow_unverified)
        report["selected"] = str(target) if target else None
        if target is None:
            report["errors"].append(f"no complete backup set found under {args.latest_of}")
            _emit(report, args.json, EXIT_MISSING)
            return EXIT_MISSING

    ok, set_report = verify_backup_set(
        target,
        require_outbox=require_outbox,
        max_age_hours=age,
        require_verified=not args.allow_unverified,
        check_upload_archive=args.deep,
    )
    report["checks"]["backup_set"] = set_report
    report["errors"].extend(set_report.get("errors", []))
    report["ok"] = ok and not report["errors"]
    code = EXIT_OK if report["ok"] else _classify(set_report)

    # A failed deep check must stop here: comparing rows of an archive that cannot
    # be restored would only add noise to a decision that is already "no".
    deep_ok = (not args.deep) or set_report.get("upload_archive_verified") is True
    if args.deep and not deep_ok:
        report["ok"] = False
        code = EXIT_UNVERIFIED

    if args.allow_pre_outbox_transition:
        transition_errors, transition_info, transition_snapshot = _pre_outbox_transition_checks(
            target, args.live_db)
        report["checks"]["pre_outbox_transition"] = transition_info
        transition_info["errors"] = transition_errors
        if transition_errors:
            report["ok"] = False
            code = EXIT_UNVERIFIED
            report["errors"].extend(transition_errors)
        else:
            report["warning"] = (
                "PRE-OUTBOX TRANSITION: this backup has no paid_email_outbox table and the live "
                "database does not have one either. The deployment creates the table; from then "
                "on every backup must pass the normal outbox requirement.")
            report["transition"] = True
        # Reuse the snapshot the transition check already took: reading the live
        # database twice would reintroduce exactly the inconsistency that a single
        # transaction removes.
        shared_live_snapshot = transition_snapshot
    else:
        shared_live_snapshot = None

    if args.live_db is not None:
        live_snap = shared_live_snapshot
        live_errors: list[str] = []
        if live_snap is None:
            live_snap, live_errors = _live_snapshot(args.live_db)
        report["checks"]["live_database"] = {k: v for k, v in live_snap.items()
                                             if k not in ("orders", "jobs", "outbox")}
        report["checks"]["live_database"]["errors"] = live_errors
        if live_errors:
            report["ok"] = False
            code = EXIT_UNVERIFIED
            report["errors"].extend(live_errors)
        else:
            backup_snap, backup_errors = _backup_snapshot(target)
            if backup_errors:
                report["ok"] = False
                code = EXIT_UNVERIFIED
                report["errors"].extend(backup_errors)
            else:
                compare_errors, compare_info = _compare_evidence(live_snap, backup_snap)
                report["checks"]["evidence"] = compare_info
                if compare_errors:
                    report["ok"] = False
                    code = EXIT_UNVERIFIED
                    report["errors"].extend(compare_errors)

            newest = max((row[4] for row in (live_snap.get("orders") or {}).values()
                          if row[4]), default=None)
            info = report["checks"].setdefault("live_coverage", {})
            info["live_orders"] = len(live_snap.get("orders") or {})
            info["newest_order"] = newest
            created = backup_timestamp(target)
            info["backup_created"] = created.isoformat() if created else None
            if newest and created:
                try:
                    newest_dt = datetime.fromisoformat(str(newest))
                    if newest_dt.tzinfo is None:
                        newest_dt = newest_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    report["ok"] = False
                    code = EXIT_UNVERIFIED
                    report["errors"].append(f"unparseable orders.created_at value: {newest!r}")
                else:
                    if newest_dt > created:
                        report["ok"] = False
                        code = EXIT_STALE
                        report["errors"].append(
                            "backup predates the newest order: it would lose data created after "
                            f"{created.isoformat()} (newest order at {newest_dt.isoformat()})")

    if args.require_quiescent:
        db_path = args.db or args.live_db
        worker_enabled = {"yes": True, "no": False}.get(args.worker_enabled)
        quiet, quiet_report = _quiescence_from_snapshot(db_path, worker_enabled)
        report["checks"]["quiescence"] = quiet_report
        if not quiet:
            report["ok"] = False
            code = EXIT_NOT_QUIESCENT
            report["errors"].append(
                "not quiescent: " + "; ".join(quiet_report.get("reasons", [])))

    _emit(report, args.json, code)
    return code


def _quiescence_from_snapshot(db_path: Path, worker_enabled: bool | None) -> tuple[bool, dict]:
    """Quiescence decided from one consistent snapshot, not from separate reads.

    The writer check is repeated here rather than trusted from an earlier query so
    the answer describes the same moment as the evidence comparison.
    """
    info: dict = {"db_path": str(db_path), "worker_enabled": worker_enabled,
                  "processing_jobs": None, "cleanup_in_flight": None, "quiescent": False,
                  "reasons": []}
    reasons: list[str] = info["reasons"]
    if worker_enabled:
        reasons.append("the delivery worker is enabled")

    snap, errors = _live_snapshot(Path(db_path))
    if errors:
        reasons.extend(errors)
        return False, info
    info["processing_jobs"] = snap.get("processing_jobs")
    info["cleanup_in_flight"] = snap.get("cleanup_in_flight")
    if snap.get("processing_jobs"):
        reasons.append(f"{snap['processing_jobs']} delivery job(s) are processing")
    if snap.get("cleanup_in_flight"):
        reasons.append(f"{snap['cleanup_in_flight']} cleanup operation(s) are in flight")
    info["quiescent"] = not reasons
    return info["quiescent"], info


def _classify(report: dict) -> int:
    errors = report.get("errors", [])
    if not errors:
        return EXIT_OK
    joined = " | ".join(errors)
    if "does not exist" in joined or "not found" in joined:
        return EXIT_MISSING
    if "too old" in joined:
        return EXIT_STALE
    return EXIT_UNVERIFIED


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
        print(f"Upload archive verified:  {checks.get('upload_archive_verified')}")
    live = report.get("checks", {}).get("live_database")
    if live:
        print(f"Live identity:            {live.get('application_id')} "
              f"(integrity {live.get('integrity')})")
    evidence = report.get("checks", {}).get("evidence")
    if evidence:
        print(f"Evidence compared:        orders={evidence.get('orders_compared')} "
              f"jobs={evidence.get('jobs_compared')} outbox={evidence.get('outbox_compared')}")
    transition = report.get("checks", {}).get("pre_outbox_transition")
    if transition:
        print(f"Pre-outbox transition:    requested  "
              f"live_has_outbox={transition.get('live_has_outbox')} "
              f"backup_has_outbox={transition.get('backup_has_outbox')} "
              f"applicable={transition.get('applicable')}")
    quiet = report.get("checks", {}).get("quiescence")
    if quiet:
        print(f"Quiescent:                {quiet.get('quiescent')} {quiet.get('reasons') or ''}")
    print("--------------------------------------------------")
    if report.get("warning"):
        print("####################################################################")
        print("# " + str(report["warning"]))
        print("####################################################################")
    if code == EXIT_OK:
        print("GATE: PASS — a verified backup set is available for rollback.")
    else:
        print(f"GATE: FAIL (exit {code}) — deployment must not proceed.")
        for error in report.get("errors", []):
            print(f"  - {error}")
    print("==================================================")


if __name__ == "__main__":
    raise SystemExit(main())
