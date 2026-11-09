"""Verification and enforcement of log retention policies (152-FZ compliance).

Declares and verifies the 15-day maximum retention limit for web server and access logs
as documented in reports/r1-launch/compliance/152FZ_PACKAGE.md (table row 1: daily/14).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOG_RETENTION_DAYS = 15


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
