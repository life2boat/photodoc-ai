"""Immutable backup-set contract tests (Stage 1, spec §2.4).

The previous format left `backup_manifest.json` unbound: its timestamp, verification
result and database hash could be edited without touching either artifact checksum.
These tests attack the metadata directly.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from datetime import timedelta
from pathlib import Path

import pytest

from evidence_fixtures import (
    APPLICATION_ID,
    NOW,
    build_database,
    make_set,
    uploads_tar,
    _edit_manifest,
)

import backup_format


@pytest.fixture()
def live(tmp_path: Path) -> Path:
    return build_database(tmp_path / "live.db")


@pytest.fixture()
def good_set(tmp_path, live) -> Path:
    path = make_set(live, tmp_path / "backups" / "20260101T000000Z")
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert ok, report["errors"]
    return path


# ------------------------------------------------------------------ positive control
def test_a_faithful_set_verifies(good_set):
    ok, report = backup_format.verify_set(good_set, expected_application_id=APPLICATION_ID)
    assert ok
    assert report["checks"]["artifacts"] == {
        name: report["checks"]["artifacts"][name]
        for name in backup_format.CHECKSUMMED
    }
    assert report["created_at"]


def test_manifest_records_the_full_contract(good_set):
    manifest = json.loads((good_set / backup_format.MANIFEST_JSON).read_text())
    assert manifest["format_version"] == backup_format.FORMAT_VERSION
    assert set(manifest["artifacts"]) == set(backup_format.MANIFEST_ARTIFACTS)
    assert manifest["schema_digest"] and manifest["logical_digest"]
    assert manifest["database_identity"]["application_id"] == APPLICATION_ID
    assert manifest["verification"] == "PASS"
    assert "root" in manifest["trust_note"]


# --------------------------------------------------------------- metadata binding
def test_editing_the_validation_result_breaks_the_set(good_set):
    """The recorded PASS used to be editable; manifest.sha256 now covers it."""
    _edit_manifest(good_set, {"verification": "SKIPPED"})
    ok, report = backup_format.verify_set(good_set, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any(backup_format.MANIFEST_JSON in e for e in report["errors"])


def test_editing_the_timestamp_breaks_the_set(good_set):
    _edit_manifest(good_set, {"created_at": (NOW - timedelta(days=400)).isoformat()})
    ok, report = backup_format.verify_set(good_set, expected_application_id=APPLICATION_ID)
    assert not ok


def test_a_rebound_manifest_with_a_false_artifact_hash_is_refused(tmp_path, live):
    """Recomputing manifest.sha256 cannot hide a disagreement with the artifacts."""
    path = make_set(live, tmp_path / "backups" / "s1")
    manifest = json.loads((path / backup_format.MANIFEST_JSON).read_text())
    manifest["artifacts"][backup_format.DB_FILE] = "a" * 64
    (path / backup_format.MANIFEST_JSON).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    lines = [f"{backup_format.sha256_file(path / n)}  {n}"
             for n in backup_format.CHECKSUMMED]
    (path / backup_format.MANIFEST_FILE).write_text("\n".join(lines) + "\n")

    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("disagrees with the file" in e for e in report["errors"])


def test_a_rebound_manifest_with_a_false_digest_passes_format_but_fails_the_gate(tmp_path, live):
    """Digests are bound to the *content* by the gate, not by the checksum alone."""
    path = make_set(live, tmp_path / "backups" / "s1")
    manifest = json.loads((path / backup_format.MANIFEST_JSON).read_text())
    manifest["schema_digest"] = "b" * 64
    (path / backup_format.MANIFEST_JSON).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    lines = [f"{backup_format.sha256_file(path / n)}  {n}"
             for n in backup_format.CHECKSUMMED]
    (path / backup_format.MANIFEST_FILE).write_text("\n".join(lines) + "\n")

    ok, _ = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert ok, "the checksum contract alone cannot see this"

    import evidence_gate
    code, report = evidence_gate.evaluate(live, path, expected_application_id=APPLICATION_ID,
                                          max_age_hours=48)
    assert code == evidence_gate.EXIT_INVALID
    assert any("schema_digest" in e for e in report["errors"])


# -------------------------------------------------------------------- file inventory
def test_a_missing_file_is_refused(good_set):
    (good_set / backup_format.TAR_FILE).unlink()
    ok, report = backup_format.verify_set(good_set)
    assert not ok
    assert any("required file missing" in e for e in report["errors"])


def test_an_extra_file_is_refused(good_set):
    (good_set / "notes.txt").write_text("hi")
    ok, report = backup_format.verify_set(good_set)
    assert not ok
    assert any("unexpected files" in e for e in report["errors"])


def test_an_incomplete_marker_is_refused(good_set):
    (good_set / backup_format.PARTIAL_MARKER).write_text("x")
    ok, report = backup_format.verify_set(good_set)
    assert not ok


def test_a_staging_directory_is_not_a_published_set(tmp_path, live):
    path = make_set(live, tmp_path / "backups" / "20260101T000000Z")
    staging = path.with_name(backup_format.TMP_PREFIX + path.name)
    os.replace(path, staging)
    ok, report = backup_format.verify_set(staging)
    assert not ok
    assert any("staging" in e for e in report["errors"])


@pytest.mark.skipif(os.name == "nt" and not hasattr(os, "symlink"),
                    reason="symlinks unavailable")
def test_a_symlinked_artifact_is_refused(good_set, tmp_path):
    target = good_set / backup_format.TAR_FILE
    real = tmp_path / "elsewhere.tar.gz"
    real.write_bytes(target.read_bytes())
    try:
        target.unlink()
        target.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")
    ok, report = backup_format.verify_set(good_set)
    assert not ok


# ---------------------------------------------------------------------- checksums
def test_a_missing_checksum_entry_is_refused(good_set):
    path = good_set / backup_format.MANIFEST_FILE
    kept = [line for line in path.read_text().splitlines()
            if backup_format.MANIFEST_JSON not in line]
    path.write_text("\n".join(kept) + "\n")
    ok, report = backup_format.verify_set(good_set)
    assert not ok
    assert any("does not cover" in e for e in report["errors"])


def test_a_duplicate_checksum_entry_is_refused(good_set):
    path = good_set / backup_format.MANIFEST_FILE
    text = path.read_text()
    path.write_text(text + text.splitlines()[0] + "\n")
    ok, report = backup_format.verify_set(good_set)
    assert not ok
    assert any("duplicate" in e for e in report["errors"])


def test_a_checksum_naming_a_path_is_refused(good_set):
    path = good_set / backup_format.MANIFEST_FILE
    digest = backup_format.sha256_file(good_set / backup_format.DB_FILE)
    path.write_text(f"{digest}  ../orders.db\n")
    ok, report = backup_format.verify_set(good_set)
    assert not ok


# ------------------------------------------------------------------ manifest shape
def test_an_unsupported_format_version_is_refused(tmp_path, live):
    path = make_set(live, tmp_path / "backups" / "s1")
    _edit_manifest(path, {"format_version": 1}, rebind=True)
    ok, report = backup_format.verify_set(path)
    assert not ok
    assert any("format_version" in e for e in report["errors"])


def test_a_missing_digest_is_refused(tmp_path, live):
    path = make_set(live, tmp_path / "backups" / "s1")
    _edit_manifest(path, {"logical_digest": None}, rebind=True)
    ok, report = backup_format.verify_set(path)
    assert not ok
    assert any("logical_digest" in e for e in report["errors"])


def test_a_missing_identity_is_refused(tmp_path, live):
    path = make_set(live, tmp_path / "backups" / "s1")
    _edit_manifest(path, {"database_identity": {}}, rebind=True)
    ok, report = backup_format.verify_set(path)
    assert not ok


def test_a_wrong_application_id_is_refused(good_set):
    ok, report = backup_format.verify_set(good_set, expected_application_id=42)
    assert not ok
    assert any("application_id" in e for e in report["errors"])


def test_an_unverified_set_is_refused_unless_allowed(tmp_path, live):
    path = make_set(live, tmp_path / "backups" / "s1", verification="SKIPPED")
    ok, report = backup_format.verify_set(path)
    assert not ok
    ok, _ = backup_format.verify_set(path, require_verified=False)
    assert ok


# ----------------------------------------------------------------------- archive
def _tampered_archive(good_set: Path, members) -> None:
    uploads = good_set.parent / "tamper-uploads"
    uploads.mkdir(exist_ok=True)
    import shutil
    source = backup_format.TAR_FILE
    good = good_set / source
    # rebuild with hostile members, keeping the original files
    with tarfile.open(good, "r:gz") as tar:
        original = [(m, tar.extractfile(m).read() if m.isreg() else None)
                    for m in tar.getmembers()]
    target = good_set / source
    with tarfile.open(target, "w:gz") as tar:
        for member, data in original:
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
        for member, data in members:
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    # rebind the checksums so only the archive inspection can catch this
    manifest = json.loads((good_set / backup_format.MANIFEST_JSON).read_text())
    manifest["artifacts"][backup_format.TAR_FILE] = backup_format.sha256_file(target)
    (good_set / backup_format.MANIFEST_JSON).write_text(
        json.dumps(manifest, indent=2, sort_keys=True))
    lines = [f"{backup_format.sha256_file(good_set / n)}  {n}"
             for n in backup_format.CHECKSUMMED]
    (good_set / backup_format.MANIFEST_FILE).write_text("\n".join(lines) + "\n")


def test_an_archive_with_a_duplicate_member_is_refused(good_set):
    member = tarfile.TarInfo("uploads/1_photo.jpg")
    _tampered_archive(good_set, [(member, b"duplicate")])
    ok, report = backup_format.verify_set(good_set)
    assert not ok
    assert any("duplicate archive member" in e for e in report["errors"])


def test_an_archive_with_a_link_is_refused(good_set):
    member = tarfile.TarInfo("uploads/link")
    member.type = tarfile.SYMTYPE
    member.linkname = "/etc/passwd"
    _tampered_archive(good_set, [(member, None)])
    ok, report = backup_format.verify_set(good_set)
    assert not ok
    assert any("link member" in e for e in report["errors"])


def test_an_archive_member_escaping_the_root_is_refused(good_set):
    member = tarfile.TarInfo("../../etc/passwd")
    _tampered_archive(good_set, [(member, b"x")])
    ok, report = backup_format.verify_set(good_set)
    assert not ok
    assert any("escapes the root" in e for e in report["errors"])


def test_member_count_must_match_the_manifest(good_set):
    member = tarfile.TarInfo("uploads/extra.bin")
    _tampered_archive(good_set, [(member, b"x")])
    ok, report = backup_format.verify_set(good_set)
    assert not ok
    assert any("member count" in e for e in report["errors"])


# -------------------------------------------------------------------------- age
def test_age_is_computed_from_the_manifest(good_set):
    manifest = json.loads((good_set / backup_format.MANIFEST_JSON).read_text())
    age = backup_format.age_hours(manifest["created_at"])
    assert 0 <= age < 1


def test_age_of_a_future_timestamp_is_negative(good_set):
    manifest = json.loads((good_set / backup_format.MANIFEST_JSON).read_text())
    future = (NOW + timedelta(hours=5)).isoformat()
    assert backup_format.age_hours(future) < 0
    assert manifest["created_at"]
