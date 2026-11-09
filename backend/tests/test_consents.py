"""Consent evidence: versioning, mandatory purposes, withdrawal, minimisation."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consents  # noqa: E402


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


def test_feature_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(consents.FEATURE_FLAG, raising=False)
    assert consents.feature_enabled() is False
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    assert consents.feature_enabled() is True


def test_grant_records_version_and_hash(db):
    record = consents.record_consent(db, order_ref="ord-1", purpose=consents.PURPOSE_SERVICE)

    assert record["document_version"] == consents.DOCUMENTS[consents.PURPOSE_SERVICE]["version"]
    assert record["document_hash"] == consents.document_hash(consents.PURPOSE_SERVICE)
    assert record["action"] == "granted"
    assert len(record["id"]) == 32


def test_mandatory_purposes_must_be_granted(db):
    assert set(consents.check_required(db, "ord-2")) == set(consents.MANDATORY_PURPOSES)

    consents.record_consent(db, order_ref="ord-2", purpose=consents.PURPOSE_SERVICE)
    assert consents.check_required(db, "ord-2") == [consents.PURPOSE_PERSONAL_DATA]

    consents.record_consent(db, order_ref="ord-2", purpose=consents.PURPOSE_PERSONAL_DATA)
    assert consents.check_required(db, "ord-2") == []


def test_conditional_purposes_are_required_only_when_applicable(db):
    consents.record_consent(db, order_ref="ord-3", purpose=consents.PURPOSE_SERVICE)
    consents.record_consent(db, order_ref="ord-3", purpose=consents.PURPOSE_PERSONAL_DATA)

    assert consents.check_required(db, "ord-3") == []
    assert consents.check_required(db, "ord-3", has_third_party_images=True) == [consents.PURPOSE_THIRD_PARTY_IMAGES]
    assert consents.check_required(db, "ord-3", has_minor_images=True) == [consents.PURPOSE_MINOR_IMAGES]
    assert consents.check_required(db, "ord-3", requires_cross_border=True) == [consents.PURPOSE_CROSS_BORDER]


def test_cross_border_is_a_separate_consent_not_bundled(db):
    consents.record_consent(db, order_ref="ord-4", purpose=consents.PURPOSE_SERVICE)
    consents.record_consent(db, order_ref="ord-4", purpose=consents.PURPOSE_PERSONAL_DATA)

    missing = consents.check_required(db, "ord-4", requires_cross_border=True)
    assert missing == [consents.PURPOSE_CROSS_BORDER], "cross-border must not be implied by the service consent"


def test_withdrawal_is_appended_and_blocks_service(db):
    consents.record_consent(db, order_ref="ord-5", purpose=consents.PURPOSE_SERVICE)
    consents.record_consent(db, order_ref="ord-5", purpose=consents.PURPOSE_PERSONAL_DATA)
    consents.withdraw_consent(db, order_ref="ord-5", purpose=consents.PURPOSE_PERSONAL_DATA)

    history = consents.evidence(db, "ord-5")
    actions = [(row["purpose"], row["action"]) for row in history]
    assert (consents.PURPOSE_PERSONAL_DATA, "granted") in actions
    assert (consents.PURPOSE_PERSONAL_DATA, "withdrawn") in actions
    assert consents.check_required(db, "ord-5") == [consents.PURPOSE_PERSONAL_DATA]


def test_evidence_stores_no_personal_data(db):
    record = consents.record_consent(db, order_ref="ord-6", purpose=consents.PURPOSE_SERVICE)
    assert set(record) == {"id", "order_ref", "purpose", "document_version",
                           "document_hash", "action", "recorded_at"}


def test_unknown_purpose_is_rejected(db):
    with pytest.raises(ValueError):
        consents.record_consent(db, order_ref="ord-7", purpose="marketing_forever")


def test_document_hash_changes_with_the_text(db, monkeypatch):
    original = consents.document_hash(consents.PURPOSE_SERVICE)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "text", "Изменённый текст")
    assert consents.document_hash(consents.PURPOSE_SERVICE) != original
