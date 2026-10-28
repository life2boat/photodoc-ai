#!/usr/bin/env python3
"""
PhotoDoc AI — Historical Backup Archive Auditor & Privacy Risk Assessor.

Scans historical backup archives to assess residual customer photo retention:
1. Discovers backup directories and archives (e.g. /var/backups/photodoc/release_*).
2. Reads orders.db in read-only mode to retrieve delivery job statuses.
3. Inspects uploads.tar.gz in-memory without extracting files to disk.
4. Distinguishes:
   - Retained photos from delivered orders (Residual Privacy Risk).
   - Legitimate recovery originals for unfinished orders.
   - Untracked or legacy P0 files.
5. Strictly read-only / dry-run: never modifies, moves, or deletes any backup archive.
6. Generates a comprehensive risk report and owner-controlled purge procedure.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
BACKEND_DIR = REPO_DIR / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from verify_backup_integrity import APPLICATION_ID, APPLICATION_ID_TAG, sanitize_filename
except ImportError:
    APPLICATION_ID_TAG = "PDAI"
    APPLICATION_ID = 1346650441


def audit_single_backup_dir(backup_dir: Path) -> Dict[str, Any]:
    """
    Audits a single backup directory containing orders.db and uploads.tar.gz.
    Strictly read-only.
    """
    archive_report: Dict[str, Any] = {
        "backup_dir": str(backup_dir),
        "db_found": False,
        "uploads_tar_found": False,
        "application_id": None,
        "schema_generation": "unknown",
        "total_orders": 0,
        "p1_orders": 0,
        "archived_files_total": 0,
        "retained_delivered_photos": [],
        "retained_delivered_info": [],
        "retained_unfinished_photos": [],
        "retained_unfinished_info": [],
        "untracked_archived_files": [],
        "residual_privacy_risk": "UNKNOWN",
        "errors": [],
    }

    db_path = backup_dir / "orders.db"
    tar_path = backup_dir / "uploads.tar.gz"

    if not db_path.is_file():
        archive_report["errors"].append(f"orders.db not found in {backup_dir}")
        return archive_report
    archive_report["db_found"] = True

    if not tar_path.is_file():
        archive_report["errors"].append(f"uploads.tar.gz not found in {backup_dir}")
        return archive_report
    archive_report["uploads_tar_found"] = True

    # Inspect SQLite database read-only
    file_status_map: Dict[str, Dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        app_id = conn.execute("PRAGMA application_id;").fetchone()[0]
        archive_report["application_id"] = app_id

        has_delivery = bool(
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs';").fetchone()
        )
        orders_cols = {row[1] for row in conn.execute("PRAGMA table_info(orders);").fetchall()}
        has_token = "order_token_hash" in orders_cols
        archive_report["schema_generation"] = "P1" if (has_delivery and has_token) else "P0"

        orders_count = conn.execute("SELECT COUNT(*) FROM orders;").fetchone()[0]
        archive_report["total_orders"] = orders_count

        if has_delivery:
            p1_count = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE order_token_hash IS NOT NULL AND order_token_hash != '';"
            ).fetchone()[0]
            archive_report["p1_orders"] = p1_count

            jobs = conn.execute(
                "SELECT id, order_id, kind, local_path, state, cleanup_state FROM delivery_jobs "
                "WHERE kind IN ('file', 'info');"
            ).fetchall()

            for j in jobs:
                lp = j["local_path"]
                if not lp:
                    continue
                fname = Path(lp).name
                file_status_map[fname] = {
                    "order_id": j["order_id"],
                    "job_id": j["id"],
                    "kind": j["kind"],
                    "state": j["state"],
                    "cleanup_state": j["cleanup_state"],
                }
        conn.close()
    except Exception as exc:
        archive_report["errors"].append(f"Database read error: {exc}")
        return archive_report

    # Inspect tarball in-memory
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                if not m.isfile():
                    continue
                archive_report["archived_files_total"] += 1
                fname = Path(m.name).name

                if fname in file_status_map:
                    finfo = file_status_map[fname]
                    entry = {
                        "filename": fname,
                        "order_id": finfo["order_id"],
                        "job_id": finfo["job_id"],
                        "kind": finfo["kind"],
                        "state": finfo["state"],
                        "cleanup_state": finfo["cleanup_state"],
                        "size": m.size,
                    }
                    if finfo["state"] == "delivered":
                        if finfo["kind"] == "file":
                            archive_report["retained_delivered_photos"].append(entry)
                        else:
                            archive_report["retained_delivered_info"].append(entry)
                    else:
                        if finfo["kind"] == "file":
                            archive_report["retained_unfinished_photos"].append(entry)
                        else:
                            archive_report["retained_unfinished_info"].append(entry)
                else:
                    archive_report["untracked_archived_files"].append(
                        {"filename": fname, "size": m.size}
                    )
    except Exception as exc:
        archive_report["errors"].append(f"Tar archive read error: {exc}")
        return archive_report

    delivered_photo_count = len(archive_report["retained_delivered_photos"])
    if delivered_photo_count > 0:
        archive_report["residual_privacy_risk"] = f"HIGH ({delivered_photo_count} delivered customer photos retained)"
    else:
        archive_report["residual_privacy_risk"] = "ZERO (No delivered customer photos retained)"

    return archive_report


def audit_backup_root(root_dir: Path) -> Dict[str, Any]:
    """
    Scans a root backup directory (e.g. /var/backups/photodoc) containing one or more backup dirs.
    """
    summary: Dict[str, Any] = {
        "root_dir": str(root_dir),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_archives_scanned": 0,
        "archives_with_residual_photos": 0,
        "total_retained_delivered_photos": 0,
        "total_unfinished_recovery_photos": 0,
        "overall_residual_privacy_risk": "ZERO",
        "archives": [],
    }

    if not root_dir.is_dir():
        summary["error"] = f"Root backup directory not found: {root_dir}"
        return summary

    # Find directories containing orders.db
    candidate_dirs: List[Path] = []
    if (root_dir / "orders.db").is_file():
        candidate_dirs.append(root_dir)
    else:
        for p in sorted(root_dir.iterdir()):
            if p.is_dir() and (p / "orders.db").is_file():
                candidate_dirs.append(p)

    summary["total_archives_scanned"] = len(candidate_dirs)

    for cdir in candidate_dirs:
        rep = audit_single_backup_dir(cdir)
        summary["archives"].append(rep)
        retained_photos = len(rep["retained_delivered_photos"])
        if retained_photos > 0:
            summary["archives_with_residual_photos"] += 1
            summary["total_retained_delivered_photos"] += retained_photos
        summary["total_unfinished_recovery_photos"] += len(rep["retained_unfinished_photos"])

    if summary["total_retained_delivered_photos"] > 0:
        summary["overall_residual_privacy_risk"] = (
            f"HIGH ({summary['total_retained_delivered_photos']} delivered photos retained across "
            f"{summary['archives_with_residual_photos']} archive(s))"
        )
    else:
        summary["overall_residual_privacy_risk"] = "ZERO"

    return summary


def print_audit_report(summary: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(summary, indent=2))
        return

    print("==================================================")
    print("  PhotoDoc AI — Historical Backup Privacy Audit   ")
    print("==================================================")
    print(f"Audit Target:             {summary.get('root_dir') or summary.get('backup_dir')}")
    print(f"Archives Scanned:         {summary.get('total_archives_scanned', 1)}")
    print(f"Archives with Retained:   {summary.get('archives_with_residual_photos', 0)}")
    print(f"Retained Delivered Photos:{summary.get('total_retained_delivered_photos', 0)}")
    print(f"Unfinished Recovery Photos:{summary.get('total_unfinished_recovery_photos', 0)}")
    print(f"Residual Privacy Risk:    {summary.get('overall_residual_privacy_risk')}")
    print("--------------------------------------------------")

    archives = summary.get("archives", [summary] if "retained_delivered_photos" in summary else [])
    for a in archives:
        print(f"\nArchive: {a['backup_dir']}")
        print(f"  Schema:                 {a.get('schema_generation')}")
        print(f"  Total Orders:           {a.get('total_orders')}")
        print(f"  Archived Files Total:   {a.get('archived_files_total')}")
        print(f"  Retained Delivered:     {len(a.get('retained_delivered_photos', []))} photos, {len(a.get('retained_delivered_info', []))} info")
        print(f"  Unfinished Recovery:    {len(a.get('retained_unfinished_photos', []))} photos")
        print(f"  Residual Privacy Risk:  {a.get('residual_privacy_risk')}")
        if a.get("retained_delivered_photos"):
            print("  Retained Delivered Files:")
            for item in a["retained_delivered_photos"][:10]:
                print(f"    - {item['filename']} (Order #{item['order_id']}, {item['size']} bytes)")
            if len(a["retained_delivered_photos"]) > 10:
                print(f"    ... and {len(a['retained_delivered_photos']) - 10} more")

    print("\n==================================================")
    print("      OWNER-CONTROLLED PURGE PROCEDURE RUNBOOK     ")
    print("==================================================")
    print("NOTICE: Historical backup archives represent a residual privacy risk")
    print("if they contain customer photos from orders that were already delivered.")
    print("This audit tool NEVER deletes or modifies backup files automatically.")
    print("\nIf the owner decides to purge delivered photos from a specific archive:")
    print("1. Identify the specific archive and files from this report.")
    print("2. Run the owner-controlled redaction command:")
    print("   tar --delete -f <archive_dir>/uploads.tar.gz uploads/<delivered_photo_filename>")
    print("3. Recompute the cryptographic checksum manifest:")
    print("   cd <archive_dir> && sha256sum orders.db uploads.tar.gz > manifest.sha256")
    print("4. Re-run this audit tool to confirm ZERO retained delivered photos.")
    print("==================================================")


def main_cli() -> int:
    parser = argparse.ArgumentParser(
        description="PhotoDoc AI — Historical Backup Privacy Risk Auditor",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--backup-root",
        type=Path,
        help="Root directory containing backup archives (e.g. /var/backups/photodoc)",
    )
    group.add_argument(
        "--archive-dir",
        type=Path,
        help="Specific backup directory containing orders.db and uploads.tar.gz",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output report as JSON",
    )

    args = parser.parse_args()

    if args.archive_dir:
        report = audit_single_backup_dir(args.archive_dir)
        print_audit_report(report, as_json=args.json)
        return 0
    else:
        summary = audit_backup_root(args.backup_root)
        print_audit_report(summary, as_json=args.json)
        return 0


if __name__ == "__main__":
    sys.exit(main_cli())
