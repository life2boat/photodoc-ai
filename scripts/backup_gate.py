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
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backup_safety import (  # noqa: E402
    OUTBOX_TABLE,
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


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _pre_outbox_transition_checks(backup_dir: Path, live_db: Path) -> tuple[list[str], dict]:
    """Authorise a backup that lacks ``paid_email_outbox`` — once, and narrowly.

    PR #26 added the paid-email outbox. The first deployment onto a database that
    predates it cannot produce a backup containing the table without migrating the
    live database first, so the standard gate (``--require-outbox``) would refuse
    the very transition that creates the table.

    This check exists only for that single step. It is deliberately strict:

    * the operator must ask for it explicitly;
    * the **live** database must lack the table (already transitioned => refuse);
    * the **backup** must lack the table (use the normal gate instead);
    * the backup must not hold less email-delivery evidence than the live
      database — fewer orders, fewer delivery jobs, fewer ``paid_email`` jobs or
      fewer ``payment_email_sent_at`` timestamps all mean evidence could be lost.

    Once the live database has the table, this path can never be taken again: the
    flag itself becomes an error, so it cannot silently degrade the gate later.
    """
    errors: list[str] = []
    info: dict = {
        "requested": True,
        "applicable": False,
        "live_has_outbox": None,
        "backup_has_outbox": None,
        "evidence": {},
    }
    backup_dir = Path(backup_dir)
    live_db = Path(live_db)

    if not live_db.is_file():
        return [f"live database not found: {live_db}; the transition needs it to prove "
                "the outbox is genuinely absent"], info

    try:
        live = sqlite3.connect(f"file:{live_db.resolve().as_posix()}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        return [f"cannot open live database for the transition check: {exc}"], info
    try:
        info["live_has_outbox"] = _table_exists(live, OUTBOX_TABLE)
    except sqlite3.Error as exc:
        return [f"cannot inspect the live database for the transition check: {exc}"], info
    finally:
        live.close()

    try:
        conn = sqlite3.connect(f"file:{backup_dir.joinpath('orders.db').resolve().as_posix()}?mode=ro",
                               uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        return [f"cannot open backup database for the transition check: {exc}"], info
    try:
        info["backup_has_outbox"] = _table_exists(conn, OUTBOX_TABLE)
    except sqlite3.Error as exc:
        return [f"cannot inspect the backup database for the transition check: {exc}"], info
    finally:
        conn.close()

    if info["live_has_outbox"]:
        errors.append(
            f"the live database already contains {OUTBOX_TABLE}: the pre-outbox transition "
            "is no longer applicable. Re-run the gate without --allow-pre-outbox-transition "
            "so the table is required.")
    if info["backup_has_outbox"]:
        errors.append(
            f"the backup already contains {OUTBOX_TABLE}: it is not a pre-outbox backup, so "
            "the transition flag must not be used. Re-run without it.")
    if errors:
        return errors, info

    queries = {
        "orders": "SELECT COUNT(*) FROM orders",
        "delivery_jobs": "SELECT COUNT(*) FROM delivery_jobs",
        "paid_email_jobs": "SELECT COUNT(*) FROM delivery_jobs WHERE kind='paid_email'",
        "email_sent_orders": "SELECT COUNT(*) FROM orders WHERE payment_email_sent_at IS NOT NULL",
    }
    for label, query in queries.items():
        live_count = backup_count = None
        try:
            conn = sqlite3.connect(f"file:{live_db.resolve().as_posix()}?mode=ro", uri=True, timeout=10.0)
            try:
                live_count = conn.execute(query).fetchone()[0]
            finally:
                conn.close()
            conn = sqlite3.connect(
                f"file:{backup_dir.joinpath('orders.db').resolve().as_posix()}?mode=ro",
                uri=True, timeout=10.0)
            try:
                backup_count = conn.execute(query).fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            errors.append(f"cannot compare {label} between live and backup: {exc}")
            continue
        info["evidence"][label] = {"live": live_count, "backup": backup_count}
        if backup_count < live_count:
            errors.append(
                f"the backup holds fewer {label} than the live database "
                f"({backup_count} < {live_count}); refusing the transition because "
                "email-delivery evidence could be lost")

    info["applicable"] = not errors
    return errors, info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PhotoDoc AI — deployment backup gate")
    parser.add_argument("--backup-dir", type=Path, help="backup set directory to verify")
    parser.add_argument("--latest-of", type=Path, help="directory containing backup sets")
    parser.add_argument("--require-outbox", action="store_true",
                        help="require the paid-email outbox table (required when running PR #26+)")
    parser.add_argument("--allow-pre-outbox-transition", action="store_true",
                        help="ONE-TIME pre-PR#26 transition: allow a backup without "
                             "paid_email_outbox, but only when the live database also lacks "
                             "it and no email evidence would be lost. Requires --live-db.")
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

    # The transition flag and the outbox requirement contradict each other: one
    # says the table is absent, the other says it must be present. Refuse rather
    # than pick a winner.
    if args.allow_pre_outbox_transition and args.require_outbox:
        report["errors"].append(
            "--require-outbox and --allow-pre-outbox-transition are mutually exclusive: "
            "the transition is for backups without the table, the requirement is for "
            "backups with it")
        _emit(report, args.json, EXIT_UNVERIFIED)
        return EXIT_UNVERIFIED

    if args.allow_pre_outbox_transition and args.live_db is None:
        report["errors"].append(
            "--allow-pre-outbox-transition requires --live-db so the gate can prove the "
            "live database genuinely lacks paid_email_outbox")
        _emit(report, args.json, EXIT_UNVERIFIED)
        return EXIT_UNVERIFIED

    # While transitioning, the outbox requirement is off — but only until the live
    # database gains the table, at which point the transition check fails closed.
    require_outbox = args.require_outbox and not args.allow_pre_outbox_transition

    target = args.backup_dir
    if target is None:
        target = latest_backup_set(args.latest_of, require_outbox=require_outbox,
                                   require_verified=not args.allow_unverified)
        report["selected"] = str(target) if target else None
        if target is None:
            report["errors"].append(f"no complete backup set found under {args.latest_of}")
            report["ok"] = False
            _emit(report, args.json, EXIT_MISSING)
            return EXIT_MISSING

    ok, set_report = verify_backup_set(
        target,
        require_outbox=require_outbox,
        max_age_hours=args.max_age_hours,
        require_verified=not args.allow_unverified,
        check_upload_archive=args.deep,
    )
    report["checks"]["backup_set"] = set_report
    report["errors"].extend(set_report.get("errors", []))
    report["ok"] = ok and not report["errors"]
    code = EXIT_OK if report["ok"] else _classify(set_report)

    if args.allow_pre_outbox_transition:
        transition_errors, transition_info = _pre_outbox_transition_checks(target, args.live_db)
        report["checks"]["pre_outbox_transition"] = transition_info
        transition_info["errors"] = transition_errors
        if transition_errors:
            report["ok"] = False
            code = EXIT_UNVERIFIED
            report["errors"].extend(transition_errors)
        else:
            report["warning"] = (
                "PRE-OUTBOX TRANSITION: this backup has no paid_email_outbox table and the "
                "live database does not have one either. The deployment creates the table; "
                "from then on every backup must pass the normal outbox requirement.")
            report["transition"] = True

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
    transition = report.get("checks", {}).get("pre_outbox_transition")
    if transition:
        print(f"Pre-outbox transition:    requested  "
              f"live_has_outbox={transition.get('live_has_outbox')} "
              f"backup_has_outbox={transition.get('backup_has_outbox')} "
              f"applicable={transition.get('applicable')}")
        for label, counts in (transition.get("evidence") or {}).items():
            print(f"  evidence {label:<18} live={counts['live']} backup={counts['backup']}")
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
