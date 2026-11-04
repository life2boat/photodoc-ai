#!/usr/bin/env python3
"""PhotoDoc AI — deployment backup gate.

Answers one question before a cutover is allowed to proceed: *is there a complete,
checksum-verified, current backup set that a rollback could actually restore from,
and does it preserve the live database exactly?*

Exit code 0 means yes. Any other exit code means the deployment must stop. The
gate never writes to either database.

Exit codes::

    0  pass
    2  no complete backup set found
    3  a set exists but is not verified / evidence comparison failed
    4  too old, or older than the live data
    5  writers are in flight, or quiescence could not be proven
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

#: SQLite's own bookkeeping. ``sqlite_sequence`` is *not* ignored: it records the
#: high-water mark of every AUTOINCREMENT table and is part of the database's
#: identity, so a backup that lost it would hand out ids again after a restore.
_INTERNAL_PREFIX = "sqlite_"
_COMPARED_INTERNAL = {"sqlite_sequence"}


# --------------------------------------------------------------------------- inputs
def parse_age_hours(raw: object) -> tuple[float | None, str | None]:
    """Validate ``--max-age-hours``.

    ``float("nan")`` is a real hazard: every comparison with NaN is False, so a
    stray ``--max-age-hours nan`` would silently disable the freshness check.
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
        return None, (f"--max-age-hours is unreasonably large ({value}); a freshness limit "
                      "above ten years is a configuration error")
    return value, None


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(readonly_uri(path), uri=True, timeout=10.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _open_for_lock(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    """Open for a ``BEGIN EXCLUSIVE`` probe.

    ``mode=ro`` cannot take a write lock, so the probe needs ``mode=rw``. ``rw``
    never creates a missing file (unlike ``rwc``), and the transaction is rolled
    back without writing anything — the only goal is to prove that no other
    connection holds a lock.
    """
    uri = readonly_uri(path).replace("mode=ro", "mode=rw")
    return sqlite3.connect(uri, uri=True, timeout=timeout)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _comparable_tables(tables: set[str]) -> list[str]:
    """Every durable table whose contents must survive a restore.

    Derived from the schema instead of a hand-written list, so a new stateful
    table is compared automatically rather than silently ignored.
    """
    return sorted(t for t in tables
                  if t in _COMPARED_INTERNAL or not t.startswith(_INTERNAL_PREFIX))


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{table}")'))


def _table_rows(conn: sqlite3.Connection, table: str) -> dict[object, tuple]:
    """All rows of ``table`` keyed by rowid (or the first column without rowid)."""
    columns = _table_columns(conn, table)
    if not columns:
        return {}
    projection = ", ".join(f'"{c}"' for c in columns)
    try:
        cursor = conn.execute(f'SELECT rowid, {projection} FROM "{table}"')
        return {row[0]: tuple(row[1:]) for row in cursor}
    except sqlite3.OperationalError:
        # WITHOUT ROWID table; fall back to the declared primary key column.
        cursor = conn.execute(f'SELECT {projection} FROM "{table}"')
        return {row[0]: tuple(row) for row in cursor}


# ---------------------------------------------------------------------- snapshots
def _snapshot_into(conn: sqlite3.Connection, info: dict) -> None:
    """Populate ``info`` with schema and full row contents (caller holds the txn)."""
    tables = _table_names(conn)
    info["tables"] = sorted(tables)
    compared = _comparable_tables(tables)
    info["compared_tables"] = compared
    info["schema"] = {t: _table_columns(conn, t) for t in compared}
    info["data"] = {t: _table_rows(conn, t) for t in compared}


def _live_snapshot(live_db: Path, *, exclusive: bool = False) -> tuple[dict, list[str]]:
    """Read everything the gate needs from the live database in ONE transaction.

    A single ``BEGIN`` fixes the snapshot, and the quiescence counters are read
    from that same snapshot so the comparison and the "no writer in flight" answer
    describe one moment rather than two. With ``exclusive`` the transaction is
    ``BEGIN EXCLUSIVE``, which additionally proves that no other connection holds
    a lock at that instant. Neither can prove what happens after this process
    exits, which is why the deployment protocol also pins the writers down — see
    ``scripts/writer_barrier.py`` and the trusted-boundary note in
    ``docs/BACKUP_AND_RESTORE.md``.
    """
    errors: list[str] = []
    info: dict = {
        "live_db": str(live_db),
        "integrity": None,
        "application_id": None,
        "tables": [],
        "compared_tables": [],
        "schema": {},
        "data": {},
        "processing_jobs": None,
        "cleanup_in_flight": None,
        "exclusive_lock": None,
        "snapshot": "single-read-transaction",
    }
    if not Path(live_db).is_file():
        errors.append(f"live database not found: {live_db}")
        return info, errors

    try:
        conn = _open_for_lock(live_db) if exclusive else _open_ro(live_db)
    except sqlite3.Error as exc:
        errors.append(f"cannot open the live database: {exc}")
        return info, errors

    try:
        if exclusive:
            try:
                conn.execute("BEGIN EXCLUSIVE")
                info["exclusive_lock"] = True
            except sqlite3.OperationalError as exc:
                info["exclusive_lock"] = False
                errors.append(
                    f"cannot acquire an exclusive lock on the live database ({exc}); another "
                    "connection is using it, so quiescence is not proven")
                return info, errors
        else:
            conn.execute("BEGIN")
        info["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        info["application_id"] = conn.execute("PRAGMA application_id").fetchone()[0]
        _snapshot_into(conn, info)

        if "delivery_jobs" in info["tables"]:
            now = datetime.now(timezone.utc).isoformat()
            info["processing_jobs"] = conn.execute(
                "SELECT COUNT(*) FROM delivery_jobs WHERE state='processing'").fetchone()[0]
            info["cleanup_in_flight"] = conn.execute(
                "SELECT COUNT(*) FROM delivery_jobs WHERE cleanup_phase='deleting' OR "
                "(cleanup_state='processing' AND "
                "(cleanup_lease_until IS NULL OR cleanup_lease_until > ?))", (now,)).fetchone()[0]
        conn.rollback()
    except sqlite3.Error as exc:
        errors.append(f"cannot read the live database: {exc}")
    finally:
        conn.close()

    if info["integrity"] not in (None, "ok"):
        errors.append(f"live database integrity_check failed: {info['integrity']}")
    if info["application_id"] is not None and info["application_id"] != APPLICATION_ID:
        errors.append(
            f"live database application_id is {info['application_id']}, expected {APPLICATION_ID} "
            "— refusing to compare against a different database")
    for table in CORE_TABLES:
        if table not in info["tables"]:
            errors.append(f"live database is missing required table: {table}")
    if not info["data"] and not errors:
        errors.append("live database has no comparable tables; refusing to treat it as verified")
    return info, errors


def _backup_snapshot(backup_dir: Path) -> tuple[dict, list[str]]:
    """Read the same shapes from the backup database, in one transaction."""
    errors: list[str] = []
    info: dict = {"tables": [], "compared_tables": [], "schema": {}, "data": {},
                  "application_id": None}
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
        _snapshot_into(conn, info)
        conn.rollback()
    except sqlite3.Error as exc:
        errors.append(f"cannot read the backup database: {exc}")
    finally:
        conn.close()
    return info, errors


# --------------------------------------------------------------------- comparison
def _compare_schema(live: dict, backup: dict) -> list[str]:
    """Reject an unexpected schema on either side.

    A table or column that exists in one database and not the other means the
    backup does not describe the live database: restoring it would either drop
    data or restore into a schema the new code cannot use.
    """
    errors: list[str] = []
    live_tables = set(live.get("compared_tables") or [])
    backup_tables = set(backup.get("compared_tables") or [])
    for missing in sorted(live_tables - backup_tables):
        errors.append(f"the backup is missing the table {missing!r} that the live database has")
    for extra in sorted(backup_tables - live_tables):
        errors.append(f"the backup has the table {extra!r} that the live database does not")
    for table in sorted(live_tables & backup_tables):
        live_cols = tuple(live["schema"].get(table) or ())
        backup_cols = tuple(backup["schema"].get(table) or ())
        if live_cols != backup_cols:
            errors.append(
                f"table {table!r} has a different schema in the backup: "
                f"live={live_cols} backup={backup_cols}")
    return errors


def _compare_evidence(live: dict, backup: dict) -> tuple[list[str], dict]:
    """Prove the backup preserves every persisted row of the live database.

    Comparison is schema-derived: every column of every durable table, keyed by
    rowid. That is deliberate — a hand-picked field list is exactly how an earlier
    revision missed ``order_amount``, customer details and ``remote_path``. An
    order whose price or recipient changed between the backup and now would be
    silently rolled back by a restore.
    """
    errors: list[str] = []
    info: dict = {"tables": {}, "differences": []}
    errors.extend(_compare_schema(live, backup))

    live_tables = set(live.get("compared_tables") or [])
    backup_tables = set(backup.get("compared_tables") or [])
    for table in sorted(live_tables & backup_tables):
        columns = tuple(live["schema"].get(table) or ())
        live_rows = live["data"].get(table) or {}
        backup_rows = backup["data"].get(table) or {}
        compared = 0
        for key, live_row in live_rows.items():
            backup_row = backup_rows.get(key)
            if backup_row is None:
                info["differences"].append({"table": table, "key": key, "issue": "missing"})
                errors.append(
                    f"the backup does not contain row {key!r} of table {table!r}: restoring it "
                    "would delete data that exists only in the live database")
                continue
            compared += 1
            if tuple(live_row) != tuple(backup_row):
                for name, live_value, backup_value in zip(columns, live_row, backup_row):
                    if live_value != backup_value:
                        info["differences"].append(
                            {"table": table, "key": key, "column": name,
                             "live": live_value, "backup": backup_value})
                        errors.append(
                            f"table {table!r} row {key!r} column {name!r} differs between live and "
                            f"backup (live={live_value!r}, backup={backup_value!r}): restoring the "
                            "backup would roll this record back")
                        break
        info["tables"][table] = {"live_rows": len(live_rows), "backup_rows": len(backup_rows),
                                 "compared": compared}
    return errors, info


def _order_timestamps(snapshot: dict) -> list[str]:
    """``created_at`` values from the live snapshot, for the coverage check."""
    table = "orders"
    if table not in (snapshot.get("compared_tables") or []):
        return []
    columns = snapshot["schema"].get(table) or ()
    if "created_at" not in columns:
        return []
    index = columns.index("created_at")
    values = []
    for row in (snapshot["data"].get(table) or {}).values():
        value = row[index]
        if value:
            values.append(str(value))
    return values


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
def _pre_outbox_transition_checks(backup_dir: Path, live_db: Path,
                                  live_snap: dict | None = None) -> tuple[list[str], dict]:
    """Authorise a backup that lacks ``paid_email_outbox`` — once, and narrowly."""
    errors: list[str] = []
    info: dict = {"requested": True, "applicable": False,
                  "live_has_outbox": None, "backup_has_outbox": None}
    backup_dir, live_db = Path(backup_dir), Path(live_db)

    if live_snap is None:
        if not live_db.is_file():
            return ([f"live database not found: {live_db}; the transition needs it to prove the "
                     "outbox is genuinely absent"], info)
        live_snap, live_errors = _live_snapshot(live_db)
        if live_errors:
            return live_errors, info

    backup_snap, backup_errors = _backup_snapshot(backup_dir)
    if backup_errors:
        return backup_errors, info

    info["live_has_outbox"] = OUTBOX_TABLE in (live_snap.get("tables") or [])
    info["backup_has_outbox"] = OUTBOX_TABLE in (backup_snap.get("tables") or [])

    if info["live_has_outbox"]:
        errors.append(
            f"the live database already contains {OUTBOX_TABLE}: the pre-outbox transition is no "
            "longer applicable. Re-run without --allow-pre-outbox-transition so the table is "
            "required.")
    if info["backup_has_outbox"]:
        errors.append(
            f"the backup already contains {OUTBOX_TABLE}: it is not a pre-outbox backup, so the "
            "transition flag must not be used. Re-run without it.")
    if errors:
        return errors, info

    compare_errors, compare_info = _compare_evidence(live_snap, backup_snap)
    info["evidence"] = compare_info
    errors.extend(compare_errors)
    info["applicable"] = not errors
    return errors, info


# ---------------------------------------------------------------------- quiescence
def _quiescence_from(live_snap: dict, *, worker_state: str) -> tuple[bool, dict]:
    """Decide quiescence from the SAME snapshot the evidence comparison used.

    Reading it separately is a false-PASS generator: an order created between the
    two reads passes both checks. ``--worker-enabled unknown`` is not evidence of
    a stopped worker, so it is treated as unproven rather than assumed.
    """
    info: dict = {
        "worker_enabled": {"yes": True, "no": False}.get(worker_state),
        "worker_state": worker_state,
        "processing_jobs": live_snap.get("processing_jobs"),
        "cleanup_in_flight": live_snap.get("cleanup_in_flight"),
        "exclusive_lock": live_snap.get("exclusive_lock"),
        "source": "same snapshot as the evidence comparison",
        "quiescent": False,
        "reasons": [],
    }
    reasons: list[str] = info["reasons"]
    if worker_state == "unknown":
        reasons.append("the delivery worker state is unknown; a running worker cannot be ruled out")
    elif info["worker_enabled"]:
        reasons.append("the delivery worker is enabled")
    if live_snap.get("processing_jobs"):
        reasons.append(f"{live_snap['processing_jobs']} delivery job(s) are processing")
    if live_snap.get("cleanup_in_flight"):
        reasons.append(f"{live_snap['cleanup_in_flight']} cleanup operation(s) are in flight")
    info["quiescent"] = not reasons
    return info["quiescent"], info


def _same_database(path_a: Path, path_b: Path) -> tuple[bool, str]:
    try:
        a, b = Path(path_a).resolve(), Path(path_b).resolve()
        a_stat, b_stat = a.stat(), b.stat()
    except OSError as exc:
        return False, f"cannot stat both database paths: {exc}"
    if (a_stat.st_dev, a_stat.st_ino) != (b_stat.st_dev, b_stat.st_ino):
        return False, (f"--db ({a}) and --live-db ({b}) are different files; the quiescence check "
                       "must inspect the database that will be migrated")
    return True, "same file"


# ----------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PhotoDoc AI — deployment backup gate")
    parser.add_argument("--backup-dir", type=Path, help="backup set directory to verify")
    parser.add_argument("--latest-of", type=Path, help="directory containing backup sets")
    parser.add_argument("--quiescence-only", action="store_true",
                        help="run only the writer-quiescence check (requires --db or --live-db)")
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
    parser.add_argument("--require-exclusive-lock", action="store_true",
                        help="also take BEGIN EXCLUSIVE to prove no other connection holds a lock")
    parser.add_argument("--worker-enabled", choices=("yes", "no", "unknown"), default="unknown")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report: dict = {"gate": "backup", "ok": False, "errors": [], "checks": {}}

    age, age_error = parse_age_hours(args.max_age_hours)
    if age_error:
        report["errors"].append(age_error)
        _emit(report, args.json, EXIT_CONFIG)
        return EXIT_CONFIG

    if args.quiescence_only:
        return _run_quiescence_only(args, report)

    if not args.backup_dir and not args.latest_of:
        report["errors"].append("provide --backup-dir, --latest-of or --quiescence-only")
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

    if args.require_quiescent and args.db is None and args.live_db is None:
        report["errors"].append(
            "--require-quiescent needs --db or --live-db: without a database the gate cannot "
            "prove quiescence, and pretending it did would defeat the check")
        _emit(report, args.json, EXIT_CONFIG)
        return EXIT_CONFIG

    # The database that is checked must be the database that will be migrated.
    if args.db is not None and args.live_db is not None:
        same, detail = _same_database(args.db, args.live_db)
        report["checks"]["database_identity"] = {"same_file": same, "detail": detail}
        if not same:
            report["errors"].append(detail)
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

    deep_ok = (not args.deep) or set_report.get("upload_archive_verified") is True
    if args.deep and not deep_ok:
        report["ok"] = False
        code = EXIT_UNVERIFIED

    # One live snapshot feeds the comparison, coverage and quiescence.
    live_snap: dict | None = None
    if args.live_db is not None:
        live_snap, live_errors = _live_snapshot(
            args.live_db, exclusive=args.require_exclusive_lock)
        report["checks"]["live_database"] = {
            k: v for k, v in live_snap.items() if k not in ("schema", "data")}
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

            timestamps = _order_timestamps(live_snap)
            newest = max(timestamps, default=None)
            info = report["checks"].setdefault("live_coverage", {})
            info["live_orders"] = len(timestamps)
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

    if args.allow_pre_outbox_transition:
        transition_errors, transition_info = _pre_outbox_transition_checks(
            target, args.live_db, live_snap)
        report["checks"]["pre_outbox_transition"] = transition_info
        transition_info["errors"] = transition_errors
        if transition_errors:
            report["ok"] = False
            code = EXIT_UNVERIFIED
            report["errors"].extend(transition_errors)
        else:
            report["warning"] = (
                "PRE-OUTBOX TRANSITION: this backup has no paid_email_outbox table and the live "
                "database does not have one either. The deployment creates the table; from then on "
                "every backup must pass the normal outbox requirement.")
            report["transition"] = True

    if args.require_quiescent:
        if live_snap is None:
            # --db without --live-db: take the one snapshot we still need.
            live_snap, snap_errors = _live_snapshot(
                args.db, exclusive=args.require_exclusive_lock)
            if snap_errors:
                report["ok"] = False
                code = EXIT_UNVERIFIED
                report["errors"].extend(snap_errors)
        quiet, quiet_report = _quiescence_from(live_snap or {}, worker_state=args.worker_enabled)
        report["checks"]["quiescence"] = quiet_report
        if not quiet:
            report["ok"] = False
            code = EXIT_NOT_QUIESCENT
            report["errors"].append(
                "not quiescent: " + "; ".join(quiet_report.get("reasons", [])))

    _emit(report, args.json, code)
    return code


def _run_quiescence_only(args, report: dict) -> int:
    """Standalone writer check: usable before any backup exists (see runbook §3)."""
    if args.db is None and args.live_db is None:
        report["errors"].append("--quiescence-only needs --db or --live-db")
        _emit(report, args.json, EXIT_CONFIG)
        return EXIT_CONFIG
    db = args.db or args.live_db
    snap, errors = _live_snapshot(db, exclusive=args.require_exclusive_lock)
    report["checks"]["live_database"] = {
        k: v for k, v in snap.items() if k not in ("schema", "data")}
    report["checks"]["live_database"]["errors"] = errors
    if errors:
        report["errors"].extend(errors)
        _emit(report, args.json, EXIT_UNVERIFIED)
        return EXIT_UNVERIFIED
    quiet, quiet_report = _quiescence_from(snap, worker_state=args.worker_enabled)
    report["checks"]["quiescence"] = quiet_report
    report["ok"] = quiet
    code = EXIT_OK if quiet else EXIT_NOT_QUIESCENT
    if not quiet:
        report["errors"].append("not quiescent: " + "; ".join(quiet_report.get("reasons", [])))
    _emit(report, args.json, code)
    return code


def _classify(report: dict) -> int:
    errors = report.get("errors", [])
    if not errors:
        return EXIT_OK
    joined = " | ".join(errors)
    if "does not exist" in joined or "not found" in joined or "no complete backup set" in joined:
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
        print(f"Paid-email outbox:        {'present' if checks.get('outbox_present') else 'absent'}")
        print(f"Age (hours):              {checks.get('age_hours')}")
        print(f"Recovery verification:    {checks.get('verified')}")
        print(f"Upload archive verified:  {checks.get('upload_archive_verified')}")
    live = report.get("checks", {}).get("live_database")
    if live:
        print(f"Live identity:            {live.get('application_id')} "
              f"(integrity {live.get('integrity')}, exclusive_lock={live.get('exclusive_lock')})")
    evidence = report.get("checks", {}).get("evidence")
    if evidence:
        for table, counts in (evidence.get("tables") or {}).items():
            print(f"Evidence {table:<18} live={counts['live_rows']} backup={counts['backup_rows']}")
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
        print("GATE: PASS — a verified backup set that matches the live database is available.")
    else:
        print(f"GATE: FAIL (exit {code}) — deployment must not proceed.")
        for error in report.get("errors", []):
            print(f"  - {error}")
    print("==================================================")


if __name__ == "__main__":
    raise SystemExit(main())
