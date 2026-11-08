"""Secure operator workflow for Release 1 (contract PHOTODOC-R1-OPERATOR-CONTRACT 1.0).

Artifacts
---------
Two distinct artifacts exist per order and they are never interchangeable:

* **source** — the immutable original uploaded by the customer. It is stored under the
  ``remote/`` namespace (``remote_path``) and is *never* handed to the customer delivery
  worker; its only consumer is the operator.
* **result** — the processed image the operator uploads. It is tracked in
  ``order_results`` (one active row per order) and is the only artifact eligible for
  delivery.

Authorization is a server-side bearer token from ``OPERATOR_TOKEN``; without it the whole
surface answers 503. Every route re-checks authentication, the order state and the order
ownership.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

OPERATOR_TOKEN_ENV = "OPERATOR_TOKEN"
MAX_RESULT_BYTES = 25 * 1024 * 1024
# A real image is never a handful of bytes; this rejects the "magic bytes only" trick.
MIN_RESULT_BYTES = 16
ALLOWED_RESULT_TYPES = {
    "image/jpeg": ("jpg", b"\xff\xd8\xff"),
    "image/png": ("png", b"\x89PNG\r\n\x1a\n"),
    "image/webp": ("webp", b"RIFF"),
}
SAFE_ORDER_ID = re.compile(r"^\d{1,9}$")
SOURCE_NAMESPACE = "remote/"
RESULT_SUFFIX = "_result_"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_results (
    order_id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    order_id INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operator_audit_order ON operator_audit(order_id, created_at);
"""


def operator_token() -> str:
    return (os.getenv(OPERATOR_TOKEN_ENV) or "").strip()


def require_operator(authorization: str | None) -> str:
    configured = operator_token()
    if not configured:
        raise HTTPException(status_code=503, detail="Operator access is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Operator authentication required")
    presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented, configured):
        raise HTTPException(status_code=401, detail="Operator authentication required")
    return "operator"


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(RESULT_SCHEMA)
    conn.commit()


def audit(conn: sqlite3.Connection, event: str, order_id: int | None = None, detail: str = "") -> None:
    initialize(conn)
    conn.execute(
        "INSERT INTO operator_audit (event, order_id, detail, created_at) VALUES (?, ?, ?, ?)",
        (event, order_id, detail[:200], datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def uploads_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads"))


def require_processable(conn: sqlite3.Connection, order_id: int) -> dict[str, Any]:
    """Paid, and not cancelled/refunded. The single state guard for all routes."""
    row = conn.execute(
        "SELECT id, payment_status, status FROM orders WHERE id=?", (order_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if row["payment_status"] != "paid":
        raise HTTPException(status_code=409, detail="Order is not paid")
    if (row["status"] or "") in ("cancelled", "refunded"):
        raise HTTPException(status_code=409, detail="Order is cancelled or refunded")
    return dict(row)


def _is_result_job(job) -> bool:
    remote = job["remote_path"] or ""
    local = job["local_path"] or ""
    return remote.endswith("_result.bin") or RESULT_SUFFIX in local


def _source_job(conn: sqlite3.Connection, order_id: int):
    rows = conn.execute(
        "SELECT id, local_path, remote_path FROM delivery_jobs "
        "WHERE order_id=? AND kind='file' ORDER BY created_at, id",
        (order_id,),
    ).fetchall()
    for row in rows:
        if _is_result_job(row):
            continue
        return row
    return None


def _source_path(conn: sqlite3.Connection, order_id: int) -> Path | None:
    job = _source_job(conn, order_id)
    if job is None or not job["local_path"]:
        return None
    uploads = uploads_dir().resolve()
    try:
        path = Path(job["local_path"])
        if path.is_symlink():
            return None
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.parent != uploads:
        return None
    if not resolved.name.startswith(f"{order_id}_"):
        return None
    return resolved if resolved.is_file() else None


def _quarantine_for(conn: sqlite3.Connection, order_id: int) -> Path | None:
    job = _source_job(conn, order_id)
    if job is None:
        return None
    quarantine = uploads_dir() / ".quarantine"
    if not quarantine.is_dir():
        return None
    for claim in quarantine.glob(f"{job['id']}.*.claim"):
        return claim
    return None


def list_paid_orders(db_path: Path) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, format, paper, crop, order_amount, payment_status, status, paid_at, created_at "
            "FROM orders WHERE payment_status='paid' AND COALESCE(status,'') NOT IN ('cancelled','refunded') "
            "ORDER BY id"
        ).fetchall()
        return [dict(row) | {"delivered": _has_result(db_path, row["id"])} for row in rows]
    finally:
        conn.close()


def _has_result(db_path: Path, order_id: int) -> bool:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT 1 FROM order_results WHERE order_id=?", (order_id,)).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def order_metadata(db_path: Path, order_id: int) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        require_processable(conn, order_id)
        row = conn.execute(
            "SELECT id, format, paper, crop, comment, order_amount, payment_status, status, paid_at, created_at "
            "FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        item = dict(row)
        item["source_available"] = _source_path(conn, order_id) is not None
        item["result_ready"] = conn.execute(
            "SELECT 1 FROM order_results WHERE order_id=?", (order_id,)
        ).fetchone() is not None
        return item
    finally:
        conn.close()


def source_for_download(db_path: Path, order_id: int) -> Path:
    conn = _connect(db_path)
    try:
        require_processable(conn, order_id)
        path = _source_path(conn, order_id)
        if path is not None:
            return path
        if _quarantine_for(conn, order_id) is not None:
            raise HTTPException(status_code=409, detail="Source is in recovery; manual intervention required")
        raise HTTPException(status_code=410, detail="Source image is no longer available")
    finally:
        conn.close()


def read_limited(upload: UploadFile, limit: int = MAX_RESULT_BYTES) -> bytes:
    """Read at most limit+1 bytes so the memory bound does not depend on the client."""
    chunks = []
    total = 0
    while True:
        chunk = upload.file.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="Result exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def validate_result(upload: UploadFile, payload: bytes) -> tuple[str, str]:
    if len(payload) == 0:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(payload) < MIN_RESULT_BYTES:
        raise HTTPException(status_code=400, detail="Malformed image")
    if len(payload) > MAX_RESULT_BYTES:
        raise HTTPException(status_code=413, detail="Result exceeds the size limit")
    content_type = (upload.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_RESULT_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported result type")
    suffix, magic = ALLOWED_RESULT_TYPES[content_type]
    if content_type == "image/webp":
        if not (payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"):
            raise HTTPException(status_code=400, detail="Malformed image")
    elif not payload.startswith(magic):
        raise HTTPException(status_code=400, detail="Malformed image")
    return suffix, hashlib.sha256(payload).hexdigest()


def _result_is_locked(conn: sqlite3.Connection, order_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM delivery_jobs WHERE order_id=? AND state IN ('processing','delivered') "
        "AND (remote_path LIKE '%_result.bin')", (order_id,)
    ).fetchone()
    return row is not None


def store_result(db_path: Path, order_id: int, suffix: str, payload: bytes, sha256: str,
                 *, replace: bool = False) -> Path:
    """Publish exactly one active result per order; the loser of a race gets 409."""
    uploads = uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    target = uploads / f"{order_id}_result_{uuid.uuid4().hex}.{suffix}"
    tmp = uploads / f".{target.name}.part"
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)

        conn = _connect(db_path)
        try:
            initialize(conn)
            require_processable(conn, order_id)
            if _result_is_locked(conn, order_id):
                raise HTTPException(status_code=409, detail="Result is already queued or delivered")
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT path FROM order_results WHERE order_id=?", (order_id,)
            ).fetchone()
            if existing and not replace:
                conn.rollback()
                raise HTTPException(status_code=409, detail="Result already uploaded; use replace=true")
            now = datetime.now(timezone.utc).isoformat()
            if existing:
                conn.execute(
                    "UPDATE order_results SET path=?, sha256=?, size=?, version=version+1, updated_at=? "
                    "WHERE order_id=?", (str(target), sha256, len(payload), now, order_id))
            else:
                conn.execute(
                    "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?)", (order_id, str(target), sha256, len(payload), now))
            conn.commit()
            audit(conn, "result_uploaded", order_id, f"bytes={len(payload)} sha256={sha256[:16]}")
            if existing and existing["path"] != str(target):
                try:
                    Path(existing["path"]).unlink()
                except OSError:
                    pass
            return target
        finally:
            conn.close()
    except BaseException:
        # M-04: never leave an untracked partial copy behind.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        raise


def submit_delivery(db_path: Path, order_id: int) -> str:
    """Queue the active result; a replaced result updates the same job in place (H-02)."""
    conn = _connect(db_path)
    try:
        initialize(conn)
        require_processable(conn, order_id)
        row = conn.execute("SELECT path, sha256, size FROM order_results WHERE order_id=?",
                           (order_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="No processed result uploaded yet")
        path = Path(row["path"])
        if not path.is_file() or path.is_symlink():
            raise HTTPException(status_code=409, detail="Result file is missing")
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT id, state FROM delivery_jobs WHERE order_id=? AND remote_path LIKE '%_result.bin'",
            (order_id,),
        ).fetchone()
        if job is not None and job["state"] in ("processing", "delivered"):
            conn.rollback()
            raise HTTPException(status_code=409, detail="Result is already being delivered")
        remote_path = f"{os.getenv('YANDEX_DISK_ROOT_DIR', 'PhotoDoc_AI')}/{order_id}_result.bin"
        if job is None:
            from delivery import enqueue
            job_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO delivery_jobs (id, order_id, kind, local_path, original_name, remote_path,"
                " content_sha256, content_size, next_attempt_at, created_at, cleanup_state, cleanup_phase)"
                " VALUES (?, ?, 'file', ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending')",
                (job_id, order_id, str(path), path.name, remote_path, row["sha256"], row["size"], now, now))
        else:
            job_id = job["id"]
            conn.execute(
                "UPDATE delivery_jobs SET local_path=?, original_name=?, content_sha256=?, content_size=?,"
                " state='pending', attempts=0, next_attempt_at=?, last_error=NULL WHERE id=?",
                (str(path), path.name, row["sha256"], row["size"],
                 datetime.now(timezone.utc).isoformat(), job_id))
        conn.commit()
        audit(conn, "delivery_submitted", order_id, f"job={job_id}")
        return job_id
    finally:
        conn.close()


def delivery_state(db_path: Path, order_id: int) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        require_processable(conn, order_id)
        rows = conn.execute(
            "SELECT id, kind, state, attempts, last_error, delivered_at, cleanup_state "
            "FROM delivery_jobs WHERE order_id=? ORDER BY created_at, id", (order_id,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
