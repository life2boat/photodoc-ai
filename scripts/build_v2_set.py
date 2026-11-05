#!/usr/bin/env python3
"""PhotoDoc AI — Stage 1 v2 backup-set producer.

The verifier in this package accepts **format 2** only. `scripts/create_backup.py`
still writes the legacy format 1, so a set it produces is refused rather than
quietly reinterpreted. This tool is the explicit bridge:

    # produce a fresh v2 set from the live database and uploads
    python3 scripts/build_v2_set.py --live-db /data/orders.db \
        --uploads /data/uploads --output /backups/20260101T000000Z

    # re-manifest an existing legacy set as v2 (evidence is recomputed, not copied)
    python3 scripts/build_v2_set.py --from-v1 /backups/OLD --output /backups/NEW

Nothing here is wired into a deployment. It is an isolated interface: producing a
set is not authorising a cutover, and Stage 1 still cannot freeze writers.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backup_format  # noqa: E402
import db_evidence  # noqa: E402
import recovery_artifacts  # noqa: E402

APPLICATION_ID = 1346650441
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


def _sha256_file(path: Path) -> str:
    return backup_format.sha256_file(path)


def _snapshot_database(source: Path, target: Path) -> None:
    """Copy the database inside one read transaction (consistent, no writers held)."""
    src = db_evidence.open_readonly(Path(source))
    dst = sqlite3.connect(target)
    try:
        src.execute("BEGIN")
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        try:
            src.rollback()
        except sqlite3.Error:
            pass
        src.close()


def _required_copies(db_path: Path, uploads_dir: Path) -> tuple[dict, list[str]]:
    """Map each required recovery file to its bytes on disk, verifying identity."""
    required, errors, _stats = recovery_artifacts.required_sources(db_path)
    copies: dict[str, Path] = {}
    for basename, spec in sorted(required.items()):
        candidate = Path(uploads_dir) / basename
        if not candidate.is_file():
            errors.append(f"required recovery original is missing on disk: {candidate}")
            continue
        size = candidate.stat().st_size
        if size != spec["size"]:
            errors.append(
                f"{candidate} is {size} bytes but the database records {spec['size']}")
            continue
        digest = _sha256_file(candidate)
        if digest != spec["sha256"]:
            errors.append(
                f"{candidate} has sha256 {digest} but the database records {spec['sha256']}")
            continue
        copies[basename] = candidate
    return copies, errors


def _write_archive(copies: dict[str, Path], target: Path) -> None:
    with tarfile.open(target, "w:gz") as tar:
        root = tarfile.TarInfo(recovery_artifacts.UPLOAD_ROOT)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        tar.addfile(root)
        for basename in sorted(copies):
            path = copies[basename]
            info = tar.gettarinfo(str(path), arcname=f"{recovery_artifacts.UPLOAD_ROOT}/{basename}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                tar.addfile(info, handle)


def _build(db_path: Path, copies: dict[str, Path], output: Path,
           limits) -> dict:
    staging = backup_format.staging_dir_for(Path(output))
    staging.mkdir(parents=True)
    try:
        target_db = staging / backup_format.DB_FILE
        shutil.copyfile(db_path, target_db)

        conn = db_evidence.open_readonly(target_db)
        try:
            conn.execute("BEGIN")
            schema = db_evidence.canonical_schema(conn)
            logical = db_evidence.logical_state_digest(conn)
            application_id = conn.execute("PRAGMA application_id").fetchone()[0]
            sqlite_version = ".".join(str(p) for p in sqlite3.sqlite_version_info)
        finally:
            conn.rollback()
            conn.close()

        archive = staging / backup_format.TAR_FILE
        _write_archive(copies, archive)
        info, archive_errors = backup_format.inspect_archive(archive, limits)
        if archive_errors:
            raise backup_format.FormatError(
                "the produced archive failed inspection: " + "; ".join(archive_errors))

        ok, recovery = recovery_artifacts.verify_recovery(target_db, archive, limits)
        if not ok:
            raise backup_format.FormatError(
                "the produced archive does not satisfy the database: "
                + "; ".join(recovery["errors"][:3]))

        manifest = backup_format.build_manifest(
            artifacts={backup_format.DB_FILE: _sha256_file(target_db),
                       backup_format.TAR_FILE: _sha256_file(archive)},
            created_at=datetime.now(timezone.utc),
            application_id=application_id,
            schema_digest=schema["digest"], logical_digest=logical,
            database_identity={"application_id": application_id,
                               "schema_digest": schema["digest"],
                               "logical_digest": logical,
                               "sqlite_version": sqlite_version},
            upload_members=info["members"], upload_bytes=info["bytes"],
            verification="PASS")
        backup_format.write_manifest_files(staging, manifest)

        verified, report = backup_format.verify_set(
            staging, expected_application_id=application_id, deep=True, limits=limits,
            allow_staging=True)
        if not verified:
            raise backup_format.FormatError(
                "the produced set does not verify: " + "; ".join(report["errors"][:3]))

        publication = backup_format.publish_set(staging, Path(output))
        return {"output": str(output), "set_id": manifest["set_id"],
                "schema_digest": schema["digest"], "logical_digest": logical,
                "recovery_files": len(copies), "publication": publication}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# --------------------------------------------------------------------- v1 upgrade
detect_format = backup_format.detect_format


def upgrade_legacy(source: Path, output: Path, limits) -> dict:
    """Re-manifest a legacy set as v2, recomputing every claim from the bytes."""
    source = Path(source)
    source_db = source / backup_format.DB_FILE
    source_tar = source / backup_format.TAR_FILE
    if not source_db.is_file() or not source_tar.is_file():
        raise backup_format.FormatError(
            f"{source} does not look like a legacy backup set (orders.db + uploads.tar.gz)")
    if detect_format(source) == "v2":
        raise backup_format.FormatError(f"{source} is already a v2 set; nothing to upgrade")

    staging = backup_format.staging_dir_for(Path(output))
    staging.mkdir(parents=True)
    try:
        target_db = staging / backup_format.DB_FILE
        shutil.copyfile(source_db, target_db)
        target_tar = staging / backup_format.TAR_FILE
        shutil.copyfile(source_tar, target_tar)

        info, archive_errors = backup_format.inspect_archive(target_tar, limits)
        if archive_errors:
            raise backup_format.FormatError(
                "the legacy archive failed inspection: " + "; ".join(archive_errors))
        ok, recovery = recovery_artifacts.verify_recovery(target_db, target_tar, limits)
        if not ok:
            raise backup_format.FormatError(
                "the legacy set cannot restore its own database: "
                + "; ".join(recovery["errors"][:3]))

        conn = db_evidence.open_readonly(target_db)
        try:
            conn.execute("BEGIN")
            schema = db_evidence.canonical_schema(conn)
            logical = db_evidence.logical_state_digest(conn)
            application_id = conn.execute("PRAGMA application_id").fetchone()[0]
            sqlite_version = ".".join(str(p) for p in sqlite3.sqlite_version_info)
        finally:
            conn.rollback()
            conn.close()

        manifest = backup_format.build_manifest(
            artifacts={backup_format.DB_FILE: _sha256_file(target_db),
                       backup_format.TAR_FILE: _sha256_file(target_tar)},
            created_at=datetime.now(timezone.utc),
            application_id=application_id,
            schema_digest=schema["digest"], logical_digest=logical,
            database_identity={"application_id": application_id,
                               "schema_digest": schema["digest"],
                               "logical_digest": logical,
                               "sqlite_version": sqlite_version},
            upload_members=info["members"], upload_bytes=info["bytes"],
            verification="PASS")
        backup_format.write_manifest_files(staging, manifest)

        verified, report = backup_format.verify_set(
            staging, deep=True, limits=limits, allow_staging=True)
        if not verified:
            raise backup_format.FormatError(
                "the upgraded set does not verify: " + "; ".join(report["errors"][:3]))
        publication = backup_format.publish_set(staging, Path(output))
        return {"output": str(output), "set_id": manifest["set_id"],
                "source_format": "legacy-v1", "recovery_files": len(
                    recovery_artifacts.required_sources(target_db)[0]),
                "publication": publication}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a Stage 1 v2 backup set")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--live-db", type=Path)
    source.add_argument("--from-v1", type=Path)
    parser.add_argument("--uploads", type=Path, help="uploads directory (with --live-db)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    limits = recovery_artifacts.DEFAULT_LIMITS
    try:
        if args.from_v1 is not None:
            result = upgrade_legacy(args.from_v1, args.output, limits)
        else:
            if args.uploads is None:
                print("CONFIG ERROR: --uploads is required with --live-db", file=sys.stderr)
                return EXIT_CONFIG
            if not args.live_db.is_file():
                print(f"CONFIG ERROR: live database not found: {args.live_db}", file=sys.stderr)
                return EXIT_CONFIG
            if not args.uploads.is_dir():
                print(f"CONFIG ERROR: uploads directory not found: {args.uploads}",
                      file=sys.stderr)
                return EXIT_CONFIG
            if Path(args.output).exists():
                print(f"CONFIG ERROR: output already exists: {args.output}", file=sys.stderr)
                return EXIT_CONFIG
            staging_db = Path(tempfile.mkdtemp()) / "snapshot.db"
            _snapshot_database(args.live_db, staging_db)
            copies, errors = _required_copies(staging_db, args.uploads)
            if errors:
                print("REFUSED: the live files do not match the live database:",
                      file=sys.stderr)
                for error in errors:
                    print(f"  - {error}", file=sys.stderr)
                return EXIT_FAILED
            result = _build(staging_db, copies, args.output, limits)
    except backup_format.FormatError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print("v2 backup set published")
        print(f"  output:         {result['output']}")
        print(f"  set_id:         {result['set_id']}")
        print(f"  recovery files: {result['recovery_files']}")
    return EXIT_OK
