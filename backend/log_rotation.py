"""Verification and enforcement of log retention policies (152-FZ compliance).

Declares and enforces the 15-day maximum retention limit for application logs
as documented in reports/r1-launch/compliance/152FZ_PACKAGE.md (table row 1: daily/14).

Scope & Boundaries:
-------------------
* Application logs: Managed by this module inside ``LOG_DIR`` (e.g. uvicorn.log,
  access.log, app.log, and rotated historical generations).
* Host Nginx web server logs: External infrastructure component managed on the host
  system via standard OS logrotate (/etc/logrotate.d/nginx), not by this Python process.
* Docker container logs: Container engine infrastructure managed via the Docker daemon
  logging driver (e.g. json-file with max-size and max-file retention settings).
"""
from __future__ import annotations

import fnmatch
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOG_RETENTION_DAYS = 15

# Standard base active log file names that running servers append to
ACTIVE_BASE_LOG_NAMES = frozenset({
    "app.log", "access.log", "error.log", "uvicorn.log",
    "server.log", "worker.log", "photodoc.log", "audit.log",
})


def validate_log_dir_safety(log_dir: Path | str) -> Path:
    """Ensure log directory exists, is not root, contains no path traversal and no symlink."""
    path = Path(log_dir)
    if ".." in path.parts:
        raise ValueError(f"Path traversal detected in log directory: {log_dir}")
    if path.is_symlink():
        raise ValueError(f"Log directory cannot be a symlink: {log_dir}")
    if not path.is_dir():
        raise ValueError(f"Log directory does not exist or is not a directory: {log_dir}")
    resolved = path.resolve()
    if resolved == resolved.parent:
        raise ValueError(f"Log directory cannot be filesystem root: {log_dir}")
    return resolved


def rotate_application_logs(
    log_dir: Path | str,
    *,
    max_days: int = DEFAULT_LOG_RETENTION_DAYS,
    now: datetime | None = None,
    dry_run: bool = False,
    legal_hold_patterns: tuple[str, ...] = ("*.hold", "*incident*", "*legal*", "*.preserve"),
) -> dict[str, Any]:
    """Execute scheduled log rotation for application logs inside log_dir.

    Safely purges rotated application log files exceeding max_days retention.
    Strictly protects active log files currently in use and files under legal hold.
    Any OS unlink failure (e.g. read-only permissions or file lock) is recorded in errors
    without crashing the cycle.

    Scope:
        Manages application logs in LOG_DIR. Host Nginx logs are managed by system logrotate
        and Docker logs are managed by Docker logging driver.
    """
    if max_days <= 0:
        raise ValueError(f"max_days must be > 0 (got {max_days})")

    resolved_dir = validate_log_dir_safety(log_dir)
    current = now or datetime.now(timezone.utc)

    summary: dict[str, Any] = {
        "ok": True,
        "log_dir": str(resolved_dir),
        "max_days": max_days,
        "dry_run": dry_run,
        "scanned": 0,
        "expired": 0,
        "purged": 0,
        "held": [],
        "protected_active": [],
        "violations": [],
        "errors": [],
    }

    for entry in resolved_dir.iterdir():
        if entry.is_symlink():
            continue
        if not entry.is_file():
            continue

        name_lower = entry.name.lower()

        # Check legal hold patterns
        is_held = any(fnmatch.fnmatch(name_lower, pat.lower()) for pat in legal_hold_patterns)
        if not is_held and (resolved_dir / f"{entry.name}.hold").exists():
            is_held = True

        if is_held:
            summary["held"].append(entry.name)
            continue

        if not any(name_lower.endswith(ext) or ext in name_lower for ext in (".log", ".gz", ".1", ".2", ".old", ".txt")):
            continue

        summary["scanned"] += 1
        stat = entry.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        age_days = (current - mtime).total_seconds() / 86400.0

        # Protect active log files (base active files or modified within 24 hours)
        is_active = (name_lower in ACTIVE_BASE_LOG_NAMES) or (age_days < 1.0)
        if is_active:
            summary["protected_active"].append(entry.name)
            continue

        if age_days > max_days:
            summary["expired"] += 1
            summary["violations"].append({
                "file": entry.name,
                "age_days": round(age_days, 2),
                "mtime": mtime.isoformat(),
            })
            if not dry_run:
                try:
                    entry.unlink()
                    summary["purged"] += 1
                except OSError as exc:
                    summary["ok"] = False
                    summary["errors"].append(f"Failed to unlink {entry.name}: {exc}")

    if summary["errors"]:
        summary["ok"] = False

    return summary


def verify_log_retention_policy(
    log_dir: Path | str,
    *,
    max_days: int = DEFAULT_LOG_RETENTION_DAYS,
    now: datetime | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Verify all log files inside log_dir comply with the maximum retention window.

    If dry_run is False, purges any log file strictly exceeding max_days.
    Returns audit summary with violations list.
    """
    if max_days <= 0:
        raise ValueError(f"max_days must be > 0 (got {max_days})")

    resolved_dir = validate_log_dir_safety(log_dir)
    current = now or datetime.now(timezone.utc)

    summary: dict[str, Any] = {
        "ok": True,
        "log_dir": str(resolved_dir),
        "max_days": max_days,
        "dry_run": dry_run,
        "scanned": 0,
        "expired": 0,
        "purged": 0,
        "violations": [],
        "errors": [],
    }

    # Match log files directly in log_dir
    for entry in resolved_dir.iterdir():
        if entry.is_symlink():
            continue
        if not entry.is_file():
            continue
        name_lower = entry.name.lower()
        if not any(name_lower.endswith(ext) or ext in name_lower for ext in (".log", ".gz", ".1", ".2", ".old", ".txt")):
            continue

        summary["scanned"] += 1
        stat = entry.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        age_days = (current - mtime).total_seconds() / 86400.0

        if age_days > max_days:
            summary["expired"] += 1
            summary["violations"].append({
                "file": entry.name,
                "age_days": round(age_days, 2),
                "mtime": mtime.isoformat(),
            })
            if not dry_run:
                try:
                    entry.unlink()
                    summary["purged"] += 1
                except OSError as exc:
                    summary["errors"].append(f"Failed to unlink {entry.name}: {exc}")

    if summary["expired"] > 0:
        summary["ok"] = False

    return summary
