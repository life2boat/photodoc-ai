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


@pytest.fixture
def synthetic_approval(monkeypatch):
    """Isolated synthetic approval for tests exercising consent recording mechanics."""
    for p in consents.MANDATORY_PURPOSES:
        monkeypatch.setitem(consents.DOCUMENTS[p], "approved", True)
        monkeypatch.setitem(consents.DOCUMENTS[p], "legal_status", "SYNTHETIC_TEST_APPROVED")


def test_feature_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(consents.FEATURE_FLAG, raising=False)
    assert consents.feature_enabled() is False
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    assert consents.feature_enabled() is True


def test_all_documents_unapproved_by_default(db):
    assert consents.mandatory_documents_approved() is False
    for purpose, doc in consents.DOCUMENTS.items():
        assert doc["approved"] is False
        assert consents.is_document_approved(purpose) is False
        with pytest.raises(ValueError, match="cannot be granted"):
            consents.record_consent(db, order_ref="ord-unapproved", purpose=purpose)


def test_record_consent_allows_unapproved_only_with_explicit_flag(db):
    with pytest.raises(ValueError, match="cannot be granted"):
        consents.record_consent(db, order_ref="ord-u", purpose=consents.PURPOSE_SERVICE)

    rec = consents.record_consent(db, order_ref="ord-u", purpose=consents.PURPOSE_SERVICE, allow_unapproved=True)
    assert rec["purpose"] == consents.PURPOSE_SERVICE


def test_grant_records_version_and_hash(db, synthetic_approval):
    record = consents.record_consent(db, order_ref="ord-1", purpose=consents.PURPOSE_SERVICE)

    assert record["document_version"] == consents.DOCUMENTS[consents.PURPOSE_SERVICE]["version"]
    assert record["document_hash"] == consents.document_hash(consents.PURPOSE_SERVICE)
    assert record["action"] == "granted"
    assert len(record["id"]) == 32


def test_mandatory_purposes_must_be_granted(db, synthetic_approval):
    assert set(consents.check_required(db, "ord-2")) == set(consents.MANDATORY_PURPOSES)

    consents.record_consent(db, order_ref="ord-2", purpose=consents.PURPOSE_SERVICE)
    assert consents.check_required(db, "ord-2") == [consents.PURPOSE_PERSONAL_DATA]

    consents.record_consent(db, order_ref="ord-2", purpose=consents.PURPOSE_PERSONAL_DATA)
    assert consents.check_required(db, "ord-2") == []


def test_conditional_purposes_are_required_only_when_applicable(db, synthetic_approval):
    consents.record_consent(db, order_ref="ord-3", purpose=consents.PURPOSE_SERVICE)
    consents.record_consent(db, order_ref="ord-3", purpose=consents.PURPOSE_PERSONAL_DATA)

    assert consents.check_required(db, "ord-3") == []
    assert consents.check_required(db, "ord-3", has_third_party_images=True) == [consents.PURPOSE_THIRD_PARTY_IMAGES]
    assert consents.check_required(db, "ord-3", has_minor_images=True) == [consents.PURPOSE_MINOR_IMAGES]
    assert consents.check_required(db, "ord-3", requires_cross_border=True) == [consents.PURPOSE_CROSS_BORDER]


def test_cross_border_is_a_separate_consent_not_bundled(db, synthetic_approval):
    consents.record_consent(db, order_ref="ord-4", purpose=consents.PURPOSE_SERVICE)
    consents.record_consent(db, order_ref="ord-4", purpose=consents.PURPOSE_PERSONAL_DATA)

    missing = consents.check_required(db, "ord-4", requires_cross_border=True)
    assert missing == [consents.PURPOSE_CROSS_BORDER], "cross-border must not be implied by the service consent"


def test_withdrawal_is_appended_and_blocks_service(db, synthetic_approval):
    consents.record_consent(db, order_ref="ord-5", purpose=consents.PURPOSE_SERVICE)
    consents.record_consent(db, order_ref="ord-5", purpose=consents.PURPOSE_PERSONAL_DATA)
    consents.withdraw_consent(db, order_ref="ord-5", purpose=consents.PURPOSE_PERSONAL_DATA)

    history = consents.evidence(db, "ord-5")
    actions = [(row["purpose"], row["action"]) for row in history]
    assert (consents.PURPOSE_PERSONAL_DATA, "granted") in actions
    assert (consents.PURPOSE_PERSONAL_DATA, "withdrawn") in actions
    assert consents.check_required(db, "ord-5") == [consents.PURPOSE_PERSONAL_DATA]


def test_evidence_stores_no_personal_data(db, synthetic_approval):
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


def test_m02_canonical_document_hashes_match_approved():
    assert consents.document_hash(consents.PURPOSE_SERVICE) == consents.CANONICAL_OFFER_HASH
    assert consents.document_hash(consents.PURPOSE_PERSONAL_DATA) == consents.CANONICAL_PRIVACY_HASH
    assert consents.verify_document_hash(consents.PURPOSE_SERVICE, consents.CANONICAL_OFFER_HASH)
    assert consents.verify_document_hash(consents.PURPOSE_PERSONAL_DATA, consents.CANONICAL_PRIVACY_HASH)
    assert not consents.verify_document_hash(consents.PURPOSE_SERVICE, "deadbeef" * 8)


def test_m02_conditional_purposes_are_blocked_pending_approval(db):
    for cond_purpose in consents.CONDITIONAL_PURPOSES:
        assert consents.DOCUMENTS[cond_purpose]["approved"] is False
        assert consents.DOCUMENTS[cond_purpose]["legal_status"] == "BLOCKED_PENDING_LEGAL_APPROVAL"
        with pytest.raises(ValueError, match="(?i)BLOCKED_PENDING_LEGAL_APPROVAL"):
            consents.record_consent(db, order_ref="ord-blocked", purpose=cond_purpose)



def test_m02_record_consent_validates_hash_and_version(db, synthetic_approval):
    # Valid hash and version succeed
    rec = consents.record_consent(
        db, order_ref="ord-v", purpose=consents.PURPOSE_SERVICE,
        expected_hash=consents.CANONICAL_OFFER_HASH,
        expected_version="2026-11-08",
    )
    assert rec["document_hash"] == consents.CANONICAL_OFFER_HASH

    # Hash mismatch fails
    with pytest.raises(ValueError, match="document hash mismatch"):
        consents.record_consent(
            db, order_ref="ord-v", purpose=consents.PURPOSE_SERVICE,
            expected_hash="0000000000000000000000000000000000000000000000000000000000000000",
        )

    # Version mismatch fails
    with pytest.raises(ValueError, match="document version mismatch"):
        consents.record_consent(
            db, order_ref="ord-v", purpose=consents.PURPOSE_SERVICE,
            expected_version="1999-01-01",
        )

