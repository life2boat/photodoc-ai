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
        "p1_missing_files": [],
        "p1_size_mismatches": [],
        "p1_checksum_mismatches": [],
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

        # 3. Detect P1 delivery_jobs
        has_delivery = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs';"
        ).fetchone()
        orders_cols = {row[1] for row in conn.execute("PRAGMA table_info(orders);").fetchall()}
        has_order_token = "order_token_hash" in orders_cols

        report["schema_generation"] = "P1" if (has_delivery and has_order_token) else "P0"

        # 4. P1 Verification via delivery_jobs
        p1_order_ids = set()
        if has_delivery:
            jobs = conn.execute(
                "SELECT id, order_id, kind, local_path, original_name, content_sha256, content_size "
                "FROM delivery_jobs WHERE kind IN ('file', 'info') ORDER BY order_id, id;"
            ).fetchall()

            for job in jobs:
                p1_order_ids.add(job["order_id"])
                job_id = job["id"]
                order_id = job["order_id"]
                kind = job["kind"]
                local_path_raw = job["local_path"] or ""
                expected_sha256 = (job["content_sha256"] or "").lower()
                expected_size = job["content_size"]

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

                if not candidate_file.is_file():
                    report["p1_missing_files"].append(
                        {"order_id": order_id, "job_id": job_id, "kind": kind, "filename": filename}
                    )
                    continue

                actual_size = candidate_file.stat().st_size
                if expected_size is not None and actual_size != expected_size:
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
                if expected_sha256 and actual_sha256 != expected_sha256:
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

        report["p1_orders_count"] = len(p1_order_ids)

        # 5. P0 Verification (orders without delivery_jobs)
        p0_query = (
            "SELECT id, filename FROM orders WHERE id NOT IN ({}) ORDER BY id;"
            .format(",".join(str(i) for i in p1_order_ids))
            if p1_order_ids
            else "SELECT id, filename FROM orders ORDER BY id;"
        )
        p0_orders = conn.execute(p0_query).fetchall()
        report["p0_orders_count"] = len(p0_orders)

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
        and len(report["p1_size_mismatches"]) == 0
        and len(report["p1_checksum_mismatches"]) == 0
        and len(report["path_traversal_violations"]) == 0
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
    print(f"P1 Missing Files:         {len(report['p1_missing_files'])}")
    print(f"P1 Size Mismatches:       {len(report['p1_size_mismatches'])}")
    print(f"P1 Checksum Mismatches:   {len(report['p1_checksum_mismatches'])}")
    print(f"P0 Orders:                {report['p0_orders_count']}")
    print(f"P0 Confirmed Local Files: {report['p0_confirmed_local_files']}")
    print(f"P0 Unretained Historical: {report['p0_unretained_historical_files']}")
    print(f"Path Traversal Escapes:   {len(report['path_traversal_violations'])}")

    if report["errors"]:
        print("\nErrors / Warnings:")
        for err in report["errors"]:
            print(f"  [!] {err}")

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
