"""Secure operator workflow for Release 1 (contract PHOTODOC-R1-OPERATOR-CONTRACT 1.0).

Authorization
-------------
A single operator role, enforced server-side with a bearer token read from
``OPERATOR_TOKEN``. There is no default: when the variable is absent the whole operator
surface answers 503 and nothing is reachable. The comparison is constant-time, the token is
never logged, and no cookie is used, so CSRF does not apply to this API.

What the operator may do
------------------------
* list paid orders that still need processing;
* read limited metadata for one order;
* download the original image of a paid order;
* upload a processed result (validated, atomic, order-scoped);
* submit the result to the existing delivery queue;
* read the delivery state.

Every operation re-checks authentication and the order state. Nothing here deletes or
overwrites the original: the source file is read-only for this module, which preserves the
C-01/C-02 guarantees of the retention engine.
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
ALLOWED_RESULT_TYPES = {
    "image/jpeg": ("jpg", b"\xff\xd8\xff"),
    "image/png": ("png", b"\x89PNG\r\n\x1a\n"),
    "image/webp": ("webp", b"RIFF"),
}
SAFE_ORDER_ID = re.compile(r"^\d{1,9}$")

AUDIT_SCHEMA = """
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
    """Return the presented identity or raise. Default deny."""
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
    conn.executescript(AUDIT_SCHEMA)
    conn.commit()


def audit(conn: sqlite3.Connection, event: str, order_id: int | None = None, detail: str = "") -> None:
    """Record an operator action without secrets, contact data or image bytes."""
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


def list_paid_orders(db_path: Path) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, format, paper, crop, order_amount, payment_status, status, paid_at, created_at "
            "FROM orders WHERE payment_status = 'paid' ORDER BY id"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["delivered"] = _has_delivered_job(conn, row["id"])
            result.append(item)
        return result
    finally:
        conn.close()


def order_metadata(db_path: Path, order_id: int) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, format, paper, crop, comment, order_amount, payment_status, status, paid_at, created_at "
            "FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Order not found")
        item = dict(row)
        item["delivered"] = _has_delivered_job(conn, order_id)
        item["source_available"] = _source_or_none(conn, order_id, uploads_dir()) is not None
        return item
    finally:
        conn.close()


def _has_delivered_job(conn: sqlite3.Connection, order_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM delivery_jobs WHERE order_id=? AND kind IN ('file','info') AND state='delivered' LIMIT 1",
        (order_id,),
    ).fetchone()
    return row is not None


def uploads_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads"))


def _source_or_none(conn: sqlite3.Connection, order_id: int, uploads: Path) -> Path | None:
    """The stored original for an order, verified to live inside the uploads directory."""
    rows = conn.execute(
        "SELECT local_path FROM delivery_jobs WHERE order_id=? AND kind='file' AND local_path IS NOT NULL",
        (order_id,),
    ).fetchall()
    candidates = [row[0] for row in rows]
    resolved_uploads = uploads.resolve()
    for candidate in candidates:
        try:
            path = Path(candidate)
            if path.is_symlink():
                continue
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.parent != resolved_uploads:
            continue
        if not resolved.name.startswith(f"{order_id}_"):
            continue
        if resolved.is_file():
            return resolved
    return None


def require_paid(conn: sqlite3.Connection, order_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, payment_status, status FROM orders WHERE id=?", (order_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if row["payment_status"] != "paid":
        raise HTTPException(status_code=409, detail="Order is not paid")
    return dict(row)


def source_for_download(db_path: Path, order_id: int) -> Path:
    """Resolve the original for a paid order, or fail with an actionable status."""
    uploads = uploads_dir()
    conn = _connect(db_path)
    try:
        require_paid(conn, order_id)
        path = _source_or_none(conn, order_id, uploads)
        if path is not None:
            return path
        # Distinguish a missing file from a quarantined one (C-01 state).
        quarantine = uploads / ".quarantine"
        if quarantine.is_dir() and any(quarantine.glob(f"*.claim")):
            raise HTTPException(status_code=409, detail="Source is in recovery; manual intervention required")
        raise HTTPException(status_code=410, detail="Source image is no longer available")
    finally:
        conn.close()


def validate_result(upload: UploadFile, payload: bytes) -> tuple[str, str]:
    """Validate type, magic bytes and size; return (suffix, sha256)."""
    if len(payload) == 0:
        raise HTTPException(status_code=400, detail="Empty upload")
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


def store_result(db_path: Path, order_id: int, suffix: str, payload: bytes, sha256: str,
                 *, replace: bool = False) -> Path:
    """Atomically store a processed result for one order; never overwrite another order."""
    uploads = uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        require_paid(conn, order_id)
        if _has_delivered_job(conn, order_id):
            raise HTTPException(status_code=409, detail="Order is already delivered")
        existing = [p for p in uploads.iterdir()
                    if p.is_file() and not p.is_symlink() and p.name.startswith(f"{order_id}_result_")]
        if existing and not replace:
            raise HTTPException(status_code=409, detail="Result already uploaded; use replace=true")
        name = f"{order_id}_result_{uuid.uuid4().hex}.{suffix}"
        target = uploads / name
        tmp = uploads / f".{name}.part"
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if target.is_symlink():
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail="Refusing to replace a symlink")
        os.replace(tmp, target)
        audit(conn, "result_uploaded", order_id, f"bytes={len(payload)} sha256={sha256[:16]}")
        return target
    finally:
        conn.close()


def submit_delivery(db_path: Path, order_id: int, result_path: Path, sha256: str) -> str:
    """Queue the processed result for the existing delivery worker."""
    from delivery import enqueue

    conn = _connect(db_path)
    try:
        require_paid(conn, order_id)
        if not result_path.is_file() or result_path.is_symlink():
            raise HTTPException(status_code=409, detail="Result file is missing")
        size = result_path.stat().st_size
        remote_path = f"{os.getenv('YANDEX_DISK_ROOT_DIR', 'PhotoDoc_AI')}/{order_id}_result.bin"
        job_id = enqueue(conn, order_id=order_id, kind="file", local_path=str(result_path),
                         original_name=result_path.name, remote_path=remote_path,
                         sha256=sha256, size=size)
        conn.commit()
        audit(conn, "delivery_submitted", order_id, f"job={job_id}")
        return job_id
    finally:
        conn.close()


def delivery_state(db_path: Path, order_id: int) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, state, attempts, last_error, delivered_at, cleanup_state "
            "FROM delivery_jobs WHERE order_id=? ORDER BY created_at, id", (order_id,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
