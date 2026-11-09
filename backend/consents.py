"""Versioned consent records for the future ordering platform.

The module is deliberately server-side and inert: it records what the customer agreed
to, with the exact document version, so an operator can prove consent later without
storing unnecessary personal data. Nothing here is wired into a public route; the
feature flag ``CONSENT_FEATURE_ENABLED`` stays ``0`` until the owner approves activation.

Design notes
------------
* One record per (order, purpose, document). A grant and a withdrawal are separate rows so
  the history is auditable and nothing is overwritten.
* Only the order reference is stored; the customer identity is the order itself. No IP
  address, no user agent, no free text.
* ``document_version`` and ``document_hash`` pin the exact text the customer saw.
* Effective state is derived from the append order (rowid), never from a caller-supplied
  timestamp, so a future-dated grant cannot shadow a later withdrawal (H-04).
* Read paths never execute DDL and never commit the caller's transaction (H-07).
* ``check_required()`` returns the purposes still missing a valid grant, so the caller can
  refuse to accept a photo until the mandatory consents exist.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FEATURE_FLAG = "CONSENT_FEATURE_ENABLED"

# Canonical cryptographic hashes of approved legal documents (LF normalized UTF-8 SHA-256)
CANONICAL_OFFER_HASH = "3eb6615b5b5a7da42d7c4e86b5cd692b3daf20ef5b160481b4cbbed789c2c9c0"
CANONICAL_PRIVACY_HASH = "7a4e0fde5beaf6f1ef15c5d7b8fa3c6bbab8b9d6633a1e2be5f6c5c8c87fe6cf"

# Purpose identifiers. Adding a purpose is a deliberate act: the tests assert the exact set.
PURPOSE_SERVICE = "service_terms"
PURPOSE_PERSONAL_DATA = "personal_data_processing"
PURPOSE_THIRD_PARTY_IMAGES = "third_party_images"
PURPOSE_MINOR_IMAGES = "minor_images"
PURPOSE_CROSS_BORDER = "cross_border_transfer"

MANDATORY_PURPOSES = (PURPOSE_SERVICE, PURPOSE_PERSONAL_DATA)
CONDITIONAL_PURPOSES = (PURPOSE_THIRD_PARTY_IMAGES, PURPOSE_MINOR_IMAGES, PURPOSE_CROSS_BORDER)

DOCUMENTS: dict[str, dict[str, Any]] = {
    PURPOSE_SERVICE: {
        "version": "2026-11-08",
        "title": "Публичная оферта",
        "canonical_path": "public-preview/offer.html",
        "pinned_hash": CANONICAL_OFFER_HASH,
        "approved": True,
        "legal_status": "APPROVED",
        "text": None,
    },
    PURPOSE_PERSONAL_DATA: {
        "version": "2026-11-08",
        "title": "Согласие на обработку персональных данных",
        "canonical_path": "public-preview/privacy.html",
        "pinned_hash": CANONICAL_PRIVACY_HASH,
        "approved": True,
        "legal_status": "APPROVED",
        "text": None,
    },
    PURPOSE_THIRD_PARTY_IMAGES: {
        "version": "2026-11-08",
        "title": "Подтверждение прав на изображение третьего лица",
        "canonical_path": None,
        "pinned_hash": None,
        "approved": False,
        "legal_status": "BLOCKED_PENDING_LEGAL_APPROVAL",
        "text": "Подтверждение, что заказчик вправе передать изображение и получил согласие изображённого лица.",
    },
    PURPOSE_MINOR_IMAGES: {
        "version": "2026-11-08",
        "title": "Изображение несовершеннолетнего",
        "canonical_path": None,
        "pinned_hash": None,
        "approved": False,
        "legal_status": "BLOCKED_PENDING_LEGAL_APPROVAL",
        "text": "Подтверждение согласия законного представителя на обработку изображения несовершеннолетнего.",
    },
    PURPOSE_CROSS_BORDER: {
        "version": "2026-11-08",
        "title": "Трансграничная передача",
        "canonical_path": None,
        "pinned_hash": None,
        "approved": False,
        "legal_status": "BLOCKED_PENDING_LEGAL_APPROVAL",
        "text": "Отдельное согласие на передачу изображения за пределы территории Российской Федерации.",
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS consent_records (
    id TEXT PRIMARY KEY,
    order_ref TEXT NOT NULL,
    purpose TEXT NOT NULL,
    document_version TEXT NOT NULL,
    document_hash TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('granted', 'withdrawn')),
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consent_order ON consent_records(order_ref, purpose, recorded_at);
"""


def feature_enabled() -> bool:
    """The public feature stays off unless the owner explicitly enables it."""
    return os.getenv(FEATURE_FLAG, "0") == "1"


def canonical_content_hash(content: bytes | str) -> str:
    """Compute deterministic SHA-256 digest normalized to LF newlines."""
    if isinstance(content, str):
        normalized = content.replace("\r\n", "\n").encode("utf-8")
    else:
        normalized = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def _resolve_canonical_file(rel_path: str) -> Path | None:
    """Resolve a path relative to repository root or backend parent."""
    candidates = [
        Path(rel_path),
        Path(__file__).resolve().parent / rel_path,
        Path(__file__).resolve().parent.parent / rel_path,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def document_hash(purpose: str) -> str:
    """Return cryptographic hash of approved document or text content."""
    if purpose not in DOCUMENTS:
        raise ValueError(f"unknown consent purpose: {purpose}")
    doc = DOCUMENTS[purpose]

    # If text is explicitly provided or monkeypatched in tests, hash the text payload
    if doc.get("text") is not None:
        payload = f"{doc.get('title', '')}|{doc.get('version', '')}|{doc['text']}"
        return canonical_content_hash(payload)

    # For approved documents with canonical files, hash canonical file contents
    rel_path = doc.get("canonical_path")
    if rel_path:
        file_path = _resolve_canonical_file(rel_path)
        if file_path is not None:
            computed = canonical_content_hash(file_path.read_bytes())
            pinned = doc.get("pinned_hash")
            if pinned and not hmac.compare_digest(computed, pinned):
                raise RuntimeError(
                    f"Canonical document integrity failure for {purpose}: "
                    f"computed {computed} != pinned {pinned}"
                )
            return computed
        if doc.get("pinned_hash"):
            return doc["pinned_hash"]

    payload = f"{doc.get('title', '')}|{doc.get('version', '')}|{doc.get('text', '')}"
    return canonical_content_hash(payload)


def verify_document_hash(purpose: str, candidate_hash: str) -> bool:
    """Constant-time verification of supplied document hash."""
    if not candidate_hash or purpose not in DOCUMENTS:
        return False
    return hmac.compare_digest(document_hash(purpose), candidate_hash.strip().lower())


def verify_document_version(purpose: str, candidate_version: str) -> bool:
    """Verify supplied document version matches pinned version."""
    if not candidate_version or purpose not in DOCUMENTS:
        return False
    return DOCUMENTS[purpose].get("version") == candidate_version.strip()


def initialize(conn: sqlite3.Connection) -> None:
    """Create the schema. Writers call this; readers use ``_schema_ready`` instead."""
    conn.executescript(SCHEMA)
    conn.commit()


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """Read-only check: never runs DDL and never commits the caller's transaction (H-07)."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='consent_records'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def record_consent(conn: sqlite3.Connection, *, order_ref: str, purpose: str,
                   action: str = "granted", recorded_at: str | None = None,
                   expected_hash: str | None = None, expected_version: str | None = None,
                   auto_commit: bool = True, allow_unapproved: bool = False) -> dict[str, Any]:
    """Append a consent event; returns the stored record."""
    if purpose not in DOCUMENTS:
        raise ValueError(f"unknown consent purpose: {purpose}")
    doc = DOCUMENTS[purpose]
    if not doc.get("approved", True) and not allow_unapproved:
        raise ValueError(
            f"purpose '{purpose}' cannot be granted: {doc.get('legal_status', 'NOT_APPROVED')}"
        )
    if action not in ("granted", "withdrawn"):
        raise ValueError("action must be granted or withdrawn")
    if not order_ref:
        raise ValueError("order_ref is required")

    actual_hash = document_hash(purpose)
    actual_version = doc["version"]

    if expected_hash is not None and not hmac.compare_digest(actual_hash, expected_hash.strip().lower()):
        raise ValueError(f"document hash mismatch for {purpose}: expected {expected_hash}, actual {actual_hash}")
    if expected_version is not None and expected_version.strip() != actual_version:
        raise ValueError(f"document version mismatch for {purpose}: expected {expected_version}, actual {actual_version}")

    if not _schema_ready(conn):
        conn.execute("""
        CREATE TABLE IF NOT EXISTS consent_records (
            id TEXT PRIMARY KEY,
            order_ref TEXT NOT NULL,
            purpose TEXT NOT NULL,
            document_version TEXT NOT NULL,
            document_hash TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('granted', 'withdrawn')),
            recorded_at TEXT NOT NULL
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_consent_order ON consent_records(order_ref, purpose, recorded_at)")

    record = {
        "id": uuid.uuid4().hex,
        "order_ref": order_ref,
        "purpose": purpose,
        "document_version": actual_version,
        "document_hash": actual_hash,
        "action": action,
        "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(),
    }
    conn.execute(
        "INSERT INTO consent_records (id, order_ref, purpose, document_version, document_hash, action, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        tuple(record.values()),
    )
    if auto_commit:
        conn.commit()
    return record


def withdraw_consent(conn: sqlite3.Connection, *, order_ref: str, purpose: str,
                     recorded_at: str | None = None) -> dict[str, Any]:
    return record_consent(conn, order_ref=order_ref, purpose=purpose,
                          action="withdrawn", recorded_at=recorded_at)


def current_state(conn: sqlite3.Connection, order_ref: str) -> dict[str, str]:
    """Latest action per purpose for an order."""
    if not _schema_ready(conn):
        return {}
    # H-04: authoritative order is the immutable insertion order, not a caller-supplied
    # timestamp. A future-dated grant can therefore never override a later withdrawal.
    rows = conn.execute(
        "SELECT purpose, action FROM consent_records WHERE order_ref=? ORDER BY rowid",
        (order_ref,),
    ).fetchall()
    state: dict[str, str] = {}
    for purpose, action in rows:
        state[purpose] = action
    return state


def check_required(conn: sqlite3.Connection, order_ref: str,
                   requires_cross_border: bool = False,
                   has_third_party_images: bool = False,
                   has_minor_images: bool = False) -> list[str]:
    """Purposes that still lack a valid grant for this order."""
    state = current_state(conn, order_ref)
    needed = list(MANDATORY_PURPOSES)
    if has_third_party_images:
        needed.append(PURPOSE_THIRD_PARTY_IMAGES)
    if has_minor_images:
        needed.append(PURPOSE_MINOR_IMAGES)
    if requires_cross_border:
        needed.append(PURPOSE_CROSS_BORDER)
    return [purpose for purpose in needed if state.get(purpose) != "granted"]


def evidence(conn: sqlite3.Connection, order_ref: str) -> list[dict[str, Any]]:
    """Full auditable history for an order (no personal data beyond the order reference)."""
    if not _schema_ready(conn):
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM consent_records WHERE order_ref=? ORDER BY rowid", (order_ref,)
    ).fetchall()
    return [dict(row) for row in rows]
