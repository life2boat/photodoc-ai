#!/usr/bin/env python3
"""PhotoDoc AI — Stage 1 v2 backup-set producer.

The verifier accepts **format 2** only. `scripts/create_backup.py` still writes the
legacy format 1, so a set it produces is refused rather than quietly reinterpreted. This
tool is the explicit bridge, and it uses the *same* recovery contract as the verifier
(`scripts/recovery_artifacts.py`), so it cannot build a set the gate would reject for
selection reasons.

    # produce a fresh v2 set from the live database and uploads
    python3 scripts/build_v2_set.py --live-db /data/orders.db \\
        --uploads /app/uploads --output /backups/20260101T000000Z

    # re-manifest an existing legacy set as v2 (evidence is recomputed, not copied)
    python3 scripts/build_v2_set.py --from-v1 /backups/OLD --output /backups/NEW

Nothing here is wired into a deployment. Producing a set is not authorising a cutover,
and Stage 1 still cannot freeze writers.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tarfile
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

detect_format = backup_format.detect_format


def _sha256_file(path: Path) -> str:
    return backup_format.sha256_file(path)


def _snapshot_database(source: Path, target: Path) -> None:
    """Copy the database inside one read transaction into the staging directory.

    The copy lives only inside the set being built: it is published with the set or
    removed with the staging directory. An earlier version wrote it to a generic system
    temporary directory and leaked a complete customer database there.
    """
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


def _check_upload_candidate(root: Path, candidate: Path) -> tuple[bool, str]:
    """A source file must be a regular non-symlink directly inside the uploads root."""
    if candidate.is_symlink():
        return False, f"{candidate} is a symlink; source selection refuses symlinks"
    if not candidate.is_file():
        return False, f"{candidate} is missing"
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError as exc:
        return False, f"{candidate} cannot be resolved: {exc}"
    if resolved.parent != root_resolved:
        return False, (f"{candidate} resolves outside the uploads root ({resolved}); "
                       "a nested or escaping path cannot be restored to the recorded location")
    return True, ""


def _required_copies(db_path: Path, uploads_dir: Path, policy: str
                     ) -> tuple[dict[str, Path], list[str], dict]:
    """Map each required archive member to its verified source file."""
    required, groups, errors, stats = recovery_artifacts.required_sources(
        db_path, legacy_p0_policy=policy)
    uploads_dir = Path(uploads_dir)
    copies: dict[str, Path] = {}

    for member, spec in sorted(required.items()):
        basename = Path(member).name
        candidate = uploads_dir / basename
        ok, problem = _check_upload_candidate(uploads_dir, candidate)
        if not ok:
            errors.append(problem)
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
        copies[member] = candidate

    for group in groups:
        present = []
        for member in group["candidates"]:
            candidate = uploads_dir / Path(member).name
            ok, _problem = _check_upload_candidate(uploads_dir, candidate)
            if ok:
                present.append((member, candidate))
        if not present:
            errors.append(
                f"legacy order {group['order_id']} lists {group['name']!r} but no stored "
                f"layout exists under {uploads_dir}")
            continue
        if len(present) > 1:
            errors.append(
                f"legacy order {group['order_id']} matches more than one file "
                f"({', '.join(m for m, _ in present)}); the original is ambiguous")
            continue
        member, candidate = present[0]
        copies[member] = candidate

    return copies, errors, stats


def _write_archive(copies: dict[str, Path], target: Path) -> None:
    """Write exactly the required members, at their exact contract paths."""
    with tarfile.open(target, "w:gz") as tar:
        root = tarfile.TarInfo(recovery_artifacts.UPLOAD_ROOT)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        tar.addfile(root)
        for member in sorted(copies):
            path = copies[member]
            info = tar.gettarinfo(str(path), arcname=member)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                tar.addfile(info, handle)


def _evidence(db_path: Path) -> dict:
    conn = db_evidence.open_readonly(db_path)
    try:
        conn.execute("BEGIN")
        schema = db_evidence.canonical_schema(conn)
        logical = db_evidence.logical_state_digest(conn)
        application_id = conn.execute("PRAGMA application_id").fetchone()[0]
        sqlite_version = ".".join(str(p) for p in sqlite3.sqlite_version_info)
    finally:
        conn.rollback()
        conn.close()
    return {"schema_digest": schema["digest"], "logical_digest": logical,
            "application_id": application_id, "sqlite_version": sqlite_version}


def _finalise(staging: Path, output: Path, evidence: dict, limits,
              policy: str, restore_uploads_root: str) -> dict:
    archive = staging / backup_format.TAR_FILE
    info, archive_errors = backup_format.inspect_archive(archive, limits)
    if archive_errors:
        raise backup_format.FormatError(
            "the produced archive failed inspection: " + "; ".join(archive_errors))

    ok, recovery = recovery_artifacts.verify_recovery(
        staging / backup_format.DB_FILE, archive, limits, legacy_p0_policy=policy,
        expected_uploads_root=restore_uploads_root)
    if not ok:
        raise backup_format.FormatError(
            "the produced archive does not satisfy the database: "
            + "; ".join(recovery["errors"][:3]))
    # H2: the recorded root must be the deployment's, not a value echoed from the database.
    derived_root = recovery.get("required", {}).get("uploads_root")
    generation = recovery.get("required", {}).get("generation")
    if derived_root is None and generation == "P0" and recovery.get("p0_groups"):
        # Legacy originals have no per-job absolute path; the archive root maps onto the
        # deployment's own uploads root, so record that and skip the derived comparison.
        if restore_uploads_root is None:
            raise backup_format.FormatError(
                "a P0 set requires --restore-uploads-root (the application's uploads root)")
        derived_root = restore_uploads_root
    elif derived_root is None:
        # Nothing to place: the manifest records a null root and no root is required.
        restore_uploads_root = None
    elif restore_uploads_root is None:
        raise backup_format.FormatError(
            "the database requires originals but no --restore-uploads-root was supplied; "
            "the application's uploads root must come from the deployment configuration, "
            "not from the database")
    if derived_root is not None and recovery_artifacts.normalise_root(derived_root) != \
            recovery_artifacts.normalise_root(restore_uploads_root):
        raise backup_format.FormatError(
            f"the database records uploads root {derived_root!r} but the deployment root is "
            f"{restore_uploads_root!r}; a restore would leave every recorded path wrong")

    manifest = backup_format.build_manifest(
        artifacts={backup_format.DB_FILE: _sha256_file(staging / backup_format.DB_FILE),
                   backup_format.TAR_FILE: _sha256_file(archive)},
        created_at=datetime.now(timezone.utc),
        application_id=evidence["application_id"],
        schema_digest=evidence["schema_digest"], logical_digest=evidence["logical_digest"],
        database_identity={"application_id": evidence["application_id"],
                           "schema_digest": evidence["schema_digest"],
                           "logical_digest": evidence["logical_digest"],
                           "sqlite_version": evidence["sqlite_version"]},
        upload_members=info["members"], upload_bytes=info["bytes"],
        verification="PASS",
        restore_uploads_root=(None if restore_uploads_root is None
                              else recovery_artifacts.normalise_root(restore_uploads_root)))
    backup_format.write_manifest_files(staging, manifest)

    verified, report = backup_format.verify_set(
        staging, expected_application_id=evidence["application_id"], deep=True,
        limits=limits, allow_staging=True, legacy_p0_policy=policy,
        expected_uploads_root=restore_uploads_root)
    if not verified:
        raise backup_format.FormatError(
            "the produced set does not verify: " + "; ".join(report["errors"][:3]))

    publication = backup_format.publish_set(staging, Path(output))
    return {"output": str(output), "set_id": manifest["set_id"],
            "schema_digest": evidence["schema_digest"],
            "logical_digest": evidence["logical_digest"],
            "recovery_files": info["files"], "publication": publication,
            "generation": recovery["required"].get("generation")}


def build_from_live(live_db: Path, uploads_dir: Path, output: Path,
                    limits, policy: str, restore_uploads_root: str) -> dict:
    staging = backup_format.staging_dir_for(Path(output))
    staging.mkdir(parents=True)
    try:
        target_db = staging / backup_format.DB_FILE
        _snapshot_database(live_db, target_db)
        copies, errors, _stats = _required_copies(target_db, uploads_dir, policy)
        if errors:
            raise backup_format.FormatError(
                "the live files do not match the live database: " + "; ".join(errors[:3]))
        _write_archive(copies, staging / backup_format.TAR_FILE)
        return _finalise(staging, output, _evidence(target_db), limits, policy,
                         restore_uploads_root)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# --------------------------------------------------------------------- v1 upgrade
def upgrade_legacy(source: Path, output: Path, limits, policy: str,
                   restore_uploads_root: str) -> dict:
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

        # A legacy PASS is never inherited: the archive must satisfy this contract.
        recovery_ok, recovery = recovery_artifacts.verify_recovery(
            target_db, target_tar, limits, legacy_p0_policy=policy,
            expected_uploads_root=restore_uploads_root)
        if not recovery_ok:
            raise backup_format.FormatError(
                "the legacy set cannot restore its own database: "
                + "; ".join(recovery["errors"][:3]))

        result = _finalise(staging, output, _evidence(target_db), limits, policy,
                           restore_uploads_root)
        result["source_format"] = "legacy-v1"
        return result
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
    parser.add_argument("--restore-uploads-root", default=None,
                        help="the APPLICATION's uploads root after restore (the container's "
                             "UPLOAD_DIR / Compose binding), not a value read from the "
                             "database; every recorded local_path must live under it. "
                             "Required whenever the database requires any original.")
    parser.add_argument("--legacy-p0-policy", default="fail",
                        choices=recovery_artifacts.P0_POLICIES,
                        help="how to treat orders without delivery_jobs (default: fail)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    limits = recovery_artifacts.DEFAULT_LIMITS
    try:
        if args.from_v1 is not None:
            result = upgrade_legacy(args.from_v1, args.output, limits,
                                    args.legacy_p0_policy, args.restore_uploads_root)
        else:
            if args.uploads is None:
                print("CONFIG ERROR: --uploads is required with --live-db", file=sys.stderr)
                return EXIT_CONFIG
            if not args.live_db.is_file():
                print(f"CONFIG ERROR: live database not found: {args.live_db}",
                      file=sys.stderr)
                return EXIT_CONFIG
            if not args.uploads.is_dir():
                print(f"CONFIG ERROR: uploads directory not found: {args.uploads}",
                      file=sys.stderr)
                return EXIT_CONFIG
            if args.uploads.is_symlink():
                print(f"CONFIG ERROR: uploads directory is a symlink: {args.uploads}",
                      file=sys.stderr)
                return EXIT_CONFIG
            if Path(args.output).exists():
                print(f"CONFIG ERROR: output already exists: {args.output}", file=sys.stderr)
                return EXIT_CONFIG
            result = build_from_live(args.live_db, args.uploads, args.output, limits,
                                     args.legacy_p0_policy, args.restore_uploads_root)
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
        print(f"  schema:         {result.get('generation')}")
    return EXIT_OK
