#!/usr/bin/env python3
"""
PhotoDoc AI — Backup Integrity & Storage Rehearsal Verifier.

Validates extracted database and uploads consistency across P0 and P1 formats:
- Verifies SQLite application_id identity (PDAI / 1346650441).
- Verifies SQLite PRAGMA integrity_check.
- For P1 orders: verifies delivery_jobs linkage, safe path confinement,
  file existence, exact content_size, and exact content_sha256.
- For P0 orders: inspects legacy files, distinguishes confirmed local copies
  from unretained historical attachments without false-positive failures.
- Prevents directory traversal attacks (.. or path escape).
- Generates a sanitized summary report free of customer PII.
- Returns non-zero exit code on confirmed corruption, missing files, or path traversal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APPLICATION_ID_TAG = "PDAI"
APPLICATION_ID = int.from_bytes(APPLICATION_ID_TAG.encode("ascii"), "big")  # 1346650441


SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def is_valid_sha256(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    return bool(SHA256_HEX_RE.match(val))


def is_valid_size(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, int) and val >= 0:
        return True
    return False


def sanitize_filename(filename: str | None) -> str:
    base = (filename or "upload").replace("\\", "/").split("/")[-1]
    safe = re.sub(r"[^\w\.-]", "_", base, flags=re.UNICODE).strip("._")
    return safe[:120] or "upload"


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def is_safe_child_path(base_dir: Path, target_path: Path) -> bool:
    try:
        base_resolved = base_dir.resolve()
        target_resolved = target_path.resolve()
        return base_resolved in target_resolved.parents or target_resolved.parent == base_resolved
    except (ValueError, RuntimeError):
        return False


def verify_backup(
    db_path: Path,
    uploads_dir: Path,
    *,
    allow_legacy_unmarked: bool = False,
    require_p0_local: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    report: Dict[str, Any] = {
        "db_exists": False,
        "uploads_exists": False,
        "sqlite_integrity": "unknown",
        "application_id": None,
        "application_id_valid": False,
        "schema_generation": "unknown",
        "orders_count": 0,
        "p1_orders_count": 0,
        "p1_verified_files": 0,
        "p1_verified_info": 0,
        "p1_cleaned_files": 0,
        "p1_cleaned_info": 0,
        "p1_residual_uncleaned_files": [],
        "p1_delivered_uncleaned_anomalies": [],
        "p1_unexplained_missing_sources": [],
        "p1_missing_files": [],
        "p1_size_mismatches": [],
        "p1_checksum_mismatches": [],
        "p1_metadata_errors": [],
        "p1_completeness_errors": [],
        "orphan_delivery_jobs": [],
        "p0_orders_count": 0,
        "p0_confirmed_local_files": 0,
        "p0_unretained_historical_files": 0,
        "path_traversal_violations": [],
        "errors": [],
    }

    if not db_path.is_file():
        report["errors"].append(f"Database file not found: {db_path}")
        return False, report
    report["db_exists"] = True

    if not uploads_dir.is_dir():
        report["errors"].append(f"Uploads directory not found: {uploads_dir}")
        return False, report
    report["uploads_exists"] = True

    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        report["errors"].append(f"Failed to open database read-only: {exc}")
        return False, report

    try:
        # 1. SQLite PRAGMA checks
        integrity_row = conn.execute("PRAGMA integrity_check;").fetchone()
        integrity = integrity_row[0] if integrity_row else "failed"
        report["sqlite_integrity"] = integrity
        if integrity != "ok":
            report["errors"].append(f"SQLite PRAGMA integrity_check failed: {integrity}")

        app_id_row = conn.execute("PRAGMA application_id;").fetchone()
        app_id = int(app_id_row[0]) if app_id_row else None
        report["application_id"] = app_id
        if app_id == APPLICATION_ID:
            report["application_id_valid"] = True
        elif allow_legacy_unmarked and app_id == 0:
            report["application_id_valid"] = True
        else:
            report["errors"].append(
                f"Invalid application_id: {app_id} (expected {APPLICATION_ID} / {APPLICATION_ID_TAG})"
            )

        # 2. Check orders table
        has_orders = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders';"
        ).fetchone()
        if not has_orders:
            report["errors"].append("Table 'orders' does not exist")
            return False, report

        report["orders_count"] = conn.execute("SELECT COUNT(*) FROM orders;").fetchone()[0]

        # 3. Detect P1 schema vs P0 schema
        has_delivery = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs';"
            ).fetchone()
        )
        orders_cols = {row[1] for row in conn.execute("PRAGMA table_info(orders);").fetchall()}
        has_order_token = "order_token_hash" in orders_cols

        # Partition orders into P1 vs P0 strictly by order_token_hash
        if has_order_token:
            p1_orders = conn.execute(
                "SELECT id, filename, order_token_hash FROM orders "
                "WHERE order_token_hash IS NOT NULL AND order_token_hash != '' ORDER BY id;"
            ).fetchall()
            p0_orders = conn.execute(
                "SELECT id, filename, order_token_hash FROM orders "
                "WHERE order_token_hash IS NULL OR order_token_hash = '' ORDER BY id;"
            ).fetchall()
        else:
            p1_orders = []
            p0_orders = conn.execute("SELECT id, filename FROM orders ORDER BY id;").fetchall()

        report["p1_orders_count"] = len(p1_orders)
        report["p0_orders_count"] = len(p0_orders)
        report["schema_generation"] = "P1" if (has_delivery and has_order_token) else "P0"

        # If database contains P1 orders but delivery_jobs table is missing -> critical failure
        if p1_orders and not has_delivery:
            report["errors"].append(
                f"Database contains {len(p1_orders)} P1 orders but 'delivery_jobs' table is missing"
            )

        # 4. Delivery jobs verification
        if has_delivery:
            # 4.1 Check for orphan delivery jobs (jobs pointing to non-existent orders)
            orphan_jobs = conn.execute(
                "SELECT id, order_id, kind FROM delivery_jobs "
                "WHERE order_id NOT IN (SELECT id FROM orders) ORDER BY id;"
            ).fetchall()
            for oj in orphan_jobs:
                report["orphan_delivery_jobs"].append(
                    {"job_id": oj["id"], "order_id": oj["order_id"], "kind": oj["kind"]}
                )
            if orphan_jobs:
                report["errors"].append(
                    f"Found {len(orphan_jobs)} orphan delivery job(s) referencing non-existent order_id"
                )

            # 4.2 Check completeness for each P1 order
            for p1_order in p1_orders:
                oid = p1_order["id"]
                raw_filename = p1_order["filename"] or ""
                expected_attachments = [n.strip() for n in raw_filename.split(",") if n.strip()]

                # Fetch all jobs for this P1 order
                o_jobs = conn.execute(
                    "SELECT id, order_id, kind, local_path, original_name, content_sha256, content_size "
                    "FROM delivery_jobs WHERE order_id = ? ORDER BY kind, id;",
                    (oid,),
                ).fetchall()

                # If a P1 order has zero delivery_jobs -> FAIL
                if not o_jobs:
                    report["p1_completeness_errors"].append(
                        f"P1 order #{oid} has NO entries in delivery_jobs"
                    )
                    continue

                info_jobs = [j for j in o_jobs if j["kind"] == "info"]
                file_jobs = [j for j in o_jobs if j["kind"] == "file"]

                # Must have exactly one info job
                if len(info_jobs) != 1:
                    report["p1_completeness_errors"].append(
                        f"P1 order #{oid} must have exactly 1 'info' job in delivery_jobs, found {len(info_jobs)}"
                    )

                # Must have file jobs matching expected_attachments
                if len(file_jobs) != len(expected_attachments):
                    report["p1_completeness_errors"].append(
                        f"P1 order #{oid} file count mismatch: {len(file_jobs)} 'file' job(s) in delivery_jobs "
                        f"vs {len(expected_attachments)} in orders.filename"
                    )
                else:
                    # Compare sanitized names
                    exp_names_sorted = sorted(sanitize_filename(n) for n in expected_attachments)
                    act_names_sorted = sorted(sanitize_filename(j["original_name"]) for j in file_jobs)
                    if exp_names_sorted != act_names_sorted:
                        report["p1_completeness_errors"].append(
                            f"P1 order #{oid} attachment names mismatch: "
                            f"{act_names_sorted} vs expected {exp_names_sorted}"
                        )

            # 4.3 Mandatory Integrity Metadata and file presence for all file/info delivery jobs
            dj_cols = {row[1] for row in conn.execute("PRAGMA table_info(delivery_jobs);").fetchall()}
            has_cleanup_state = "cleanup_state" in dj_cols
            has_cleanup_phase = "cleanup_phase" in dj_cols
            has_local_deleted_at = "local_deleted_at" in dj_cols
            has_cleanup_error = "cleanup_error" in dj_cols
            has_delivered_at = "delivered_at" in dj_cols
            has_state = "state" in dj_cols

            query = (
                "SELECT id, order_id, kind, local_path, original_name, content_sha256, content_size"
                + (", state" if has_state else ", 'pending' AS state")
                + (", cleanup_state" if has_cleanup_state else ", 'pending' AS cleanup_state")
                + (", cleanup_phase" if has_cleanup_phase else ", 'none' AS cleanup_phase")
                + (", local_deleted_at" if has_local_deleted_at else ", NULL AS local_deleted_at")
                + (", cleanup_error" if has_cleanup_error else ", NULL AS cleanup_error")
                + (", delivered_at" if has_delivered_at else ", NULL AS delivered_at")
                + " FROM delivery_jobs WHERE kind IN ('file', 'info') ORDER BY order_id, id;"
            )
            file_and_info_jobs = conn.execute(query).fetchall()

            for job in file_and_info_jobs:
                job_id = job["id"]
                order_id = job["order_id"]
                kind = job["kind"]
                local_path_raw = job["local_path"] or ""
                raw_sha256 = job["content_sha256"]
                raw_size = job["content_size"]
                job_state = job["state"]
                cleanup_state = job["cleanup_state"]
                cleanup_phase = job["cleanup_phase"]
                local_deleted_at = job["local_deleted_at"]
                cleanup_error = job["cleanup_error"]
                delivered_at = job["delivered_at"]

                # Mandatory SHA-256 validation
                if not is_valid_sha256(raw_sha256):
                    report["p1_metadata_errors"].append(
                        {
                            "order_id": order_id,
                            "job_id": job_id,
                            "kind": kind,
                            "field": "content_sha256",
                            "value": raw_sha256,
                            "error": "Missing, empty, or non-64-hex SHA-256",
                        }
                    )
                    continue

                # Mandatory Size validation
                if not is_valid_size(raw_size):
                    report["p1_metadata_errors"].append(
                        {
                            "order_id": order_id,
                            "job_id": job_id,
                            "kind": kind,
                            "field": "content_size",
                            "value": raw_size,
                            "error": "Missing, non-integer, or negative size",
                        }
                    )
                    continue

                expected_sha256 = raw_sha256.lower()
                expected_size = raw_size

                if not local_path_raw:
                    report["errors"].append(f"Order #{order_id} job #{job_id} ({kind}) has empty local_path")
                    continue

                # Path traversal check: reject any traversal in path components or filename
                if ".." in local_path_raw or any(part == ".." for part in Path(local_path_raw).parts):
                    report["path_traversal_violations"].append(
                        {"order_id": order_id, "job_id": job_id, "path": local_path_raw}
                    )
                    continue

                filename = Path(local_path_raw).name
                candidate_file = uploads_dir / filename
                if not is_safe_child_path(uploads_dir, candidate_file):
                    report["path_traversal_violations"].append(
                        {"order_id": order_id, "job_id": job_id, "path": local_path_raw}
                    )
                    continue

                file_exists = candidate_file.is_file()

                # Flag explicit missing source anomalies:
                if cleanup_error in ("HistoricalMissingSource", "MissingSourceBeforeCleanup"):
                    report["p1_unexplained_missing_sources"].append(
                        {
                            "order_id": order_id,
                            "job_id": job_id,
                            "kind": kind,
                            "filename": filename,
                            "error": cleanup_error,
                        }
                    )
                    report["errors"].append(
                        f"Order #{order_id} job #{job_id} ({kind}) has unexplained missing source ({cleanup_error})"
                    )
                    continue

                # Privacy-by-design & cleanup contract:
                # 1. pending, processing, failed -> local source file is strictly required
                # 2. delivered, cleanup completed -> absence of local file is valid ONLY with durable deletion evidence
                # 3. delivered, cleanup pending/failed/processing -> missing file is an anomaly
                if job_state in ("pending", "processing", "failed"):
                    if not file_exists:
                        report["p1_missing_files"].append(
                            {"order_id": order_id, "job_id": job_id, "kind": kind, "filename": filename, "state": job_state}
                        )
                        continue
                elif job_state == "delivered":
                    if cleanup_state == "completed":
                        if not file_exists:
                            has_durable_evidence = (
                                cleanup_phase in ("deleted", "deleting")
                                and local_deleted_at is not None
                                and local_deleted_at != delivered_at
                            )
                            if not has_durable_evidence:
                                report["p1_unexplained_missing_sources"].append(
                                    {
                                        "order_id": order_id,
                                        "job_id": job_id,
                                        "kind": kind,
                                        "filename": filename,
                                        "error": f"MissingDurableEvidence(phase={cleanup_phase}, local_deleted_at={local_deleted_at})",
                                    }
                                )
                                report["errors"].append(
                                    f"Order #{order_id} job #{job_id} ({kind}) marked cleanup completed but lacks durable deletion evidence"
                                )
                                continue

                            if kind == "file":
                                report["p1_cleaned_files"] += 1
                            elif kind == "info":
                                report["p1_cleaned_info"] += 1
                            continue
                        else:
                            report["p1_residual_uncleaned_files"].append(
                                {
                                    "order_id": order_id,
                                    "job_id": job_id,
                                    "kind": kind,
                                    "filename": filename,
                                }
                            )
                            report["errors"].append(
                                f"Order #{order_id} job #{job_id} ({kind}) marked cleanup completed but residual file still exists on disk"
                            )
                            continue
                    else:
                        if not file_exists:
                            report["p1_delivered_uncleaned_anomalies"].append(
                                {
                                    "order_id": order_id,
                                    "job_id": job_id,
                                    "kind": kind,
                                    "filename": filename,
                                    "cleanup_state": cleanup_state,
                                }
                            )
                            report["errors"].append(
                                f"Order #{order_id} job #{job_id} ({kind}) is delivered but cleanup is '{cleanup_state}', yet local file is missing"
                            )
                            continue
                else:
                    if not file_exists:
                        report["p1_missing_files"].append(
                            {"order_id": order_id, "job_id": job_id, "kind": kind, "filename": filename, "state": job_state}
                        )
                        continue

                actual_size = candidate_file.stat().st_size
                if actual_size != expected_size:
                    report["p1_size_mismatches"].append(
                        {
                            "order_id": order_id,
                            "job_id": job_id,
                            "kind": kind,
                            "filename": filename,
                            "expected_size": expected_size,
                            "actual_size": actual_size,
                        }
                    )
                    continue

                actual_sha256 = sha256_file(candidate_file).lower()
                if actual_sha256 != expected_sha256:
                    report["p1_checksum_mismatches"].append(
                        {
                            "order_id": order_id,
                            "job_id": job_id,
                            "kind": kind,
                            "filename": filename,
                            "expected_sha256": expected_sha256,
                            "actual_sha256": actual_sha256,
                        }
                    )
                    continue

                if kind == "file":
                    report["p1_verified_files"] += 1
                elif kind == "info":
                    report["p1_verified_info"] += 1

        # 5. P0 Verification (orders without order_token_hash)
        for order in p0_orders:
            order_id = order["id"]
            filenames_raw = order["filename"] or ""
            names = [n.strip() for n in filenames_raw.split(",") if n.strip()]
            for orig_name in names:
                safe_name = sanitize_filename(orig_name)
                candidates = [
                    f"{order_id}_{safe_name}",
                    f"{order_id}_{orig_name}",
                    safe_name,
                    orig_name,
                ]
                found = False
                for c in candidates:
                    target = uploads_dir / c
                    if is_safe_child_path(uploads_dir, target) and target.is_file() and target.stat().st_size > 0:
                        found = True
                        break

                if found:
                    report["p0_confirmed_local_files"] += 1
                else:
                    report["p0_unretained_historical_files"] += 1
                    if require_p0_local:
                        report["errors"].append(
                            f"P0 order #{order_id} missing local file copy for '{orig_name}'"
                        )

    except Exception as exc:
        report["errors"].append(f"Database query execution error: {exc}")
        return False, report
    finally:
        conn.close()

    is_valid = (
        report["sqlite_integrity"] == "ok"
        and report["application_id_valid"]
        and len(report["p1_missing_files"]) == 0
        and len(report["p1_residual_uncleaned_files"]) == 0
        and len(report["p1_unexplained_missing_sources"]) == 0
        and len(report["p1_size_mismatches"]) == 0
        and len(report["p1_checksum_mismatches"]) == 0
        and len(report["path_traversal_violations"]) == 0
        and len(report["orphan_delivery_jobs"]) == 0
        and len(report["p1_metadata_errors"]) == 0
        and len(report["p1_completeness_errors"]) == 0
        and len(report["p1_delivered_uncleaned_anomalies"]) == 0
        and len(report["errors"]) == 0
    )

    return is_valid, report


def print_report(is_valid: bool, report: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        data = {"valid": is_valid, "report": report}
        print(json.dumps(data, indent=2))
        return

    print("==================================================")
    print("      PhotoDoc AI — Backup Integrity Report       ")
    print("==================================================")
    print(f"Schema Generation:        {report['schema_generation']}")
    print(f"SQLite Integrity Check:   {report['sqlite_integrity']}")
    print(f"Application Identity:     {report['application_id']} (valid: {report['application_id_valid']})")
    print(f"Total Orders:             {report['orders_count']}")
    print(f"P1 Orders:                {report['p1_orders_count']}")
    print(f"P1 Verified Photos:       {report['p1_verified_files']}")
    print(f"P1 Verified Info Files:   {report['p1_verified_info']}")
    print(f"P1 Cleaned Photos:        {report['p1_cleaned_files']}")
    print(f"P1 Cleaned Info Files:    {report['p1_cleaned_info']}")
    print(f"P1 Residual Uncleaned:    {len(report['p1_residual_uncleaned_files'])}")
    print(f"P1 Delivered Missing:     {len(report['p1_delivered_uncleaned_anomalies'])}")
    print(f"P1 Unexplained Missing:   {len(report['p1_unexplained_missing_sources'])}")
    print(f"P1 Completeness Errors:   {len(report['p1_completeness_errors'])}")
    print(f"P1 Metadata Errors:       {len(report['p1_metadata_errors'])}")
    print(f"P1 Missing Files:         {len(report['p1_missing_files'])}")
    print(f"P1 Size Mismatches:       {len(report['p1_size_mismatches'])}")
    print(f"P1 Checksum Mismatches:   {len(report['p1_checksum_mismatches'])}")
    print(f"Orphan Delivery Jobs:     {len(report['orphan_delivery_jobs'])}")
    print(f"P0 Orders:                {report['p0_orders_count']}")
    print(f"P0 Confirmed Local Files: {report['p0_confirmed_local_files']}")
    print(f"P0 Unretained Historical: {report['p0_unretained_historical_files']}")
    print(f"Path Traversal Escapes:   {len(report['path_traversal_violations'])}")

    if report["errors"]:
        print("\nErrors / Warnings:")
        for err in report["errors"]:
            print(f"  [!] {err}")

    if report["p1_completeness_errors"]:
        print("\nP1 Completeness Errors:")
        for item in report["p1_completeness_errors"][:10]:
            print(f"  - {item}")

    if report["p1_metadata_errors"]:
        print("\nP1 Metadata Errors:")
        for item in report["p1_metadata_errors"][:10]:
            print(f"  - Order #{item['order_id']} job #{item['job_id']} ({item['kind']}): {item['field']}={item['value']!r} ({item['error']})")

    if report["orphan_delivery_jobs"]:
        print("\nOrphan Delivery Jobs:")
        for item in report["orphan_delivery_jobs"][:10]:
            print(f"  - Job #{item['job_id']} references non-existent order #{item['order_id']} ({item['kind']})")

    if report["p1_unexplained_missing_sources"]:
        print("\nP1 Unexplained Missing Sources (Anomaly):")
        for item in report["p1_unexplained_missing_sources"][:10]:
            print(f"  - Order #{item['order_id']} job #{item['job_id']}: {item['filename']} ({item.get('error', 'MissingEvidence')})")

    if report["p1_residual_uncleaned_files"]:
        print("\nP1 Residual Uncleaned Files (Privacy Violation):")
        for item in report["p1_residual_uncleaned_files"][:10]:
            print(f"  - Order #{item['order_id']} job #{item['job_id']}: {item['filename']} ({item['kind']})")

    if report["p1_missing_files"]:
        print("\nMissing P1 Files:")
        for item in report["p1_missing_files"][:10]:
            print(f"  - Order #{item['order_id']} job #{item['job_id']}: {item['filename']} ({item['kind']})")

    if report["p1_size_mismatches"]:
        print("\nP1 Size Mismatches:")
        for item in report["p1_size_mismatches"][:10]:
            print(f"  - Order #{item['order_id']} {item['filename']}: expected {item['expected_size']}, got {item['actual_size']}")

    if report["p1_checksum_mismatches"]:
        print("\nP1 Checksum Mismatches:")
        for item in report["p1_checksum_mismatches"][:10]:
            print(f"  - Order #{item['order_id']} {item['filename']}: hash mismatch")

    if report["path_traversal_violations"]:
        print("\nPath Traversal Violations:")
        for item in report["path_traversal_violations"][:10]:
            print(f"  - Order #{item['order_id']} job #{item['job_id']}: {item['path']}")

    print("--------------------------------------------------")
    print(f"VERDICT: {'INTEGRITY_CHECK_PASSED' if is_valid else 'INTEGRITY_CHECK_FAILED'}")
    print("==================================================")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify PhotoDoc AI backup integrity.")
    parser.add_argument("--backup-dir", type=Path, help="Path to restored backup directory containing orders.db and uploads/")
    parser.add_argument("--db", type=Path, help="Explicit path to orders.db")
    parser.add_argument("--uploads", type=Path, help="Explicit path to uploads directory")
    parser.add_argument("--allow-legacy-unmarked", action="store_true", help="Allow application_id == 0 for legacy P0 DBs")
    parser.add_argument("--require-p0-local", action="store_true", help="Fail if any historical P0 order lacks a local copy")
    parser.add_argument("--json", action="store_true", help="Output report in JSON format")

    args = parser.parse_args()

    if args.backup_dir:
        db_path = args.backup_dir / "orders.db"
        uploads_dir = args.backup_dir / "uploads"
    elif args.db and args.uploads:
        db_path = args.db
        uploads_dir = args.uploads
    else:
        parser.error("Must provide either --backup-dir or both --db and --uploads")

    is_valid, report = verify_backup(
        db_path,
        uploads_dir,
        allow_legacy_unmarked=args.allow_legacy_unmarked,
        require_p0_local=args.require_p0_local,
    )

    print_report(is_valid, report, as_json=args.json)
    return 0 if is_valid else 1


if __name__ == "__main__":
    sys.exit(main())
