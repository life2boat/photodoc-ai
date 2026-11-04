#!/usr/bin/env python3
"""PhotoDoc AI — backup safety primitives.

Small, dependency-free building blocks shared by the backup tooling, the
deployment gate and the tests:

* :func:`atomic_publish` — build a backup set off to the side and publish it with
  a rename, so a crashed or failed run can never leave a partial set where a
  restore procedure would find it, and can never destroy the previous good set.
* :func:`verify_backup_set` — decide whether a directory really is a complete,
  verified, fresh backup set worth staking a deployment on.
* :func:`check_quiescence` — report whether writers are in flight, which is what
  makes an upload snapshot consistent rather than merely plausible.
* :func:`plan_retention` — pure pruning plan that always keeps the newest set.

Nothing here writes to a live database. All database access is ``mode=ro``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # bare import path used by the scripts/ entry points
    from verify_backup_integrity import APPLICATION_ID, APPLICATION_ID_TAG
except ImportError:  # pragma: no cover - defensive fallback
    APPLICATION_ID_TAG = "PDAI"
    APPLICATION_ID = int.from_bytes(APPLICATION_ID_TAG.encode("ascii"), "big")

#: Tables a restore must be able to rely on in every schema generation.
CORE_TABLES = ("orders", "delivery_jobs", "sqlite_sequence")
#: Added by PR #26. Optional for older backups, mandatory when required.
OUTBOX_TABLE = "paid_email_outbox"
OUTBOX_STATES = frozenset({"pending", "retry", "sending", "sent", "uncertain"})


# ``readonly_uri`` lives in its own dependency-free module so every script can
# import it without a cycle; re-exported here for existing callers.
from sqlite_uri import readonly_uri  # noqa: E402,F401

REQUIRED_FILES = ("orders.db", "uploads.tar.gz", "manifest.sha256", "backup_manifest.json")

TMP_PREFIX = ".tmp-"
PARTIAL_MARKER = ".incomplete"
PREVIOUS_SUFFIX = ".previous"


class BackupSafetyError(RuntimeError):
    """Raised when a safety invariant cannot be upheld."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_safe_child(base: Path, target: Path) -> bool:
    try:
        base_resolved = Path(base).resolve()
        target_resolved = Path(target).resolve()
        return base_resolved == target_resolved or base_resolved in target_resolved.parents
    except (OSError, RuntimeError):
        return False


# --------------------------------------------------------------------------- publish
#: Set when the platform cannot flush a directory entry (Windows). Durability of
#: the rename itself is then unprovable, which is reported rather than assumed.
DIRECTORY_FSYNC_SUPPORTED = os.name == "posix"


def fsync_file(path: Path) -> bool:
    """Flush one file's contents to stable storage. Returns False if unsupported.

    ``os.replace`` makes a new name *visible*; it does not make the bytes durable.
    Every artifact is flushed before the rename so a published set can never
    reference data that only exists in cache.

    Platform note: Windows only flushes handles with write access, and the
    directory-entry barrier is not exposed at all. Rather than pretend, an
    unsupported flush returns False so the caller can report degraded durability.
    """
    path = Path(path)
    if path.is_symlink():
        raise BackupSafetyError(f"refusing to fsync a symlink: {path}")

    fd = None
    for flags in (os.O_RDWR, os.O_RDONLY):
        try:
            fd = os.open(path, flags)
            break
        except OSError:
            continue
    if fd is None:
        if os.name == "nt":  # pragma: no cover - Windows handle semantics
            return False
        raise BackupSafetyError(f"cannot open for fsync: {path}")
    try:
        os.fsync(fd)
        return True
    except OSError:
        if os.name == "nt":  # pragma: no cover - Windows handle semantics
            return False
        raise BackupSafetyError(f"fsync failed for {path}")
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> bool:
    """Flush a directory entry (the rename/rotation itself).

    Returns False when the platform does not support it instead of pretending the
    barrier happened.
    """
    path = Path(path)
    if not DIRECTORY_FSYNC_SUPPORTED:
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> Tuple[int, int]:
    """Flush every regular file below ``root``.

    Returns ``(flushed, unsupported)``. Symlinks are rejected rather than
    followed: a backup set is produced by this project and must contain only
    regular files.
    """
    root = Path(root)
    flushed = unsupported = 0
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            raise BackupSafetyError(f"backup set contains a symlink: {entry}")
        if entry.is_file():
            if fsync_file(entry):
                flushed += 1
            else:
                unsupported += 1
    return flushed, unsupported


def atomic_publish(tmp_dir: Path, final_dir: Path, *, replace: bool = True,
                   durable: bool = True,
                   durability: Optional[Dict[str, Any]] = None) -> Path:
    """Publish ``tmp_dir`` as ``final_dir`` without ever exposing a partial set.

    The previous set, if any, is moved aside to ``<final>.previous`` and restored
    if publishing fails. A failed run therefore leaves either the old set or the
    new one — never a half-written mixture, and never nothing.

    With ``durable`` (the default) the contents are flushed before the rename and
    the containing directories are flushed after it and after the rotation, so the
    published name points at data that reached storage. This is a best-effort
    barrier: it is what the filesystem and platform expose, not a proof of
    power-loss survival (see docs/BACKUP_AND_RESTORE.md).
    """
    tmp_dir = Path(tmp_dir)
    final_dir = Path(final_dir)
    if not tmp_dir.is_dir():
        raise BackupSafetyError(f"staging directory does not exist: {tmp_dir}")
    if tmp_dir == final_dir:
        raise BackupSafetyError("staging and destination directories are the same path")
    if not _is_safe_child(final_dir.parent, tmp_dir):
        raise BackupSafetyError("staging directory is outside the destination parent")

    if durable:
        # 1. Contents first: the manifest must never describe unflushed bytes.
        flushed, unsupported = fsync_tree(tmp_dir)
        dir_ok = fsync_dir(tmp_dir)
        if durability is not None:
            durability.update({
                "files_flushed": flushed,
                "files_unsupported": unsupported,
                "staging_dir_flushed": dir_ok,
                "directory_fsync_supported": DIRECTORY_FSYNC_SUPPORTED,
            })

    previous = final_dir.with_name(final_dir.name + PREVIOUS_SUFFIX)
    moved_previous = False
    parent = final_dir.parent

    if final_dir.exists():
        if not replace:
            raise BackupSafetyError(f"destination already exists: {final_dir}")
        if previous.exists():
            # Keep the older copy under a timestamp so nothing is silently lost.
            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            displaced = previous.with_name(f"{previous.name}-{stamp}")
            os.replace(previous, displaced)
        os.replace(final_dir, previous)
        moved_previous = True
        if durable:
            # The rotation must be durable before the new set takes the name,
            # otherwise a power cut could leave neither.
            fsync_dir(parent)

    try:
        os.replace(tmp_dir, final_dir)
    except OSError as exc:
        if moved_previous and not final_dir.exists():
            try:
                os.replace(previous, final_dir)
                if durable:
                    fsync_dir(parent)
            except OSError:  # pragma: no cover - last-resort reporting
                raise BackupSafetyError(
                    f"publish failed ({exc}) and the previous set could not be restored; "
                    f"it remains at {previous}") from exc
        raise BackupSafetyError(f"failed to publish backup set: {exc}") from exc

    if durable:
        fsync_dir(parent)
    return final_dir


def staging_dir_for(final_dir: Path) -> Path:
    """Sibling staging directory used while a set is being built."""
    final_dir = Path(final_dir)
    return final_dir.parent / f"{TMP_PREFIX}{final_dir.name}-{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ staging sweep
STAGING_LOCK_NAME = ".attempt-lock"


def staging_is_active(staging: Path, *, now: Optional[datetime] = None,
                      active_window_minutes: int = 180) -> bool:
    """Report whether a staging directory may still belong to a live attempt.

    A backup for a small VPS finishes in minutes, so an attempt whose lock (or
    directory) is younger than the window is treated as live regardless of age
    thresholds. Liveness by PID is not portable, so freshness is the signal.
    """
    now = now or utc_now()
    staging = Path(staging)
    for candidate in (staging / STAGING_LOCK_NAME, staging):
        try:
            mtime = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if now - mtime < timedelta(minutes=active_window_minutes):
            return True
    return False


def sweep_staging_dirs(
    root: Path,
    *,
    min_age_hours: float = 24.0,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    active_window_minutes: int = 180,
) -> Tuple[List[Path], List[Dict[str, Any]]]:
    """Remove abandoned ``.tmp-*`` staging directories under ``root``.

    Safety rules, in order:

    * only entries named with the staging prefix are considered at all, so a
      published backup set can never be removed by this function;
    * symlinks and non-directories are refused, never followed;
    * an attempt that still looks live (lock file or directory touched within
      ``active_window_minutes``) is skipped, so a concurrent run is not clobbered;
    * anything younger than ``min_age_hours`` is skipped.

    Returns ``(removed, skipped)`` where each skip records the reason, so a caller
    can log a bounded, explainable outcome.
    """
    now = now or utc_now()
    root = Path(root)
    removed: List[Path] = []
    skipped: List[Dict[str, Any]] = []
    if not root.is_dir():
        return removed, [{"path": str(root), "reason": "root does not exist"}]

    for entry in sorted(root.iterdir()):
        if not entry.name.startswith(TMP_PREFIX):
            continue
        record = {"path": str(entry)}
        if entry.is_symlink():
            skipped.append({**record, "reason": "symlink refused"})
            continue
        if not entry.is_dir():
            skipped.append({**record, "reason": "not a directory"})
            continue
        try:
            age_hours = (now - datetime.fromtimestamp(
                entry.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600.0
        except OSError as exc:
            skipped.append({**record, "reason": f"unreadable: {exc}"})
            continue
        if staging_is_active(entry, now=now, active_window_minutes=active_window_minutes):
            skipped.append({**record, "reason": "active attempt"})
            continue
        if age_hours < min_age_hours:
            skipped.append({**record, "reason": f"younger than {min_age_hours}h"})
            continue
        if dry_run:
            skipped.append({**record, "reason": "dry run"})
            continue
        try:
            shutil.rmtree(entry)
            removed.append(entry)
        except OSError as exc:
            skipped.append({**record, "reason": f"delete failed: {exc}"})
    return removed, skipped


# ------------------------------------------------------------------- selection
def backup_timestamp(backup_dir: Path) -> Optional[datetime]:
    """Creation time from the manifest, falling back to the directory mtime.

    The manifest is the artifact the verification path already trusts; mtime is
    only a copy artifact (``cp``, restore, archive extraction) and must not decide
    which backup is newest.
    """
    backup_dir = Path(backup_dir)
    manifest = backup_dir / "backup_manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        stamp = payload.get("timestamp")
        if stamp:
            parsed = datetime.fromisoformat(str(stamp))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    try:
        return datetime.fromtimestamp(backup_dir.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


# --------------------------------------------------------- previous retention
def plan_previous_retention(
    previous_sets: Sequence[Tuple[Path, datetime]],
    *,
    keep: int = 2,
    now: Optional[datetime] = None,
) -> Tuple[List[Path], List[Path]]:
    """Plan pruning of ``.previous`` sets while keeping the recent ones.

    ``.previous`` exists so a replaced backup remains recoverable. Keeping every
    one forever doubles disk use, but keeping none removes the fallback. The
    newest ``keep`` are always retained regardless of age.
    """
    now = now or utc_now()
    ordered = sorted(previous_sets, key=lambda item: item[1], reverse=True)
    keep_paths = [path for path, _ in ordered[:max(1, keep)]]
    prune_paths = [path for path, _ in ordered[max(1, keep):]]
    return keep_paths, prune_paths


# --------------------------------------------------------------------------- verify
def _read_manifest_checksums(manifest_path: Path) -> Tuple[Dict[str, str], List[str]]:
    errors: List[str] = []
    checksums: Dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            errors.append(f"malformed manifest line: {line!r}")
            continue
        digest, name = parts
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append(f"manifest digest is not sha256: {line!r}")
            continue
        name = name.lstrip("*")
        if Path(name).name != name:
            errors.append(f"manifest entry is not a bare filename: {name!r}")
            continue
        checksums[name] = digest
    return checksums, errors


def _check_tables(conn: sqlite3.Connection, *, require_outbox: bool) -> List[str]:
    errors: List[str] = []
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in CORE_TABLES:
        if table not in tables:
            errors.append(f"required table missing from backup: {table}")
    if require_outbox and OUTBOX_TABLE not in tables:
        errors.append(
            f"paid-email outbox table '{OUTBOX_TABLE}' is missing but required: restoring "
            "this backup would lose at-most-once email attempt evidence")
    if OUTBOX_TABLE in tables:
        errors.extend(_check_outbox(conn))
    return errors


def _check_outbox(conn: sqlite3.Connection) -> List[str]:
    errors: List[str] = []
    bad_states = [
        row[0] for row in conn.execute(
            f"SELECT DISTINCT state FROM {OUTBOX_TABLE}")
        if row[0] not in OUTBOX_STATES
    ]
    if bad_states:
        errors.append(f"outbox contains unknown states: {bad_states}")

    # A 'sent' row without acceptance evidence is not proof of delivery.
    sent_without_evidence = conn.execute(
        f"SELECT COUNT(*) FROM {OUTBOX_TABLE} WHERE state='sent' AND accepted_at IS NULL"
    ).fetchone()[0]
    if sent_without_evidence:
        errors.append(
            f"{sent_without_evidence} outbox row(s) marked 'sent' without accepted_at evidence")

    # Attempts must always be attributable to an order.
    orphaned = conn.execute(
        f"SELECT COUNT(*) FROM {OUTBOX_TABLE} e "
        "WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.id = e.order_id)"
    ).fetchone()[0]
    if orphaned:
        errors.append(f"{orphaned} outbox row(s) reference a missing order")
    return errors


def verify_backup_set(
    backup_dir: Path,
    *,
    require_outbox: bool = False,
    max_age_hours: Optional[float] = None,
    require_verified: bool = True,
    now: Optional[datetime] = None,
    check_upload_archive: bool = False,
    allow_staging: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """Decide whether ``backup_dir`` is a complete, verified backup set.

    Returns ``(ok, report)``. ``report['errors']`` is empty exactly when ``ok``.
    Every check is fail-closed: an unreadable or ambiguous artifact is an error,
    never a pass.

    ``allow_staging`` exists for the one caller that legitimately inspects a set
    *before* publishing it. External callers (the deployment gate, retention) must
    leave it False so an unpublished staging directory can never be mistaken for a
    usable backup.
    """
    now = now or utc_now()
    backup_dir = Path(backup_dir)
    report: Dict[str, Any] = {
        "backup_dir": str(backup_dir),
        "checked_at": now.isoformat(),
        "errors": [],
        "warnings": [],
        "required_outbox": require_outbox,
        "files": {},
        "checksums": {},
        "sqlite_integrity": "unknown",
        "application_id": None,
        "tables": [],
        "orders_count": None,
        "manifest_orders_count": None,
        "outbox_present": False,
        "outbox_states": {},
        "age_hours": None,
        "verified": None,
    }
    errors: List[str] = report["errors"]

    # --- structure
    if not backup_dir.exists():
        errors.append(f"backup directory does not exist: {backup_dir}")
        return False, report
    if backup_dir.is_symlink():
        errors.append("backup directory is a symbolic link")
        return False, report
    if not backup_dir.is_dir():
        errors.append("backup path is not a directory")
        return False, report
    if backup_dir.name.startswith(TMP_PREFIX) and not allow_staging:
        errors.append("backup directory is an unfinished staging directory")
        return False, report
    if (backup_dir / PARTIAL_MARKER).exists():
        errors.append("backup directory carries an incomplete marker")
    if any(backup_dir.glob(f"{TMP_PREFIX}*")):
        errors.append("backup directory contains leftover staging artifacts")

    for name in REQUIRED_FILES:
        path = backup_dir / name
        present = path.is_file() and not path.is_symlink()
        report["files"][name] = {"present": present, "size": path.stat().st_size if present else None}
        if not present:
            errors.append(f"required backup file missing or not a regular file: {name}")
    if errors and not all(entry["present"] for entry in report["files"].values()):
        return False, report

    # --- manifest checksums
    try:
        checksums, manifest_errors = _read_manifest_checksums(backup_dir / "manifest.sha256")
        errors.extend(manifest_errors)
        for name in ("orders.db", "uploads.tar.gz"):
            if name not in checksums:
                errors.append(f"manifest.sha256 does not cover {name}")
                continue
            actual = sha256_file(backup_dir / name)
            report["checksums"][name] = {"expected": checksums[name], "actual": actual}
            if actual != checksums[name]:
                errors.append(f"checksum mismatch for {name}")
    except OSError as exc:
        errors.append(f"cannot read manifest.sha256: {exc}")

    # --- metadata manifest
    manifest: Dict[str, Any] = {}
    try:
        manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
        report["manifest_orders_count"] = manifest.get("orders_count")
        report["verified"] = manifest.get("recovery_verification_result")
        if require_verified and manifest.get("recovery_verification_result") != "PASS":
            errors.append(
                "backup manifest does not record a PASS recovery verification "
                f"(got {manifest.get('recovery_verification_result')!r})")
        if manifest.get("orders_db_sha256") not in checksums.values():
            report["warnings"].append("manifest orders_db_sha256 not present in manifest.sha256")
        stamp = manifest.get("timestamp")
        if stamp:
            try:
                created = datetime.fromisoformat(stamp)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = now - created
                report["age_hours"] = round(age.total_seconds() / 3600.0, 3)
                if max_age_hours is not None and report["age_hours"] > max_age_hours:
                    errors.append(
                        f"backup is too old: {report['age_hours']}h > {max_age_hours}h")
                if age < timedelta(minutes=-5):
                    errors.append("backup timestamp is in the future")
            except ValueError:
                errors.append(f"unparseable backup timestamp: {stamp!r}")
        elif max_age_hours is not None:
            errors.append("backup manifest has no timestamp; freshness cannot be proven")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read backup_manifest.json: {exc}")

    # --- database
    db_path = backup_dir / "orders.db"
    uri = readonly_uri(db_path)
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        try:
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
            report["sqlite_integrity"] = integrity
            if integrity != "ok":
                errors.append(f"sqlite integrity_check failed: {integrity}")

            app_id = conn.execute("PRAGMA application_id;").fetchone()[0]
            report["application_id"] = app_id
            if app_id != APPLICATION_ID:
                errors.append(
                    f"application_id mismatch: {app_id} != {APPLICATION_ID} ({APPLICATION_ID_TAG})")

            report["tables"] = sorted(
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            errors.extend(_check_tables(conn, require_outbox=require_outbox))

            fk_problems = conn.execute("PRAGMA foreign_key_check;").fetchall()
            if fk_problems:
                errors.append(f"{len(fk_problems)} foreign-key violation(s) in backup database")

            if "orders" in report["tables"]:
                report["orders_count"] = conn.execute("SELECT COUNT(*) FROM orders;").fetchone()[0]
                if (report["manifest_orders_count"] is not None
                        and report["manifest_orders_count"] != report["orders_count"]):
                    errors.append(
                        "orders count disagrees with the manifest: "
                        f"{report['orders_count']} != {report['manifest_orders_count']}")

            report["outbox_present"] = OUTBOX_TABLE in report["tables"]
            if report["outbox_present"]:
                report["outbox_states"] = dict(conn.execute(
                    f"SELECT state, COUNT(*) FROM {OUTBOX_TABLE} GROUP BY state").fetchall())
        finally:
            conn.close()
    except sqlite3.Error as exc:
        errors.append(f"cannot inspect backup database: {exc}")

    # --- optional deep check of the upload archive
    if check_upload_archive and not errors:
        errors.extend(_check_upload_archive(backup_dir, report))

    return (len(errors) == 0), report


def _check_upload_archive(backup_dir: Path, report: Dict[str, Any]) -> List[str]:
    """Extract the archive safely and re-run the project's own verifier on it."""
    errors: List[str] = []
    try:
        from verify_backup_integrity import verify_backup
    except ImportError as exc:
        # Fail closed. A deep check that silently degrades to "not checked" is
        # worse than no check at all: the deployment would proceed believing the
        # archive had been verified.
        report["upload_archive_verified"] = False
        return [f"deep upload verification is unavailable ({exc}); refusing to treat the "
                "archive as verified"]
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        try:
            with tarfile.open(backup_dir / "uploads.tar.gz", "r:gz") as tar:
                members = tar.getmembers()
                if len(members) > 100_000:
                    return ["upload archive has too many members"]
                for member in members:
                    if member.issym() or member.islnk():
                        return [f"upload archive contains a link member: {member.name}"]
                    dest = (target / member.name).resolve()
                    if not _is_safe_child(target, dest):
                        return [f"upload archive member escapes the extraction root: {member.name}"]
                    if not (member.isreg() or member.isdir()):
                        return [f"upload archive contains a disallowed member: {member.name}"]
                    if member.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        source = tar.extractfile(member)
                        if source is None:
                            return [f"cannot read archive member: {member.name}"]
                        with dest.open("wb") as handle:
                            shutil.copyfileobj(source, handle, length=65536)
        except (OSError, tarfile.TarError) as exc:
            return [f"cannot read upload archive: {exc}"]

        uploads_dir = target / "uploads"
        if not uploads_dir.is_dir():
            return ["upload archive has no uploads/ directory"]
        valid, verify_report = verify_backup(backup_dir / "orders.db", uploads_dir)
        report["upload_archive_verified"] = valid
        if not valid:
            # A failed verification is fatal regardless of *where* the verifier
            # recorded the problem. Some failures (a missing source for an
            # unfinished delivery, a size or digest mismatch) are reported in
            # dedicated lists and leave `errors` empty — copying only `errors`
            # made the deep check pass on an archive that could not restore an
            # order, which is precisely what it exists to prevent.
            detail_keys = (
                "errors",
                "p1_missing_files",
                "p1_size_mismatches",
                "p1_checksum_mismatches",
                "p1_metadata_errors",
                "p1_completeness_errors",
                "p1_unexplained_missing_sources",
                "orphan_delivery_jobs",
            )
            details = []
            for key in detail_keys:
                items = verify_report.get(key) or []
                if items:
                    details.append(f"{key}: {len(items)} issue(s) {items[:3]}")
            if not details:
                details.append("verification reported invalid without detail")
            errors.append("upload archive verification failed — " + " | ".join(details))
    return errors


# ----------------------------------------------------------------------- quiescence
def check_quiescence(
    db_path: Path,
    *,
    worker_enabled: Optional[bool] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Report whether the database still has writers that could race a snapshot.

    A backup of the database is transactional by itself, but a *set* (database +
    uploads) is only consistent if no cleanup is deleting files while the archive
    is built. This reports the evidence instead of assuming it.
    """
    now = now or utc_now()
    db_path = Path(db_path)
    report: Dict[str, Any] = {
        "db_path": str(db_path),
        "checked_at": now.isoformat(),
        "worker_enabled": worker_enabled,
        "in_flight_jobs": 0,
        "processing_rows": 0,
        "cleanup_in_flight": 0,
        "quiescent": False,
        "reasons": [],
    }
    reasons: List[str] = report["reasons"]

    if worker_enabled:
        reasons.append("delivery worker is enabled")

    if not db_path.is_file():
        reasons.append(f"database not found: {db_path}")
        return False, report

    uri = readonly_uri(db_path)
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "delivery_jobs" in tables:
                processing = conn.execute(
                    "SELECT COUNT(*) FROM delivery_jobs WHERE state='processing'").fetchone()[0]
                report["processing_rows"] = processing
                live = conn.execute(
                    "SELECT COUNT(*) FROM delivery_jobs "
                    "WHERE state='processing' AND (lease_until IS NULL OR lease_until > ?)",
                    (now.isoformat(),)).fetchone()[0]
                report["in_flight_jobs"] = live
                if live:
                    reasons.append(f"{live} job(s) hold a live lease")
                cleanup = conn.execute(
                    "SELECT COUNT(*) FROM delivery_jobs "
                    "WHERE cleanup_phase='deleting' OR "
                    "(cleanup_state='processing' AND (cleanup_lease_until IS NULL OR cleanup_lease_until > ?))",
                    (now.isoformat(),)).fetchone()[0]
                report["cleanup_in_flight"] = cleanup
                if cleanup:
                    reasons.append(f"{cleanup} cleanup operation(s) in flight")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        reasons.append(f"cannot inspect database: {exc}")

    report["quiescent"] = not reasons
    return report["quiescent"], report


# ------------------------------------------------------------------------ retention
def plan_retention(
    entries: Sequence[Tuple[Path, datetime]],
    *,
    keep_last: int = 7,
    keep_daily: int = 14,
    keep_weekly: int = 8,
    now: Optional[datetime] = None,
) -> Tuple[List[Path], List[Path]]:
    """Return ``(keep, prune)`` for a list of ``(path, created_at)`` backup sets.

    The newest set is always kept, ``keep_last`` most recent sets are always kept,
    and nothing unreachable from the current time window is pruned unless it is
    outside every window. Deleting is the caller's job — this only plans.
    """
    now = now or utc_now()
    ordered = sorted(entries, key=lambda item: item[1], reverse=True)
    if not ordered:
        return [], []

    keep: List[Path] = []
    keep_set = set()

    def keep_entry(item):
        if item[0] not in keep_set:
            keep_set.add(item[0])
            keep.append(item[0])

    for item in ordered[:max(1, keep_last)]:
        keep_entry(item)

    daily_floor = now - timedelta(days=max(0, keep_daily))
    seen_days = set()
    for item in ordered:
        if item[1] < daily_floor:
            continue
        day = item[1].date()
        if day in seen_days:
            continue
        seen_days.add(day)
        keep_entry(item)

    weekly_floor = now - timedelta(weeks=max(0, keep_weekly))
    seen_weeks = set()
    for item in ordered:
        if item[1] < weekly_floor:
            continue
        iso = item[1].isocalendar()
        week = (iso[0], iso[1])
        if week in seen_weeks:
            continue
        seen_weeks.add(week)
        keep_entry(item)

    prune = [path for path, _ in ordered if path not in keep_set]
    return keep, prune
