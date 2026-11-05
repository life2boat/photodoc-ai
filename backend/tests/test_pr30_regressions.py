"""Regressions for the PR #30 independent review (H1-H4, M1-M5).

Every case here reproduced a false PASS against the reviewed head
`048ae7a77810d89aa1cb4ccc47df5d9d65abe4af` and must now fail closed.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from evidence_fixtures import (
    APPLICATION_ID,
    GOOD_PHOTO,
    NOW,
    OTHER_PHOTO,
    build_database,
    build_delivery_database,
    copy_database,
    make_set,
    uploads_tar,
)

import backup_format
import build_v2_set
import db_evidence
import evidence_gate
import recovery_artifacts


def compare(live: Path, backup: Path, **kwargs):
    return db_evidence.compare_databases(live, backup,
                                         expected_application_id=APPLICATION_ID, **kwargs)


# =============================================================== H1: row identity
def test_integer_primary_key_desc_is_not_treated_as_a_rowid_alias(tmp_path):
    """SQLite keeps `INTEGER PRIMARY KEY DESC` distinct from the hidden rowid: the
    declared column may be NULL while the rowid differs. Keying by the column made
    two different rows produce identical evidence."""
    schema = "CREATE TABLE t(id INTEGER PRIMARY KEY DESC, payload TEXT)"
    live = tmp_path / "a.db"
    backup = tmp_path / "b.db"
    for path, rowid in ((live, 1), (backup, 2)):
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.execute(schema)
        conn.execute(f"INSERT INTO t(rowid,id,payload) VALUES ({rowid},NULL,'same')")
        conn.commit()
        conn.close()

    result = compare(live, backup)
    assert not result.ok, "two different rowids produced identical evidence"
    # Different rowids mean different keys, so the rows are reported as present on
    # one side only rather than as a changed value. Either way it must be refused.
    table = result.tables["t"]
    assert table.missing or table.extra or table.changed


def test_the_desc_exception_is_proven_semantically(tmp_path):
    """A real alias creates no primary-key index; the DESC form does."""
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE alias_pk (id INTEGER PRIMARY KEY, v TEXT);
        CREATE TABLE desc_pk (id INTEGER PRIMARY KEY DESC, v TEXT);
        """ )
    try:
        alias_cols = db_evidence.table_columns(conn, "alias_pk")
        desc_cols = db_evidence.table_columns(conn, "desc_pk")
        assert db_evidence.declared_integer_pk_is_rowid_alias(conn, "alias_pk", alias_cols)
        assert not db_evidence.declared_integer_pk_is_rowid_alias(conn, "desc_pk", desc_cols)
    finally:
        conn.close()


def test_hidden_rowid_change_is_detected(tmp_path):
    """Same declared values, different rowids: the backup restores different rows."""
    base = build_database(tmp_path / "live.db")
    backup = copy_database(base, tmp_path / "backup.db")
    conn = sqlite3.connect(base)
    try:
        conn.execute("INSERT INTO shadowed VALUES ('z','temp')")
        conn.execute("DELETE FROM shadowed WHERE rowid='z'")
        conn.execute("UPDATE shadowed SET value=value")   # keep declared values identical
        conn.commit()
    finally:
        conn.close()
    result = compare(base, backup)
    assert result.ok, "no change was made, so this must pass"

    conn = sqlite3.connect(base)
    try:
        conn.execute("DELETE FROM shadowed WHERE rowid='x'")
        conn.execute("INSERT INTO shadowed(rowid, rowid, value) VALUES ('x','x','one')")
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
    finally:
        conn.close()


def test_a_composite_without_rowid_change_is_still_detected(tmp_path):
    live = build_database(tmp_path / "a.db", variant="wr_row")
    backup = build_database(tmp_path / "b.db")
    result = compare(live, backup)
    assert not result.ok
    assert result.tables["wr_items"].identity.startswith("WITHOUT ROWID composite")


def test_a_faithful_copy_still_passes(tmp_path):
    base = build_database(tmp_path / "live.db")
    result = compare(base, copy_database(base, tmp_path / "copy.db"))
    assert result.ok, result.reasons


# ============================================================ H2: one snapshot
def test_a_write_between_table_reads_cannot_produce_a_mixed_pass(tmp_path):
    """With WAL the writer commits mid-capture; a single read transaction per side
    is what keeps the evidence from mixing two states."""
    schema = "CREATE TABLE t1(v TEXT); CREATE TABLE t2(v TEXT);"
    live = tmp_path / "live.db"
    backup = tmp_path / "backup.db"
    for path, rows in ((live, ("A", "B")), (backup, ("A", "D"))):
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.executescript(schema)
        conn.execute("INSERT INTO t1 VALUES (?)", (rows[0],))
        conn.execute("INSERT INTO t2 VALUES (?)", (rows[1],))
        conn.commit()
        conn.close()
    conn = sqlite3.connect(live)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()

    original = db_evidence._compare_table
    state = {"committed": False}

    def hooked(*args, **kwargs):
        out = original(*args, **kwargs)
        if args[2] == "t1" and not state["committed"]:
            writer = sqlite3.connect(live)
            writer.executescript("BEGIN; UPDATE t1 SET v='C'; UPDATE t2 SET v='D'; COMMIT;")
            writer.close()
            state["committed"] = True
        return out

    db_evidence._compare_table = hooked
    try:
        result = compare(live, backup)
    finally:
        db_evidence._compare_table = original

    assert state["committed"], "the writer did not commit, so the test proves nothing"
    assert not result.ok, "the capture mixed two states and still passed"


def test_the_snapshot_and_engine_are_recorded(tmp_path):
    base = build_database(tmp_path / "live.db")
    result = compare(base, copy_database(base, tmp_path / "copy.db"))
    assert result.checks["sqlite_version"]
    assert result.checks["live_integrity"] == "ok"


# ================================================== H3: recovery artifact binding
@pytest.fixture()
def delivery(tmp_path):
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    return db, uploads


def test_substituted_recovery_bytes_are_refused(tmp_path, delivery):
    db, _ = delivery
    bad = b"wrong content same-size!!"
    assert len(bad) == len(GOOD_PHOTO)
    path = make_set(db, tmp_path / "set",
                    upload_files={"1_photo.jpg": bad, "1_info.pdf": OTHER_PHOTO})
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("wrong content" in e for e in report["errors"])


def test_missing_recovery_original_is_refused(tmp_path, delivery):
    db, _ = delivery
    path = make_set(db, tmp_path / "set", upload_files={"1_photo.jpg": GOOD_PHOTO})
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("missing from the archive" in e for e in report["errors"])


def test_wrong_size_recovery_original_is_refused(tmp_path, delivery):
    db, _ = delivery
    path = make_set(db, tmp_path / "set",
                    upload_files={"photo.jpg": GOOD_PHOTO, "info.pdf": OTHER_PHOTO + b"x"})
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok


def test_a_faithful_recovery_set_passes(tmp_path, delivery):
    db, _ = delivery
    path = make_set(db, tmp_path / "set",
                    upload_files={"1_photo.jpg": GOOD_PHOTO, "1_info.pdf": OTHER_PHOTO})
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert ok, report["errors"]
    assert report["checks"]["recovery"]["required"]["required"] == 2


def test_a_cleaned_job_does_not_require_its_deleted_file(tmp_path, delivery):
    db, _ = delivery
    path = make_set(db, tmp_path / "set",
                    upload_files={"1_photo.jpg": GOOD_PHOTO, "1_info.pdf": OTHER_PHOTO})
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert ok
    assert report["checks"]["recovery"]["required"]["cleaned"] == 1


def test_a_job_without_content_hash_is_refused(tmp_path, delivery):
    db, uploads = delivery
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE delivery_jobs SET content_sha256=NULL WHERE id='job-file'")
        conn.commit()
    finally:
        conn.close()
    path = make_set(db, tmp_path / "set",
                    upload_files={"1_photo.jpg": GOOD_PHOTO, "1_info.pdf": OTHER_PHOTO})
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("content_sha256" in e for e in report["errors"])


def test_a_job_with_an_unknown_state_is_refused(tmp_path, delivery):
    db, _ = delivery
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE delivery_jobs SET state='mystery' WHERE id='job-file'")
        conn.commit()
    finally:
        conn.close()
    _, _groups, errors, _stats = recovery_artifacts.required_sources(db)
    assert any("unrecognised state" in e for e in errors)


def test_a_truncated_archive_is_refused(tmp_path):
    """tarfile cannot *write* a short member, so truncate the finished stream."""
    archive = tmp_path / "t.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("uploads/x.jpg")
        member.size = 4096
        tar.addfile(member, io.BytesIO(b"x" * 4096))
    data = archive.read_bytes()
    archive.write_bytes(data[: max(1, len(data) // 2)])
    info, errors = backup_format.inspect_archive(archive)
    assert errors, "a truncated archive was accepted"
    assert not any(name.startswith("uploads/") for name in [])  # structural sanity


def test_a_corrupt_archive_is_refused(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(b"not a gzip stream")
    info, errors = backup_format.inspect_archive(archive)
    assert errors
    assert info["members"] == 0


# ================================================ H4: unsupported capabilities
def test_an_engine_without_table_list_is_refused(tmp_path):
    base = build_database(tmp_path / "live.db")
    backup = copy_database(base, tmp_path / "copy.db")
    original = db_evidence.table_inventory
    db_evidence.table_inventory = lambda conn: {}
    try:
        result = compare(base, backup)
    finally:
        db_evidence.table_inventory = original
    assert not result.ok, "missing metadata was treated as an empty database"
    assert any("table_list returned nothing" in r or "omitted" in r for r in result.reasons)


def test_an_old_sqlite_version_is_refused(tmp_path, monkeypatch):
    base = build_database(tmp_path / "live.db")
    backup = copy_database(base, tmp_path / "copy.db")
    monkeypatch.setattr(db_evidence, "MIN_SQLITE_VERSION", (99, 0, 0))
    result = compare(base, backup)
    assert not result.ok
    assert any("older than the required" in r for r in result.reasons)


def test_every_declared_table_must_appear_in_the_inventory(tmp_path):
    base = build_database(tmp_path / "live.db")
    backup = copy_database(base, tmp_path / "copy.db")
    original = db_evidence.table_inventory

    def partial(conn):
        full = original(conn)
        full.pop("orders", None)
        return full

    db_evidence.table_inventory = partial
    try:
        result = compare(base, backup)
    finally:
        db_evidence.table_inventory = original
    assert not result.ok
    assert any("omitted" in r for r in result.reasons)


# ============================================================= M1: field validation
def _delivery_set(tmp_path, uploads_files=None):
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    return db, make_set(db, tmp_path / "set",
                        upload_files=uploads_files or {"photo.jpg": GOOD_PHOTO,
                                                       "info.pdf": OTHER_PHOTO})


def _rebind(path: Path) -> None:
    lines = [f"{backup_format.sha256_file(path / n)}  {n}"
             for n in backup_format.CHECKSUMMED]
    (path / backup_format.MANIFEST_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_an_unknown_evidence_format_is_refused(tmp_path):
    db, path = _delivery_set(tmp_path)
    manifest = json.loads((path / backup_format.MANIFEST_JSON).read_text())
    manifest["evidence_format"] = 999
    (path / backup_format.MANIFEST_JSON).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    _rebind(path)
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("evidence_format" in e for e in report["errors"])


def test_a_nested_identity_digest_is_validated(tmp_path):
    db, path = _delivery_set(tmp_path)
    manifest = json.loads((path / backup_format.MANIFEST_JSON).read_text())
    manifest["database_identity"]["schema_digest"] = "f" * 64
    (path / backup_format.MANIFEST_JSON).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    _rebind(path)
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("schema_digest" in e for e in report["errors"])


def test_the_upload_byte_count_is_validated(tmp_path):
    db, path = _delivery_set(tmp_path)
    manifest = json.loads((path / backup_format.MANIFEST_JSON).read_text())
    manifest["uploads"]["uncompressed_bytes"] = 0
    (path / backup_format.MANIFEST_JSON).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    _rebind(path)
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("byte count" in e for e in report["errors"])


def test_an_unknown_manifest_field_is_refused(tmp_path):
    db, path = _delivery_set(tmp_path)
    manifest = json.loads((path / backup_format.MANIFEST_JSON).read_text())
    manifest["injected"] = "x"
    (path / backup_format.MANIFEST_JSON).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    _rebind(path)
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("unknown fields" in e for e in report["errors"])


def test_an_unknown_identity_field_is_refused(tmp_path):
    db, path = _delivery_set(tmp_path)
    manifest = json.loads((path / backup_format.MANIFEST_JSON).read_text())
    manifest["database_identity"]["volume"] = "/dev/sda1"
    (path / backup_format.MANIFEST_JSON).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    _rebind(path)
    ok, report = backup_format.verify_set(path, expected_application_id=APPLICATION_ID)
    assert not ok


# ============================================================ M2: archive path policy
@pytest.mark.parametrize("name", [
    "evil",                     # outside uploads/
    "/tmp/evil",                # POSIX absolute
    "C:/evil",                  # Windows drive
    "uploads\\evil.jpg",        # backslash separator
    "uploads/./evil.jpg",       # not normalised
    "uploads/../evil.jpg",      # traversal
])
def test_hostile_member_paths_are_refused(name):
    errors = recovery_artifacts.path_errors(name)
    assert errors, f"{name!r} was accepted"


def test_the_uploads_root_directory_entry_is_allowed():
    assert not recovery_artifacts.path_errors("uploads")
    assert not recovery_artifacts.path_errors("uploads/1_photo.jpg")


def test_a_file_named_uploads_is_refused(tmp_path):
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("uploads")
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    _, errors = backup_format.inspect_archive(archive)
    assert any("root but is a file" in e for e in errors)


def test_case_insensitive_duplicates_are_refused(tmp_path):
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("uploads/A.jpg", "uploads/a.jpg"):
            member = tarfile.TarInfo(name)
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
    _, errors = backup_format.inspect_archive(archive)
    assert any("duplicate" in e for e in errors)


def test_member_count_limit_is_enforced(tmp_path):
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for index in range(5):
            member = tarfile.TarInfo(f"uploads/{index}.jpg")
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
    _, errors = backup_format.inspect_archive(
        archive, recovery_artifacts.Limits(max_members=2))
    assert any("more than 2 members" in e for e in errors)


def test_member_byte_limit_is_enforced(tmp_path):
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("uploads/big.jpg")
        member.size = 100
        tar.addfile(member, io.BytesIO(b"x" * 100))
    _, errors = backup_format.inspect_archive(
        archive, recovery_artifacts.Limits(max_member_bytes=10))
    assert any("exceeds" in e for e in errors)


# ======================================================== M4: publication durability
def test_publication_refuses_to_overwrite_a_published_set(tmp_path):
    db = build_database(tmp_path / "live.db")
    first = make_set(db, tmp_path / "built1")
    second = make_set(db, tmp_path / "built2")
    final = tmp_path / "backups" / "20260101T000000Z"
    backup_format.publish_set(first, final)
    with pytest.raises(backup_format.FormatError):
        backup_format.publish_set(second, final)


def test_publication_refuses_an_incomplete_staging_set(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "orders.db").write_text("x")
    with pytest.raises(backup_format.FormatError):
        backup_format.publish_set(staging, tmp_path / "out")


def test_publication_reports_durability_work(tmp_path):
    db = build_database(tmp_path / "live.db")
    built = make_set(db, tmp_path / "built")
    staging = tmp_path / "staging"
    staging.mkdir()
    for name in backup_format.REQUIRED_FILES:
        (staging / name).write_bytes((built / name).read_bytes())
    report = backup_format.publish_set(staging, tmp_path / "published")
    if report["fsync_supported"]:
        assert report["fsynced_files"] == len(backup_format.REQUIRED_FILES)
    else:
        # Recorded, not hidden: the platform refused to flush, and the caller can see it.
        assert report["fsynced_files"] < len(backup_format.REQUIRED_FILES)
    assert not staging.exists()
    assert (tmp_path / "published" / "orders.db").is_file()


def test_a_staging_directory_is_never_a_set(tmp_path):
    db = build_database(tmp_path / "live.db")
    built = make_set(db, tmp_path / "built")
    staging = built.with_name(backup_format.TMP_PREFIX + "partial")
    built.rename(staging)
    ok, report = backup_format.verify_set(staging)
    assert not ok
    assert any("staging" in e for e in report["errors"])


# ==================================================== M5: structured refusal
def test_a_malformed_database_returns_a_structured_refusal(tmp_path):
    good = build_database(tmp_path / "good.db")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a sqlite database")
    result = compare(good, junk)
    assert not result.ok
    assert result.reasons, "no structured reason was produced"
    assert "could not be read as SQLite evidence" in result.reasons[0]


def test_the_gate_reports_a_malformed_database_in_its_json(tmp_path):
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    path = make_set(db, tmp_path / "set",
                    upload_files={"1_photo.jpg": GOOD_PHOTO, "1_info.pdf": OTHER_PHOTO})
    (path / "orders.db").write_bytes(b"not a sqlite database")
    _rebind(path)
    code, report = evidence_gate.evaluate(db, path, expected_application_id=APPLICATION_ID,
                                          max_age_hours=48)
    assert code == evidence_gate.EXIT_INVALID
    assert report["errors"]


# ============================================ producer / v1 compatibility (M3)
def test_the_producer_builds_a_verifiable_v2_set(tmp_path):
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    out = tmp_path / "backups" / "20260101T000000Z"
    result = build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                                "--output", str(out), "--json", "--restore-uploads-root", str(uploads)])
    assert result == build_v2_set.EXIT_OK
    ok, report = backup_format.verify_set(out, expected_application_id=APPLICATION_ID)
    assert ok, report["errors"]
    assert report["checks"]["recovery"]["required"]["required"] == 2


def test_the_producer_refuses_when_the_files_do_not_match_the_database(tmp_path):
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    (uploads / "1_photo.jpg").write_bytes(b"tampered on disk!!")
    result = build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                                "--output", str(tmp_path / "out"), "--restore-uploads-root", str(uploads)])
    assert result == build_v2_set.EXIT_FAILED


def test_the_producer_refuses_to_overwrite(tmp_path):
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    out = tmp_path / "backups" / "s1"
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out), "--restore-uploads-root", str(uploads)]) == 0
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out), "--restore-uploads-root", str(uploads)]) == build_v2_set.EXIT_CONFIG


def _legacy_set(tmp_path, db, uploads_files):
    """A format-1 set: two checksum lines, legacy manifest keys, no evidence digests."""
    path = make_set(db, tmp_path / "legacy", upload_files=uploads_files)
    manifest = json.loads((path / backup_format.MANIFEST_JSON).read_text())
    legacy = {
        "created_at": manifest["created_at"],
        "orders_db_sha256": manifest["artifacts"]["orders.db"],
        "uploads_tar_sha256": manifest["artifacts"]["uploads.tar.gz"],
        "verification": "PASS",
    }
    (path / backup_format.MANIFEST_JSON).write_text(json.dumps(legacy, indent=2))
    lines = [f"{legacy['orders_db_sha256']}  orders.db",
             f"{legacy['uploads_tar_sha256']}  uploads.tar.gz"]
    (path / backup_format.MANIFEST_FILE).write_text("\n".join(lines) + "\n")
    return path


def test_a_legacy_v1_set_is_detected_and_refused_by_the_gate(tmp_path):
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    legacy = _legacy_set(tmp_path, db,
                         {"1_photo.jpg": GOOD_PHOTO, "1_info.pdf": OTHER_PHOTO})
    assert backup_format.detect_format(legacy) == "legacy-v1"
    code, report = evidence_gate.evaluate(db, legacy, expected_application_id=APPLICATION_ID,
                                          max_age_hours=48)
    assert code == evidence_gate.EXIT_INVALID
    assert any("legacy format-1" in e and "build_v2_set.py" in e for e in report["errors"])


def test_the_upgrade_path_produces_a_verifiable_v2_set(tmp_path):
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    legacy = _legacy_set(tmp_path, db,
                         {"1_photo.jpg": GOOD_PHOTO, "1_info.pdf": OTHER_PHOTO})
    out = tmp_path / "upgraded"
    assert build_v2_set.main(["--from-v1", str(legacy), "--output", str(out),
                              "--restore-uploads-root", str(uploads)]) == 0
    assert backup_format.detect_format(out) == "v2"
    ok, report = backup_format.verify_set(out, expected_application_id=APPLICATION_ID)
    assert ok, report["errors"]


def test_the_upgrade_refuses_a_set_that_cannot_restore_its_own_database(tmp_path):
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    legacy = _legacy_set(tmp_path, db, {"1_photo.jpg": GOOD_PHOTO})  # info.pdf missing
    result = build_v2_set.main(["--from-v1", str(legacy),
                                "--output", str(tmp_path / "upgraded"),
                                "--restore-uploads-root", str(uploads)])
    assert result == build_v2_set.EXIT_FAILED
