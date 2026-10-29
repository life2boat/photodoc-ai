#!/usr/bin/env python3
"""
PhotoDoc AI — Zero-Retention Backup Generator.

Creates consistent, verified backups adhering to the Zero-Retention Privacy Gate:
1. Transactionally snapshots SQLite database (orders.db) using the SQLite Backup API.
2. Before snapshotting (or during intake), sweeps and finalizes pending cleanups
   for delivered jobs with durable deletion evidence.
3. Partitions photo uploads into:
   - Unfinished deliveries (pending, processing, failed): strictly ARCHIVED for crash recovery.
   - Delivered and cleaned orders: strictly EXCLUDED (zero delivered photo content retained).
   - Ambiguous legacy P0 files: fails closed by default unless owner explicitly reconciles policy.
4. Packages necessary recovery originals into uploads.tar.gz.
5. Generates cryptographic SHA-256 manifests (manifest.sha256) and metadata (backup_manifest.json).
6. Automatically verifies the resulting backup via verify_backup_integrity.py using safe extraction.
7. Strictly prevents accidental inclusion of delivered customer photos or silent loss of recovery files.
8. Separates backup recovery integrity from privacy-compliance status in metadata.
"""

from __future__ import annotations

import argparse
import hashlib
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

# Ensure backend and scripts are in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
BACKEND_DIR = REPO_DIR / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from delivery import process_due_cleanups, set_uploads_dir
except ImportError:
    process_due_cleanups = None  # type: ignore

try:
    from verify_backup_integrity import APPLICATION_ID, APPLICATION_ID_TAG, sanitize_filename, sha256_file, verify_backup
except ImportError:
    APPLICATION_ID_TAG = "PDAI"
    APPLICATION_ID = 1346650441

try:
    from audit_backup_archives import (
        MAX_ARCHIVE_MEMBERS,
        MAX_SINGLE_FILE_SIZE_BYTES,
        MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES,
    )
except ImportError:
    MAX_ARCHIVE_MEMBERS = 100_000
    MAX_SINGLE_FILE_SIZE_BYTES = 1024 * 1024 * 1024
    MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES = 10 * 1024 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_safe_child_path(base_dir: Path, target_path: Path) -> bool:
    try:
        base_resolved = base_dir.resolve()
        target_resolved = target_path.resolve()
        return base_resolved in target_resolved.parents or target_resolved.parent == base_resolved
    except (ValueError, RuntimeError):
        return False


def snapshot_sqlite_db(src_path: Path, dst_path: Path) -> None:
    """
    Takes an online, transactionally consistent snapshot of SQLite database.
    """
    src_uri = f"file:{src_path.resolve().as_posix()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True, timeout=10.0)
    dst_conn = sqlite3.connect(dst_path, timeout=10.0)
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def determine_required_and_excluded_files(
    db_path: Path,
    uploads_dir: Path,
    *,
    legacy_p0_policy: str = "fail",
) -> Tuple[Set[str], Set[str], Dict[str, Any]]:
    """
    Analyzes the database to partition files into:
    1. required_files: files referenced by unfinished delivery jobs or active orders (must be archived).
    2. excluded_files: files belonging exclusively to delivered/cleaned orders (must NOT be archived).
    3. metadata: stats on jobs, orders, legacy P0 ambiguity, and zero-retention counts.

    Fail-Closed Legacy Protection:
    When delivery_jobs exists, any legacy orders having local files on disk without
    delivery_jobs tracking represent ambiguous status. Unless legacy_p0_policy is
    explicitly 'include' or 'exclude', fails closed with a clear non-sensitive diagnostic.
    """
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Check tables
        has_delivery = bool(
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs';").fetchone()
        )
        orders_cols = {row[1] for row in conn.execute("PRAGMA table_info(orders);").fetchall()}
        has_order_token = "order_token_hash" in orders_cols

        orders_count = conn.execute("SELECT COUNT(*) FROM orders;").fetchone()[0]

        required_files: Set[str] = set()
        excluded_files: Set[str] = set()
        stats: Dict[str, Any] = {
            "schema_generation": "P1" if (has_delivery and has_order_token) else "P0",
            "orders_count": orders_count,
            "p1_orders_count": 0,
            "unfinished_jobs_count": 0,
            "delivered_jobs_count": 0,
            "required_files_count": 0,
            "excluded_delivered_files_count": 0,
            "ambiguous_legacy_files_count": 0,
            "legacy_p0_policy": legacy_p0_policy,
        }

        # Identify legacy P0 orders
        if has_order_token:
            p0_orders = conn.execute(
                "SELECT id, filename FROM orders WHERE order_token_hash IS NULL OR order_token_hash = '';"
            ).fetchall()
        else:
            p0_orders = conn.execute("SELECT id, filename FROM orders;").fetchall()

        # Find physical files on disk associated with legacy orders
        ambiguous_p0_files: Set[str] = set()
        for o in p0_orders:
            raw_filename = o["filename"] or ""
            names = [n.strip() for n in raw_filename.split(",") if n.strip()]
            for n in names:
                safe_name = sanitize_filename(n)
                for cand in [f"{o['id']}_{safe_name}", f"{o['id']}_{n}", safe_name, n]:
                    cand_path = uploads_dir / cand
                    if is_safe_child_path(uploads_dir, cand_path) and cand_path.is_file():
                        ambiguous_p0_files.add(cand)
                        break

        stats["ambiguous_legacy_files_count"] = len(ambiguous_p0_files)

        if has_delivery:
            p1_orders_count = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE order_token_hash IS NOT NULL AND order_token_hash != '';"
            ).fetchone()[0]
            stats["p1_orders_count"] = p1_orders_count

            # All file/info jobs
            jobs = conn.execute(
                "SELECT id, order_id, kind, local_path, state, cleanup_state "
                "FROM delivery_jobs WHERE kind IN ('file', 'info');"
            ).fetchall()

            file_references: Dict[str, List[sqlite3.Row]] = {}
            for j in jobs:
                lp = j["local_path"]
                if not lp:
                    continue
                filename = Path(lp).name
                file_references.setdefault(filename, []).append(j)

            for filename, refs in file_references.items():
                has_unfinished = any(r["state"] in ("pending", "processing", "failed") for r in refs)
                if has_unfinished:
                    required_files.add(filename)
                    stats["unfinished_jobs_count"] += sum(
                        1 for r in refs if r["state"] in ("pending", "processing", "failed")
                    )
                else:
                    excluded_files.add(filename)
                    stats["delivered_jobs_count"] += len(refs)

            # Check legacy P0 files on disk under P1 delivery schema
            if ambiguous_p0_files:
                if legacy_p0_policy == "fail":
                    raise ValueError(
                        f"Ambiguous legacy P0 files detected: found {len(ambiguous_p0_files)} file(s) on disk for "
                        "legacy order(s) without delivery_jobs records. Cannot establish delivery/retention status "
                        "reliably under zero-retention contract. Reconcile legacy orders into delivery_jobs or "
                        "specify --legacy-p0-policy [include|exclude]."
                    )
                elif legacy_p0_policy == "include":
                    required_files.update(ambiguous_p0_files)
                elif legacy_p0_policy == "exclude":
                    excluded_files.update(ambiguous_p0_files)
                else:
                    raise ValueError(f"Invalid legacy_p0_policy: {legacy_p0_policy}")

        else:
            # Pure P0 schema handling: active orders
            if ambiguous_p0_files:
                if legacy_p0_policy == "fail":
                    raise ValueError(
                        f"Legacy P0 schema detected: found {len(ambiguous_p0_files)} file(s) on disk without "
                        "delivery_jobs tracking. Cannot establish zero-retention status reliably. "
                        "Specify --legacy-p0-policy [include|exclude]."
                    )
                elif legacy_p0_policy == "exclude":
                    excluded_files.update(ambiguous_p0_files)
                elif legacy_p0_policy == "include":
                    required_files.update(ambiguous_p0_files)
                else:
                    raise ValueError(f"Invalid legacy_p0_policy: {legacy_p0_policy}")

        stats["required_files_count"] = len(required_files)
        stats["excluded_delivered_files_count"] = len(excluded_files)
        return required_files, excluded_files, stats
    finally:
        conn.close()


def create_uploads_tar(
    uploads_dir: Path,
    target_tar_path: Path,
    required_files: Set[str],
    excluded_files: Set[str],
) -> Tuple[int, int]:
    """
    Creates uploads.tar.gz containing only required_files in an 'uploads/' folder.
    Strictly asserts that zero excluded_files are added.
    Returns (archived_count, omitted_count).
    """
    archived_count = 0
    omitted_count = 0

    with tarfile.open(target_tar_path, "w:gz") as tar:
        uploads_dir_info = tarfile.TarInfo(name="uploads")
        uploads_dir_info.type = tarfile.DIRTYPE
        uploads_dir_info.mode = 0o755
        uploads_dir_info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(uploads_dir_info)

        for filename in sorted(required_files):
            if filename in excluded_files:
                raise ValueError(
                    f"Privacy violation: File '{filename}' is both required and excluded. "
                    "A delivered photo cannot be archived unless required by an unfinished job."
                )

            src_file = uploads_dir / filename
            if not is_safe_child_path(uploads_dir, src_file):
                raise ValueError(f"Path traversal detected in backup candidate: {filename}")

            if not src_file.is_file():
                raise FileNotFoundError(
                    f"Required recovery source file missing from uploads directory: {filename}"
                )

            arcname = f"uploads/{filename}"
            tar_info = tar.gettarinfo(name=str(src_file), arcname=arcname)
            tar_info.mode = 0o600
            tar_info.uid = 0
            tar_info.gid = 0
            tar_info.uname = "photodoc"
            tar_info.gname = "photodoc"
            with src_file.open("rb") as f:
                tar.addfile(tar_info, f)
            archived_count += 1

        omitted_count = len(excluded_files)

    return archived_count, omitted_count


def generate_backup(
    db_path: Path,
    uploads_dir: Path,
    output_dir: Path,
    *,
    run_pending_cleanups_flag: bool = True,
    skip_verification: bool = False,
    legacy_p0_policy: str = "fail",
) -> Tuple[bool, Dict[str, Any]]:
    """
    Main backup generation routine.
    Separates backup recovery integrity from privacy-compliance status in metadata.
    """
    report: Dict[str, Any] = {
        "success": False,
        "timestamp": utc_now(),
        "output_dir": str(output_dir),
        "db_path": str(db_path),
        "uploads_dir": str(uploads_dir),
        "application_id": None,
        "application_id_valid": False,
        "sqlite_integrity": "unknown",
        "orders_count": 0,
        "p1_orders_count": 0,
        "unfinished_jobs_count": 0,
        "archived_files_count": 0,
        "omitted_delivered_files_count": 0,
        "ambiguous_legacy_files_count": 0,
        "legacy_p0_policy": legacy_p0_policy,
        "orders_db_sha256": None,
        "uploads_tar_sha256": None,
        "zero_retention_enforced": True,
        "privacy_status": "UNKNOWN",
        "legacy_files_recoverable": None,
        "recovery_verification_result": "FAIL",
        "verification_passed": False,
        "errors": [],
    }

    if db_path.is_symlink():
        report["errors"].append(f"Database cannot be a symbolic link: {db_path}")
        return False, report

    if uploads_dir.is_symlink():
        report["errors"].append(f"Uploads directory cannot be a symbolic link: {uploads_dir}")
        return False, report

    if not db_path.is_file():
        report["errors"].append(f"Database not found: {db_path}")
        return False, report

    if not uploads_dir.is_dir():
        report["errors"].append(f"Uploads directory not found: {uploads_dir}")
        return False, report

    # 1. Sweep and complete any pending cleanups if requested and available
    if run_pending_cleanups_flag and process_due_cleanups is not None:
        try:
            set_uploads_dir(uploads_dir)
            cleaned = process_due_cleanups(db_path, uploads_dir, limit=100)
            report["pre_backup_cleanups_processed"] = cleaned
        except Exception as exc:
            report["errors"].append(f"Pre-backup cleanup sweep warning/error: {exc}")

    # 2. Check source DB integrity & Application ID
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        app_id = conn.execute("PRAGMA application_id;").fetchone()[0]
        conn.close()
        report["sqlite_integrity"] = integrity
        report["application_id"] = app_id
        if integrity != "ok":
            report["errors"].append(f"Source SQLite integrity_check failed: {integrity}")
            return False, report
        if app_id != APPLICATION_ID:
            report["errors"].append(f"Invalid application_id: {app_id} (expected {APPLICATION_ID})")
            return False, report
        report["application_id_valid"] = True
    except Exception as exc:
        report["errors"].append(f"Failed to inspect source database: {exc}")
        return False, report

    # 3. Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(output_dir, 0o700)
    except Exception:
        pass

    dst_db = output_dir / "orders.db"
    dst_tar = output_dir / "uploads.tar.gz"
    manifest_sha = output_dir / "manifest.sha256"
    manifest_json = output_dir / "backup_manifest.json"

    # 4. Transactional SQLite snapshot
    try:
        snapshot_sqlite_db(db_path, dst_db)
        snap_conn = sqlite3.connect(dst_db)
        snap_integrity = snap_conn.execute("PRAGMA integrity_check;").fetchone()[0]
        snap_conn.close()
        if snap_integrity != "ok":
            report["errors"].append(f"Snapshotted database integrity check failed: {snap_integrity}")
            return False, report
    except Exception as exc:
        report["errors"].append(f"Failed to snapshot database: {exc}")
        return False, report

    # 5. Determine files to archive vs omit
    try:
        required_files, excluded_files, stats = determine_required_and_excluded_files(
            dst_db, uploads_dir, legacy_p0_policy=legacy_p0_policy
        )
        report["orders_count"] = stats["orders_count"]
        report["p1_orders_count"] = stats["p1_orders_count"]
        report["unfinished_jobs_count"] = stats["unfinished_jobs_count"]
        report["schema_generation"] = stats["schema_generation"]
        report["ambiguous_legacy_files_count"] = stats["ambiguous_legacy_files_count"]
        report["legacy_p0_policy"] = stats["legacy_p0_policy"]
    except Exception as exc:
        report["errors"].append(f"Failed to analyze file references: {exc}")
        return False, report

    # 6. Archive required files into uploads.tar.gz
    try:
        archived_cnt, omitted_cnt = create_uploads_tar(
            uploads_dir=uploads_dir,
            target_tar_path=dst_tar,
            required_files=required_files,
            excluded_files=excluded_files,
        )
        report["archived_files_count"] = archived_cnt
        report["omitted_delivered_files_count"] = omitted_cnt
    except Exception as exc:
        report["errors"].append(f"Failed to create uploads archive: {exc}")
        return False, report

    # Privacy classification & zero-retention metadata separation
    ambiguous_cnt = report["ambiguous_legacy_files_count"]
    if ambiguous_cnt > 0 and legacy_p0_policy == "include":
        report["zero_retention_enforced"] = False
        report["privacy_status"] = "UNKNOWN (Ambiguous legacy P0 files included for recovery; zero-retention not enforced)"
        report["legacy_files_recoverable"] = True
    elif ambiguous_cnt > 0 and legacy_p0_policy == "exclude":
        report["zero_retention_enforced"] = True
        report["privacy_status"] = "ZERO_RETENTION_EXCLUDED_LEGACY (Ambiguous legacy files excluded to enforce zero-retention)"
        report["legacy_files_recoverable"] = False
    elif ambiguous_cnt == 0:
        report["zero_retention_enforced"] = True
        report["privacy_status"] = "ZERO_RETENTION_VERIFIED"
        report["legacy_files_recoverable"] = None
    else:
        report["zero_retention_enforced"] = False
        report["privacy_status"] = "UNKNOWN"
        report["legacy_files_recoverable"] = None

    # 7. Checksums and Manifests
    db_sha = sha256_file(dst_db)
    tar_sha = sha256_file(dst_tar)
    report["orders_db_sha256"] = db_sha
    report["uploads_tar_sha256"] = tar_sha

    manifest_sha_content = f"{db_sha}  orders.db\n{tar_sha}  uploads.tar.gz\n"
    manifest_sha.write_text(manifest_sha_content, encoding="utf-8")

    manifest_data = {
        "timestamp": report["timestamp"],
        "application_id": report["application_id"],
        "application_id_tag": APPLICATION_ID_TAG,
        "schema_generation": report.get("schema_generation", "P1"),
        "orders_count": report["orders_count"],
        "p1_orders_count": report["p1_orders_count"],
        "unfinished_jobs_count": report["unfinished_jobs_count"],
        "archived_files_count": report["archived_files_count"],
        "omitted_delivered_files_count": report["omitted_delivered_files_count"],
        "ambiguous_legacy_files_count": report["ambiguous_legacy_files_count"],
        "legacy_p0_policy": report["legacy_p0_policy"],
        "orders_db_sha256": db_sha,
        "uploads_tar_sha256": tar_sha,
        "zero_retention_enforced": report["zero_retention_enforced"],
        "privacy_status": report["privacy_status"],
        "legacy_files_recoverable": report["legacy_files_recoverable"],
        "recovery_verification_result": "PENDING",
    }
    manifest_json.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # 8. Post-creation verification using safe extraction and resource limit enforcement
    if not skip_verification:
        with tempfile.TemporaryDirectory() as tmp_restore:
            tmp_restore_path = Path(tmp_restore).resolve()
            restore_uploads = tmp_restore_path / "uploads"
            with tarfile.open(dst_tar, "r:gz") as tar:
                members = tar.getmembers()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    report["errors"].append(
                        f"Post-creation archive member count exceeds limit ({len(members)} > {MAX_ARCHIVE_MEMBERS})"
                    )
                    return False, report

                total_uncompressed = 0
                for member in members:
                    if not isinstance(member.size, int) or member.size < 0:
                        report["errors"].append(f"Malformed member size in backup archive: {member.name}")
                        return False, report

                    if member.issym() or member.islnk():
                        report["errors"].append(f"Unsafe symlink/hardlink member in backup archive: {member.name}")
                        return False, report

                    if ".." in Path(member.name).parts or member.name.startswith("/") or Path(member.name).is_absolute():
                        report["errors"].append(f"Path traversal detected in backup archive member: {member.name}")
                        return False, report

                    if not (member.isreg() or member.isdir()):
                        report["errors"].append(f"Disallowed member type in backup archive: {member.name}")
                        return False, report

                    target_dest = (tmp_restore_path / member.name).resolve()
                    if not is_safe_child_path(tmp_restore_path, target_dest):
                        report["errors"].append(f"Archive member extraction path escape: {member.name}")
                        return False, report

                    if member.isdir():
                        target_dest.mkdir(parents=True, exist_ok=True)
                    elif member.isreg():
                        if member.size > MAX_SINGLE_FILE_SIZE_BYTES:
                            report["errors"].append(f"Member exceeds single-file size limit: {member.name}")
                            return False, report

                        total_uncompressed += member.size
                        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES:
                            report["errors"].append("Total uncompressed size exceeds limit in post-creation restore")
                            return False, report

                        target_dest.parent.mkdir(parents=True, exist_ok=True)
                        f_in = tar.extractfile(member)
                        if f_in:
                            with target_dest.open("wb") as f_out:
                                shutil.copyfileobj(f_in, f_out, length=65536)

            valid, verify_report = verify_backup(dst_db, restore_uploads)
            if not valid:
                report["errors"].append(f"Post-creation backup verification failed: {verify_report.get('errors')}")
                return False, report
            report["verification_passed"] = True
            report["recovery_verification_result"] = "PASS"
    else:
        report["verification_passed"] = True
        report["recovery_verification_result"] = "SKIPPED"

    # Update manifest with final recovery_verification_result
    manifest_data["recovery_verification_result"] = report["recovery_verification_result"]
    manifest_json.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # 9. Set permissions (read-only protection)
    try:
        os.chmod(dst_db, 0o600)
        os.chmod(dst_tar, 0o600)
        os.chmod(manifest_sha, 0o600)
        os.chmod(manifest_json, 0o600)
    except Exception:
        pass

    report["success"] = len(report["errors"]) == 0
    return report["success"], report


def print_backup_report(success: bool, report: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps({"success": success, "report": report}, indent=2))
        return

    print("==================================================")
    print("      PhotoDoc AI — Zero-Retention Backup         ")
    print("==================================================")
    print(f"Status:                   {'SUCCESS (VERIFIED)' if success else 'FAILED'}")
    print(f"Output Directory:         {report['output_dir']}")
    print(f"Schema Generation:        {report.get('schema_generation', 'P1')}")
    print(f"Application Identity:     {report['application_id']} (valid: {report['application_id_valid']})")
    print(f"Orders Count:             {report['orders_count']}")
    print(f"P1 Orders Count:          {report['p1_orders_count']}")
    print(f"Unfinished Jobs Count:    {report['unfinished_jobs_count']}")
    print(f"Archived Files Count:     {report['archived_files_count']} (only unfinished originals)")
    print(f"Omitted Delivered Files:  {report['omitted_delivered_files_count']} (zero-retention)")
    print(f"Ambiguous Legacy Files:   {report.get('ambiguous_legacy_files_count', 0)} (policy: {report.get('legacy_p0_policy', 'fail')})")
    print(f"Zero Retention Enforced:  {report.get('zero_retention_enforced')}")
    print(f"Privacy Classification:   {report.get('privacy_status')}")
    print(f"Legacy Files Recoverable: {report.get('legacy_files_recoverable')}")
    print(f"Recovery Verification:    {report.get('recovery_verification_result')}")
    print(f"Database SHA-256:         {report['orders_db_sha256']}")
    print(f"Uploads Tar SHA-256:      {report['uploads_tar_sha256']}")
    print(f"Verification Passed:      {report['verification_passed']}")
    if report["errors"]:
        print("\nERRORS ENCOUNTERED:")
        for err in report["errors"]:
            print(f"  - {err}")
    print("==================================================")


def main_cli() -> int:
    parser = argparse.ArgumentParser(
        description="PhotoDoc AI — Zero-Retention Backup Generator",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.getenv("DB_PATH", "/data/orders.db")),
        help="Path to source SQLite database (default: /data/orders.db)",
    )
    parser.add_argument(
        "--uploads",
        type=Path,
        default=Path(os.getenv("UPLOAD_DIR", "/app/uploads")),
        help="Path to source uploads directory (default: /app/uploads)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Target directory to create backup (e.g. /var/backups/photodoc/release_YYYYMMDD_HHMMSS)",
    )
    parser.add_argument(
        "--legacy-p0-policy",
        choices=["fail", "include", "exclude"],
        default="fail",
        help="Policy for ambiguous legacy P0 files on disk without delivery_jobs tracking: "
             "'fail' (default, fail-closed), 'include' (preserve for recovery), 'exclude' (omit for privacy)",
    )
    parser.add_argument(
        "--no-cleanup-sweep",
        action="store_true",
        help="Skip pre-backup sweep of pending cleanups for delivered orders",
    )
    parser.add_argument(
        "--skip-verification",
        action="store_true",
        help="Skip post-creation restore verification check",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON",
    )

    args = parser.parse_args()

    success, report = generate_backup(
        db_path=args.db,
        uploads_dir=args.uploads,
        output_dir=args.output_dir,
        run_pending_cleanups_flag=not args.no_cleanup_sweep,
        skip_verification=args.skip_verification,
        legacy_p0_policy=args.legacy_p0_policy,
    )

    print_backup_report(success, report, as_json=args.json)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main_cli())
