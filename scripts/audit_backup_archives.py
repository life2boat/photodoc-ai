#!/usr/bin/env python3
"""
PhotoDoc AI — Historical Backup Archive Auditor & Privacy Risk Assessor.

Fail-Closed Privacy Audit Contract:
1. Discovers and classifies all backup archives in the configured backup root:
   - Standard release directories (containing orders.db and uploads.tar.gz / uploads.tar).
   - Standalone combined archives (e.g. photodoc_cutover.tar.gz, .tgz, .tar).
   - Identifies and flags unsupported or unclassified archive formats (.zip, .tar.bz2, etc.).
   - Recursively traverses subdirectories to arbitrary depth without double-counting internal archives.
2. Symbolic Link Boundary Security:
   - Never follows symbolic links when discovering or inspecting backup archives.
   - Rejects symlinked directories, database files, upload archives, and combined archives.
   - Prunes symlinked directories during traversal and excludes symlinked files.
   - Fails closed with BLOCKED (exit code 2) upon detecting any symlink in backup paths.
3. Centralized Resource Limits:
   - Enforces MAX_ARCHIVE_MEMBERS across ALL archive inspection paths.
   - Enforces MAX_SINGLE_FILE_SIZE_BYTES and MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES.
   - Rejects malformed metadata, impossible sizes (size < 0), and disallowed member types.
   - Streams database extraction with strict size bounding to prevent decompression bombs.
4. Reads SQLite database in read-only mode to retrieve delivery job and order records.
5. Inspects uploads archive in-memory without extracting files to permanent storage.
6. Distinguishes:
   - Confirmed delivered photos and customer-information metadata files (HIGH risk).
   - Legitimate recovery originals for unfinished deliveries (pending/processing/failed).
   - Untracked or unclassified archive files (UNKNOWN / BLOCKED: never falsely classified as ZERO).
7. Strictly read-only / dry-run: never modifies, moves, or deletes any backup archive.
8. CLI Exit Code Contract:
   - 0: Strictly all archives are readable, valid, fully classified, with ZERO retained delivered data and ZERO untracked files.
   - Nonzero: Confirmed privacy risk (1), unclassified/untracked/blocked files or empty inventory (2), read/integrity/resource error (3).
9. Output sanitization: customer PII and sensitive filenames are withheld unless --show-details is explicitly requested.
10. Provides an authoritative, executable 7-step owner-controlled remediation runbook.
"""

from __future__ import annotations

import argparse
import json
import shlex
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

# Centralized Resource Limits Contract
MAX_ARCHIVE_MEMBERS = 100_000
MAX_DATABASE_MEMBER_SIZE_BYTES = 500 * 1024 * 1024           # 500 MB
MAX_SINGLE_FILE_SIZE_BYTES = 1024 * 1024 * 1024              # 1 GB
MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB


def audit_single_backup_dir(backup_dir: Path) -> Dict[str, Any]:
    """
    Audits a single backup directory containing orders.db and uploads.tar.gz (or uploads.tar).
    Strictly read-only, symlink-resistant, and fail-closed.
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
        "privacy_status": "UNKNOWN",
        "errors": [],
    }

    # Symlink boundary enforcement
    if backup_dir.is_symlink():
        archive_report["errors"].append(f"Backup directory cannot be a symbolic link: {backup_dir}")
        archive_report["residual_privacy_risk"] = "BLOCKED (Symlinked backup directory)"
        return archive_report

    if not backup_dir.exists():
        archive_report["errors"].append(f"Backup directory does not exist: {backup_dir}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report

    if not backup_dir.is_dir():
        archive_report["errors"].append(f"Backup target is not a directory: {backup_dir}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report

    # Check for symlink entries inside backup_dir
    try:
        for entry in backup_dir.iterdir():
            if entry.is_symlink():
                archive_report["errors"].append(f"Symbolic link detected inside backup directory: {entry.name}")
                archive_report["residual_privacy_risk"] = "BLOCKED (Symlinked entry in backup directory)"
                return archive_report
    except Exception as exc:
        archive_report["errors"].append(f"Failed to inspect backup directory entries: {exc}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report

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

    if db_path.is_symlink():
        archive_report["errors"].append(f"Database file cannot be a symbolic link: {db_path}")
        archive_report["residual_privacy_risk"] = "BLOCKED (Symlinked database file)"
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

    if tar_path.is_symlink():
        archive_report["errors"].append(f"Uploads archive cannot be a symbolic link: {tar_path}")
        archive_report["residual_privacy_risk"] = "BLOCKED (Symlinked uploads archive)"
        return archive_report

    archive_report["uploads_tar_found"] = True

    return _audit_db_and_tar(db_path, tar_path, archive_report)


def audit_single_combined_tar(tar_path: Path) -> Dict[str, Any]:
    """
    Audits a standalone combined archive (e.g. photodoc_cutover.tar.gz or .tar containing orders.db and uploads/).
    Strictly read-only, symlink-resistant, safe extraction with bounded copying, and fail-closed.
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
        "privacy_status": "UNKNOWN",
        "errors": [],
    }

    if tar_path.is_symlink():
        archive_report["errors"].append(f"Combined archive cannot be a symbolic link: {tar_path}")
        archive_report["residual_privacy_risk"] = "BLOCKED (Symlinked combined archive)"
        return archive_report

    if not tar_path.is_file():
        archive_report["errors"].append(f"Archive file not found: {tar_path}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report

    try:
        with tarfile.open(tar_path, "r:*") as tar:
            members = tar.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                archive_report["errors"].append(
                    f"Combined archive exceeds member count limit ({len(members)} > {MAX_ARCHIVE_MEMBERS})"
                )
                archive_report["privacy_status"] = "ERROR_LIMIT_EXCEEDED"
                archive_report["residual_privacy_risk"] = "ERROR"
                return archive_report

            total_uncompressed_bytes = 0
            db_candidates: List[tarfile.TarInfo] = []

            for m in members:
                # Malformed size validation
                if not isinstance(m.size, int) or m.size < 0:
                    archive_report["errors"].append(f"Malformed member size in archive: {m.name} ({m.size})")
                    archive_report["privacy_status"] = "ERROR_CORRUPT"
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                # Security: reject path traversal and absolute paths
                if ".." in Path(m.name).parts or m.name.startswith("/") or Path(m.name).is_absolute():
                    archive_report["errors"].append(f"Path traversal detected in archive member: {m.name}")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                # Security: reject symlinks and hardlinks inside archive
                if m.issym() or m.islnk():
                    archive_report["errors"].append(f"Combined archive {tar_path.name} contains unsafe symlink/hardlink member: {m.name}")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                if not (m.isreg() or m.isdir()):
                    archive_report["errors"].append(f"Disallowed archive member type: {m.name} (type={m.type})")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                if m.isreg():
                    if m.size > MAX_SINGLE_FILE_SIZE_BYTES:
                        archive_report["errors"].append(
                            f"Archive member exceeds single-file size limit: {m.name} ({m.size} > {MAX_SINGLE_FILE_SIZE_BYTES})"
                        )
                        archive_report["privacy_status"] = "ERROR_LIMIT_EXCEEDED"
                        archive_report["residual_privacy_risk"] = "ERROR"
                        return archive_report

                    total_uncompressed_bytes += m.size
                    if total_uncompressed_bytes > MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES:
                        archive_report["errors"].append(
                            f"Archive exceeds total uncompressed size limit ({total_uncompressed_bytes} > {MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES})"
                        )
                        archive_report["privacy_status"] = "ERROR_LIMIT_EXCEEDED"
                        archive_report["residual_privacy_risk"] = "ERROR"
                        return archive_report

                base_name = Path(m.name).name
                is_db_name = (
                    base_name in ("orders.db", "orders.db.cutover")
                    or (base_name.endswith(".db") and "orders" in base_name.lower())
                )
                if is_db_name and m.isreg():
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
                archive_report["privacy_status"] = "ERROR_LIMIT_EXCEEDED"
                archive_report["residual_privacy_risk"] = "ERROR"
                return archive_report

            # Safe, strictly bounded streaming extraction to a fixed temporary file
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                tmp_db_file = tmp_path / "orders_inspected.db"
                f_in = tar.extractfile(db_member)
                if not f_in:
                    archive_report["errors"].append(f"Failed to read database member stream from {tar_path}")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                bytes_copied = 0
                chunk_size = 65536
                with tmp_db_file.open("wb") as f_out:
                    while True:
                        chunk = f_in.read(chunk_size)
                        if not chunk:
                            break
                        bytes_copied += len(chunk)
                        if bytes_copied > MAX_DATABASE_MEMBER_SIZE_BYTES:
                            archive_report["errors"].append(
                                f"Database member stream exceeded maximum permitted size ({bytes_copied} > {MAX_DATABASE_MEMBER_SIZE_BYTES})"
                            )
                            archive_report["residual_privacy_risk"] = "ERROR"
                            return archive_report
                        f_out.write(chunk)

                if bytes_copied != db_member.size:
                    archive_report["errors"].append(
                        f"Database member size mismatch: declared {db_member.size} bytes but extracted {bytes_copied} bytes"
                    )
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

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

    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row

        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if integrity != "ok":
            archive_report["errors"].append(f"SQLite PRAGMA integrity_check failed: {integrity}")
            archive_report["residual_privacy_risk"] = "ERROR"
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
        if "filename" in orders_cols:
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
    except Exception as exc:
        archive_report["errors"].append(f"Database read error: {exc}")
        archive_report["residual_privacy_risk"] = "ERROR"
        return archive_report
    finally:
        if conn is not None:
            conn.close()

    # 2. Inspect tarball in-memory with strict resource limit enforcement
    try:
        with tarfile.open(tar_path, "r:*") as tar:
            members = tar.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                archive_report["errors"].append(
                    f"Archive exceeds member count limit ({len(members)} > {MAX_ARCHIVE_MEMBERS}): {tar_path}"
                )
                archive_report["privacy_status"] = "ERROR_LIMIT_EXCEEDED"
                archive_report["residual_privacy_risk"] = "ERROR"
                return archive_report

            total_uncompressed_bytes = 0

            for m in members:
                # Malformed size validation
                if not isinstance(m.size, int) or m.size < 0:
                    archive_report["errors"].append(f"Malformed member size in archive: {m.name} ({m.size})")
                    archive_report["privacy_status"] = "ERROR_CORRUPT"
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                # Security: check traversal and symlinks
                if ".." in Path(m.name).parts or m.name.startswith("/") or Path(m.name).is_absolute():
                    archive_report["errors"].append(f"Path traversal detected in archive member: {m.name}")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                if m.issym() or m.islnk():
                    archive_report["errors"].append(f"Uploads archive {tar_path.name} contains unsafe symlink/hardlink member: {m.name}")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                if not (m.isreg() or m.isdir()):
                    archive_report["errors"].append(f"Disallowed archive member type: {m.name} (type={m.type})")
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                if not m.isfile():
                    continue

                if m.size > MAX_SINGLE_FILE_SIZE_BYTES:
                    archive_report["errors"].append(
                        f"Archive member exceeds size limit ({m.size} > {MAX_SINGLE_FILE_SIZE_BYTES}): {m.name}"
                    )
                    archive_report["privacy_status"] = "ERROR_LIMIT_EXCEEDED"
                    archive_report["residual_privacy_risk"] = "ERROR"
                    return archive_report

                total_uncompressed_bytes += m.size
                if total_uncompressed_bytes > MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES:
                    archive_report["errors"].append(
                        f"Archive exceeds total uncompressed size limit ({total_uncompressed_bytes} > {MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES})"
                    )
                    archive_report["privacy_status"] = "ERROR_LIMIT_EXCEEDED"
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
    Fail-closed:
    - Prunes symlinked directories and excludes symlinked files.
    - Returns BLOCKED if symlinks are detected, if root is a symlink, if empty, or if unsupported formats are present.
    - Avoids double-counting internal uploads archives that belong to an already discovered backup directory.
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

    if root_dir.is_symlink():
        summary["errors"].append(f"Configured backup root cannot be a symbolic link: {root_dir}")
        summary["overall_residual_privacy_risk"] = "BLOCKED (Backup root is a symbolic link)"
        return summary

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

    # Recursive traversal with symlink pruning
    for current_dir_str, dirnames, filenames in os.walk(str(root_dir), followlinks=False, onerror=_walk_error):
        current_dir = Path(current_dir_str)

        # 1. Prune symbolic-link directories from recursive traversal
        for dname in list(dirnames):
            dp = current_dir / dname
            if dp.is_symlink():
                summary["symlinked_entries"].append(str(dp))
                dirnames.remove(dname)  # NEVER traverse into symlink directories!

        # 2. Check current_dir for candidate directory backup (only if not a symlink)
        if not current_dir.is_symlink():
            has_symlink_file = False
            has_db = False
            has_tar = False

            # Check DB candidates in current_dir without following symlinks
            for cand_db_name in ["orders.db", "orders.db.cutover"]:
                p = current_dir / cand_db_name
                if p.is_symlink():
                    summary["symlinked_entries"].append(str(p))
                    has_symlink_file = True
                elif p.is_file():
                    has_db = True

            for cand_tar_name in ["uploads.tar.gz", "uploads.tgz", "uploads.tar"]:
                p = current_dir / cand_tar_name
                if p.is_symlink():
                    summary["symlinked_entries"].append(str(p))
                    has_symlink_file = True
                elif p.is_file():
                    has_tar = True

            if has_db and has_tar and not has_symlink_file:
                candidate_dirs.add(current_dir)

        # 3. Check filenames in current_dir
        for fname in filenames:
            fp = current_dir / fname
            if fp.is_symlink():
                summary["symlinked_entries"].append(str(fp))
                continue  # EXCLUDE symbolic-link files from candidate archives!

            lower_name = fname.lower()
            for ext in UNSUPPORTED_ARCHIVE_EXTENSIONS:
                if lower_name.endswith(ext):
                    summary["unsupported_archives"].append(
                        {"path": str(fp), "format": ext, "size": fp.stat().st_size if fp.exists() else 0}
                    )
                    break

            for ext in SUPPORTED_ARCHIVE_EXTENSIONS:
                if lower_name.endswith(ext):
                    candidate_tarballs.add(fp)
                    break

    # Filter candidate_tarballs:
    # Avoid double-counting internal uploads archives that belong to an already discovered backup directory
    filtered_tarballs: Set[Path] = set()
    for ctar in candidate_tarballs:
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
    elif summary["symlinked_entries"]:
        summary["overall_residual_privacy_risk"] = (
            f"BLOCKED (Symlinked backup entries detected: {len(summary['symlinked_entries'])} symlink(s))"
        )
    elif summary["total_archives_scanned"] == 0:
        summary["overall_residual_privacy_risk"] = "BLOCKED (No supported archives found in configured backup root)"
    elif summary["unsupported_archives"]:
        summary["overall_residual_privacy_risk"] = (
            f"BLOCKED (Unsupported archive format(s) detected: {len(summary['unsupported_archives'])} unscanned file(s))"
        )
    elif any(a["residual_privacy_risk"].startswith("ERROR") for a in summary["archives"]):
        summary["overall_residual_privacy_risk"] = "ERROR (One or more archives failed inspection)"
    elif any(a["residual_privacy_risk"].startswith("BLOCKED") for a in summary["archives"]):
        summary["overall_residual_privacy_risk"] = "BLOCKED (One or more archives blocked by security policy)"
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


def validate_removal_target(filename: str) -> str:
    """
    Validates that a removal target is a safe single filename within uploads:
    - Rejects path traversal (..), absolute paths, and path separators (/ or \\).
    - Rejects control characters and dangerous shell injection characters.
    - Allows valid filenames containing spaces and Unicode characters.
    """
    if not filename or not isinstance(filename, str):
        raise ValueError("Removal target must be a non-empty string")
    if "\x00" in filename or "\n" in filename or "\r" in filename:
        raise ValueError(f"Removal target contains control characters: {repr(filename)}")
    if "/" in filename or "\\" in filename:
        raise ValueError(f"Removal target cannot contain path separators: {filename}")
    if filename in (".", "..") or ".." in filename:
        raise ValueError(f"Removal target cannot contain path traversal: {filename}")
    for ch in [";", "&", "|", "`", "$", "(", ")", "<", ">", '"', "'", "*", "?", "[", "]"]:
        if ch in filename:
            raise ValueError(f"Removal target contains disallowed shell character '{ch}': {filename}")
    return filename


def generate_purge_runbook_script(
    archive_dir: Path,
    repo_root: Path,
    delivered_files_to_remove: Optional[List[str]] = None,
) -> str:
    """
    Authoritative generator of the two-phase owner-controlled remediation runbook script.
    - Phase 1 (--prepare): Prepares and validates candidate; verifies original archive immutability;
      preserves race-safe pre-purge baseline. NEVER replaces historical archives.
    - Phase 2 (--replace-authorized <CANDIDATE_SHA>): Owner-authorized atomic replacement. Fails closed
      if authorization is missing or mismatched, or if any verification check fails.
    """
    arch_resolved = Path(archive_dir).resolve()
    repo_resolved = Path(repo_root).resolve()
    arch_posix = arch_resolved.as_posix()
    repo_posix = repo_resolved.as_posix()
    q_arch = shlex.quote(arch_posix)
    q_repo = shlex.quote(repo_posix)

    if delivered_files_to_remove:
        validated_targets = [validate_removal_target(f) for f in delivered_files_to_remove]
        rm_lines = [f'rm -f "${{REBUILD_DIR}}/uploads/"{shlex.quote(f)}' for f in validated_targets]
        rm_cmds = "\n        ".join(rm_lines)
    else:
        rm_cmds = 'rm -f "${REBUILD_DIR}/uploads/<approved_delivered_filename>"'

    return f"""#!/usr/bin/env bash
set -euo pipefail
# PhotoDoc AI — Safe Owner-Controlled Archive Remediation Runbook
# Two-Phase Workflow: Prepare & Verify (Phase 1) -> Owner-Authorized Replacement (Phase 2)

ARCHIVE_DIR={q_arch}
REPO_ROOT={q_repo}
STAGING_DIR="${{ARCHIVE_DIR}}/.purge_staging"
REBUILD_DIR="${{STAGING_DIR}}/rebuild"
CANDIDATE_DIR="${{STAGING_DIR}}/candidate"
META_FILE="${{STAGING_DIR}}/candidate_metadata.json"

# Detect Python interpreter
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' 2>/dev/null; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1 && python -c 'import sys' 2>/dev/null; then
    PYTHON_BIN="python"
else
    echo "ERROR: Python interpreter not found" >&2
    exit 1
fi

ACTION="${{1:---prepare}}"

case "${{ACTION}}" in
    --prepare)
        echo "=== [Phase 1: Candidate Preparation & Verification] ==="
        if [ ! -d "${{ARCHIVE_DIR}}" ]; then
            echo "ERROR: Archive directory does not exist: ${{ARCHIVE_DIR}}" >&2
            exit 1
        fi
        if [ ! -f "${{ARCHIVE_DIR}}/uploads.tar.gz" ] || [ ! -f "${{ARCHIVE_DIR}}/orders.db" ]; then
            echo "ERROR: Missing uploads.tar.gz or orders.db in ${{ARCHIVE_DIR}}" >&2
            exit 1
        fi

        # Pre-purge baseline overwrite protection
        if [ -e "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" ]; then
            echo "ERROR: uploads.tar.gz.pre-purge already exists; aborting to prevent overwrite" >&2
            exit 1
        fi

        # Calculate original archive checksum
        ORIG_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        tar --force-local -ztvf "${{ARCHIVE_DIR}}/uploads.tar.gz" > "${{ARCHIVE_DIR}}/original_manifest.txt"

        # Race-safe atomic exclusive creation of .pre-purge baseline
        "${{PYTHON_BIN}}" -c '
import sys, shutil
src = sys.argv[1]
dst = sys.argv[2]
try:
    with open(src, "rb") as f_in, open(dst, "xb") as f_out:
        shutil.copyfileobj(f_in, f_out)
except FileExistsError:
    sys.stderr.write("ERROR: Pre-purge baseline already exists; aborting to prevent overwrite\\n")
    sys.exit(1)
' "${{ARCHIVE_DIR}}/uploads.tar.gz" "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge"

        # Verify preserved baseline matches recorded original checksum
        PRE_PURGE_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" | awk '{{print $1}}')
        if [ "${{PRE_PURGE_SHA}}" != "${{ORIG_SHA}}" ]; then
            echo "ERROR: Preserved baseline SHA-256 (${{PRE_PURGE_SHA}}) does not match original (${{ORIG_SHA}})" >&2
            exit 1
        fi

        # Prepare isolated workspace
        rm -rf "${{STAGING_DIR}}"
        mkdir -p "${{REBUILD_DIR}}" "${{CANDIDATE_DIR}}"
        cp "${{ARCHIVE_DIR}}/uploads.tar.gz" "${{STAGING_DIR}}/uploads.tar.gz.orig"
        cp "${{ARCHIVE_DIR}}/orders.db" "${{CANDIDATE_DIR}}/orders.db"

        # Extract uploads
        tar --force-local -xzf "${{STAGING_DIR}}/uploads.tar.gz.orig" -C "${{REBUILD_DIR}}"

        # Remove approved target delivered files
        {rm_cmds}

        # Repackage candidate archive
        tar --force-local --numeric-owner -czf "${{CANDIDATE_DIR}}/uploads.tar.gz" -C "${{REBUILD_DIR}}" uploads
        tar --force-local -ztvf "${{CANDIDATE_DIR}}/uploads.tar.gz" >/dev/null

        # Candidate manifest & checksum
        (cd "${{CANDIDATE_DIR}}" && sha256sum orders.db uploads.tar.gz > manifest.sha256)
        CANDIDATE_SHA=$(sha256sum "${{CANDIDATE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')

        # Run verification CLIs
        echo "Running backup integrity verifier on candidate..."
        "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/verify_backup_integrity.py" --db "${{CANDIDATE_DIR}}/orders.db" --uploads "${{REBUILD_DIR}}/uploads"
        echo "Running privacy risk auditor on candidate..."
        "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/audit_backup_archives.py" --archive-dir "${{CANDIDATE_DIR}}"

        # Guard original archive immutability during preparation
        CHECK_ORIG_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        if [ "${{CHECK_ORIG_SHA}}" != "${{ORIG_SHA}}" ]; then
            echo "ERROR: Original archive was modified during preparation; aborting" >&2
            exit 1
        fi
        CHECK_PRE_PURGE_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" | awk '{{print $1}}')
        if [ "${{CHECK_PRE_PURGE_SHA}}" != "${{ORIG_SHA}}" ]; then
            echo "ERROR: Preserved baseline was modified during preparation; aborting" >&2
            exit 1
        fi

        # Save metadata for authorization gate
        "${{PYTHON_BIN}}" -c '
import json, sys
data = {{
    "archive_dir": sys.argv[1],
    "orig_sha": sys.argv[2],
    "candidate_sha": sys.argv[3],
    "prepared": True
}}
with open(sys.argv[4], "w") as f:
    json.dump(data, f)
' "${{ARCHIVE_DIR}}" "${{ORIG_SHA}}" "${{CANDIDATE_SHA}}" "${{META_FILE}}"

        echo ""
        echo "================================================================================"
        echo "CANDIDATE PREPARATION & VERIFICATION COMPLETE (PHASE 1)"
        echo "Target Archive:            ${{ARCHIVE_DIR}}"
        echo "Original SHA-256:          ${{ORIG_SHA}}"
        echo "Preserved Baseline:        ${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge"
        echo "Preserved Baseline SHA:    ${{ORIG_SHA}}"
        echo "Candidate SHA-256:         ${{CANDIDATE_SHA}}"
        echo "Integrity Verification:    PASS"
        echo "Privacy Audit Verdict:     ZERO (residual privacy risk eliminated)"
        echo "Historical Archive State:  UNCHANGED / IMMUTABLE"
        echo ""
        echo "[OWNER ACTION REQUIRED FOR REPLACEMENT]"
        echo "Phase 1 has NOT modified or deleted the historical archive."
        echo "To authorize replacement, run Phase 2 with explicit authorization:"
        echo "  $0 --replace-authorized ${{CANDIDATE_SHA}}"
        echo "================================================================================"
        ;;

    --replace-authorized)
        echo "=== [Phase 2: Owner-Authorized Replacement] ==="
        AUTHORIZED_SHA="${{2:-}}"
        if [ -z "${{AUTHORIZED_SHA}}" ]; then
            echo "ERROR: Owner authorization missing. To authorize replacement, pass candidate SHA-256: --replace-authorized <SHA256>" >&2
            exit 2
        fi

        if [ ! -f "${{META_FILE}}" ] || [ ! -f "${{CANDIDATE_DIR}}/uploads.tar.gz" ]; then
            echo "ERROR: Candidate workspace or metadata missing; run preparation phase first" >&2
            exit 1
        fi

        # Check candidate SHA-256
        ACTUAL_CAND_SHA=$(sha256sum "${{CANDIDATE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        if [ "${{AUTHORIZED_SHA}}" != "${{ACTUAL_CAND_SHA}}" ]; then
            echo "ERROR: Authorization SHA-256 mismatch (provided: ${{AUTHORIZED_SHA}}, candidate: ${{ACTUAL_CAND_SHA}}); aborting" >&2
            exit 1
        fi

        # Read expected original SHA
        EXPECTED_ORIG_SHA=$("${{PYTHON_BIN}}" -c 'import json, sys; print(json.load(open(sys.argv[1]))["orig_sha"])' "${{META_FILE}}")

        # Verify original archive and baseline have not changed
        CURRENT_ORIG_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        if [ "${{CURRENT_ORIG_SHA}}" != "${{EXPECTED_ORIG_SHA}}" ]; then
            echo "ERROR: Original archive was modified after preparation; aborting replacement" >&2
            exit 1
        fi

        if [ ! -f "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" ]; then
            echo "ERROR: Preserved baseline uploads.tar.gz.pre-purge is missing; aborting replacement" >&2
            exit 1
        fi

        CURRENT_PRE_PURGE_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" | awk '{{print $1}}')
        if [ "${{CURRENT_PRE_PURGE_SHA}}" != "${{EXPECTED_ORIG_SHA}}" ]; then
            echo "ERROR: Preserved baseline was modified; aborting replacement" >&2
            exit 1
        fi

        echo "Re-verifying candidate integrity and privacy audit prior to replacement..."
        "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/verify_backup_integrity.py" --db "${{CANDIDATE_DIR}}/orders.db" --uploads "${{REBUILD_DIR}}/uploads"
        "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/audit_backup_archives.py" --archive-dir "${{CANDIDATE_DIR}}"

        # Atomic replacement on same filesystem
        TMP_REPLACE="${{ARCHIVE_DIR}}/.uploads.tar.gz.tmp_replacement"
        cp "${{CANDIDATE_DIR}}/uploads.tar.gz" "${{TMP_REPLACE}}"
        TMP_SHA=$(sha256sum "${{TMP_REPLACE}}" | awk '{{print $1}}')
        if [ "${{TMP_SHA}}" != "${{ACTUAL_CAND_SHA}}" ]; then
            echo "ERROR: Temporary replacement file checksum mismatch; aborting" >&2
            rm -f "${{TMP_REPLACE}}"
            exit 1
        fi

        mv "${{TMP_REPLACE}}" "${{ARCHIVE_DIR}}/uploads.tar.gz"
        (cd "${{ARCHIVE_DIR}}" && sha256sum orders.db uploads.tar.gz > manifest.sha256)

        # Post-replacement verification
        echo "Running post-replacement privacy audit on active archive..."
        "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/audit_backup_archives.py" --archive-dir "${{ARCHIVE_DIR}}"

        # Cleanup staging workspace ONLY after all verification passes
        rm -rf "${{STAGING_DIR}}"

        echo ""
        echo "================================================================================"
        echo "OWNER-AUTHORIZED REPLACEMENT COMPLETED SUCCESSFULLY"
        echo "Archive:                   ${{ARCHIVE_DIR}}"
        echo "Preserved Baseline:        ${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge"
        echo "New Archive SHA-256:       ${{ACTUAL_CAND_SHA}}"
        echo "Post-Replacement Audit:    PASS (ZERO residual privacy risk)"
        echo "================================================================================"
        ;;

    *)
        echo "Usage: $0 [--prepare | --replace-authorized <CANDIDATE_SHA>]" >&2
        exit 2
        ;;
esac
"""


def generate_purge_runbook_commands(
    archive_dir: Path,
    repo_root: Path,
    delivered_files_to_remove: Optional[List[str]] = None,
) -> List[str]:
    """
    Returns the individual command lines from the authoritative two-phase runbook script.
    Guarantees that generate_purge_runbook_commands and generate_purge_runbook_script never diverge.
    """
    script = generate_purge_runbook_script(archive_dir, repo_root, delivered_files_to_remove)
    return [line for line in script.splitlines() if line.strip() and not line.strip().startswith("#")]


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
    print(f"Symlinked Entries:        {len(summary.get('symlinked_entries', []))}")
    print(f"Residual Privacy Risk:    {summary.get('overall_residual_privacy_risk') or summary.get('residual_privacy_risk')}")
    print("--------------------------------------------------")

    if summary.get("symlinked_entries"):
        print("\n[SECURITY WARNING: SYMBOLIC LINKS DETECTED]")
        for se in summary["symlinked_entries"]:
            print(f"  - {se} (symlink rejected)")

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

    target_archive = Path(summary.get("backup_dir") or "<archive_dir>")
    repo_root = REPO_DIR
    print("\n==================================================")
    print("      OWNER-CONTROLLED PURGE PROCEDURE RUNBOOK     ")
    print("==================================================")
    print("NOTICE: Historical backup archives represent a residual privacy risk")
    print("if they contain customer photos or metadata from orders that were already delivered.")
    print("This audit tool NEVER deletes or modifies backup files automatically.")
    print("GNU tar does not support in-place deletion on compressed archives (.tar.gz).")
    print("\nAuthoritative Executable Remediation Script:")
    print(generate_purge_runbook_script(target_archive, repo_root))
    print("==================================================")


def get_cli_exit_code(risk_or_summary: Any) -> int:
    """
    Deterministic CLI Exit Code Contract:
    0: Valid, complete inventory with ZERO confirmed retained delivered photos/metadata and ZERO untracked files.
    1: Confirmed privacy risk (HIGH).
    2: Unclassified, untracked, empty inventory, unsupported formats, or symlinks (UNKNOWN / BLOCKED).
    3: Read or integrity error (ERROR).
    """
    if isinstance(risk_or_summary, dict):
        overall_risk = str(
            risk_or_summary.get("overall_residual_privacy_risk")
            or risk_or_summary.get("residual_privacy_risk")
            or "UNKNOWN"
        )
    else:
        overall_risk = str(risk_or_summary or "UNKNOWN")

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
        "--backup-dir",
        type=Path,
        dest="archive_dir",
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
