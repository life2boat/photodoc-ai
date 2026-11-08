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
* ``check_required()`` returns the purposes still missing a valid grant, so the caller can
  refuse to accept a photo until the mandatory consents exist.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

FEATURE_FLAG = "CONSENT_FEATURE_ENABLED"

# Purpose identifiers. Adding a purpose is a deliberate act: the tests assert the exact set.
PURPOSE_SERVICE = "service_terms"
PURPOSE_PERSONAL_DATA = "personal_data_processing"
PURPOSE_THIRD_PARTY_IMAGES = "third_party_images"
PURPOSE_MINOR_IMAGES = "minor_images"
PURPOSE_CROSS_BORDER = "cross_border_transfer"

MANDATORY_PURPOSES = (PURPOSE_SERVICE, PURPOSE_PERSONAL_DATA)
CONDITIONAL_PURPOSES = (PURPOSE_THIRD_PARTY_IMAGES, PURPOSE_MINOR_IMAGES, PURPOSE_CROSS_BORDER)

DOCUMENTS: dict[str, dict[str, str]] = {
    PURPOSE_SERVICE: {
        "version": "2026-11-08",
        "title": "Публичная оферта",
        "text": "Условия оказания услуг PhotoDoc AI (проект, вступает в силу после запуска).",
    },
    PURPOSE_PERSONAL_DATA: {
        "version": "2026-11-08",
        "title": "Согласие на обработку персональных данных",
        "text": "Согласие на обработку персональных данных для оказания услуг.",
    },
    PURPOSE_THIRD_PARTY_IMAGES: {
        "version": "2026-11-08",
        "title": "Подтверждение прав на изображение третьего лица",
        "text": "Подтверждение, что заказчик вправе передать изображение и получил согласие изображённого лица.",
    },
    PURPOSE_MINOR_IMAGES: {
        "version": "2026-11-08",
        "title": "Изображение несовершеннолетнего",
        "text": "Подтверждение согласия законного представителя на обработку изображения несовершеннолетнего.",
    },
    PURPOSE_CROSS_BORDER: {
        "version": "2026-11-08",
        "title": "Трансграничная передача",
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


def document_hash(purpose: str) -> str:
    doc = DOCUMENTS[purpose]
    payload = f"{doc['title']}|{doc['version']}|{doc['text']}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def record_consent(conn: sqlite3.Connection, *, order_ref: str, purpose: str,
                   action: str = "granted", recorded_at: str | None = None) -> dict[str, Any]:
    """Append a consent event; returns the stored record."""
    if purpose not in DOCUMENTS:
        raise ValueError(f"unknown consent purpose: {purpose}")
    if action not in ("granted", "withdrawn"):
        raise ValueError("action must be granted or withdrawn")
    if not order_ref:
        raise ValueError("order_ref is required")
    initialize(conn)
    record = {
        "id": uuid.uuid4().hex,
        "order_ref": order_ref,
        "purpose": purpose,
        "document_version": DOCUMENTS[purpose]["version"],
        "document_hash": document_hash(purpose),
        "action": action,
        "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(),
    }
    conn.execute(
        "INSERT INTO consent_records (id, order_ref, purpose, document_version, document_hash, action, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        tuple(record.values()),
    )
    conn.commit()
    return record


def withdraw_consent(conn: sqlite3.Connection, *, order_ref: str, purpose: str,
                     recorded_at: str | None = None) -> dict[str, Any]:
    return record_consent(conn, order_ref=order_ref, purpose=purpose,
                          action="withdrawn", recorded_at=recorded_at)


def current_state(conn: sqlite3.Connection, order_ref: str) -> dict[str, str]:
    """Latest action per purpose for an order."""
    initialize(conn)
    rows = conn.execute(
        "SELECT purpose, action FROM consent_records WHERE order_ref=? ORDER BY recorded_at, rowid",
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
    initialize(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM consent_records WHERE order_ref=? ORDER BY recorded_at, rowid", (order_ref,)
    ).fetchall()
    return [dict(row) for row in rows]
