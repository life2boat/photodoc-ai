#!/usr/bin/env python3
"""
PhotoDoc AI — Historical Backup Archive Auditor & Privacy Risk Assessor.

Fail-Closed Privacy Audit Contract:
1. Discovers and classifies all backup archives in the configured backup root:
   - Standard release directories (containing orders.db and uploads.tar.gz).
   - Standalone combined archives (e.g. autodoc_cutover.tar.gz).
   - Identifies and flags unsupported or unclassified archive formats (.zip, .tar.bz2, etc.).
2. Reads SQLite database in read-only mode to retrieve delivery job and order records.
3. Inspects uploads archive in-memory without extracting files to permanent storage.
4. Distinguishes:
   - Confirmed delivered photos (HIGH risk: retained customer photo bytes).
   - Legitimate recovery originals for unfinished deliveries (pending/processing/failed).
   - Untracked or unclassified archive files (UNKNOWN / BLOCKED: never falsely classified as ZERO).
5. Strictly read-only / dry-run: never modifies, moves, or deletes any backup archive.
6. CLI Exit Code Contract:
   - 0: Strictly all archives are readable, valid, fully classified, and have ZERO retained delivered photos and ZERO untracked files.
   - Nonzero: Confirmed privacy risk (1), unclassified/untracked files or empty inventory (2), read/integrity error (3).
7. Default output sanitization: customer PII and sensitive filenames are withheld unless --show-details is explicitly requested.
8. Provides a safe, verified 7-step owner-controlled remediation workflow for compressed archives.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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

SUPPORTED_ARCHIVE_EXTENSIONS = {".tar.gz", ".tgz"}
UNSUPPORTED_ARCHIVE_EXTENSIONS = {".zip", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".7z", ".rar"}


def audit_single_backup_dir(backup_dir: Path) -> Dict[str, Any]:
    """
    Audits a single backup directory containing orders.db and uploads.tar.gz.
    Strictly read-only and fail-closed.
    """
    archive_report: Dict[str, Any] = {
        "archive_type": "directory",
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
        "unclassified_legacy_files": [],
        "untracked_archived_files": [],
        "residual_privacy_risk": "UNKNOWN",
        "errors": [],
    }

    # Locate database
    db_candidates = [
        backup_dir / "orders.db",
        backup_dir / "orders.db.cutover",
    ]
    db_path = next((p for p in db_candidates if p.is_file()), None)
    if not db_path:
        # Check any *.db file in directory
        other_dbs = list(backup_dir.glob("*.db"))
        if other_dbs:
            db_path = other_dbs[0]

    if not db_path or not db_path.is_file():
        archive_report["errors"].append(f"No SQLite database found in backup directory: {backup_dir}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report
    archive_report["db_found"] = True

    # Locate uploads tar
    tar_candidates = [
        backup_dir / "uploads.tar.gz",
        backup_dir / "uploads.tgz",
    ]
    tar_path = next((p for p in tar_candidates if p.is_file()), None)
    if not tar_path or not tar_path.is_file():
        archive_report["errors"].append(f"No uploads.tar.gz archive found in backup directory: {backup_dir}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report
    archive_report["uploads_tar_found"] = True

    return _audit_db_and_tar(db_path, tar_path, archive_report)


def audit_single_combined_tar(tar_path: Path) -> Dict[str, Any]:
    """
    Audits a standalone combined archive (e.g. photodoc_cutover.tar.gz containing orders.db and uploads/).
    Strictly read-only and fail-closed.
    """
    archive_report: Dict[str, Any] = {
        "archive_type": "combined_tar",
        "backup_dir": str(tar_path),
        "db_found": False,
        "uploads_tar_found": True,
        "application_id": None,
        "schema_generation": "unknown",
        "total_orders": 0,
        "p1_orders": 0,
        "archived_files_total": 0,
        "retained_delivered_photos": [],
        "retained_delivered_info": [],
        "retained_unfinished_photos": [],
        "retained_unfinished_info": [],
        "unclassified_legacy_files": [],
        "untracked_archived_files": [],
        "residual_privacy_risk": "UNKNOWN",
        "errors": [],
    }

    if not tar_path.is_file():
        archive_report["errors"].append(f"Archive file not found: {tar_path}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_extracted = None

            with tarfile.open(tar_path, "r:*") as tar:
                # Find db member
                for m in tar.getmembers():
                    if m.isfile() and (m.name.endswith(".db") or "orders.db" in m.name):
                        tar.extract(m, path=tmp_path)
                        db_extracted = tmp_path / m.name
                        break

            if not db_extracted or not db_extracted.is_file():
                archive_report["errors"].append(f"No database member found inside combined archive: {tar_path}")
                archive_report["residual_privacy_risk"] = "ERROR"
                return archive_report
            archive_report["db_found"] = True

            return _audit_db_and_tar(db_extracted, tar_path, archive_report)
    except Exception as exc:
        archive_report["errors"].append(f"Failed to inspect combined archive {tar_path}: {exc}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report


def _audit_db_and_tar(db_path: Path, tar_path: Path, archive_report: Dict[str, Any]) -> Dict[str, Any]:
    # 1. Inspect SQLite database read-only
    file_status_map: Dict[str, Dict[str, Any]] = {}
    legacy_order_files: Dict[str, int] = {}

    try:
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row

        # Integrity check
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if integrity != "ok":
            archive_report["errors"].append(f"SQLite PRAGMA integrity_check failed: {integrity}")
            archive_report["residual_privacy_risk"] = "ERROR"
            conn.close()
            return archive_report

        app_id_row = conn.execute("PRAGMA application_id;").fetchone()
        app_id = app_id_row[0] if app_id_row else None
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

        # Also map legacy P0 orders
        p0_orders = conn.execute(
            "SELECT id, filename FROM orders WHERE order_token_hash IS NULL OR order_token_hash = '';"
            if has_token else "SELECT id, filename FROM orders;"
        ).fetchall()
        for o in p0_orders:
            raw_fn = o["filename"] or ""
            names = [n.strip() for n in raw_fn.split(",") if n.strip()]
            for n in names:
                safe_n = sanitize_filename(n)
                legacy_order_files[f"{o['id']}_{safe_n}"] = o["id"]
                legacy_order_files[f"{o['id']}_{n}"] = o["id"]
                legacy_order_files[safe_n] = o["id"]
                legacy_order_files[n] = o["id"]

        conn.close()
    except Exception as exc:
        archive_report["errors"].append(f"Database read error: {exc}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report

    # 2. Inspect tarball in-memory
    try:
        with tarfile.open(tar_path, "r:*") as tar:
            members = tar.getmembers()
            for m in members:
                if not m.isfile():
                    continue
                # Ignore the database itself if inside a combined tar
                if m.name.endswith(".db"):
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
                elif fname in legacy_order_files:
                    # File belongs to a legacy order without delivery_jobs
                    archive_report["unclassified_legacy_files"].append(
                        {
                            "filename": fname,
                            "order_id": legacy_order_files[fname],
                            "size": m.size,
                        }
                    )
                else:
                    # Untracked file: completely unindexed
                    archive_report["untracked_archived_files"].append(
                        {"filename": fname, "size": m.size}
                    )
    except Exception as exc:
        archive_report["errors"].append(f"Tar archive read error: {exc}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report

    # Fail-Closed Risk Classification Contract:
    delivered_photo_count = len(archive_report["retained_delivered_photos"])
    untracked_count = len(archive_report["untracked_archived_files"])
    legacy_unclassified_count = len(archive_report["unclassified_legacy_files"])

    if archive_report["errors"]:
        archive_report["residual_privacy_risk"] = "ERROR"
    elif delivered_photo_count > 0:
        archive_report["residual_privacy_risk"] = f"HIGH ({delivered_photo_count} delivered customer photo(s) retained)"
    elif untracked_count > 0 or legacy_unclassified_count > 0:
        # FAIL-CLOSED: Untracked or unclassified files must NEVER yield ZERO!
        total_unknown = untracked_count + legacy_unclassified_count
        archive_report["residual_privacy_risk"] = f"UNKNOWN ({total_unknown} untracked/unclassified file(s) present)"
    elif archive_report["total_orders"] == 0 and archive_report["archived_files_total"] > 0:
        archive_report["residual_privacy_risk"] = "UNKNOWN (Archived files exist but database has 0 orders)"
    else:
        archive_report["residual_privacy_risk"] = "ZERO (0 delivered customer photos retained)"

    return archive_report


def audit_backup_root(root_dir: Path) -> Dict[str, Any]:
    """
    Scans a root backup directory (e.g. /var/backups/photodoc) containing one or more backup dirs or archives.
    Fail-closed: returns BLOCKED if empty or if unsupported archive formats are detected.
    """
    summary: Dict[str, Any] = {
        "root_dir": str(root_dir),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_archives_scanned": 0,
        "archives_with_residual_photos": 0,
        "total_retained_delivered_photos": 0,
        "total_unfinished_recovery_photos": 0,
        "total_untracked_files": 0,
        "unsupported_archives": [],
        "overall_residual_privacy_risk": "BLOCKED",
        "archives": [],
        "errors": [],
    }

    if not root_dir.exists():
        summary["errors"].append(f"Configured backup root does not exist: {root_dir}")
        summary["overall_residual_privacy_risk"] = "ERROR (Configured backup root does not exist)"
        return summary

    if not root_dir.is_dir():
        summary["errors"].append(f"Configured backup root is not a directory: {root_dir}")
        summary["overall_residual_privacy_risk"] = "ERROR (Backup root is not a directory)"
        return summary

    # Detect unsupported or unscanned archive files in root_dir tree
    for item in root_dir.rglob("*"):
        if item.is_file():
            lower_name = item.name.lower()
            for ext in UNSUPPORTED_ARCHIVE_EXTENSIONS:
                if lower_name.endswith(ext):
                    summary["unsupported_archives"].append(
                        {"path": str(item), "format": ext, "size": item.stat().st_size}
                    )
                    break

    # Discover candidate backup locations
    # 1. Directory backups (directories with orders.db or *.db)
    # 2. Combined tarballs (files ending with .tar.gz / .tgz / .tar)
    candidate_dirs: Set[Path] = set()
    candidate_tarballs: Set[Path] = set()

    # Check root itself
    if (root_dir / "orders.db").is_file() or (root_dir / "orders.db.cutover").is_file():
        candidate_dirs.add(root_dir)

    for item in sorted(root_dir.iterdir()):
        if item.is_dir():
            # Check if this dir or subdirs contain a database
            if (item / "orders.db").is_file() or (item / "orders.db.cutover").is_file():
                candidate_dirs.add(item)
            else:
                for sub in item.iterdir():
                    if sub.is_dir() and ((sub / "orders.db").is_file() or (sub / "orders.db.cutover").is_file()):
                        candidate_dirs.add(sub)
        elif item.is_file():
            lower = item.name.lower()
            if any(lower.endswith(ext) for ext in SUPPORTED_ARCHIVE_EXTENSIONS):
                # Check if it is a standalone combined tarball (not an uploads.tar.gz inside a backup dir)
                if not item.name.startswith("uploads"):
                    candidate_tarballs.add(item)

    # Process all discovered archives
    for cdir in sorted(candidate_dirs):
        rep = audit_single_backup_dir(cdir)
        summary["archives"].append(rep)
        _aggregate_archive_report(summary, rep)

    for ctar in sorted(candidate_tarballs):
        rep = audit_single_combined_tar(ctar)
        summary["archives"].append(rep)
        _aggregate_archive_report(summary, rep)

    summary["total_archives_scanned"] = len(summary["archives"])

    # Determine overall risk
    if summary["errors"]:
        summary["overall_residual_privacy_risk"] = "ERROR"
    elif summary["total_archives_scanned"] == 0:
        summary["overall_residual_privacy_risk"] = "BLOCKED (No supported archives found in configured backup root)"
    elif summary["unsupported_archives"]:
        summary["overall_residual_privacy_risk"] = (
            f"BLOCKED (Unsupported archive format(s) detected: {len(summary['unsupported_archives'])} unscanned file(s))"
        )
    elif any(a["residual_privacy_risk"].startswith("ERROR") for a in summary["archives"]):
        summary["overall_residual_privacy_risk"] = "ERROR (One or more archives failed inspection)"
    elif summary["total_retained_delivered_photos"] > 0:
        summary["overall_residual_privacy_risk"] = (
            f"HIGH ({summary['total_retained_delivered_photos']} delivered photo(s) retained across "
            f"{summary['archives_with_residual_photos']} archive(s))"
        )
    elif summary["total_untracked_files"] > 0 or any(a["residual_privacy_risk"].startswith("UNKNOWN") for a in summary["archives"]):
        summary["overall_residual_privacy_risk"] = (
            f"UNKNOWN ({summary['total_untracked_files']} unclassified/untracked file(s) present)"
        )
    else:
        summary["overall_residual_privacy_risk"] = "ZERO (0 delivered customer photos retained across all archives)"

    return summary


def _aggregate_archive_report(summary: Dict[str, Any], rep: Dict[str, Any]) -> None:
    retained_photos = len(rep["retained_delivered_photos"])
    if retained_photos > 0:
        summary["archives_with_residual_photos"] += 1
        summary["total_retained_delivered_photos"] += retained_photos
    summary["total_unfinished_recovery_photos"] += len(rep["retained_unfinished_photos"])
    summary["total_untracked_files"] += len(rep["untracked_archived_files"]) + len(rep["unclassified_legacy_files"])
    if rep["errors"]:
        summary["errors"].extend(rep["errors"])


def print_audit_report(summary: Dict[str, Any], *, as_json: bool = False, show_details: bool = False) -> None:
    if as_json:
        # Sanitize sensitive filenames in JSON unless show_details is requested
        if not show_details:
            sanitized = json.loads(json.dumps(summary))
            archives = sanitized.get("archives", [sanitized] if "retained_delivered_photos" in sanitized else [])
            for a in archives:
                for k in ["retained_delivered_photos", "retained_delivered_info", "retained_unfinished_photos",
                          "retained_unfinished_info", "unclassified_legacy_files", "untracked_archived_files"]:
                    if k in a:
                        a[k] = [
                            {"order_id": item.get("order_id"), "job_id": item.get("job_id"), "size": item.get("size")}
                            for item in a[k]
                        ]
            print(json.dumps(sanitized, indent=2))
        else:
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
    print(f"Untracked/Legacy Files:   {summary.get('total_untracked_files', 0)}")
    print(f"Unsupported Formats:      {len(summary.get('unsupported_archives', []))}")
    print(f"Residual Privacy Risk:    {summary.get('overall_residual_privacy_risk') or summary.get('residual_privacy_risk')}")
    print("--------------------------------------------------")

    if summary.get("unsupported_archives"):
        print("\n[WARNING: UNSUPPORTED ARCHIVE FORMATS DETECTED]")
        for ua in summary["unsupported_archives"]:
            print(f"  - {ua['path']} ({ua['format']}, {ua['size']} bytes)")

    archives = summary.get("archives", [summary] if "retained_delivered_photos" in summary else [])
    for a in archives:
        print(f"\nArchive: {a['backup_dir']}")
        print(f"  Schema:                 {a.get('schema_generation')}")
        print(f"  Total Orders:           {a.get('total_orders')}")
        print(f"  Archived Files Total:   {a.get('archived_files_total')}")
        print(f"  Retained Delivered:     {len(a.get('retained_delivered_photos', []))} photos, {len(a.get('retained_delivered_info', []))} info")
        print(f"  Unfinished Recovery:    {len(a.get('retained_unfinished_photos', []))} photos")
        print(f"  Untracked / Legacy:     {len(a.get('untracked_archived_files', [])) + len(a.get('unclassified_legacy_files', []))} files")
        print(f"  Residual Privacy Risk:  {a.get('residual_privacy_risk')}")

        if a.get("errors"):
            print("  Errors:")
            for err in a["errors"]:
                print(f"    - {err}")

        if show_details and a.get("retained_delivered_photos"):
            print("  [SENSITIVE DIAGNOSTIC DATA - OWNER REMEDIATION ONLY]")
            for item in a["retained_delivered_photos"][:10]:
                print(f"    - Order #{item['order_id']} (Job: {item['job_id'][:8]}..., {item['size']} bytes, file: {item['filename']})")
            if len(a["retained_delivered_photos"]) > 10:
                print(f"    ... and {len(a['retained_delivered_photos']) - 10} more items")
        elif not show_details and a.get("retained_delivered_photos"):
            print("  (Sensitive filenames withheld. Pass --show-details to inspect specific items for remediation)")

    print("\n==================================================")
    print("      OWNER-CONTROLLED PURGE PROCEDURE RUNBOOK     ")
    print("==================================================")
    print("NOTICE: Historical backup archives represent a residual privacy risk")
    print("if they contain customer photos from orders that were already delivered.")
    print("This audit tool NEVER deletes or modifies backup files automatically.")
    print("GNU tar does not support in-place deletion on compressed archives (.tar.gz).")
    print("\nSafe 7-Step Owner-Controlled Remediation Workflow:")
    print("1. Inventory exact archive and verify its cryptographic checksum:")
    print("   cd <archive_dir> && sha256sum -c manifest.sha256")
    print("   tar -ztvf uploads.tar.gz > original_manifest.txt")
    print("2. Create an isolated temporary working directory:")
    print("   WORK_DIR=$(mktemp -d -t photodoc_rebuild_XXXXXX)")
    print("   cp uploads.tar.gz \"${WORK_DIR}/uploads.tar.gz.orig\"")
    print("3. Rebuild a sanitized archive without the approved target entries:")
    print("   mkdir -p \"${WORK_DIR}/rebuild\"")
    print("   tar -xzf \"${WORK_DIR}/uploads.tar.gz.orig\" -C \"${WORK_DIR}/rebuild\"")
    print("   rm -f \"${WORK_DIR}/rebuild/uploads/<approved_delivered_filename>\"")
    print("   tar --numeric-owner -czvf \"${WORK_DIR}/uploads.tar.gz.sanitized\" -C \"${WORK_DIR}/rebuild\" uploads")
    print("4. Verify rebuilt archive readability, member inventory, and checksums:")
    print("   tar -ztvf \"${WORK_DIR}/uploads.tar.gz.sanitized\"")
    print("5. Run integrity verification and privacy audit on the candidate copy:")
    print("   cp orders.db \"${WORK_DIR}/orders.db\"")
    print("   python3 /candidate/scripts/verify_backup_integrity.py --db \"${WORK_DIR}/orders.db\" --uploads \"${WORK_DIR}/rebuild/uploads\"")
    print("   python3 /candidate/scripts/audit_backup_archives.py --archive-dir \"${WORK_DIR}\"")
    print("6. Confirm that unfinished delivery recovery data remains intact.")
    print("7. Preserve original archive as .pre-purge until owner explicitly authorizes replacement:")
    print("   cp uploads.tar.gz uploads.tar.gz.pre-purge")
    print("   mv \"${WORK_DIR}/uploads.tar.gz.sanitized\" uploads.tar.gz")
    print("   sha256sum orders.db uploads.tar.gz > manifest.sha256")
    print("   rm -rf \"${WORK_DIR}\"")
    print("==================================================")


def get_cli_exit_code(overall_risk: str) -> int:
    """
    Deterministic CLI Exit Code Contract:
    0: Valid, complete inventory with ZERO confirmed retained delivered photos and ZERO untracked files.
    1: Confirmed privacy risk (HIGH).
    2: Unclassified, untracked, empty inventory, or unsupported archive formats (UNKNOWN / BLOCKED).
    3: Read or integrity error (ERROR).
    """
    if overall_risk.startswith("ZERO"):
        return 0
    elif overall_risk.startswith("HIGH"):
        return 1
    elif overall_risk.startswith("UNKNOWN") or overall_risk.startswith("BLOCKED"):
        return 2
    elif overall_risk.startswith("ERROR"):
        return 3
    return 1


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
    group.add_argument(
        "--archive-tar",
        type=Path,
        help="Specific combined backup archive file (e.g. photodoc_cutover.tar.gz)",
    )
    parser.add_argument(
        "--show-details",
        action="store_true",
        help="Display sensitive filenames and job identifiers (owner-controlled remediation only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output report as JSON",
    )

    args = parser.parse_args()

    if args.archive_dir:
        report = audit_single_backup_dir(args.archive_dir)
        print_audit_report(report, as_json=args.json, show_details=args.show_details)
        return get_cli_exit_code(report["residual_privacy_risk"])
    elif args.archive_tar:
        report = audit_single_combined_tar(args.archive_tar)
        print_audit_report(report, as_json=args.json, show_details=args.show_details)
        return get_cli_exit_code(report["residual_privacy_risk"])
    else:
        summary = audit_backup_root(args.backup_root)
        print_audit_report(summary, as_json=args.json, show_details=args.show_details)
        return get_cli_exit_code(summary["overall_residual_privacy_risk"])


if __name__ == "__main__":
    sys.exit(main_cli())
