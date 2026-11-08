"""Deletion of PhotoDoc-owned files on Yandex.Disk after the retention window.

This module is a **prototype**: it is not wired into the application and does not run in
production. It exists so the design can be reviewed and merged after PR #31.

Safety model
------------
* Only paths under the configured root are ever considered, and only when the path carries
  no ``..`` segment and no empty remainder after the root.
* A path is enqueued as an ``intent`` row **before** any network call, so a crash between
  the intent and the deletion is recoverable: the next run sees the row and retries.
* The client reports ``deleted``, ``missing`` or ``error``. ``missing`` is success (the file
  is already gone); ``error`` is retried with bounded exponential backoff and finally parked
  as ``failed`` so it is visible instead of looping forever.
* ``dry_run`` performs the planning and the ownership checks but neither writes intents nor
  calls the client.

The client is injected, so tests never touch the network and never need a production token.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol

DEFAULT_RETENTION_HOURS = 72
DEFAULT_MAX_ATTEMPTS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS remote_deletion_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_path TEXT NOT NULL UNIQUE,
    order_id INTEGER,
    state TEXT NOT NULL CHECK(state IN ('intent', 'deleted', 'missing', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_remote_audit_state ON remote_deletion_audit(state, next_attempt_at);
"""


class DiskClient(Protocol):
    def delete(self, remote_path: str) -> tuple[str, str]:
        """Return ("deleted" | "missing" | "error", detail)."""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def is_owned_path(remote_path: str, root: str) -> bool:
    """True only for a path strictly inside the PhotoDoc root."""
    if not remote_path or not root:
        return False
    normalized_root = root.strip("/")
    normalized = remote_path.strip("/")
    if ".." in normalized.split("/"):
        return False
    if not normalized.startswith(normalized_root + "/"):
        return False
    remainder = normalized[len(normalized_root) + 1:]
    return bool(remainder)


def plan_candidates(jobs: Iterable[dict[str, Any]], *, now: datetime, root: str,
                    ttl_hours: int = DEFAULT_RETENTION_HOURS) -> list[dict[str, Any]]:
    """Delivered jobs whose retention window has passed and whose path is owned."""
    cutoff = now - timedelta(hours=ttl_hours)
    planned = []
    for job in jobs:
        delivered_at = job.get("delivered_at")
        path = job.get("remote_path")
        if not delivered_at or not path:
            continue
        try:
            delivered_dt = datetime.fromisoformat(str(delivered_at))
        except ValueError:
            continue
        if delivered_dt.tzinfo is None:
            delivered_dt = delivered_dt.replace(tzinfo=timezone.utc)
        if delivered_dt > cutoff:
            continue
        if not is_owned_path(str(path), root):
            continue
        planned.append({"remote_path": str(path).strip("/"), "order_id": job.get("order_id")})
    return planned


def purge(
    conn: sqlite3.Connection,
    client: DiskClient,
    *,
    now: datetime | None = None,
    root: str,
    ttl_hours: int = DEFAULT_RETENTION_HOURS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    dry_run: bool = False,
    jobs: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Delete owned remote files, recovering interrupted intents on the way."""
    current = now or datetime.now(timezone.utc)
    now_iso = _iso(current)
    conn.executescript(SCHEMA)

    summary: dict[str, Any] = {
        "dry_run": dry_run, "ttl_hours": ttl_hours, "root": root,
        "planned": 0, "deleted": 0, "missing": 0, "failed": 0,
        "recovered": 0, "skipped_unowned": 0, "errors": [],
    }

    pending = conn.execute(
        "SELECT remote_path, order_id, attempts FROM remote_deletion_audit "
        "WHERE state='intent' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
        (now_iso,),
    ).fetchall()
    summary["recovered"] = len(pending)

    planned = plan_candidates(jobs or [], now=current, root=root, ttl_hours=ttl_hours)
    known = {row[0] for row in conn.execute("SELECT remote_path FROM remote_deletion_audit")}
    candidates = [(row[0], row[1], row[2]) for row in pending]
    candidates += [(item["remote_path"], item["order_id"], 0) for item in planned
                   if item["remote_path"] not in known]
    summary["planned"] = len(candidates)

    for path, order_id, attempts in candidates:
        if not is_owned_path(path, root):
            summary["skipped_unowned"] += 1
            summary["errors"].append(f"{path}: path outside the PhotoDoc root")
            continue
        if dry_run:
            continue
        if attempts == 0 and path not in known:
            conn.execute(
                "INSERT OR IGNORE INTO remote_deletion_audit "
                "(remote_path, order_id, state, attempts, created_at, updated_at) VALUES (?, ?, 'intent', 0, ?, ?)",
                (path, order_id, now_iso, now_iso),
            )
            conn.commit()
        try:
            status, detail = client.delete(path)
        except Exception as exc:  # noqa: BLE001 - the client contract is defensive
            status, detail = "error", f"{type(exc).__name__}: {exc}"

        if status in ("deleted", "missing"):
            conn.execute(
                "UPDATE remote_deletion_audit SET state=?, attempts=?, updated_at=?, deleted_at=?, error=NULL "
                "WHERE remote_path=?",
                (status, attempts + 1, now_iso, now_iso if status == "deleted" else None, path),
            )
            conn.commit()
            summary["deleted" if status == "deleted" else "missing"] += 1
            continue

        next_attempts = attempts + 1
        if next_attempts >= max_attempts:
            conn.execute(
                "UPDATE remote_deletion_audit SET state='failed', attempts=?, updated_at=?, error=? "
                "WHERE remote_path=?",
                (next_attempts, now_iso, detail[:300], path),
            )
            summary["failed"] += 1
            summary["errors"].append(f"{path}: {detail[:120]}")
        else:
            delay = min(3600, 60 * (2 ** (next_attempts - 1)))
            conn.execute(
                "UPDATE remote_deletion_audit SET state='intent', attempts=?, next_attempt_at=?, "
                "updated_at=?, error=? WHERE remote_path=?",
                (next_attempts, _iso(current + timedelta(seconds=delay)), now_iso, detail[:300], path),
            )
        conn.commit()

    return summary


def pending_audit(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Operator view: everything not yet resolved."""
    conn.executescript(SCHEMA)
    rows = conn.execute(
        "SELECT remote_path, order_id, state, attempts, next_attempt_at, error, created_at "
        "FROM remote_deletion_audit WHERE state IN ('intent','failed') ORDER BY id"
    ).fetchall()
    keys = ("remote_path", "order_id", "state", "attempts", "next_attempt_at", "error", "created_at")
    return [dict(zip(keys, row)) for row in rows]

def plan_from_db(conn: sqlite3.Connection, *, now: datetime, root: str,
                 ttl_hours: int = DEFAULT_RETENTION_HOURS) -> list[dict[str, Any]]:
    """Candidates straight from delivery_jobs: delivered, older than the window, owned."""
    rows = conn.execute(
        "SELECT order_id, remote_path, delivered_at FROM delivery_jobs "
        "WHERE kind IN ('file','info') AND state='delivered' AND remote_path IS NOT NULL "
        "  AND delivered_at IS NOT NULL"
    ).fetchall()
    jobs = [{"order_id": row[0], "remote_path": row[1], "delivered_at": row[2]} for row in rows]
    # plan_candidates() re-validates the window, so the timestamp must travel with the item.
    planned = plan_candidates(jobs, now=now, root=root, ttl_hours=ttl_hours)
    by_path = {job["remote_path"]: job["delivered_at"] for job in jobs}
    for item in planned:
        item["delivered_at"] = by_path.get(item["remote_path"])
    return planned
