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
    Authoritative generator of the transaction-safe two-phase owner-controlled remediation runbook script.
    - Phase 1 (--prepare): Prepares and validates candidate; verifies original archive immutability;
      preserves race-safe pre-purge baseline and metadata. Never modifies historical archives. Fails closed if staging exists.
    - Phase 2 (--confirm <CANDIDATE_SHA> | --replace-authorized <CANDIDATE_SHA>):
      Exclusive transaction-locked, atomic replacement of archive and manifest. Fails closed if confirmation
      is missing/mismatched or verification fails. Automatically rolls back to pre-purge baseline if post-replacement
      verification fails or manifest rename fails, preserving all recovery artifacts.
    """
    p_arch = Path(archive_dir)
    if p_arch.is_symlink() or os.path.islink(str(p_arch)):
        raise ValueError(f"Archive directory cannot be a symbolic link: {archive_dir}")
    arch_abs = Path(os.path.abspath(str(p_arch)))
    if arch_abs.is_symlink() or os.path.islink(str(arch_abs)):
        raise ValueError(f"Archive directory cannot be a symbolic link: {archive_dir}")

    p_repo = Path(repo_root)
    if p_repo.is_symlink() or os.path.islink(str(p_repo)):
        raise ValueError(f"Repository root cannot be a symbolic link: {repo_root}")
    repo_abs = Path(os.path.abspath(str(p_repo)))
    if repo_abs.is_symlink() or os.path.islink(str(repo_abs)):
        raise ValueError(f"Repository root cannot be a symbolic link: {repo_root}")

    arch_posix = arch_abs.as_posix()
    repo_posix = repo_abs.as_posix()
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
# PhotoDoc AI — Transaction-Safe Owner-Controlled Archive Remediation Runbook
# Two-Phase Workflow: Prepare & Verify (Phase 1) -> Operator-Confirmed Replacement & Rollback Guard (Phase 2)

ARCHIVE_DIR={q_arch}
REPO_ROOT={q_repo}
STAGING_DIR="${{ARCHIVE_DIR}}/.purge_staging"
REBUILD_DIR="${{STAGING_DIR}}/rebuild"
CANDIDATE_DIR="${{STAGING_DIR}}/candidate"
META_FILE="${{STAGING_DIR}}/candidate_metadata.json"
JOURNAL_FILE="${{ARCHIVE_DIR}}/.transaction_journal.json"
LOCK_DIR="${{ARCHIVE_DIR}}/.remediation.lock"
MV_BIN="${{MV_BIN:-mv}}"
TRANSACTION_PHASE="INITIALIZING"
INTERRUPTED=0
TMP_ARCHIVE=""
TMP_MANIFEST=""

# Detect Python interpreter
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' 2>/dev/null; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1 && python -c 'import sys' 2>/dev/null; then
    PYTHON_BIN="python"
else
    echo "ERROR: Python interpreter not found" >&2
    exit 1
fi

LOCK_ACQUIRED=0

acquire_lock() {{
    if [ -L "${{LOCK_DIR}}" ]; then
        echo "ERROR: Lock path is a symbolic link: ${{LOCK_DIR}}" >&2
        exit 1
    fi
    if ! mkdir "${{LOCK_DIR}}" 2>/dev/null; then
        echo "ERROR: Remediation transaction lock is held by another process: ${{LOCK_DIR}}" >&2
        exit 1
    fi
    echo "$$" > "${{LOCK_DIR}}/pid"
    LOCK_ACQUIRED=1
}}

release_lock() {{
    if [ "${{LOCK_ACQUIRED:-0}}" = "1" ]; then
        rm -rf "${{LOCK_DIR}}" 2>/dev/null || true
        LOCK_ACQUIRED=0
    fi
}}

update_journal() {{
    local phase="$1"
    local err_msg="${{2:-}}"
    "${{PYTHON_BIN}}" -c '
import json, sys, os
from datetime import datetime, timezone

journal_path = sys.argv[1]
phase = sys.argv[2]
err_msg = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None

data = {{}}
if os.path.exists(journal_path):
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        pass

data["phase"] = phase
data["updated_at"] = datetime.now(timezone.utc).isoformat()
data["pid"] = os.getpid()
if err_msg:
    data["last_error"] = err_msg

tmp_path = journal_path + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
os.replace(tmp_path, journal_path)
' "${{JOURNAL_FILE}}" "${{phase}}" "${{err_msg}}" 2>/dev/null || true
}}

init_journal() {{
    "${{PYTHON_BIN}}" -c '
import json, sys, os
from datetime import datetime, timezone

journal_path = sys.argv[1]
data = {{
    "archive_dir": sys.argv[2],
    "phase": "PRE_COMMIT",
    "started_at": datetime.now(timezone.utc).isoformat(),
    "pid": int(sys.argv[3]),
    "orig_archive_sha": sys.argv[4],
    "orig_db_sha": sys.argv[5],
    "had_manifest": sys.argv[6] == "true",
    "orig_manifest_sha": sys.argv[7] if sys.argv[7] != "" else None,
    "candidate_archive_sha": sys.argv[8],
    "candidate_db_sha": sys.argv[9],
    "candidate_manifest_sha": sys.argv[10],
    "last_error": None
}}
tmp_path = journal_path + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
os.replace(tmp_path, journal_path)
' "${{JOURNAL_FILE}}" "${{ARCHIVE_DIR}}" "$$" \
  "${{EXPECTED_ORIG_ARCH_SHA}}" "${{EXPECTED_ORIG_DB_SHA}}" \
  "${{HAD_MANIFEST}}" "${{EXPECTED_ORIG_MAN_SHA}}" \
  "${{ACTUAL_CAND_SHA}}" "${{EXPECTED_CAND_DB_SHA}}" "${{EXPECTED_CAND_MAN_SHA}}"
}}

execute_rollback() {{
    TRANSACTION_PHASE="ROLLING_BACK"
    update_journal "ROLLING_BACK"
    echo "CRITICAL: Post-replacement verification or mutation failed! Initiating safe atomic rollback..." >&2

    # 1. Check if baseline exists
    if [ ! -f "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" ]; then
        TRANSACTION_PHASE="FAILED"
        update_journal "FAILED" "Preserved baseline uploads.tar.gz.pre-purge is missing"
        echo "RECOVERY_REQUIRED: Manual intervention required! uploads.tar.gz.pre-purge missing" >&2
        return 1
    fi

    # 2. Restore archive from pre-purge baseline using exclusive open fd & fsync
    local rollback_arch
    rollback_arch=$("${{PYTHON_BIN}}" -c '
import sys, tempfile, shutil, os, pathlib

arch_dir = sys.argv[1]
pre_purge = sys.argv[2]

with tempfile.NamedTemporaryFile(dir=arch_dir, prefix=".uploads.rollback_", delete=False) as tf:
    p = tf.name
    try:
        os.chmod(tf.fileno(), 0o600)
    except Exception:
        pass
    with open(pre_purge, "rb") as src:
        shutil.copyfileobj(src, tf)
    tf.flush()
    os.fsync(tf.fileno())

if os.path.islink(p):
    os.unlink(p)
    sys.exit(1)

print(pathlib.Path(p).as_posix())
' "${{ARCHIVE_DIR}}" "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge")

    $MV_BIN "${{rollback_arch}}" "${{ARCHIVE_DIR}}/uploads.tar.gz"

    # 3. Restore or remove manifest based on HAD_MANIFEST
    if [ "${{HAD_MANIFEST}}" = "true" ]; then
        if [ ! -f "${{ARCHIVE_DIR}}/manifest.sha256.pre-purge" ]; then
            TRANSACTION_PHASE="FAILED"
            update_journal "FAILED" "manifest.sha256.pre-purge missing during rollback"
            echo "RECOVERY_REQUIRED: Manual intervention required! manifest.sha256.pre-purge missing" >&2
            return 1
        fi

        local rollback_man
        rollback_man=$("${{PYTHON_BIN}}" -c '
import sys, tempfile, shutil, os, pathlib

arch_dir = sys.argv[1]
pre_purge = sys.argv[2]

with tempfile.NamedTemporaryFile(dir=arch_dir, prefix=".manifest.rollback_", delete=False) as tf:
    p = tf.name
    try:
        os.chmod(tf.fileno(), 0o600)
    except Exception:
        pass
    with open(pre_purge, "rb") as src:
        shutil.copyfileobj(src, tf)
    tf.flush()
    os.fsync(tf.fileno())

if os.path.islink(p):
    os.unlink(p)
    sys.exit(1)

print(pathlib.Path(p).as_posix())
' "${{ARCHIVE_DIR}}" "${{ARCHIVE_DIR}}/manifest.sha256.pre-purge")

        $MV_BIN "${{rollback_man}}" "${{ARCHIVE_DIR}}/manifest.sha256"
    else
        # Manifest was absent originally: ensure any newly created manifest is removed
        rm -f "${{ARCHIVE_DIR}}/manifest.sha256"
    fi

    # Simulated rollback corruption hook for tests
    if [ "${{RUNBOOK_TEST_CORRUPT_ROLLBACK:-0}}" = "1" ]; then
        echo "CORRUPTED_DURING_TEST" > "${{ARCHIVE_DIR}}/uploads.tar.gz"
    fi

    # 4. Verify restored archive checksum against expected original
    local rollback_check_arch_sha
    rollback_check_arch_sha=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
    if [ "${{rollback_check_arch_sha}}" != "${{EXPECTED_ORIG_ARCH_SHA}}" ]; then
        TRANSACTION_PHASE="FAILED"
        update_journal "FAILED" "Restored archive checksum mismatch"
        echo "RECOVERY_REQUIRED: Manual intervention required! Restored archive checksum (${{rollback_check_arch_sha}}) does not match expected (${{EXPECTED_ORIG_ARCH_SHA}})" >&2
        echo "Pre-purge baseline remains preserved at ${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" >&2
        return 1
    fi

    # 5. Verify restored manifest state
    if [ "${{HAD_MANIFEST}}" = "true" ]; then
        local rollback_check_man_sha
        rollback_check_man_sha=$(sha256sum "${{ARCHIVE_DIR}}/manifest.sha256" | awk '{{print $1}}')
        if [ "${{rollback_check_man_sha}}" != "${{EXPECTED_ORIG_MAN_SHA}}" ]; then
            TRANSACTION_PHASE="FAILED"
            update_journal "FAILED" "Restored manifest checksum mismatch"
            echo "RECOVERY_REQUIRED: Manual intervention required! Restored manifest checksum (${{rollback_check_man_sha}}) does not match expected (${{EXPECTED_ORIG_MAN_SHA}})" >&2
            echo "Pre-purge baseline remains preserved at ${{ARCHIVE_DIR}}/manifest.sha256.pre-purge" >&2
            return 1
        fi
    else
        if [ -e "${{ARCHIVE_DIR}}/manifest.sha256" ]; then
            TRANSACTION_PHASE="FAILED"
            update_journal "FAILED" "Manifest file still present after rollback when originally absent"
            echo "RECOVERY_REQUIRED: Manual intervention required! Manifest file still present after rollback although originally absent" >&2
            return 1
        fi
    fi

    # Rollback verified successfully!
    TRANSACTION_PHASE="ROLLED_BACK"
    update_journal "ROLLED_BACK"
    echo "ROLLBACK_SUCCESS: Historical archive and manifest safely restored to original baseline (${{EXPECTED_ORIG_ARCH_SHA}})" >&2
    echo "Recovery artifacts and candidate files preserved in ${{STAGING_DIR}} for diagnostics" >&2
    return 0
}}

on_signal() {{
    local sig="$1"
    INTERRUPTED=1
    echo "ERROR: Received signal ${{sig}}; transaction interrupted" >&2
    if [ "${{sig}}" = "INT" ]; then
        exit 130
    elif [ "${{sig}}" = "TERM" ]; then
        exit 143
    else
        exit 1
    fi
}}

on_exit() {{
    local exit_code=$?
    trap - EXIT INT TERM

    # If failure/interrupt occurred after any mutation started
    if [ "${{TRANSACTION_PHASE}}" = "ARCHIVE_MUTATED" ] || [ "${{TRANSACTION_PHASE}}" = "MANIFEST_MUTATING" ] || [ "${{TRANSACTION_PHASE}}" = "MANIFEST_MUTATED" ] || [ "${{TRANSACTION_PHASE}}" = "VERIFYING" ]; then
        echo "CRITICAL: Transaction aborted or failed during/after mutation (phase: ${{TRANSACTION_PHASE}}, exit: ${{exit_code}}). Invoking recovery..." >&2
        execute_rollback || true
    fi

    # Clean up temporary replacement files if left over
    if [ -n "${{TMP_ARCHIVE:-}}" ] && [ -f "${{TMP_ARCHIVE}}" ]; then
        rm -f "${{TMP_ARCHIVE}}" 2>/dev/null || true
    fi
    if [ -n "${{TMP_MANIFEST:-}}" ] && [ -f "${{TMP_MANIFEST}}" ]; then
        rm -f "${{TMP_MANIFEST}}" 2>/dev/null || true
    fi

    # If transaction is in FAILED state (RECOVERY_REQUIRED), preserve lock!
    if [ "${{TRANSACTION_PHASE}}" = "FAILED" ]; then
        echo "CRITICAL: Transaction in FAILED state. Lock ${{LOCK_DIR}} and recovery artifacts preserved for manual recovery." >&2
        exit "${{exit_code:-1}}"
    fi

    # For PRE_COMMIT, ROLLED_BACK, or COMMITTED, release lock
    release_lock
    exit "${{exit_code}}"
}}

trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap on_exit EXIT

validate_paths() {{
    if [ -L "${{ARCHIVE_DIR}}" ]; then
        echo "ERROR: Archive directory is a symbolic link: ${{ARCHIVE_DIR}}" >&2
        exit 1
    fi
    if [ ! -d "${{ARCHIVE_DIR}}" ]; then
        echo "ERROR: Archive directory does not exist: ${{ARCHIVE_DIR}}" >&2
        exit 1
    fi
    for item in "uploads.tar.gz" "orders.db" "manifest.sha256" "uploads.tar.gz.pre-purge" "manifest.sha256.pre-purge" ".uploads.tar.gz.tmp_replacement"; do
        if [ -L "${{ARCHIVE_DIR}}/${{item}}" ]; then
            echo "ERROR: Path is a symbolic link: ${{ARCHIVE_DIR}}/${{item}}" >&2
            exit 1
        fi
    done
    if [ -L "${{STAGING_DIR}}" ]; then
        echo "ERROR: Staging directory is a symbolic link: ${{STAGING_DIR}}" >&2
        exit 1
    fi
    if [ -e "${{ARCHIVE_DIR}}/.uploads.tar.gz.tmp_replacement" ]; then
        echo "ERROR: Predictable replacement path ${{ARCHIVE_DIR}}/.uploads.tar.gz.tmp_replacement exists; aborting to prevent unsafe redirection" >&2
        exit 1
    fi
}}

ACTION="${{1:---prepare}}"

case "${{ACTION}}" in
    --prepare)
        acquire_lock
        validate_paths

        echo "=== [Phase 1: Candidate Preparation & Verification] ==="
        if [ ! -f "${{ARCHIVE_DIR}}/uploads.tar.gz" ] || [ ! -f "${{ARCHIVE_DIR}}/orders.db" ]; then
            echo "ERROR: Missing uploads.tar.gz or orders.db in ${{ARCHIVE_DIR}}" >&2
            exit 1
        fi

        # Staging directory preservation: refuse to overwrite existing recovery artifacts
        if [ -e "${{STAGING_DIR}}" ]; then
            echo "ERROR: Staging directory ${{STAGING_DIR}} already exists; aborting to preserve recovery artifacts" >&2
            echo "Inspect recovery evidence before manually archiving or removing ${{STAGING_DIR}}" >&2
            exit 1
        fi

        # Pre-purge baseline overwrite protection
        if [ -e "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" ]; then
            echo "ERROR: uploads.tar.gz.pre-purge already exists; aborting to prevent overwrite" >&2
            exit 1
        fi

        # Calculate original archive & database checksums
        ORIG_ARCH_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        ORIG_DB_SHA=$(sha256sum "${{ARCHIVE_DIR}}/orders.db" | awk '{{print $1}}')

        # Race-safe atomic exclusive creation of uploads.tar.gz.pre-purge baseline
        "${{PYTHON_BIN}}" -c '
import sys, shutil
src = sys.argv[1]
dst = sys.argv[2]
try:
    with open(src, "rb") as f_in, open(dst, "xb") as f_out:
        shutil.copyfileobj(f_in, f_out)
except FileExistsError:
    print("ERROR: Pre-purge baseline already exists; aborting to prevent overwrite", file=sys.stderr)
    sys.exit(1)
' "${{ARCHIVE_DIR}}/uploads.tar.gz" "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge"

        # Verify preserved archive baseline matches recorded original checksum
        PRE_PURGE_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" | awk '{{print $1}}')
        if [ "${{PRE_PURGE_SHA}}" != "${{ORIG_ARCH_SHA}}" ]; then
            echo "ERROR: Preserved baseline SHA-256 (${{PRE_PURGE_SHA}}) does not match original (${{ORIG_ARCH_SHA}})" >&2
            exit 1
        fi

        # Preserve and verify original manifest if present
        HAS_MANIFEST="false"
        ORIG_MAN_SHA=""
        if [ -f "${{ARCHIVE_DIR}}/manifest.sha256" ]; then
            if ! (cd "${{ARCHIVE_DIR}}" && sha256sum -c manifest.sha256 >/dev/null 2>&1); then
                echo "ERROR: Existing manifest.sha256 failed checksum verification; aborting" >&2
                exit 1
            fi
            if [ -e "${{ARCHIVE_DIR}}/manifest.sha256.pre-purge" ]; then
                echo "ERROR: manifest.sha256.pre-purge already exists; aborting to prevent overwrite" >&2
                exit 1
            fi
            "${{PYTHON_BIN}}" -c '
import sys, shutil
src = sys.argv[1]
dst = sys.argv[2]
try:
    with open(src, "rb") as f_in, open(dst, "xb") as f_out:
        shutil.copyfileobj(f_in, f_out)
except FileExistsError:
    print("ERROR: manifest.sha256.pre-purge already exists", file=sys.stderr)
    sys.exit(1)
' "${{ARCHIVE_DIR}}/manifest.sha256" "${{ARCHIVE_DIR}}/manifest.sha256.pre-purge"
            ORIG_MAN_SHA=$(sha256sum "${{ARCHIVE_DIR}}/manifest.sha256" | awk '{{print $1}}')
            PRE_PURGE_MAN_SHA=$(sha256sum "${{ARCHIVE_DIR}}/manifest.sha256.pre-purge" | awk '{{print $1}}')
            if [ "${{PRE_PURGE_MAN_SHA}}" != "${{ORIG_MAN_SHA}}" ]; then
                echo "ERROR: Preserved manifest baseline does not match original; aborting" >&2
                exit 1
            fi
            HAS_MANIFEST="true"
        fi

        # Prepare isolated workspace
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

        # Candidate manifest & checksums
        (cd "${{CANDIDATE_DIR}}" && sha256sum orders.db uploads.tar.gz > manifest.sha256)
        (cd "${{CANDIDATE_DIR}}" && sha256sum -c manifest.sha256 >/dev/null)
        CANDIDATE_ARCH_SHA=$(sha256sum "${{CANDIDATE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        CANDIDATE_DB_SHA=$(sha256sum "${{CANDIDATE_DIR}}/orders.db" | awk '{{print $1}}')
        CANDIDATE_MAN_SHA=$(sha256sum "${{CANDIDATE_DIR}}/manifest.sha256" | awk '{{print $1}}')

        # Run verification CLIs
        echo "Running backup integrity verifier on candidate..."
        "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/verify_backup_integrity.py" --db "${{CANDIDATE_DIR}}/orders.db" --uploads "${{REBUILD_DIR}}/uploads"
        echo "Running privacy risk auditor on candidate..."
        "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/audit_backup_archives.py" --archive-dir "${{CANDIDATE_DIR}}"

        # Guard original archive & database immutability during preparation
        CHECK_ORIG_ARCH_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        if [ "${{CHECK_ORIG_ARCH_SHA}}" != "${{ORIG_ARCH_SHA}}" ]; then
            echo "ERROR: Original archive was modified during preparation; aborting" >&2
            exit 1
        fi
        CHECK_ORIG_DB_SHA=$(sha256sum "${{ARCHIVE_DIR}}/orders.db" | awk '{{print $1}}')
        if [ "${{CHECK_ORIG_DB_SHA}}" != "${{ORIG_DB_SHA}}" ]; then
            echo "ERROR: Original database was modified during preparation; aborting" >&2
            exit 1
        fi
        if [ "${{HAS_MANIFEST}}" = "true" ]; then
            CHECK_ORIG_MAN_SHA=$(sha256sum "${{ARCHIVE_DIR}}/manifest.sha256" | awk '{{print $1}}')
            if [ "${{CHECK_ORIG_MAN_SHA}}" != "${{ORIG_MAN_SHA}}" ]; then
                echo "ERROR: Original manifest was modified during preparation; aborting" >&2
                exit 1
            fi
        fi

        CHECK_PRE_PURGE_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" | awk '{{print $1}}')
        if [ "${{CHECK_PRE_PURGE_SHA}}" != "${{ORIG_ARCH_SHA}}" ]; then
            echo "ERROR: Preserved baseline was modified during preparation; aborting" >&2
            exit 1
        fi

        # Save metadata for confirmation gate
        "${{PYTHON_BIN}}" -c '
import json, sys
data = {{
    "archive_dir": sys.argv[1],
    "orig_sha": sys.argv[2],
    "orig_db_sha": sys.argv[3],
    "has_manifest": sys.argv[4] == "true",
    "orig_manifest_sha": sys.argv[5],
    "candidate_sha": sys.argv[6],
    "candidate_db_sha": sys.argv[7],
    "candidate_manifest_sha": sys.argv[8],
    "prepared": True
}}
with open(sys.argv[9], "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
' "${{ARCHIVE_DIR}}" "${{ORIG_ARCH_SHA}}" "${{ORIG_DB_SHA}}" "${{HAS_MANIFEST}}" "${{ORIG_MAN_SHA}}" "${{CANDIDATE_ARCH_SHA}}" "${{CANDIDATE_DB_SHA}}" "${{CANDIDATE_MAN_SHA}}" "${{META_FILE}}"

        echo ""
        echo "================================================================================"
        echo "CANDIDATE PREPARATION & VERIFICATION COMPLETE (PHASE 1)"
        echo "Target Archive:            ${{ARCHIVE_DIR}}"
        echo "Original Archive SHA-256:  ${{ORIG_ARCH_SHA}}"
        echo "Original DB SHA-256:       ${{ORIG_DB_SHA}}"
        echo "Preserved Baseline:        ${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge"
        echo "Candidate Archive SHA-256: ${{CANDIDATE_ARCH_SHA}}"
        echo "Integrity Verification:    PASS"
        echo "Privacy Audit Verdict:     ZERO (residual privacy risk eliminated)"
        echo "Historical Archive State:  UNCHANGED / IMMUTABLE"
        echo ""
        echo "[OPERATOR ACTION REQUIRED FOR REPLACEMENT]"
        echo "Phase 1 has NOT modified or deleted the historical archive."
        echo "To confirm replacement, run Phase 2 with explicit confirmation:"
        echo "  $0 --confirm ${{CANDIDATE_ARCH_SHA}}"
        echo "================================================================================"
        ;;

    --confirm|--replace-authorized)
        CONFIRM_SHA="${{2:-}}"
        if [ -z "${{CONFIRM_SHA}}" ]; then
            echo "ERROR: Owner authorization missing. To confirm replacement, pass candidate SHA-256: --confirm <SHA256>" >&2
            exit 2
        fi

        if ! echo "${{CONFIRM_SHA}}" | grep -Eq '^[0-9a-fA-F]{{64}}$'; then
            echo "ERROR: Confirmation value is malformed or invalid (must be 64-character hex SHA-256): '''${{CONFIRM_SHA}}'''" >&2
            exit 2
        fi

        acquire_lock
        validate_paths
        TRANSACTION_PHASE="PRE_COMMIT"

        echo "=== [Phase 2: Operator-Confirmed Replacement] ==="

        if [ ! -f "${{META_FILE}}" ] || [ ! -f "${{CANDIDATE_DIR}}/uploads.tar.gz" ] || [ ! -f "${{CANDIDATE_DIR}}/orders.db" ] || [ ! -f "${{CANDIDATE_DIR}}/manifest.sha256" ]; then
            echo "ERROR: Candidate workspace or metadata missing; run preparation phase first" >&2
            exit 1
        fi

        # Read metadata
        META_DATA=$("${{PYTHON_BIN}}" -c '
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
had_m = "true" if m.get("has_manifest") else "false"
fields = [m.get("orig_sha", ""), m.get("orig_db_sha", ""), had_m, m.get("orig_manifest_sha", ""), m.get("candidate_sha", ""), m.get("candidate_db_sha", ""), m.get("candidate_manifest_sha", "")]
print("|".join(str(x) for x in fields))
' "${{META_FILE}}")

        EXPECTED_ORIG_ARCH_SHA=$(echo "${{META_DATA}}" | cut -d'|' -f1)
        EXPECTED_ORIG_DB_SHA=$(echo "${{META_DATA}}" | cut -d'|' -f2)
        HAD_MANIFEST=$(echo "${{META_DATA}}" | cut -d'|' -f3)
        EXPECTED_ORIG_MAN_SHA=$(echo "${{META_DATA}}" | cut -d'|' -f4)
        EXPECTED_CAND_ARCH_SHA=$(echo "${{META_DATA}}" | cut -d'|' -f5)
        EXPECTED_CAND_DB_SHA=$(echo "${{META_DATA}}" | cut -d'|' -f6)
        EXPECTED_CAND_MAN_SHA=$(echo "${{META_DATA}}" | cut -d'|' -f7)

        # Check candidate SHA-256 against confirmation argument
        ACTUAL_CAND_SHA=$(sha256sum "${{CANDIDATE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        if [ "${{CONFIRM_SHA}}" != "${{ACTUAL_CAND_SHA}}" ]; then
            echo "ERROR: Confirmation / Authorization SHA-256 mismatch (provided: ${{CONFIRM_SHA}}, candidate: ${{ACTUAL_CAND_SHA}}); aborting replacement" >&2
            exit 1
        fi

        # Verify candidate database and manifest have not changed after preparation
        if [ -n "${{EXPECTED_CAND_DB_SHA}}" ]; then
            ACTUAL_CAND_DB_SHA=$(sha256sum "${{CANDIDATE_DIR}}/orders.db" | awk '{{print $1}}')
            if [ "${{ACTUAL_CAND_DB_SHA}}" != "${{EXPECTED_CAND_DB_SHA}}" ]; then
                echo "ERROR: Candidate database was modified after preparation; aborting replacement" >&2
                exit 1
            fi
        fi

        if [ -n "${{EXPECTED_CAND_MAN_SHA}}" ]; then
            ACTUAL_CAND_MAN_SHA=$(sha256sum "${{CANDIDATE_DIR}}/manifest.sha256" | awk '{{print $1}}')
            if [ "${{ACTUAL_CAND_MAN_SHA}}" != "${{EXPECTED_CAND_MAN_SHA}}" ]; then
                echo "ERROR: Candidate manifest was modified after preparation; aborting replacement" >&2
                exit 1
            fi
        fi

        # Verify candidate manifest
        if ! (cd "${{CANDIDATE_DIR}}" && sha256sum -c manifest.sha256 >/dev/null 2>&1); then
            echo "ERROR: Candidate manifest verification failed; aborting replacement" >&2
            exit 1
        fi

        # Verify active archive, database, manifest, and baselines have not changed before mutation
        CURRENT_ORIG_ARCH_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        if [ "${{CURRENT_ORIG_ARCH_SHA}}" != "${{EXPECTED_ORIG_ARCH_SHA}}" ]; then
            echo "ERROR: Original archive was modified after preparation; aborting replacement" >&2
            exit 1
        fi

        if [ -n "${{EXPECTED_ORIG_DB_SHA}}" ]; then
            CURRENT_ORIG_DB_SHA=$(sha256sum "${{ARCHIVE_DIR}}/orders.db" | awk '{{print $1}}')
            if [ "${{CURRENT_ORIG_DB_SHA}}" != "${{EXPECTED_ORIG_DB_SHA}}" ]; then
                echo "ERROR: Original database was modified after preparation; aborting replacement" >&2
                exit 1
            fi
        fi

        if [ ! -f "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" ]; then
            echo "ERROR: Preserved baseline uploads.tar.gz.pre-purge is missing; aborting replacement" >&2
            exit 1
        fi

        CURRENT_PRE_PURGE_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge" | awk '{{print $1}}')
        if [ "${{CURRENT_PRE_PURGE_SHA}}" != "${{EXPECTED_ORIG_ARCH_SHA}}" ]; then
            echo "ERROR: Preserved baseline uploads.tar.gz.pre-purge was modified; aborting replacement" >&2
            exit 1
        fi

        if [ "${{HAD_MANIFEST}}" = "true" ]; then
            if [ ! -f "${{ARCHIVE_DIR}}/manifest.sha256" ]; then
                echo "ERROR: Active manifest.sha256 was removed after preparation; aborting replacement" >&2
                exit 1
            fi
            CURRENT_ORIG_MAN_SHA=$(sha256sum "${{ARCHIVE_DIR}}/manifest.sha256" | awk '{{print $1}}')
            if [ "${{CURRENT_ORIG_MAN_SHA}}" != "${{EXPECTED_ORIG_MAN_SHA}}" ]; then
                echo "ERROR: Active manifest.sha256 was modified after preparation; aborting replacement" >&2
                exit 1
            fi
            if [ ! -f "${{ARCHIVE_DIR}}/manifest.sha256.pre-purge" ]; then
                echo "ERROR: Preserved baseline manifest.sha256.pre-purge is missing; aborting replacement" >&2
                exit 1
            fi
            CURRENT_PRE_PURGE_MAN_SHA=$(sha256sum "${{ARCHIVE_DIR}}/manifest.sha256.pre-purge" | awk '{{print $1}}')
            if [ "${{CURRENT_PRE_PURGE_MAN_SHA}}" != "${{EXPECTED_ORIG_MAN_SHA}}" ]; then
                echo "ERROR: Preserved baseline manifest.sha256.pre-purge was modified; aborting replacement" >&2
                exit 1
            fi
        else
            if [ -e "${{ARCHIVE_DIR}}/manifest.sha256" ]; then
                echo "ERROR: Active manifest.sha256 unexpectedly created after preparation; aborting replacement" >&2
                exit 1
            fi
            if [ -e "${{ARCHIVE_DIR}}/manifest.sha256.pre-purge" ]; then
                echo "ERROR: manifest.sha256.pre-purge unexpectedly created; aborting replacement" >&2
                exit 1
            fi
        fi

        echo "Re-verifying candidate integrity and privacy audit prior to replacement..."
        "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/verify_backup_integrity.py" --db "${{CANDIDATE_DIR}}/orders.db" --uploads "${{REBUILD_DIR}}/uploads"
        "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/audit_backup_archives.py" --archive-dir "${{CANDIDATE_DIR}}"

        # Initialize transaction journal
        init_journal

        # Prepare temporary replacement files securely via Python (write through open fd, restrictive mode 0o600, fsync)
        TMP_FILES=$("${{PYTHON_BIN}}" -c '
import os, sys, tempfile, shutil, pathlib

arch_dir = sys.argv[1]
cand_dir = sys.argv[2]

with tempfile.NamedTemporaryFile(dir=arch_dir, prefix=".uploads.tmp_", delete=False) as tf_arch:
    tf_arch_path = tf_arch.name
    try:
        os.chmod(tf_arch.fileno(), 0o600)
    except Exception:
        pass
    with open(os.path.join(cand_dir, "uploads.tar.gz"), "rb") as src:
        shutil.copyfileobj(src, tf_arch)
    tf_arch.flush()
    os.fsync(tf_arch.fileno())

if os.path.islink(tf_arch_path):
    os.unlink(tf_arch_path)
    print("ERROR: Temp archive file is a symlink", file=sys.stderr)
    sys.exit(1)

with tempfile.NamedTemporaryFile(dir=arch_dir, prefix=".manifest.tmp_", delete=False) as tf_man:
    tf_man_path = tf_man.name
    try:
        os.chmod(tf_man.fileno(), 0o600)
    except Exception:
        pass
    with open(os.path.join(cand_dir, "manifest.sha256"), "rb") as src:
        shutil.copyfileobj(src, tf_man)
    tf_man.flush()
    os.fsync(tf_man.fileno())

if os.path.islink(tf_man_path):
    os.unlink(tf_man_path)
    print("ERROR: Temp manifest file is a symlink", file=sys.stderr)
    sys.exit(1)

print(pathlib.Path(tf_arch_path).as_posix() + "|" + pathlib.Path(tf_man_path).as_posix())
' "${{ARCHIVE_DIR}}" "${{CANDIDATE_DIR}}")

        TMP_ARCHIVE=$(echo "${{TMP_FILES}}" | cut -d'|' -f1)
        TMP_MANIFEST=$(echo "${{TMP_FILES}}" | cut -d'|' -f2)

        # Validate temp archive & manifest checksums against expected candidate checksums
        TMP_ARCH_SHA=$(sha256sum "${{TMP_ARCHIVE}}" | awk '{{print $1}}')
        if [ "${{TMP_ARCH_SHA}}" != "${{ACTUAL_CAND_SHA}}" ]; then
            echo "ERROR: Temporary replacement archive checksum mismatch; aborting" >&2
            rm -f "${{TMP_ARCHIVE}}" "${{TMP_MANIFEST}}"
            exit 1
        fi
        TMP_MAN_SHA=$(sha256sum "${{TMP_MANIFEST}}" | awk '{{print $1}}')
        if [ "${{TMP_MAN_SHA}}" != "${{EXPECTED_CAND_MAN_SHA}}" ]; then
            echo "ERROR: Temporary replacement manifest checksum mismatch; aborting" >&2
            rm -f "${{TMP_ARCHIVE}}" "${{TMP_MANIFEST}}"
            exit 1
        fi

        # Optional deterministic sync point for signal testing before commit point
        if [ -n "${{RUNBOOK_TEST_SYNC_FILE:-}}" ]; then
            touch "${{RUNBOOK_TEST_SYNC_FILE}}"
            while [ -f "${{RUNBOOK_TEST_SYNC_FILE}}" ]; do
                sleep 0.05
            done
        fi

        # Immediate pre-commit revalidation: assert active files have not changed immediately before replacement
        FINAL_PRECOMMIT_ARCH_SHA=$(sha256sum "${{ARCHIVE_DIR}}/uploads.tar.gz" | awk '{{print $1}}')
        if [ "${{FINAL_PRECOMMIT_ARCH_SHA}}" != "${{EXPECTED_ORIG_ARCH_SHA}}" ]; then
            echo "ERROR: Original archive was modified immediately prior to replacement commit; aborting" >&2
            rm -f "${{TMP_ARCHIVE}}" "${{TMP_MANIFEST}}"
            exit 1
        fi
        FINAL_PRECOMMIT_DB_SHA=$(sha256sum "${{ARCHIVE_DIR}}/orders.db" | awk '{{print $1}}')
        if [ "${{FINAL_PRECOMMIT_DB_SHA}}" != "${{EXPECTED_ORIG_DB_SHA}}" ]; then
            echo "ERROR: Original database was modified immediately prior to replacement commit; aborting" >&2
            rm -f "${{TMP_ARCHIVE}}" "${{TMP_MANIFEST}}"
            exit 1
        fi
        if [ "${{HAD_MANIFEST}}" = "true" ]; then
            FINAL_PRECOMMIT_MAN_SHA=$(sha256sum "${{ARCHIVE_DIR}}/manifest.sha256" | awk '{{print $1}}')
            if [ "${{FINAL_PRECOMMIT_MAN_SHA}}" != "${{EXPECTED_ORIG_MAN_SHA}}" ]; then
                echo "ERROR: Original manifest was modified immediately prior to replacement commit; aborting" >&2
                rm -f "${{TMP_ARCHIVE}}" "${{TMP_MANIFEST}}"
                exit 1
            fi
        fi

        # Commit point: Mutate active archive and manifest with recoverable transaction journal
        TRANSACTION_PHASE="ARCHIVE_MUTATED"
        update_journal "ARCHIVE_MUTATED"
        $MV_BIN "${{TMP_ARCHIVE}}" "${{ARCHIVE_DIR}}/uploads.tar.gz"

        TRANSACTION_PHASE="MANIFEST_MUTATING"
        if [ "${{RUNBOOK_TEST_FAIL_MANIFEST_RENAME:-0}}" = "1" ] || ! $MV_BIN "${{TMP_MANIFEST}}" "${{ARCHIVE_DIR}}/manifest.sha256"; then
            echo "ERROR: Manifest replacement rename failed after archive replacement" >&2
            execute_rollback
            exit 1
        fi
        TRANSACTION_PHASE="MANIFEST_MUTATED"
        update_journal "MANIFEST_MUTATED"

        # Post-replacement verification with rollback guard
        POST_CHECK_OK=true
        TRANSACTION_PHASE="VERIFYING"
        update_journal "VERIFYING"

        if [ "${{RUNBOOK_TEST_FAIL_POST_VERIFY:-0}}" = "1" ]; then
            echo "SIMULATED: Test injected post-replacement verification failure" >&2
            POST_CHECK_OK=false
        fi

        if [ "${{POST_CHECK_OK}}" = "true" ]; then
            echo "Verifying active manifest checksum..."
            if ! (cd "${{ARCHIVE_DIR}}" && sha256sum -c manifest.sha256 >/dev/null 2>&1); then
                echo "ERROR: Post-replacement manifest verification failed" >&2
                POST_CHECK_OK=false
            fi
        fi

        if [ "${{POST_CHECK_OK}}" = "true" ]; then
            echo "Running post-replacement privacy audit on active archive..."
            if ! "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/audit_backup_archives.py" --archive-dir "${{ARCHIVE_DIR}}"; then
                echo "ERROR: Post-replacement privacy audit failed" >&2
                POST_CHECK_OK=false
            fi
        fi

        if [ "${{POST_CHECK_OK}}" = "true" ]; then
            echo "Running post-replacement integrity verification on active archive..."
            POST_EXTRACT_DIR="${{STAGING_DIR}}/post_verify"
            mkdir -p "${{POST_EXTRACT_DIR}}"
            tar --force-local -xzf "${{ARCHIVE_DIR}}/uploads.tar.gz" -C "${{POST_EXTRACT_DIR}}"
            if ! "${{PYTHON_BIN}}" "${{REPO_ROOT}}/scripts/verify_backup_integrity.py" --db "${{ARCHIVE_DIR}}/orders.db" --uploads "${{POST_EXTRACT_DIR}}/uploads"; then
                echo "ERROR: Post-replacement integrity verification failed" >&2
                POST_CHECK_OK=false
            fi
        fi

        if [ "${{POST_CHECK_OK}}" != "true" ]; then
            execute_rollback
            exit 1
        fi

        # Complete Success!
        TRANSACTION_PHASE="COMMITTED"
        update_journal "COMMITTED"
        rm -rf "${{STAGING_DIR}}" "${{JOURNAL_FILE}}"

        echo ""
        echo "================================================================================"
        echo "OWNER-AUTHORIZED REPLACEMENT COMPLETED SUCCESSFULLY (TRANSACTION-SAFE)"
        echo "Archive:                   ${{ARCHIVE_DIR}}"
        echo "Preserved Baseline:        ${{ARCHIVE_DIR}}/uploads.tar.gz.pre-purge"
        echo "New Archive SHA-256:       ${{ACTUAL_CAND_SHA}}"
        echo "Post-Replacement Audit:    PASS (ZERO residual privacy risk)"
        echo "Post-Replacement Check:    PASS (Manifest & Integrity Verified)"
        echo "================================================================================"
        ;;

    *)
        echo "Usage: $0 [--prepare | --confirm <CANDIDATE_SHA> | --replace-authorized <CANDIDATE_SHA>]" >&2
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
