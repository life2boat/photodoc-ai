#!/usr/bin/env python3
"""PhotoDoc AI — immutable backup-set format.

A backup set is four files:

    orders.db              per-table dump, produced inside one SQLite transaction
    uploads.tar.gz         the recovery originals
    backup_manifest.json   metadata, including every artifact hash and the evidence digests
    manifest.sha256        checksums covering the **three** files above

The previous format left `backup_manifest.json` unbound: the timestamp, the recorded
verification result and the database hash could all be edited without touching either
artifact checksum. Every field below is therefore bound to the set:

* `manifest.sha256` covers `orders.db`, `uploads.tar.gz` and `backup_manifest.json`;
* the manifest repeats the artifact hashes, and a disagreement between the manifest and
  the checksum file is fatal (never a warning);
* the manifest records its own format version, so a future change is explicit;
* verification re-reads every artifact and refuses on any mismatch, extra file,
  symlink or unsafe archive member.

A plain hash cannot authenticate a malicious root: root-owned storage remains a trust
boundary, and the manifest says so in `trust_note`.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SCRIPT_DIR))

import db_evidence  # noqa: E402
import recovery_artifacts  # noqa: E402

FORMAT_VERSION = 2
MANIFEST_FILE = "manifest.sha256"
MANIFEST_JSON = "backup_manifest.json"
DB_FILE = "orders.db"
TAR_FILE = "uploads.tar.gz"
#: Files whose hashes are recorded in ``manifest.sha256``.
CHECKSUMMED = (DB_FILE, TAR_FILE, MANIFEST_JSON)
#: Files whose hashes the manifest itself records. The manifest cannot contain its
#: own hash, so ``manifest.sha256`` is what binds it to the set.
MANIFEST_ARTIFACTS = (DB_FILE, TAR_FILE)
REQUIRED_FILES = CHECKSUMMED + (MANIFEST_FILE,)

TMP_PREFIX = ".tmp-"
PREVIOUS_SUFFIX = ".previous"
PARTIAL_MARKER = ".incomplete"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
EVIDENCE_FORMAT_VERSION = db_evidence.EVIDENCE_FORMAT_VERSION

#: A set identifier must be a UUID or 32 hex characters. Anything else — including a
#: non-empty arbitrary string, which an earlier boolean expression let through — is
#: refused, because the identifier is what a journal or directory binding refers to.
SET_ID_RE = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                       r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
ALLOWED_UPLOAD_KEYS = {"members", "uncompressed_bytes"}

#: Top-level manifest keys this version understands. An unknown key means the set
#: was produced by a different contract, which must not be interpreted silently.
ALLOWED_MANIFEST_KEYS = {
    "format_version", "set_id", "created_at", "artifacts", "application_id",
    "evidence_format", "schema_digest", "logical_digest", "database_identity",
    "uploads", "verification", "trust_note",
}
ALLOWED_IDENTITY_KEYS = {"application_id", "schema_digest", "logical_digest",
                         "sqlite_version"}

TRUST_NOTE = (
    "Hashes bind the set to itself. They do not authenticate the producer: storage "
    "writable by root remains a trust boundary.")


class FormatError(RuntimeError):
    """Raised when a backup set violates the format contract."""


# ------------------------------------------------------------------------- utilities
def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


# --------------------------------------------------------------------------- writing
def build_manifest(*, artifacts: Dict[str, str], created_at: datetime,
                   application_id: int, schema_digest: str, logical_digest: str,
                   database_identity: Dict[str, Any],
                   upload_members: int, upload_bytes: int,
                   verification: str = "PASS",
                   evidence_format: int = EVIDENCE_FORMAT_VERSION,
                   set_id: Optional[str] = None) -> Dict[str, Any]:
    """Assemble the metadata document for a verified set."""
    for name in MANIFEST_ARTIFACTS:
        if not _is_hex64(artifacts.get(name)):
            raise FormatError(f"artifact hash for {name!r} is not a sha256 digest")
    for label, value in (("schema_digest", schema_digest),
                         ("logical_digest", logical_digest)):
        if not _is_hex64(value):
            raise FormatError(f"{label} is not a sha256 digest")
    if verification not in ("PASS", "SKIPPED"):
        raise FormatError(f"verification must be PASS or SKIPPED, got {verification!r}")
    if evidence_format != EVIDENCE_FORMAT_VERSION:
        raise FormatError(
            f"evidence_format must be {EVIDENCE_FORMAT_VERSION}, got {evidence_format!r}")
    if not isinstance(upload_members, int) or upload_members < 0:
        raise FormatError("upload_members must be a non-negative integer")
    if not isinstance(upload_bytes, int) or upload_bytes < 0:
        raise FormatError("upload_bytes must be a non-negative integer")
    identity = dict(database_identity)
    identity.setdefault("application_id", application_id)
    identity.setdefault("schema_digest", schema_digest)
    identity.setdefault("logical_digest", logical_digest)
    unexpected = set(identity) - ALLOWED_IDENTITY_KEYS
    if unexpected:
        raise FormatError(f"database_identity has unknown keys: {sorted(unexpected)}")
    return {
        "format_version": FORMAT_VERSION,
        "set_id": set_id or uuid.uuid4().hex,
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "artifacts": {name: artifacts[name] for name in MANIFEST_ARTIFACTS},
        "application_id": application_id,
        "evidence_format": evidence_format,
        "schema_digest": schema_digest,
        "logical_digest": logical_digest,
        "database_identity": identity,
        "uploads": {"members": upload_members, "uncompressed_bytes": upload_bytes},
        "verification": verification,
        "trust_note": TRUST_NOTE,
    }


def write_manifest_files(directory: Path, manifest: Dict[str, Any]) -> None:
    """Write ``backup_manifest.json`` then ``manifest.sha256`` covering all three."""
    directory = Path(directory)
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    (directory / MANIFEST_JSON).write_bytes(payload)
    lines = []
    for name in CHECKSUMMED:
        lines.append(f"{sha256_file(directory / name)}  {name}")
    (directory / MANIFEST_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def staging_dir_for(final_dir: Path) -> Path:
    final_dir = Path(final_dir)
    return final_dir.parent / f"{TMP_PREFIX}{final_dir.name}-{uuid.uuid4().hex[:12]}"


#: errno values that mean "this platform cannot flush this kind of handle", as opposed
#: to an I/O failure that must abort publication.
_FSYNC_UNSUPPORTED_ERRNOS = {errno.EINVAL, errno.ENOSYS, errno.EBADF}
if hasattr(errno, "ENOTSUP"):
    _FSYNC_UNSUPPORTED_ERRNOS.add(errno.ENOTSUP)
if hasattr(errno, "EOPNOTSUPP"):
    _FSYNC_UNSUPPORTED_ERRNOS.add(errno.EOPNOTSUPP)
if hasattr(errno, "EPERM"):
    _FSYNC_UNSUPPORTED_ERRNOS.add(errno.EPERM)


def fsync_file(path: Path) -> str:
    """Flush a file. Returns ``ok`` | ``unsupported``; raises on a real I/O failure.

    A durability barrier that silently degrades is worse than none: on a platform that
    supports ``fsync`` a failure must abort before the rename, so the caller never
    publishes a set it could not flush. Only a genuine "cannot flush this handle here"
    is reported as ``unsupported``.
    """
    binary = getattr(os, "O_BINARY", 0)
    last: OSError | None = None
    for flags in (os.O_RDWR | binary, os.O_RDONLY | binary):
        try:
            handle = os.open(str(path), flags)
        except OSError as exc:
            last = exc
            continue
        try:
            os.fsync(handle)
            return "ok"
        except OSError as exc:
            last = exc
            if exc.errno in _FSYNC_UNSUPPORTED_ERRNOS:
                continue
            raise FormatError(f"cannot flush {path.name} to stable storage: {exc}") from exc
        finally:
            os.close(handle)
    if last is not None and last.errno not in _FSYNC_UNSUPPORTED_ERRNOS:
        raise FormatError(f"cannot open {path.name} to flush it: {last}")
    return "unsupported"


def fsync_path(path: Path) -> bool:
    """Best-effort directory flush: a filesystem may not support flushing directories."""
    try:
        return fsync_file(Path(path)) == "ok"
    except FormatError:
        return False


def publish_set(staging: Path, final_dir: Path) -> Dict[str, Any]:
    """Publish a complete set under a never-reused identifier.

    Differences from a plain rename, each one closing a review finding:

    * the destination must not exist — a set ID identifies one set forever, so a
      second publication never overwrites the first;
    * every file is fsynced, and the parent directory where the platform allows it,
      so a crash cannot leave a published set with unflushed contents;
    * a crash mid-publication cannot destroy the destination, because the
      destination is never moved away.

    A leftover ``.tmp-*`` directory is never a set: it is skipped by discovery and
    reported as an incomplete staging directory, so it cannot be mistaken for a
    rollback point.
    """
    staging, final_dir = Path(staging), Path(final_dir)
    report: Dict[str, Any] = {"staging": str(staging), "final": str(final_dir),
                              "fsynced_files": 0, "fsync_directory": False}
    if not staging.is_dir():
        raise FormatError(f"staging directory does not exist: {staging}")
    if staging.is_symlink():
        raise FormatError("staging directory is a symlink")
    if final_dir.exists() or final_dir.is_symlink():
        raise FormatError(
            f"refusing to publish over an existing set: {final_dir.name}. A set ID "
            "identifies one set forever; choose a new name.")
    missing = [name for name in REQUIRED_FILES if not (staging / name).is_file()]
    if missing:
        raise FormatError(f"staging set is incomplete, missing: {missing}")

    final_dir.parent.mkdir(parents=True, exist_ok=True)
    # File data must reach stable storage before the rename is visible. A failure here
    # aborts publication; only an unsupported platform is tolerated, and it is recorded.
    statuses = []
    for name in REQUIRED_FILES:
        status = fsync_file(staging / name)
        statuses.append(status)
        if status == "ok":
            report["fsynced_files"] += 1
    report["fsync_supported"] = all(status == "ok" for status in statuses)
    report["fsync_directory"] = fsync_path(staging)

    try:
        os.replace(staging, final_dir)
    except OSError as exc:
        raise FormatError(f"failed to publish the backup set: {exc}") from exc

    # Flush the parent so the rename itself survives a crash where supported.
    report["fsync_parent"] = fsync_path(final_dir.parent)
    return report


# ------------------------------------------------------------------------ verification
def _read_checksums(path: Path) -> Tuple[Dict[str, str], List[str]]:
    errors: List[str] = []
    checksums: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2:
            errors.append(f"malformed checksum line: {line!r}")
            continue
        digest, name = parts
        name = name.lstrip("*")
        if not _is_hex64(digest):
            errors.append(f"checksum is not a sha256 digest: {line!r}")
        if Path(name).name != name:
            errors.append(f"checksum names a path, not a bare file: {name!r}")
            continue
        if name in checksums:
            errors.append(f"duplicate checksum entry for {name!r}")
        checksums[name] = digest
    return checksums, errors


def inspect_archive(path: Path,
                   limits: "recovery_artifacts.Limits" = recovery_artifacts.DEFAULT_LIMITS
                   ) -> Tuple[Dict[str, Any], List[str]]:
    """Stream the archive: validate member paths, hash the bytes, enforce limits.

    Header parsing alone is not evidence — a checksum-consistent archive can carry
    substituted content — so this reads every member even when only structure is
    reported.
    """
    members, info, errors = recovery_artifacts.scan_archive(Path(path), limits)
    info = dict(info)
    info["hashed_members"] = len(members)
    return info, errors


#: Fields the legacy format-1 manifest used instead of ``artifacts``.
LEGACY_HASH_KEYS = ("orders_db_sha256", "uploads_tar_sha256")


def detect_format(directory: Path) -> str:
    """``v2`` | ``legacy-v1`` | ``unknown``.

    A legacy set is never interpreted as v2: the v2 contract binds the manifest to
    the database state and to the recovery bytes, and a v1 manifest cannot express
    either claim.
    """
    directory = Path(directory)
    manifest_path = directory / MANIFEST_JSON
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "unknown"
        if isinstance(manifest, dict):
            if manifest.get("format_version") == FORMAT_VERSION:
                return "v2"
            if any(key in manifest for key in LEGACY_HASH_KEYS):
                return "legacy-v1"
    if (directory / MANIFEST_FILE).is_file() and (directory / DB_FILE).is_file():
        return "legacy-v1"
    return "unknown"


def verify_set(directory: Path, *, expected_application_id: Optional[int] = None,
               require_verified: bool = True, deep: bool = True,
               allow_staging: bool = False, legacy_p0_policy: str = "fail",
               limits: "recovery_artifacts.Limits" = recovery_artifacts.DEFAULT_LIMITS
               ) -> Tuple[bool, Dict[str, Any]]:
    """Verify a backup set's structure, checksums, metadata and recovery bytes."""
    directory = Path(directory)
    report: Dict[str, Any] = {
        "directory": str(directory),
        "format_version": FORMAT_VERSION,
        "ok": False,
        "errors": [],
        "checks": {},
    }
    errors: List[str] = report["errors"]

    if not directory.is_dir():
        errors.append(f"backup directory does not exist: {directory}")
        return False, report
    if directory.is_symlink():
        errors.append("backup directory is a symlink")
        return False, report
    if directory.name.startswith(TMP_PREFIX) and not allow_staging:
        errors.append("backup directory is an unpublished staging directory")
        return False, report
    if (directory / PARTIAL_MARKER).exists():
        errors.append("backup directory carries an incomplete marker")

    present = {entry.name for entry in directory.iterdir()}
    for name in REQUIRED_FILES:
        path = directory / name
        if not path.is_file() or path.is_symlink():
            errors.append(f"required file missing, or not a regular file: {name}")
    unexpected = sorted(present - set(REQUIRED_FILES) - {PARTIAL_MARKER})
    if unexpected:
        errors.append(f"unexpected files in the backup set: {unexpected}")
    if errors:
        return False, report

    checksums, checksum_errors = _read_checksums(directory / MANIFEST_FILE)
    errors.extend(checksum_errors)
    for name in CHECKSUMMED:
        if name not in checksums:
            errors.append(f"{MANIFEST_FILE} does not cover {name}")
    actual = {name: sha256_file(directory / name) for name in CHECKSUMMED}
    report["checks"]["artifacts"] = actual
    for name, digest in checksums.items():
        if name in actual and actual[name] != digest:
            errors.append(f"checksum mismatch for {name}")

    try:
        manifest = json.loads((directory / MANIFEST_JSON).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {MANIFEST_JSON}: {exc}")
        return False, report
    report["checks"]["manifest"] = manifest
    if not isinstance(manifest, dict):
        errors.append("the manifest is not a JSON object")
        return False, report

    # M1: every claim is validated, not merely checksum-bound. A hash proves the
    # manifest was not edited; it says nothing about whether each field is true.
    unknown = set(manifest) - ALLOWED_MANIFEST_KEYS
    if unknown:
        errors.append(f"the manifest has unknown fields: {sorted(unknown)}")
    if manifest.get("format_version") != FORMAT_VERSION:
        errors.append(
            f"unsupported manifest format_version {manifest.get('format_version')!r} "
            f"(this tool understands {FORMAT_VERSION})")
    if manifest.get("evidence_format") != EVIDENCE_FORMAT_VERSION:
        errors.append(
            f"unsupported evidence_format {manifest.get('evidence_format')!r} "
            f"(this tool understands {EVIDENCE_FORMAT_VERSION})")
    set_id = manifest.get("set_id")
    if not isinstance(set_id, str) or not SET_ID_RE.match(set_id):
        errors.append(
            f"the manifest set_id is missing or malformed ({set_id!r}); expected a UUID or "
            "32 hex characters")
    if manifest.get("upload_archive_sha256") is not None:
        errors.append("manifest uses a superseded field name; rebuild the set")
    if manifest.get("trust_note") != TRUST_NOTE:
        errors.append(
            "the manifest trust_note is not the current text: it describes a boundary the "
            "verifier does not rely on, so a modified note is refused")

    recorded = manifest.get("artifacts") or {}
    if not isinstance(recorded, dict):
        errors.append("manifest artifacts is not an object")
        recorded = {}
    for name in MANIFEST_ARTIFACTS:
        if recorded.get(name) != actual[name]:
            errors.append(
                f"the manifest's recorded hash for {name} disagrees with the file: "
                f"manifest={recorded.get(name)!r} actual={actual[name]}")

    if checksums.get(MANIFEST_JSON) != actual[MANIFEST_JSON]:
        errors.append("the manifest is not bound to manifest.sha256")

    for label in ("schema_digest", "logical_digest"):
        if not _is_hex64(manifest.get(label)):
            errors.append(f"manifest {label} is missing or not a sha256 digest")
    identity = manifest.get("database_identity")
    if not isinstance(identity, dict):
        errors.append("manifest database_identity is missing or not an object")
        identity = {}
    unknown_identity = set(identity) - ALLOWED_IDENTITY_KEYS
    if unknown_identity:
        errors.append(f"database_identity has unknown fields: {sorted(unknown_identity)}")
    for required_key in ("application_id", "schema_digest"):
        if required_key not in identity:
            errors.append(f"database_identity is missing {required_key}")
    if expected_application_id is not None:
        if manifest.get("application_id") != expected_application_id:
            errors.append(
                f"manifest application_id is {manifest.get('application_id')!r}, "
                f"expected {expected_application_id}")
        if identity.get("application_id") != expected_application_id:
            errors.append("manifest database_identity disagrees with application_id")

    try:
        created = datetime.fromisoformat(str(manifest.get("created_at")))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        report["checks"]["created_at"] = created.isoformat()
        report["created_at"] = created.isoformat()
    except (TypeError, ValueError):
        errors.append(f"manifest created_at is not ISO-8601: {manifest.get('created_at')!r}")

    archive_info, archive_errors = inspect_archive(directory / TAR_FILE, limits)
    report["checks"]["archive"] = archive_info
    errors.extend(archive_errors)
    recorded_uploads = manifest.get("uploads")
    if not isinstance(recorded_uploads, dict):
        errors.append("manifest uploads is not an object")
        recorded_uploads = {}
    else:
        unknown_uploads = set(recorded_uploads) - ALLOWED_UPLOAD_KEYS
        if unknown_uploads:
            errors.append(f"manifest uploads has unknown fields: {sorted(unknown_uploads)}")
    if recorded_uploads.get("members") != archive_info["members"]:
        errors.append(
            "manifest upload member count disagrees with the archive: "
            f"{recorded_uploads.get('members')!r} vs {archive_info['members']}")
    if recorded_uploads.get("uncompressed_bytes") != archive_info["bytes"]:
        errors.append(
            "manifest upload byte count disagrees with the archive: "
            f"{recorded_uploads.get('uncompressed_bytes')!r} vs {archive_info['bytes']}")

    database_ok = False
    if (directory / DB_FILE).is_file():
        database_ok = True
        recomputed = _recompute_evidence(directory / DB_FILE, errors)
        if recomputed is not None:
            report["checks"]["evidence"] = recomputed
            if manifest.get("schema_digest") != recomputed.get("schema_digest"):
                errors.append(
                    "manifest schema_digest does not match the backup database: the set "
                    "was not produced from this state")
            if manifest.get("logical_digest") != recomputed.get("logical_digest"):
                errors.append(
                    "manifest logical_digest does not match the backup database: the set "
                    "was not produced from this state")
            if expected_application_id is not None:
                if recomputed.get("application_id") != expected_application_id:
                    errors.append(
                        "the backup database application_id does not match the expected "
                        "application")
            for label, expected_value in (("schema_digest", recomputed.get("schema_digest")),
                                          ("logical_digest", recomputed.get("logical_digest"))):
                recorded_value = identity.get(label)
                if recorded_value is None:
                    errors.append(
                        f"database_identity.{label} is NULL; every recorded claim must be "
                        "compared, so a missing value is a refusal")
                elif recorded_value != expected_value:
                    errors.append(
                        f"database_identity.{label} disagrees with the database: "
                        f"manifest={recorded_value} actual={expected_value}")

    # H3: the archive must be able to rebuild every file the backup database needs.
    if deep and database_ok:
        recovery_ok, recovery = recovery_artifacts.verify_recovery(
            directory / DB_FILE, directory / TAR_FILE, limits,
            legacy_p0_policy=legacy_p0_policy)
        report["checks"]["recovery"] = recovery
        if not recovery_ok:
            errors.extend(recovery.get("errors", []))
    elif deep and not database_ok:
        errors.append("cannot deep-verify recovery files without the backup database")

    if require_verified and manifest.get("verification") != "PASS":
        errors.append(
            f"manifest verification is {manifest.get('verification')!r}, expected PASS")

    report["ok"] = not errors
    return report["ok"], report


def _recompute_evidence(db_path: Path, errors: List[str]) -> Optional[Dict[str, Any]]:
    """Recompute the digests a manifest claims, so a claim can be checked."""
    try:
        conn = db_evidence.open_readonly(Path(db_path))
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        errors.append(f"cannot open the backup database: {exc}")
        return None
    try:
        conn.execute("BEGIN")
        schema = db_evidence.canonical_schema(conn)
        application_id = conn.execute("PRAGMA application_id").fetchone()[0]
        logical = _logical_digest_of(conn)
        return {"schema_digest": schema["digest"], "application_id": application_id,
                "logical_digest": logical,
                "sqlite_version": ".".join(str(p) for p in __import__("sqlite3").sqlite_version_info)}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cannot compute evidence for the backup database: {exc}")
        return None
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        conn.close()


def _logical_digest_of(conn) -> str:
    """Delegate to the single evidence implementation."""
    return db_evidence.logical_state_digest(conn)


def age_hours(manifest_created_at: str, *, now: Optional[datetime] = None) -> float:
    now = now or utc_now()
    created = datetime.fromisoformat(manifest_created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() / 3600.0


def atomic_publish(staging: Path, final_dir: Path) -> Path:
    """Deprecated alias for :func:`publish_set`.

    Kept only so an outdated caller fails loudly instead of quietly overwriting a
    published set: the old semantics (replace an existing directory) are exactly
    what the review called a durability and immutability defect.
    """
    publish_set(staging, final_dir)
    return Path(final_dir)
