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
                   evidence_format: int = 1) -> Dict[str, Any]:
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
    return {
        "format_version": FORMAT_VERSION,
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "artifacts": {name: artifacts[name] for name in MANIFEST_ARTIFACTS},
        "application_id": application_id,
        "evidence_format": evidence_format,
        "schema_digest": schema_digest,
        "logical_digest": logical_digest,
        "database_identity": database_identity,
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


def atomic_publish(staging: Path, final_dir: Path) -> Path:
    """Rename a complete set into place, without ever exposing a partial one."""
    staging, final_dir = Path(staging), Path(final_dir)
    if not staging.is_dir():
        raise FormatError(f"staging directory does not exist: {staging}")
    if not final_dir.parent.exists():
        final_dir.parent.mkdir(parents=True, exist_ok=True)
    previous = final_dir.with_name(final_dir.name + PREVIOUS_SUFFIX)
    moved = False
    if final_dir.exists():
        if previous.exists():
            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            os.replace(previous, previous.with_name(f"{previous.name}-{stamp}"))
        os.replace(final_dir, previous)
        moved = True
    try:
        os.replace(staging, final_dir)
    except OSError as exc:
        if moved and not final_dir.exists():
            os.replace(previous, final_dir)
        raise FormatError(f"failed to publish the backup set: {exc}") from exc
    return final_dir


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


def inspect_archive(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    """Validate the upload archive without extracting it."""
    info: Dict[str, Any] = {"members": 0, "files": 0, "directories": 0, "bytes": 0}
    errors: List[str] = []
    seen: set[str] = set()
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                info["members"] += 1
                if member.name in seen:
                    errors.append(f"duplicate archive member: {member.name!r}")
                seen.add(member.name)
                if member.issym() or member.islnk():
                    errors.append(f"archive contains a link member: {member.name!r}")
                    continue
                if Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                    errors.append(f"archive member escapes the root: {member.name!r}")
                    continue
                if member.isdir():
                    info["directories"] += 1
                    continue
                if not member.isreg():
                    errors.append(f"archive member is not a regular file: {member.name!r}")
                    continue
                info["files"] += 1
                info["bytes"] += member.size
    except (OSError, tarfile.TarError) as exc:
        errors.append(f"cannot read the upload archive: {exc}")
    return info, errors


def verify_set(directory: Path, *, expected_application_id: Optional[int] = None,
               require_verified: bool = True) -> Tuple[bool, Dict[str, Any]]:
    """Verify a backup set's structure, checksums and metadata binding."""
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
    if directory.name.startswith(TMP_PREFIX):
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

    if manifest.get("format_version") != FORMAT_VERSION:
        errors.append(
            f"unsupported manifest format_version {manifest.get('format_version')!r} "
            f"(this tool understands {FORMAT_VERSION})")
    if manifest.get("upload_archive_sha256") is not None:
        errors.append("manifest uses a superseded field name; rebuild the set")

    recorded = manifest.get("artifacts") or {}
    for name in MANIFEST_ARTIFACTS:
        if recorded.get(name) != actual[name]:
            errors.append(
                f"the manifest's recorded hash for {name} disagrees with the file: "
                f"manifest={recorded.get(name)!r} actual={actual[name]}")

    # The manifest is itself checksummed, so editing any field breaks manifest.sha256.
    if checksums.get(MANIFEST_JSON) != actual[MANIFEST_JSON]:
        errors.append("the manifest is not bound to manifest.sha256")

    for label in ("schema_digest", "logical_digest"):
        if not _is_hex64(manifest.get(label)):
            errors.append(f"manifest {label} is missing or not a sha256 digest")
    identity = manifest.get("database_identity") or {}
    if not isinstance(identity, dict) or "application_id" not in identity:
        errors.append("manifest database_identity is missing")
    if expected_application_id is not None:
        if manifest.get("application_id") != expected_application_id:
            errors.append(
                f"manifest application_id is {manifest.get('application_id')!r}, "
                f"expected {expected_application_id}")
        if identity.get("application_id") != expected_application_id:
            errors.append("manifest database_identity disagrees with application_id")
    if require_verified and manifest.get("verification") != "PASS":
        errors.append(
            f"manifest verification is {manifest.get('verification')!r}, expected PASS")

    try:
        created = datetime.fromisoformat(str(manifest.get("created_at")))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        report["checks"]["created_at"] = created.isoformat()
        report["created_at"] = created.isoformat()
    except (TypeError, ValueError):
        errors.append(f"manifest created_at is not ISO-8601: {manifest.get('created_at')!r}")

    archive_info, archive_errors = inspect_archive(directory / TAR_FILE)
    report["checks"]["archive"] = archive_info
    errors.extend(archive_errors)
    recorded_uploads = manifest.get("uploads") or {}
    if isinstance(recorded_uploads, dict) and recorded_uploads:
        if recorded_uploads.get("members") != archive_info["members"]:
            errors.append(
                "manifest upload member count disagrees with the archive: "
                f"{recorded_uploads.get('members')!r} vs {archive_info['members']}")

    report["ok"] = not errors
    return report["ok"], report


def age_hours(manifest_created_at: str, *, now: Optional[datetime] = None) -> float:
    now = now or utc_now()
    created = datetime.fromisoformat(manifest_created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() / 3600.0
