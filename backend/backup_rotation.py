"""Backup rotation and expiration with strict ownership guards.

Prototype, not installed. It is meant to run as a daily timer **after** PR #31 is merged
and the owner approves retention policy.

Guards
------
* The directory must exist and every entry must be a known backup: either a legacy
  prefix (``pre-switch-*``, ``staging-*``, ``preview-publish-*``) or the actual archive
  name produced by ``scripts/run_backup.sh`` (``YYYYMMDDTHHMMSSZ``). Malformed names are
  ignored, never deleted.
* Symlinks are never followed or removed.
* The newest ``keep_min`` entries survive within the retention window, so a broken clock or a
  burst of backups cannot leave the operator without a restore point. ``max_age_days`` is the
  hard obligation: when set, entries older than it are removed even if fewer than ``keep_min``
  would remain — a minimum count must not become an indefinite retention exception (H-03).
* ``dry_run`` reports removals without touching the filesystem and without writing the log.
* Every removal is appended to ``<dir>/rotation.jsonl`` (path, size, age) so an audit exists
  after the fact; restored backups are re-created with new names and therefore start a new
  retention clock automatically.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_PATTERNS = ("pre-switch-", "staging-", "preview-publish-")
DEFAULT_RETENTION_DAYS = 3
DEFAULT_KEEP_MIN = 2
# The naming produced by scripts/run_backup.sh, e.g. 20260101T000000Z.
TIMESTAMP_NAME = re.compile(r"^\d{8}T\d{6}Z$")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def is_known_backup(name: str, patterns: tuple[str, ...] = DEFAULT_PATTERNS) -> bool:
    """A legacy prefix or the real timestamp archive name; anything else is foreign."""
    return any(name.startswith(prefix) for prefix in patterns) or bool(TIMESTAMP_NAME.match(name))


def list_backups(backup_dir: Path, patterns: tuple[str, ...] = DEFAULT_PATTERNS) -> list[Path]:
    """Entries that look like PhotoDoc backups, newest first."""
    if not backup_dir.is_dir():
        return []
    entries = []
    for entry in backup_dir.iterdir():
        if entry.is_symlink():
            continue
        if not is_known_backup(entry.name, patterns):
            continue
        entries.append(entry)
    return sorted(entries, key=lambda item: item.stat().st_mtime, reverse=True)


def validate_backup_rotation_params(
    retention_days: int,
    keep_min: int,
    max_age_days: int | None = None,
) -> None:
    """Validate rotation parameters against safety bounds."""
    if retention_days <= 0:
        raise ValueError(f"retention_days must be > 0 (got {retention_days})")
    if keep_min < 0:
        raise ValueError(f"keep_min must be >= 0 (got {keep_min})")
    if max_age_days is not None:
        if max_age_days < retention_days:
            raise ValueError(
                f"max_age_days ({max_age_days}) cannot be less than retention_days ({retention_days})"
            )


def validate_backup_dir_safety(backup_dir: Path | str) -> Path:
    """Strict validation for backup directory: exists, dir, not symlink, not root."""
    path = Path(backup_dir)
    if ".." in path.parts:
        raise ValueError(f"Path traversal detected in backup directory: {backup_dir}")
    if path.is_symlink():
        raise ValueError(f"Backup directory cannot be a symlink: {backup_dir}")
    if not path.is_dir():
        raise ValueError(f"Backup directory does not exist or is not a directory: {backup_dir}")
    resolved = path.resolve()
    if resolved == resolved.parent:
        raise ValueError(f"Backup directory cannot be filesystem root: {backup_dir}")
    return resolved


def rotate(
    backup_dir: Path,
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    keep_min: int = DEFAULT_KEEP_MIN,
    max_age_days: int | None = None,
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove expired backups, keeping the newest ``keep_min`` entries."""
    validate_backup_rotation_params(retention_days=retention_days, keep_min=keep_min, max_age_days=max_age_days)
    if not backup_dir.is_dir():
        return {"ok": False, "reason": "backup directory does not exist", "removed": [], "kept": 0}
    backup_dir = validate_backup_dir_safety(backup_dir)

    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=retention_days)
    entries = list_backups(backup_dir, patterns)

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "retention_days": retention_days,
        "keep_min": keep_min,
        "max_age_days": max_age_days,
        "found": len(entries),
        "removed": [],
        "kept": 0,
        "bytes_freed": 0,
        "errors": [],
    }

    for index, entry in enumerate(entries):
        age_days = (current - datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)).days
        over_hard_limit = max_age_days is not None and age_days > max_age_days
        if index < keep_min and not over_hard_limit:
            summary["kept"] += 1
            continue
        if datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc) > cutoff:
            summary["kept"] += 1
            continue

        size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file()) if entry.is_dir() else entry.stat().st_size
        record = {"path": str(entry), "age_days": age_days, "bytes": size}
        if dry_run:
            summary["removed"].append(record)
            summary["bytes_freed"] += size
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            summary["removed"].append(record)
            summary["bytes_freed"] += size
            _append_audit(backup_dir, {**record, "removed_at": _iso(current), "retention_days": retention_days})
        except OSError as exc:
            summary["errors"].append(f"{entry.name}: {exc}")

    return summary


def _append_audit(backup_dir: Path, record: dict[str, Any]) -> None:
    log = backup_dir / "rotation.jsonl"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(log, flags, 0o600)
    try:
        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
