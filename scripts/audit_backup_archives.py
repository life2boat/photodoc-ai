#!/usr/bin/env python3
"""
PhotoDoc AI — Historical Backup Archive Auditor & Privacy Risk Assessor.

Fail-Closed Privacy Audit Contract:
1. Discovers and classifies all backup archives in the configured backup root:
   - Standard release directories (containing orders.db and uploads.tar.gz / uploads.tar).
   - Standalone combined archives (e.g. photodoc_cutover.tar.gz, .tgz, .tar).
   - Identifies and flags unsupported or unclassified archive formats (.zip, .tar.bz2, etc.).
   - Recursively traverses subdirectories to arbitrary depth without double-counting internal archives.
2. Reads SQLite database in read-only mode to retrieve delivery job and order records.
3. Inspects uploads archive in-memory without extracting files to permanent storage.
4. Distinguishes:
   - Confirmed delivered photos and customer-information metadata files (HIGH risk: retained customer data).
   - Legitimate recovery originals for unfinished deliveries (pending/processing/failed).
   - Untracked or unclassified archive files (UNKNOWN / BLOCKED: never falsely classified as ZERO).
5. Strictly read-only / dry-run: never modifies, moves, or deletes any backup archive.
6. Safe combined tar extraction:
   - Extracts ONLY regular database members matching naming contract to fixed temporary path.
   - Rejects path traversal, absolute paths, symlinks, hardlinks, duplicate databases, and oversized members.
7. CLI Exit Code Contract:
   - 0: Strictly all archives are readable, valid, fully classified, and have ZERO retained delivered photos/metadata and ZERO untracked files.
   - Nonzero: Confirmed privacy risk (1), unclassified/untracked files or empty inventory (2), read/integrity error (3).
8. Default output sanitization: customer PII and sensitive filenames are withheld unless --show-details is explicitly requested.
9. Provides a safe, verified 7-step owner-controlled remediation workflow for compressed archives.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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

SUPPORTED_ARCHIVE_EXTENSIONS = {".tar.gz", ".tgz", ".tar"}
UNSUPPORTED_ARCHIVE_EXTENSIONS = {".zip", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".7z", ".rar"}

MAX_COMBINED_ARCHIVE_MEMBERS = 100_000
MAX_DATABASE_MEMBER_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB
MAX_SINGLE_FILE_SIZE_BYTES = 1024 * 1024 * 1024     # 1 GB


def audit_single_backup_dir(backup_dir: Path) -> Dict[str, Any]:
    """
    Audits a single backup directory containing orders.db and uploads.tar.gz (or uploads.tar).
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
        other_dbs = [p for p in backup_dir.glob("*.db") if p.is_file()]
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
        backup_dir / "uploads.tar",
    ]
    tar_path = next((p for p in tar_candidates if p.is_file()), None)
    if not tar_path or not tar_path.is_file():
        archive_report["errors"].append(f"No uploads archive found in backup directory: {backup_dir}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report
    archive_report["uploads_tar_found"] = True

    return _audit_db_and_tar(db_path, tar_path, archive_report)


def audit_single_combined_tar(tar_path: Path) -> Dict[str, Any]:
    """
    Audits a standalone combined archive (e.g. photodoc_cutover.tar.gz or .tar containing orders.db and uploads/).
    Strictly read-only, safe extraction, and fail-closed.
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
        with tarfile.open(tar_path, "r:*") as tar:
            members = tar.getmembers()
            if len(members) > MAX_COMBINED_ARCHIVE_MEMBERS:
                archive_report["errors"].append(
                    f"Combined archive exceeds member count limit ({len(members)} > {MAX_COMBINED_ARCHIVE_MEMBERS})"
                )
                archive_report["residual_privacy_risk"] = "ERROR"
                return archive_report

            db_candidates: List[tarfile.TarInfo] = []
            for m in members:
                # Security: reject path traversal and absolute paths
                if ".." in Path(m.name).parts or m.name.startswith("/") or Path(m.name).is_absolute():
                    archive_report["errors"].append(f"Path traversal detected in archive member: {m.name}")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                # Only regular files can be database members
                base_name = Path(m.name).name
                is_db_name = (
                    base_name in ("orders.db", "orders.db.cutover")
                    or (base_name.endswith(".db") and "orders" in base_name.lower())
                )
                if is_db_name:
                    if m.issym() or m.islnk():
                        archive_report["errors"].append(f"Database member is an unsafe symlink/hardlink: {m.name}")
                        archive_report["residual_privacy_risk"] = "ERROR"
                        return archive_report
                    if not m.isreg():
                        archive_report["errors"].append(f"Database member is not a regular file: {m.name}")
                        archive_report["residual_privacy_risk"] = "ERROR"
                        return archive_report
                    db_candidates.append(m)

            if len(db_candidates) == 0:
                archive_report["errors"].append(f"No database member found inside combined archive: {tar_path}")
                archive_report["residual_privacy_risk"] = "ERROR"
                return archive_report

            if len(db_candidates) > 1:
                archive_report["errors"].append(
                    f"Multiple database members detected ({len(db_candidates)}) in combined archive: ambiguous/untrusted metadata"
                )
                archive_report["residual_privacy_risk"] = "ERROR"
                return archive_report

            db_member = db_candidates[0]
            if db_member.size > MAX_DATABASE_MEMBER_SIZE_BYTES:
                archive_report["errors"].append(
                    f"Database member exceeds size limit ({db_member.size} > {MAX_DATABASE_MEMBER_SIZE_BYTES})"
                )
                archive_report["residual_privacy_risk"] = "ERROR"
                return archive_report

            # Safe extraction to a fixed temporary file (never using untrusted member name)
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                tmp_db_file = tmp_path / "orders_inspected.db"
                f_in = tar.extractfile(db_member)
                if not f_in:
                    archive_report["errors"].append(f"Failed to read database member stream from {tar_path}")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                with tmp_db_file.open("wb") as f_out:
                    shutil.copyfileobj(f_in, f_out, length=65536)

                archive_report["db_found"] = True
                return _audit_db_and_tar(tmp_db_file, tar_path, archive_report)

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
                # Security: check traversal and symlinks
                if ".." in Path(m.name).parts or m.name.startswith("/") or Path(m.name).is_absolute():
                    archive_report["errors"].append(f"Path traversal detected in archive member: {m.name}")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                if m.issym() or m.islnk():
                    archive_report["errors"].append(f"Symlink or hardlink entry detected in archive: {m.name}")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                if not m.isfile():
                    continue

                if m.size > MAX_SINGLE_FILE_SIZE_BYTES:
                    archive_report["errors"].append(
                        f"Archive member exceeds size limit ({m.size} > {MAX_SINGLE_FILE_SIZE_BYTES}): {m.name}"
                    )
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                # Ignore the database itself if inside a combined tar
                base_name = Path(m.name).name
                if (
                    base_name in ("orders.db", "orders.db.cutover")
                    or (base_name.endswith(".db") and "orders" in base_name.lower())
                ):
                    continue

                archive_report["archived_files_total"] += 1
                fname = base_name

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
                    archive_report["unclassified_legacy_files"].append(
                        {
                            "filename": fname,
                            "order_id": legacy_order_files[fname],
                            "size": m.size,
                        }
                    )
                else:
                    archive_report["untracked_archived_files"].append(
                        {"filename": fname, "size": m.size}
                    )
    except Exception as exc:
        archive_report["errors"].append(f"Tar archive read error: {exc}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report

    # Fail-Closed Risk Classification Contract:
    delivered_photo_count = len(archive_report["retained_delivered_photos"])
    delivered_info_count = len(archive_report["retained_delivered_info"])
    total_delivered_retained = delivered_photo_count + delivered_info_count
    untracked_count = len(archive_report["untracked_archived_files"])
    legacy_unclassified_count = len(archive_report["unclassified_legacy_files"])

    if archive_report["errors"]:
        archive_report["residual_privacy_risk"] = "ERROR"
    elif total_delivered_retained > 0:
        details: List[str] = []
        if delivered_photo_count > 0:
            details.append(f"{delivered_photo_count} delivered photo(s)")
        if delivered_info_count > 0:
            details.append(f"{delivered_info_count} metadata file(s)")
        archive_report["residual_privacy_risk"] = f"HIGH ({', '.join(details)} retained)"
    elif untracked_count > 0 or legacy_unclassified_count > 0:
        total_unknown = untracked_count + legacy_unclassified_count
        archive_report["residual_privacy_risk"] = f"UNKNOWN ({total_unknown} untracked/unclassified file(s) present)"
    elif archive_report["total_orders"] == 0 and archive_report["archived_files_total"] > 0:
        archive_report["residual_privacy_risk"] = "UNKNOWN (Archived files exist but database has 0 orders)"
    else:
        archive_report["residual_privacy_risk"] = "ZERO (0 delivered customer photos or metadata retained)"

    return archive_report


def audit_backup_root(root_dir: Path) -> Dict[str, Any]:
    """
    Recursively scans a root backup directory (e.g. /var/backups/photodoc) containing one or more backup dirs or archives.
    Fail-closed: returns BLOCKED if empty, if unsupported archive formats are detected, or if traversal is incomplete.
    Avoids double-counting internal uploads archives that belong to an already discovered backup directory.
    """
    summary: Dict[str, Any] = {
        "root_dir": str(root_dir),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_archives_scanned": 0,
        "archives_with_residual_privacy_risk": 0,
        "total_retained_delivered_photos": 0,
        "total_retained_delivered_info": 0,
        "total_unfinished_recovery_photos": 0,
        "total_unfinished_recovery_info": 0,
        "total_untracked_files": 0,
        "unsupported_archives": [],
        "symlinked_entries": [],
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

    candidate_dirs: Set[Path] = set()
    candidate_tarballs: Set[Path] = set()

    def _walk_error(err: OSError) -> None:
        summary["errors"].append(f"Directory traversal error: {err}")

    # Recursive traversal of root_dir
    for current_dir_str, dirnames, filenames in os.walk(str(root_dir), followlinks=False, onerror=_walk_error):
        current_dir = Path(current_dir_str)

        # Detect symlinks in directories
        for dname in dirnames:
            dp = current_dir / dname
            if dp.is_symlink():
                summary["symlinked_entries"].append(str(dp))

        # Check if current_dir is a backup directory candidate (contains orders.db or matching DB)
        has_db = (
            (current_dir / "orders.db").is_file()
            or (current_dir / "orders.db.cutover").is_file()
            or any(p.is_file() and p.name.endswith(".db") and "orders" in p.name.lower() for p in current_dir.iterdir())
        )
        has_tar = any(
            (current_dir / cand).is_file() for cand in ["uploads.tar.gz", "uploads.tgz", "uploads.tar"]
        )
        if has_db and has_tar:
            candidate_dirs.add(current_dir.resolve())

        # Check filenames
        for fname in filenames:
            fp = current_dir / fname
            if fp.is_symlink():
                summary["symlinked_entries"].append(str(fp))

            lower_name = fname.lower()
            for ext in UNSUPPORTED_ARCHIVE_EXTENSIONS:
                if lower_name.endswith(ext):
                    summary["unsupported_archives"].append(
                        {"path": str(fp), "format": ext, "size": fp.stat().st_size if fp.exists() else 0}
                    )
                    break

            for ext in SUPPORTED_ARCHIVE_EXTENSIONS:
                if lower_name.endswith(ext):
                    # Check whether this archive belongs to an already discovered backup directory
                    candidate_tarballs.add(fp.resolve())
                    break

    # Filter candidate_tarballs:
    # Avoid double-counting internal uploads.tar.gz / uploads.tar that belong to a discovered backup directory
    filtered_tarballs: Set[Path] = set()
    for ctar in candidate_tarballs:
        # If ctar is inside any discovered candidate_dir, it belongs to that directory backup
        inside_dir_backup = False
        for cdir in candidate_dirs:
            try:
                ctar.relative_to(cdir)
                inside_dir_backup = True
                break
            except ValueError:
                pass
        if not inside_dir_backup:
            filtered_tarballs.add(ctar)

    # Process all discovered archives
    for cdir in sorted(candidate_dirs):
        rep = audit_single_backup_dir(cdir)
        summary["archives"].append(rep)
        _aggregate_archive_report(summary, rep)

    for ctar in sorted(filtered_tarballs):
        rep = audit_single_combined_tar(ctar)
        summary["archives"].append(rep)
        _aggregate_archive_report(summary, rep)

    summary["total_archives_scanned"] = len(summary["archives"])

    # Determine overall risk
    total_delivered_retained = summary["total_retained_delivered_photos"] + summary["total_retained_delivered_info"]

    if summary["errors"]:
        summary["overall_residual_privacy_risk"] = "ERROR"
    elif summary["total_archives_scanned"] == 0:
        summary["overall_residual_privacy_risk"] = "BLOCKED (No supported archives found in configured backup root)"
    elif summary["unsupported_archives"]:
        summary["overall_residual_privacy_risk"] = (
            f"BLOCKED (Unsupported archive format(s) detected: {len(summary['unsupported_archives'])} unscanned file(s))"
        )
    elif summary["symlinked_entries"]:
        summary["overall_residual_privacy_risk"] = (
            f"BLOCKED (Symlinked backup entries detected: {len(summary['symlinked_entries'])} symlink(s))"
        )
    elif any(a["residual_privacy_risk"].startswith("ERROR") for a in summary["archives"]):
        summary["overall_residual_privacy_risk"] = "ERROR (One or more archives failed inspection)"
    elif total_delivered_retained > 0:
        parts: List[str] = []
        if summary["total_retained_delivered_photos"] > 0:
            parts.append(f"{summary['total_retained_delivered_photos']} photo(s)")
        if summary["total_retained_delivered_info"] > 0:
            parts.append(f"{summary['total_retained_delivered_info']} metadata file(s)")
        summary["overall_residual_privacy_risk"] = (
            f"HIGH ({', '.join(parts)} delivered retained across "
            f"{summary['archives_with_residual_privacy_risk']} archive(s))"
        )
    elif summary["total_untracked_files"] > 0 or any(a["residual_privacy_risk"].startswith("UNKNOWN") for a in summary["archives"]):
        summary["overall_residual_privacy_risk"] = (
            f"UNKNOWN ({summary['total_untracked_files']} unclassified/untracked file(s) present)"
        )
    else:
        summary["overall_residual_privacy_risk"] = "ZERO (0 delivered customer photos or metadata retained across all archives)"

    return summary


def _aggregate_archive_report(summary: Dict[str, Any], rep: Dict[str, Any]) -> None:
    retained_photos = len(rep.get("retained_delivered_photos", []))
    retained_info = len(rep.get("retained_delivered_info", []))
    if (retained_photos + retained_info) > 0:
        summary["archives_with_residual_privacy_risk"] += 1
    summary["total_retained_delivered_photos"] += retained_photos
    summary["total_retained_delivered_info"] += retained_info
    summary["total_unfinished_recovery_photos"] += len(rep.get("retained_unfinished_photos", []))
    summary["total_unfinished_recovery_info"] += len(rep.get("retained_unfinished_info", []))
    summary["total_untracked_files"] += len(rep.get("untracked_archived_files", [])) + len(rep.get("unclassified_legacy_files", []))
    if rep.get("errors"):
        summary["errors"].extend(rep["errors"])


def print_audit_report(summary: Dict[str, Any], *, as_json: bool = False, show_details: bool = False) -> None:
    if as_json:
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
    print(f"Archives with Retained:   {summary.get('archives_with_residual_privacy_risk', 1 if summary.get('retained_delivered_photos') or summary.get('retained_delivered_info') else 0)}")
    print(f"Retained Delivered Photos:{summary.get('total_retained_delivered_photos', len(summary.get('retained_delivered_photos', [])))}")
    print(f"Retained Delivered Info:  {summary.get('total_retained_delivered_info', len(summary.get('retained_delivered_info', [])))}")
    print(f"Unfinished Recovery Photos:{summary.get('total_unfinished_recovery_photos', len(summary.get('retained_unfinished_photos', [])))}")
    print(f"Untracked/Legacy Files:   {summary.get('total_untracked_files', len(summary.get('untracked_archived_files', [])) + len(summary.get('unclassified_legacy_files', [])))}")
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
        print(f"  Unfinished Recovery:    {len(a.get('retained_unfinished_photos', []))} photos, {len(a.get('retained_unfinished_info', []))} info")
        print(f"  Untracked / Legacy:     {len(a.get('untracked_archived_files', [])) + len(a.get('unclassified_legacy_files', []))} files")
        print(f"  Residual Privacy Risk:  {a.get('residual_privacy_risk')}")

        if a.get("errors"):
            print("  Errors:")
            for err in a["errors"]:
                print(f"    - {err}")

        retained_items = a.get("retained_delivered_photos", []) + a.get("retained_delivered_info", [])
        if show_details and retained_items:
            print("  [SENSITIVE DIAGNOSTIC DATA - OWNER REMEDIATION ONLY]")
            for item in retained_items[:10]:
                print(f"    - Order #{item['order_id']} ({item.get('kind', 'file')}, Job: {item['job_id'][:8]}..., {item['size']} bytes, file: {item['filename']})")
            if len(retained_items) > 10:
                print(f"    ... and {len(retained_items) - 10} more items")
        elif not show_details and retained_items:
            print("  (Sensitive filenames withheld. Pass --show-details to inspect specific items for remediation)")

    print("\n==================================================")
    print("      OWNER-CONTROLLED PURGE PROCEDURE RUNBOOK     ")
    print("==================================================")
    print("NOTICE: Historical backup archives represent a residual privacy risk")
    print("if they contain customer photos or metadata from orders that were already delivered.")
    print("This audit tool NEVER deletes or modifies backup files automatically.")
    print("GNU tar does not support in-place deletion on compressed archives (.tar.gz).")
    print("\nSafe 7-Step Owner-Controlled Remediation Workflow:")
    print("1. Inventory exact archive and verify its cryptographic checksum:")
    print("   cd <archive_dir> && sha256sum -c manifest.sha256")
    print("   tar -ztvf uploads.tar.gz > original_manifest.txt")
    print("2. Create an isolated temporary working directory:")
    print("   WORK_DIR=$(mktemp -d -t photodoc_rebuild_XXXXXX)")
    print("   mkdir -p \"${WORK_DIR}/rebuild\" \"${WORK_DIR}/candidate\"")
    print("   cp uploads.tar.gz \"${WORK_DIR}/uploads.tar.gz.orig\"")
    print("   cp orders.db \"${WORK_DIR}/candidate/orders.db\"")
    print("3. Preserve the original archive:")
    print("   cp uploads.tar.gz uploads.tar.gz.pre-purge")
    print("4. Rebuild candidate archive without explicitly approved delivered files:")
    print("   tar -xzf \"${WORK_DIR}/uploads.tar.gz.orig\" -C \"${WORK_DIR}/rebuild\"")
    print("   rm -f \"${WORK_DIR}/rebuild/uploads/<approved_delivered_filename>\"")
    print("   tar --numeric-owner -czvf \"${WORK_DIR}/candidate/uploads.tar.gz\" -C \"${WORK_DIR}/rebuild\" uploads")
    print("5. Place candidate under exact filenames expected by integrity verifier and privacy auditor:")
    print("   # ${WORK_DIR}/candidate contains exact expected filenames: orders.db and uploads.tar.gz")
    print("   # ${WORK_DIR}/rebuild/uploads contains uncompressed tree for integrity verifier")
    print("6. Run archive readability checks, checksum validation, backup integrity verification, privacy auditing and unfinished-delivery recovery checks:")
    print("   tar -ztvf \"${WORK_DIR}/candidate/uploads.tar.gz\"")
    print("   cd \"${WORK_DIR}/candidate\" && sha256sum orders.db uploads.tar.gz > manifest.sha256")
    print("   python3 scripts/verify_backup_integrity.py --db \"${WORK_DIR}/candidate/orders.db\" --uploads \"${WORK_DIR}/rebuild/uploads\"")
    print("   python3 scripts/audit_backup_archives.py --archive-dir \"${WORK_DIR}/candidate\"")
    print("7. Require explicit owner authorization before replacing or purging any historical archive:")
    print("   # OWNER AUTHORIZATION REQUIRED BEFORE PROCEEDING")
    print("   # Once all verification checks above PASS cleanly, owner executes replacement:")
    print("   cp \"${WORK_DIR}/candidate/uploads.tar.gz\" <archive_dir>/uploads.tar.gz")
    print("   cp \"${WORK_DIR}/candidate/manifest.sha256\" <archive_dir>/manifest.sha256")
    print("   rm -rf \"${WORK_DIR}\"")
    print("==================================================")


def get_cli_exit_code(overall_risk: str) -> int:
    """
    Deterministic CLI Exit Code Contract:
    0: Valid, complete inventory with ZERO confirmed retained delivered photos/metadata and ZERO untracked files.
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
