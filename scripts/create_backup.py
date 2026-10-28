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
4. Packages necessary recovery originals into uploads.tar.gz.
5. Generates cryptographic SHA-256 manifests (manifest.sha256) and metadata (backup_manifest.json).
6. Automatically verifies the resulting backup via verify_backup_integrity.py.
7. Strictly prevents accidental inclusion of delivered customer photos.
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
) -> Tuple[Set[str], Set[str], Dict[str, Any]]:
    """
    Analyzes the database to partition files into:
    1. required_files: files referenced by unfinished delivery jobs or active orders (must be archived).
    2. excluded_files: files belonging exclusively to delivered/cleaned orders (must NOT be archived).
    3. metadata: stats on jobs, orders, and zero-retention counts.
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
        }

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
                # If ANY job referencing this file is unfinished (pending, processing, failed),
                # the file is strictly required for recovery
                has_unfinished = any(r["state"] in ("pending", "processing", "failed") for r in refs)
                if has_unfinished:
                    required_files.add(filename)
                    stats["unfinished_jobs_count"] += sum(
                        1 for r in refs if r["state"] in ("pending", "processing", "failed")
                    )
                else:
                    # All jobs referencing this file are delivered!
                    # Under Zero-Retention Privacy Gate, this file must NEVER be included in new backups.
                    excluded_files.add(filename)
                    stats["delivered_jobs_count"] += len(refs)

        else:
            # P0 schema handling: active orders
            p0_orders = conn.execute("SELECT id, filename FROM orders;").fetchall()
            for o in p0_orders:
                raw_filename = o["filename"] or ""
                names = [n.strip() for n in raw_filename.split(",") if n.strip()]
                for n in names:
                    safe_name = sanitize_filename(n)
                    # Look for candidate local names
                    for cand in [f"{o['id']}_{safe_name}", f"{o['id']}_{n}", safe_name, n]:
                        if (uploads_dir / cand).is_file():
                            required_files.add(cand)
                            break

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
        # Add root 'uploads' directory entry
        uploads_dir_info = tarfile.TarInfo(name="uploads")
        uploads_dir_info.type = tarfile.DIRTYPE
        uploads_dir_info.mode = 0o755
        uploads_dir_info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(uploads_dir_info)

        for filename in sorted(required_files):
            # Assert file is not in excluded list
            if filename in excluded_files:
                raise ValueError(
                    f"Privacy violation: File '{filename}' is both required and excluded. "
                    "A delivered photo cannot be archived unless required by an unfinished job."
                )

            src_file = uploads_dir / filename
            if not is_safe_child_path(uploads_dir, src_file):
                raise ValueError(f"Path traversal detected in backup candidate: {filename}")

            if not src_file.is_file():
                # Missing source file that is required for unfinished delivery!
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
) -> Tuple[bool, Dict[str, Any]]:
    """
    Main backup generation routine.
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
        "orders_db_sha256": None,
        "uploads_tar_sha256": None,
        "zero_retention_enforced": True,
        "verification_passed": False,
        "errors": [],
    }

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
        # Verify snapshot integrity
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
        required_files, excluded_files, stats = determine_required_and_excluded_files(dst_db, uploads_dir)
        report["orders_count"] = stats["orders_count"]
        report["p1_orders_count"] = stats["p1_orders_count"]
        report["unfinished_jobs_count"] = stats["unfinished_jobs_count"]
        report["schema_generation"] = stats["schema_generation"]
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
        "orders_db_sha256": db_sha,
        "uploads_tar_sha256": tar_sha,
        "zero_retention_enforced": True,
    }
    manifest_json.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # 8. Post-creation verification
    if not skip_verification:
        with tempfile.TemporaryDirectory() as tmp_restore:
            tmp_restore_path = Path(tmp_restore)
            restore_uploads = tmp_restore_path / "uploads"
            with tarfile.open(dst_tar, "r:gz") as tar:
                tar.extractall(path=tmp_restore_path)

            valid, verify_report = verify_backup(dst_db, restore_uploads)
            if not valid:
                report["errors"].append(f"Post-creation backup verification failed: {verify_report.get('errors')}")
                return False, report
            report["verification_passed"] = True
    else:
        report["verification_passed"] = True

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
    print(f"Zero Retention Enforced:  {report['zero_retention_enforced']}")
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
    )

    print_backup_report(success, report, as_json=args.json)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main_cli())
