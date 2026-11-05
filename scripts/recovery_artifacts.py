#!/usr/bin/env python3
"""PhotoDoc AI — recovery-byte verification.

A backup set is only a rollback point if the archived bytes can rebuild the files the
database still needs, **at the paths the database will look for them**. This module
implements `docs/RECOVERY_CONTRACT.md`; the producer and the verifier call the same
functions, so neither can accept a set the other would not build.

Design rules, each one closing a demonstrated false PASS:

* required files come from persisted state, never from "what happens to be in the
  archive" — and never from "there is no delivery table, so there are no files";
* **state wins over cleanup metadata**: a pending/processing/failed job always requires
  its original, and contradictory cleanup flags are a refusal rather than an exemption;
* matching is by **exact member path** (``uploads/<basename>``), so correct bytes at a
  nested path do not satisfy a reference;
* the archived file set must **equal** the derived set: unneeded customer files are
  refused, not silently retained;
* the gzip stream is read to physical EOF so a truncated trailer cannot pass;
* a customer original is never "reconstructed" — if it is required and absent, the set
  is refused.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import db_evidence  # noqa: E402
from verify_backup_integrity import is_valid_sha256, is_valid_size  # noqa: E402

#: The single archive root. Restore places members under the uploads directory.
UPLOAD_ROOT = "uploads"

#: Job kinds that own a local upload file.
UPLOAD_BACKED_KINDS = ("file", "info")
#: Job kinds whose state lives in the database / paid-email outbox only.
DATABASE_KINDS = ("admin_email", "paid_email")

UNFINISHED_STATES = ("pending", "processing", "failed")
KNOWN_STATES = UNFINISHED_STATES + ("delivered",)
KNOWN_CLEANUP_STATES = ("pending", "processing", "completed", "failed", "not_applicable")
KNOWN_CLEANUP_PHASES = ("none", "pending", "deleting", "deleted", "failed")

#: Legacy P0 policies. ``include`` is opt-in because the stored filename is ambiguous;
#: there is no ``exclude``, because for a backup that means discarding the only copy.
P0_POLICIES = ("fail", "include")

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class RecoveryError(RuntimeError):
    """Raised when a recovery set cannot be produced."""


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
    """POSIX-only member-path policy, independent of the host platform.

    A file member must be exactly ``uploads/<name>``. Nested paths are refused because a
    restore preserves them, and the database looks for the file directly in its uploads
    directory — the right bytes at the wrong path are a missing file.
    """
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
        errors.append(f"archive member is outside the {UPLOAD_ROOT}/ root: {name!r}")
    elif len(parts) > 2:
        errors.append(
            f"archive member is nested below the uploads root: {name!r}; the restore path "
            "would not match the database's uploads directory")
    return errors


# ----------------------------------------------------------------- archive scan
def validate_gzip_trailer(archive_path: Path, limits: Limits = DEFAULT_LIMITS) -> List[str]:
    """Read the gzip stream to physical EOF, validating CRC and length.

    ``tarfile`` stops at the end-of-archive marker and can accept a stream whose gzip
    trailer was truncated; the bytes would still decompress locally, but the set is not a
    valid archive elsewhere.
    """
    errors: List[str] = []
    total = 0
    try:
        with gzip.open(archive_path, "rb") as handle:
            while True:
                block = handle.read(limits.chunk)
                if not block:
                    break
                total += len(block)
                if total > limits.max_total_bytes:
                    errors.append(
                        f"archive expands to more than {limits.max_total_bytes} bytes; "
                        "refusing to continue")
                    break
    except (OSError, EOFError) as exc:
        errors.append(
            f"the upload archive is not a complete gzip stream (truncated or corrupt): {exc}")
    return errors


def scan_archive(archive_path: Path, limits: Limits = DEFAULT_LIMITS
                 ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], List[str]]:
    """Stream every member: validate its path, hash its bytes, enforce limits."""
    members: Dict[str, Dict[str, Any]] = {}
    info: Dict[str, Any] = {"members": 0, "files": 0, "directories": 0,
                            "bytes": 0, "hashed_bytes": 0}
    errors: List[str] = validate_gzip_trailer(Path(archive_path), limits)
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
                errors.extend(path_errors(name, limits))
                folded = name.casefold()
                if folded in seen:
                    errors.append(f"duplicate archive member (case-insensitive): {name!r}")
                    continue
                seen[folded] = name

                if member.isdir():
                    info["directories"] += 1
                    continue
                if member.issym() or member.islnk():
                    errors.append(f"archive contains a link member: {name!r}")
                    continue
                if not member.isreg():
                    errors.append(f"archive member is not a regular file: {name!r}")
                    continue
                if name == UPLOAD_ROOT:
                    errors.append("archive member names the uploads root but is a file")
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


# ------------------------------------------------------------------- generations
def schema_generation(conn: sqlite3.Connection) -> str:
    """``P1``, ``P0`` or ``mixed`` — never "unknown, assume no files"."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    has_jobs = "delivery_jobs" in tables
    order_columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
    has_token = "order_token_hash" in order_columns
    if has_jobs and has_token:
        return "P1"
    if not has_jobs:
        return "P0"
    return "mixed"


def _p0_candidates(order_id: Any, raw_name: str) -> List[str]:
    """Stored names a legacy original may have (mirrors create_backup.py)."""
    from verify_backup_integrity import sanitize_filename

    name = (raw_name or "").strip()
    if not name:
        return []
    safe = sanitize_filename(name)
    members: List[str] = []
    for candidate in (f"{order_id}_{safe}", f"{order_id}_{name}", safe, name):
        member = f"{UPLOAD_ROOT}/{candidate}"
        if member not in members:
            members.append(member)
    return members


# --------------------------------------------------------------- required sources
def _cleanup_exemption(cleanup_state: str, cleanup_phase: str,
                       local_deleted_at: Optional[str], delivered_at: Optional[str]
                       ) -> Tuple[bool, List[str]]:
    """Decide whether a delivered job's original may be absent. ``(exempt, errors)``."""
    errors: List[str] = []
    if cleanup_state not in KNOWN_CLEANUP_STATES:
        errors.append(f"unknown cleanup_state {cleanup_state!r}")
        return False, errors
    if cleanup_phase not in KNOWN_CLEANUP_PHASES:
        errors.append(f"unknown cleanup_phase {cleanup_phase!r}")
        return False, errors
    if cleanup_state == "not_applicable":
        errors.append(
            "cleanup_state='not_applicable' is only valid for email jobs; a file-backed "
            "job always has a local original")
        return False, errors
    if cleanup_state == "completed":
        if cleanup_phase not in ("deleted", "deleting"):
            errors.append(
                f"cleanup_state='completed' with cleanup_phase={cleanup_phase!r}: the phase "
                "does not record a deletion")
            return False, errors
        if not local_deleted_at:
            errors.append(
                "cleanup_state='completed' without local_deleted_at: the deletion is not "
                "recorded")
            return False, errors
        if delivered_at and local_deleted_at == delivered_at:
            errors.append(
                "local_deleted_at equals delivered_at: the timestamp does not prove a "
                "deletion happened")
            return False, errors
        return True, errors
    # pending / processing / failed cleanup: the file may still exist and is not required.
    if local_deleted_at:
        errors.append(
            f"local_deleted_at is set while cleanup_state={cleanup_state!r}: only a "
            "completed cleanup may record a deletion")
    if cleanup_phase in ("deleted", "deleting"):
        errors.append(
            f"cleanup_phase={cleanup_phase!r} while cleanup_state={cleanup_state!r}: the "
            "phase claims a deletion the state does not record")
    return False, errors


def required_sources(db_path: Path, *, legacy_p0_policy: str = "fail",
                     limits: Limits = DEFAULT_LIMITS
                     ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]], List[str],
                                Dict[str, Any]]:
    """Derive the files a restore needs, with the exact archive path for each.

    Returns ``(required, p0_groups, errors, stats)``. ``p0_groups`` holds candidate
    alternatives for legacy orders, where exactly one must be present.
    """
    required: Dict[str, Dict[str, Any]] = {}
    groups: List[Dict[str, Any]] = []
    errors: List[str] = []
    stats: Dict[str, Any] = {"generation": None, "jobs": 0, "required": 0, "cleaned": 0,
                             "exempt": 0, "email_jobs": 0, "p0_orders": 0,
                             "policy": legacy_p0_policy}

    if legacy_p0_policy not in P0_POLICIES:
        return {}, [], [f"unknown --legacy-p0-policy {legacy_p0_policy!r}; "
                        f"expected one of {P0_POLICIES}"], stats

    try:
        conn = db_evidence.open_readonly(Path(db_path))
    except sqlite3.Error as exc:
        return {}, [], [f"cannot open the backup database for recovery checks: {exc}"], stats

    try:
        conn.execute("BEGIN")
        generation = schema_generation(conn)
        stats["generation"] = generation
        if generation == "P0":
            stats["p0_orders"] = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            if legacy_p0_policy == "fail":
                errors.append(
                    "P0 schema (no delivery_jobs): the original for every legacy order is "
                    "recorded only in orders.filename, and its stored name is ambiguous "
                    "(four candidate layouts). Refusing to treat the database as having no "
                    "customer files. Reconcile the legacy orders into delivery_jobs, or pass "
                    "--legacy-p0-policy include to require the candidates explicitly.")
            else:
                for order_id, filename in conn.execute(
                        "SELECT id, filename FROM orders ORDER BY id").fetchall():
                    raw = (filename or "").strip()
                    if not raw:
                        continue
                    members = [n.strip() for n in raw.split(",") if n.strip()]
                    for index, name in enumerate(members):
                        candidates = _p0_candidates(order_id, name)
                        if not candidates:
                            continue
                        groups.append({"order_id": order_id, "name": name,
                                       "candidates": candidates,
                                       "group": f"{order_id}:{index}"})
            return required, groups, errors, stats

        if generation == "mixed":
            errors.append(
                "mixed schema: delivery_jobs exists but orders.order_token_hash does not. "
                "The delivery records cannot be trusted to describe every order, so the "
                "required file set cannot be derived. Migrate the schema first.")

        rows = conn.execute(
            "SELECT id, order_id, kind, state, local_path, content_sha256, content_size, "
            "cleanup_state, cleanup_phase, local_deleted_at, delivered_at "
            "FROM delivery_jobs ORDER BY order_id, kind, id").fetchall()
    except sqlite3.Error as exc:
        return {}, [], [f"cannot read delivery jobs from the backup database: {exc}"], stats
    finally:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        conn.close()

    if errors:
        return required, groups, errors, stats

    for (job_id, order_id, kind, state, local_path, sha, size,
         cleanup_state, cleanup_phase, deleted_at, delivered_at) in rows:
        stats["jobs"] += 1
        kind = (kind or "").strip()
        state = (state or "").strip()
        cleanup_state = (cleanup_state or "").strip()
        cleanup_phase = (cleanup_phase or "").strip()
        local_path = local_path or ""

        if kind in DATABASE_KINDS:
            stats["email_jobs"] += 1
            if local_path:
                errors.append(
                    f"job {job_id!r} (order {order_id}, kind {kind}) is a database-backed "
                    f"job but records a local_path {local_path!r}: contradictory")
            continue
        if kind not in UPLOAD_BACKED_KINDS:
            errors.append(
                f"job {job_id!r} (order {order_id}) has unknown kind {kind!r}: the contract "
                "cannot decide whether it owns a file")
            continue

        if not local_path:
            errors.append(
                f"job {job_id!r} (order {order_id}, kind {kind}) has no local_path, so its "
                "recovery original cannot be identified")
            continue
        normalised = local_path.replace("\\", "/")
        parent_name = Path(normalised).parent.name
        basename = Path(normalised).name
        if not basename or basename in (".", ".."):
            errors.append(f"job {job_id!r} has an unusable local_path: {local_path!r}")
            continue
        if parent_name != UPLOAD_ROOT:
            errors.append(
                f"job {job_id!r} local_path {local_path!r} is not directly inside the "
                f"{UPLOAD_ROOT}/ root; a nested path cannot be restored to the recorded "
                "location")
            continue
        member = f"{UPLOAD_ROOT}/{basename}"

        if state not in KNOWN_STATES:
            errors.append(f"job {job_id!r} has unrecognised state {state!r}")
            continue

        if state in UNFINISHED_STATES:
            # State wins: cleanup metadata may never exempt an unfinished job.
            if cleanup_state == "completed":
                errors.append(
                    f"job {job_id!r} is {state} but cleanup_state='completed': contradictory "
                    "metadata must not exempt a file the job still needs")
                continue
            if cleanup_phase in ("deleted", "deleting"):
                errors.append(
                    f"job {job_id!r} is {state} but cleanup_phase={cleanup_phase!r}: "
                    "contradictory metadata must not exempt a file the job still needs")
                continue
            if deleted_at:
                errors.append(
                    f"job {job_id!r} is {state} but records local_deleted_at: contradictory "
                    "metadata must not exempt a file the job still needs")
                continue
            if cleanup_state not in ("pending", "processing", "failed"):
                errors.append(
                    f"job {job_id!r} is {state} with cleanup_state={cleanup_state!r}")
                continue
            if not is_valid_sha256(sha):
                errors.append(
                    f"job {job_id!r} is {state} without a valid content_sha256, so its "
                    "recovery original cannot be verified")
                continue
            if not is_valid_size(size):
                errors.append(
                    f"job {job_id!r} is {state} without a valid content_size, so its "
                    "recovery original cannot be verified")
                continue
            existing = required.get(member)
            if existing is not None:
                if existing["sha256"] != sha.lower() or existing["size"] != size:
                    errors.append(
                        f"two jobs require different bytes for {member!r}: the archive "
                        "layout cannot satisfy both")
                continue
            required[member] = {"sha256": sha.lower(), "size": size, "job_id": job_id,
                               "order_id": order_id, "kind": kind, "state": state}
            stats["required"] += 1
            continue

        # state == delivered
        exempt, cleanup_errors = _cleanup_exemption(
            cleanup_state, cleanup_phase, deleted_at, delivered_at)
        errors.extend(f"job {job_id!r} (order {order_id}): {e}" for e in cleanup_errors)
        if exempt:
            stats["cleaned"] += 1
        else:
            stats["exempt"] += 1

    return required, groups, errors, stats


# ------------------------------------------------------------------------ verify
def verify_recovery(db_path: Path, archive_path: Path,
                    limits: Limits = DEFAULT_LIMITS, *,
                    legacy_p0_policy: str = "fail") -> Tuple[bool, Dict[str, Any]]:
    """Prove the archive can rebuild every file the backup database still needs."""
    report: Dict[str, Any] = {
        "ok": False,
        "errors": [],
        "archive": {},
        "required": {},
        "p0_groups": [],
        "missing": [],
        "mismatched": [],
        "unexpected": [],
    }
    errors: List[str] = report["errors"]

    members, info, scan_errors = scan_archive(Path(archive_path), limits)
    report["archive"] = info
    errors.extend(scan_errors)

    required, groups, source_errors, stats = required_sources(
        Path(db_path), legacy_p0_policy=legacy_p0_policy, limits=limits)
    errors.extend(source_errors)
    report["required"] = stats

    archived_files = {name for name in members if name.startswith(f"{UPLOAD_ROOT}/")}
    expected: set[str] = set()

    for member, spec in sorted(required.items()):
        expected.add(member)
        actual = members.get(member)
        if actual is None:
            report["missing"].append(member)
            errors.append(
                f"required recovery original is missing from the archive: {member!r} "
                f"(job {spec['job_id']!r}, order {spec['order_id']}, {spec['state']})")
            continue
        if actual["sha256"] != spec["sha256"]:
            report["mismatched"].append(
                {"member": member, "expected": spec["sha256"], "actual": actual["sha256"]})
            errors.append(
                f"recovery original {member!r} has the wrong content: archive sha256 "
                f"{actual['sha256']} does not match the database's {spec['sha256']}")
        elif actual["size"] != spec["size"]:
            errors.append(
                f"recovery original {member!r} has the wrong size: archive "
                f"{actual['size']} bytes, database records {spec['size']}")

    for group in groups:
        present = [candidate for candidate in group["candidates"] if candidate in members]
        report["p0_groups"].append({"group": group["group"], "present": present})
        if not present:
            errors.append(
                f"legacy order {group['order_id']} lists {group['name']!r} but no archived "
                f"member matches any stored layout ({', '.join(group['candidates'])})")
            continue
        if len(present) > 1:
            errors.append(
                f"legacy order {group['order_id']} matches more than one archived member "
                f"({', '.join(present)}); the original is ambiguous")
            continue
        expected.add(present[0])

    if not errors:
        unexpected = sorted(archived_files - expected)
        report["unexpected"] = unexpected
        if unexpected:
            errors.append(
                f"the archive contains {len(unexpected)} file(s) the database does not "
                f"require ({', '.join(unexpected[:3])}): a backup must not retain files that "
                "were already deleted")

    report["ok"] = not errors
    return report["ok"], report
