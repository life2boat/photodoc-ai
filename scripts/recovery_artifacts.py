#!/usr/bin/env python3
"""PhotoDoc AI — recovery-byte verification.

A backup set is only a rollback point if the archived bytes can actually rebuild
the files the database still points at. Parsing tar headers proves nothing about
content: a set with correct checksums can carry a substituted file whose name and
size match, and the photo it replaces is then unrecoverable.

This module therefore:

* streams every archive member and hashes it (no extraction, so no path traversal
  can touch the filesystem);
* derives the **required recovery sources** from the backup database itself —
  the delivery jobs that still need their local original;
* requires an exact SHA-256 and size match for each of them;
* refuses anything it cannot prove: missing metadata, ambiguous basenames,
  truncated payloads, duplicate members, links and non-regular members.

Path handling is POSIX-only and host-independent. A member named ``/tmp/evil`` or
``C:/evil`` is rejected on Linux and Windows alike, because ``Path.is_absolute()``
answers a different question on each platform.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import db_evidence  # noqa: E402
from verify_backup_integrity import is_valid_sha256, is_valid_size  # noqa: E402

#: The archive is laid out as ``uploads/<basename>``; restore places members by basename.
UPLOAD_ROOT = "uploads"

#: States whose local original is strictly required for recovery.
REQUIRING_STATES = ("pending", "processing", "failed")

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class RecoveryError(RuntimeError):
    """Raised when a recovery set cannot be produced or trusted."""


# ----------------------------------------------------------------------- limits
@dataclass(frozen=True)
class Limits:
    """Explicit bounds, so a hostile archive cannot exhaust memory or time."""

    max_members: int = 200_000
    max_member_bytes: int = 4 * 1024 ** 3
    max_total_bytes: int = 64 * 1024 ** 3
    max_path_bytes: int = 1024
    chunk: int = 1 << 20


DEFAULT_LIMITS = Limits()


class _LimitExceeded(Exception):
    pass


# ------------------------------------------------------------------ path policy
def path_errors(name: str, limits: Limits = DEFAULT_LIMITS) -> List[str]:
    """POSIX-only member-path policy, independent of the host platform."""
    errors: List[str] = []
    if not name:
        return ["archive member has an empty name"]
    if "\x00" in name:
        errors.append(f"archive member name contains a NUL byte: {name!r}")
    if len(name.encode("utf-8")) > limits.max_path_bytes:
        errors.append(f"archive member name is too long: {name[:64]!r}...")
    if name.startswith("/"):
        errors.append(f"archive member is an absolute path: {name!r}")
    if "\\" in name:
        errors.append(f"archive member uses a backslash separator: {name!r}")
    if _DRIVE_RE.match(name):
        errors.append(f"archive member starts with a drive letter: {name!r}")
    parts = name.split("/")
    for part in parts:
        if part in ("", "."):
            errors.append(f"archive member path is not normalised: {name!r}")
            break
        if part == "..":
            errors.append(f"archive member escapes the root: {name!r}")
            break
    if errors:
        return errors
    if parts[0] != UPLOAD_ROOT:
        errors.append(
            f"archive member is outside the {UPLOAD_ROOT}/ root: {name!r}")
    # Exactly the root directory entry is allowed; a *file* named `uploads` is not,
    # and is rejected in scan_archive.
    return errors


# ----------------------------------------------------------------- archive scan
def scan_archive(archive_path: Path, limits: Limits = DEFAULT_LIMITS
                 ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], List[str]]:
    """Stream every member: validate its path, hash its bytes, enforce limits."""
    members: Dict[str, Dict[str, Any]] = {}
    info: Dict[str, Any] = {"members": 0, "files": 0, "directories": 0,
                            "bytes": 0, "hashed_bytes": 0}
    errors: List[str] = []
    seen: Dict[str, str] = {}

    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar:
                info["members"] += 1
                if info["members"] > limits.max_members:
                    errors.append(
                        f"archive has more than {limits.max_members} members; refusing "
                        "to continue")
                    break
                name = member.name
                policy = path_errors(name, limits)
                errors.extend(policy)
                folded = name.casefold()
                if folded in seen:
                    errors.append(f"duplicate archive member (case-insensitive): {name!r}")
                    continue
                seen[folded] = name

                if member.isdir():
                    info["directories"] += 1
                    continue
                if name == UPLOAD_ROOT:
                    errors.append("archive member names the uploads root but is a file")
                    continue
                if member.issym() or member.islnk():
                    errors.append(f"archive contains a link member: {name!r}")
                    continue
                if not member.isreg():
                    errors.append(f"archive member is not a regular file: {name!r}")
                    continue
                if member.size > limits.max_member_bytes:
                    errors.append(
                        f"archive member exceeds the {limits.max_member_bytes}-byte limit: "
                        f"{name!r}")
                    continue
                info["bytes"] += member.size
                if info["bytes"] > limits.max_total_bytes:
                    errors.append(
                        f"archive expands to more than {limits.max_total_bytes} bytes; "
                        "refusing to continue")
                    break

                digest = hashlib.sha256()
                read = 0
                handle = tar.extractfile(member)
                if handle is None:
                    errors.append(f"archive member cannot be read: {name!r}")
                    continue
                try:
                    for block in iter(lambda: handle.read(limits.chunk), b""):
                        read += len(block)
                        if read > member.size:
                            raise _LimitExceeded(name)
                        digest.update(block)
                except _LimitExceeded:
                    errors.append(f"archive member is longer than declared: {name!r}")
                    continue
                except (OSError, tarfile.TarError, EOFError) as exc:
                    # A gzip stream that ends early surfaces here, not as a short read.
                    errors.append(
                        f"archive member is truncated or corrupt: {name!r} ({exc})")
                    continue
                if read != member.size:
                    errors.append(
                        f"archive member is truncated: {name!r} declares {member.size} "
                        f"bytes but yields {read}")
                    continue
                info["files"] += 1
                info["hashed_bytes"] += read
                members[name] = {"size": member.size, "sha256": digest.hexdigest()}
    except (OSError, tarfile.TarError, EOFError) as exc:
        errors.append(f"cannot read the upload archive: {exc}")
    return members, info, errors


# --------------------------------------------------------------- required sources
def required_sources(db_path: Path) -> Tuple[Dict[str, Dict[str, Any]], List[str], Dict[str, int]]:
    """Delivery jobs whose local original must be present and byte-identical."""
    required: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    stats = {"jobs": 0, "required": 0, "cleaned": 0, "exempt": 0}

    try:
        conn = db_evidence.open_readonly(Path(db_path))
    except sqlite3.Error as exc:
        return {}, [f"cannot open the backup database for recovery checks: {exc}"], stats

    try:
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "delivery_jobs" not in tables:
            return {}, [], stats
        rows = conn.execute(
            "SELECT id, order_id, kind, state, local_path, content_sha256, content_size, "
            "cleanup_state, cleanup_phase, local_deleted_at FROM delivery_jobs").fetchall()
    except sqlite3.Error as exc:
        return {}, [f"cannot read delivery jobs from the backup database: {exc}"], stats
    finally:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        conn.close()

    for (job_id, order_id, kind, state, local_path, sha, size,
         cleanup_state, cleanup_phase, deleted_at) in rows:
        stats["jobs"] += 1
        local_path = local_path or ""
        cleaned = (cleanup_state == "completed"
                   and cleanup_phase in ("deleted", "deleting")
                   and deleted_at not in (None, ""))
        if not local_path:
            if state in REQUIRING_STATES:
                errors.append(
                    f"delivery job {job_id!r} (order {order_id}) is {state} but records no "
                    "local_path, so its recovery original cannot be identified")
            stats["exempt"] += 1
            continue
        if cleaned:
            stats["cleaned"] += 1
            continue
        if state not in REQUIRING_STATES and state != "delivered":
            # An unknown state is not evidence of "no source needed".
            errors.append(
                f"delivery job {job_id!r} has unrecognised state {state!r}; refusing to "
                "guess whether its local original is required")
            stats["exempt"] += 1
            continue
        if not is_valid_sha256(sha):
            errors.append(
                f"delivery job {job_id!r} has no valid content_sha256, so its recovery "
                "original cannot be verified")
            stats["exempt"] += 1
            continue
        if not is_valid_size(size):
            errors.append(
                f"delivery job {job_id!r} has no valid content_size, so its recovery "
                "original cannot be verified")
            stats["exempt"] += 1
            continue
        basename = Path(local_path.replace("\\", "/")).name
        if not basename or basename in (".", ".."):
            errors.append(f"delivery job {job_id!r} has an unusable local_path: {local_path!r}")
            stats["exempt"] += 1
            continue
        existing = required.get(basename)
        if existing is not None:
            if existing["sha256"] != sha.lower() or existing["size"] != size:
                errors.append(
                    f"two delivery jobs require different bytes for the same filename "
                    f"{basename!r}; the archive layout cannot satisfy both")
            continue
        required[basename] = {"sha256": sha.lower(), "size": size,
                              "job_id": job_id, "order_id": order_id, "kind": kind,
                              "state": state}
        stats["required"] += 1
    return required, errors, stats


# ------------------------------------------------------------------------ verify
def verify_recovery(db_path: Path, archive_path: Path,
                    limits: Limits = DEFAULT_LIMITS) -> Tuple[bool, Dict[str, Any]]:
    """Prove the archive can rebuild every file the backup database still needs."""
    report: Dict[str, Any] = {
        "ok": False,
        "errors": [],
        "archive": {},
        "required": {},
        "missing": [],
        "mismatched": [],
    }
    errors: List[str] = report["errors"]

    members, info, scan_errors = scan_archive(Path(archive_path), limits)
    report["archive"] = info
    errors.extend(scan_errors)

    required, source_errors, stats = required_sources(Path(db_path))
    errors.extend(source_errors)
    report["required"] = {"count": len(required), **stats}

    by_basename: Dict[str, List[str]] = {}
    for name in members:
        by_basename.setdefault(Path(name).name, []).append(name)

    for basename, spec in sorted(required.items()):
        candidates = by_basename.get(basename, [])
        if not candidates:
            report["missing"].append(basename)
            errors.append(
                f"required recovery original is missing from the archive: {basename!r} "
                f"(job {spec['job_id']!r}, order {spec['order_id']}, {spec['state']})")
            continue
        if len(candidates) > 1:
            errors.append(
                f"required recovery original {basename!r} appears more than once in the "
                "archive; the restore target is ambiguous")
            continue
        actual = members[candidates[0]]
        if actual["sha256"] != spec["sha256"]:
            report["mismatched"].append(
                {"basename": basename, "expected": spec["sha256"], "actual": actual["sha256"]})
            errors.append(
                f"recovery original {basename!r} has the wrong content: archive sha256 "
                f"{actual['sha256']} does not match the database's {spec['sha256']}")
        elif actual["size"] != spec["size"]:
            errors.append(
                f"recovery original {basename!r} has the wrong size: archive "
                f"{actual['size']} bytes, database records {spec['size']}")

    report["ok"] = not errors
    return report["ok"], report
