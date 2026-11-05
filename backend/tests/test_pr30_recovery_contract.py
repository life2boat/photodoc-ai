"""Recovery-contract regressions for the second PR #30 review.

One group per finding (`docs/RECOVERY_CONTRACT.md` is the authority), plus a full
producer → verifier → restore rehearsal that checks the restored database and the
restored files, not only the manifest.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evidence_fixtures import APPLICATION_ID, NOW, build_delivery_database

import backup_format
import build_v2_set
import db_evidence
import evidence_gate
import recovery_artifacts

GOOD = b"correct recovery original"
OTHER = b"second recovery original"

P0_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer TEXT,
    filename TEXT,
    payment_status TEXT DEFAULT 'unpaid',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


# --------------------------------------------------------------------- fixtures
def p1_db(path: Path, *, jobs=(), orders=((1, "paid", "tok"),)) -> Path:
    path = Path(path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.executescript(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer TEXT,
            filename TEXT,
            payment_status TEXT,
            order_token_hash TEXT
        );
        CREATE TABLE delivery_jobs (
            id TEXT PRIMARY KEY,
            order_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            local_path TEXT,
            content_sha256 TEXT,
            content_size INTEGER,
            state TEXT NOT NULL DEFAULT 'pending',
            cleanup_state TEXT NOT NULL DEFAULT 'pending',
            cleanup_phase TEXT NOT NULL DEFAULT 'none',
            local_deleted_at TEXT,
            delivered_at TEXT
        );
        """)
    for order_id, status, token in orders:
        conn.execute("INSERT INTO orders (id, customer, filename, payment_status, "
                     "order_token_hash) VALUES (?,?,?,?,?)",
                     (order_id, f"C{order_id}", None, status, token))
    for job in jobs:
        columns = ", ".join(job)
        placeholders = ", ".join("?" for _ in job)
        conn.execute(f"INSERT INTO delivery_jobs ({columns}) VALUES ({placeholders})",
                     tuple(job.values()))
    conn.commit()
    conn.close()
    return path


def p0_db(path: Path, *, filename: str = "photo.jpg", with_jobs: bool = False) -> Path:
    path = Path(path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.executescript(P0_SCHEMA)
    conn.execute("INSERT INTO orders (id, customer, filename, payment_status) "
                 "VALUES (1,'Ann',?,'paid')", (filename,))
    if with_jobs:
        conn.execute(
            "CREATE TABLE delivery_jobs (id TEXT PRIMARY KEY, order_id INTEGER, "
            "kind TEXT, local_path TEXT, content_sha256 TEXT, content_size INTEGER, "
            "state TEXT, cleanup_state TEXT, cleanup_phase TEXT, local_deleted_at TEXT, "
            "delivered_at TEXT)")
    conn.commit()
    conn.close()
    return path


def file_job(*, name="/app/uploads/photo.jpg", data: bytes = GOOD, state="pending",
             cleanup_state="pending", cleanup_phase="none", deleted_at=None,
             delivered_at=None, sha=None, size=None):
    return {"id": f"job-{state}-{cleanup_state}-{cleanup_phase}",
            "order_id": 1, "kind": "file", "local_path": name,
            "content_sha256": sha if sha is not None else
            (hashlib.sha256(data).hexdigest() if data is not None else None),
            "content_size": size if size is not None else
            (len(data) if data is not None else None),
            "state": state, "cleanup_state": cleanup_state,
            "cleanup_phase": cleanup_phase, "local_deleted_at": deleted_at,
            "delivered_at": delivered_at}


def make_archive(members: dict[str, bytes], target: Path) -> Path:
    with tarfile.open(target, "w:gz") as tar:
        root = tarfile.TarInfo("uploads")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, data in members.items():
            member = tarfile.TarInfo(name if name.startswith("uploads/") else f"uploads/{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return target


def build_set(db: Path, dest: Path, members: dict[str, bytes]) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db)
    target = sqlite3.connect(dest / "orders.db")
    source.backup(target)
    target.close()
    source.close()
    archive = make_archive(members, dest / "uploads.tar.gz")
    conn = db_evidence.open_readonly(dest / "orders.db")
    try:
        schema = db_evidence.canonical_schema(conn)
        logical = db_evidence.logical_state_digest(conn)
    finally:
        conn.close()
    info, _ = backup_format.inspect_archive(archive)
    manifest = backup_format.build_manifest(
        artifacts={"orders.db": backup_format.sha256_file(dest / "orders.db"),
                   "uploads.tar.gz": backup_format.sha256_file(archive)},
        created_at=NOW, application_id=APPLICATION_ID,
        schema_digest=schema["digest"], logical_digest=logical,
        database_identity={"application_id": APPLICATION_ID,
                           "schema_digest": schema["digest"],
                           "logical_digest": logical},
        upload_members=info["members"], upload_bytes=info["bytes"])
    backup_format.write_manifest_files(dest, manifest)
    return dest


def rehash(path: Path) -> None:
    lines = [f"{backup_format.sha256_file(path / n)}  {n}"
             for n in backup_format.CHECKSUMMED]
    (path / backup_format.MANIFEST_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ============================================================ HN1: P0 originals
def test_a_p0_database_is_refused_by_default(tmp_path):
    db = p0_db(tmp_path / "p0.db")
    required, groups, errors, stats = recovery_artifacts.required_sources(db)
    assert errors and not required
    assert stats["generation"] == "P0"
    assert any("P0 schema" in e for e in errors)


def test_a_p0_original_cannot_be_silently_dropped(tmp_path):
    db = p0_db(tmp_path / "p0.db")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "photo.jpg").write_bytes(b"legacy pending original")
    code = build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(tmp_path / "out")])
    assert code == build_v2_set.EXIT_FAILED
    assert not (tmp_path / "out").exists()


def test_p0_include_requires_the_original(tmp_path):
    db = p0_db(tmp_path / "p0.db")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "1_photo.jpg").write_bytes(b"legacy pending original")
    out = tmp_path / "out"
    code = build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out), "--legacy-p0-policy", "include"])
    assert code == build_v2_set.EXIT_OK, "a present P0 original must be archived"
    ok, report = backup_format.verify_set(
        out, expected_application_id=APPLICATION_ID, legacy_p0_policy="include")
    assert ok, report["errors"]
    assert report["checks"]["archive"]["files"] == 1


def test_p0_include_refuses_a_missing_original(tmp_path):
    db = p0_db(tmp_path / "p0.db")
    uploads = tmp_path / "uploads"   # empty
    uploads.mkdir()
    code = build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(tmp_path / "out"),
                              "--legacy-p0-policy", "include"])
    assert code == build_v2_set.EXIT_FAILED


def test_p0_include_refuses_an_ambiguous_original(tmp_path):
    db = p0_db(tmp_path / "p0.db")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "1_photo.jpg").write_bytes(b"one")
    (uploads / "photo.jpg").write_bytes(b"two")
    code = build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(tmp_path / "out"),
                              "--legacy-p0-policy", "include"])
    assert code == build_v2_set.EXIT_FAILED


def test_a_p0_set_missing_its_original_is_refused_by_the_verifier(tmp_path):
    db = p0_db(tmp_path / "p0.db")
    setdir = build_set(db, tmp_path / "set", {})
    ok, report = backup_format.verify_set(
        setdir, expected_application_id=APPLICATION_ID, legacy_p0_policy="include")
    assert not ok
    assert any("no archived member matches" in e for e in report["errors"])


def test_mixed_schema_is_refused(tmp_path):
    db = p0_db(tmp_path / "mixed.db", with_jobs=True)
    _required, _groups, errors, stats = recovery_artifacts.required_sources(db)
    assert stats["generation"] == "mixed"
    assert any("mixed schema" in e for e in errors)


# ================================================= HN2: state beats cleanup flags
CONTRADICTIONS = [
    ("pending", "completed", "deleted", "2026-01-01T00:10:00+00:00", None),
    ("pending", "completed", "none", None, None),
    ("pending", "pending", "deleted", None, None),
    ("processing", "completed", "deleted", "2026-01-01T00:10:00+00:00", None),
    ("failed", "completed", "deleted", "2026-01-01T00:10:00+00:00", None),
    ("pending", "pending", "none", "2026-01-01T00:10:00+00:00", None),
    ("delivered", "completed", "none", "2026-01-01T00:10:00+00:00", None),
    ("delivered", "completed", "deleted", None, None),
    ("delivered", "completed", "deleted", "2026-01-01T00:10:00+00:00",
     "2026-01-01T00:10:00+00:00"),
    ("delivered", "pending", "deleted", None, None),
    ("delivered", "not_applicable", "none", None, None),
]


@pytest.mark.parametrize("state,cleanup_state,phase,deleted_at,delivered_at", CONTRADICTIONS)
def test_contradictory_state_and_cleanup_metadata_is_refused(
        tmp_path, state, cleanup_state, phase, deleted_at, delivered_at):
    job = file_job(state=state, cleanup_state=cleanup_state, cleanup_phase=phase,
                   deleted_at=deleted_at, delivered_at=delivered_at)
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    _required, _groups, errors, _stats = recovery_artifacts.required_sources(db)
    assert errors, f"{state}/{cleanup_state}/{phase} was accepted"


def test_a_pending_job_with_contradictory_cleanup_cannot_pass_a_full_set(tmp_path):
    """HN2 end to end: no archive, checksum-bound manifest, gate must refuse."""
    job = file_job(state="pending", cleanup_state="completed", cleanup_phase="deleted",
                   deleted_at="2026-01-01T00:10:00+00:00", name="/app/uploads/missing.jpg")
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    setdir = build_set(db, tmp_path / "set", {})
    ok, report = backup_format.verify_set(setdir, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("contradictory" in e for e in report["errors"])
    code, _ = evidence_gate.evaluate(db, setdir, expected_application_id=APPLICATION_ID,
                                     max_age_hours=48)
    assert code == evidence_gate.EXIT_INVALID


def test_a_delivered_durably_deleted_job_is_exempt(tmp_path):
    job = file_job(state="delivered", cleanup_state="completed", cleanup_phase="deleted",
                   deleted_at="2026-01-01T00:10:00+00:00",
                   delivered_at="2026-01-01T00:05:00+00:00")
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    required, _groups, errors, stats = recovery_artifacts.required_sources(db)
    assert not errors
    assert not required
    assert stats["cleaned"] == 1


def test_a_delivered_uncleaned_job_is_not_required_but_must_not_be_archived(tmp_path):
    job = file_job(state="delivered", cleanup_state="pending", cleanup_phase="none",
                   delivered_at="2026-01-01T00:05:00+00:00")
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    setdir = build_set(db, tmp_path / "set", {"photo.jpg": GOOD})
    ok, report = backup_format.verify_set(setdir, expected_application_id=APPLICATION_ID)
    assert not ok
    assert report["checks"]["recovery"]["unexpected"] == ["uploads/photo.jpg"]


# ======================================================= HN3: exact archive path
def test_a_nested_member_does_not_satisfy_a_required_file(tmp_path):
    job = file_job(name="/app/uploads/photo.jpg")
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    setdir = build_set(db, tmp_path / "set", {"uploads/nested/photo.jpg": GOOD})
    ok, report = backup_format.verify_set(setdir, expected_application_id=APPLICATION_ID)
    assert not ok
    assert report["checks"]["recovery"]["missing"] == ["uploads/photo.jpg"]


class TestArchivePathContract:
    @pytest.mark.parametrize("name", ["uploads/nested/photo.jpg", "uploads/a/b/c.jpg"])
    def test_nested_paths_are_refused(self, name):
        assert recovery_artifacts.path_errors(name)

    def test_exact_paths_are_accepted(self):
        assert not recovery_artifacts.path_errors("uploads/photo.jpg")
        assert not recovery_artifacts.path_errors("uploads")


def test_a_nested_local_path_in_the_database_is_refused(tmp_path):
    job = file_job(name="/app/uploads/sub/photo.jpg")
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    _required, _groups, errors, _stats = recovery_artifacts.required_sources(db)
    assert any("not directly inside" in e for e in errors)


# ==================================================== HN4: real application fixture
@pytest.fixture(scope="module")
def real_fixture(tmp_path_factory):
    from backup_set_builder import build_database

    root = tmp_path_factory.mktemp("real")
    build_database(root / "orders.db", root / "uploads")
    return root


def test_the_producer_accepts_a_real_application_database(real_fixture, tmp_path):
    out = tmp_path / "backups" / "real"
    code = build_v2_set.main(["--live-db", str(real_fixture / "orders.db"),
                              "--uploads", str(real_fixture / "uploads"),
                              "--output", str(out)])
    assert code == build_v2_set.EXIT_OK, "the producer rejected a real application fixture"
    ok, report = backup_format.verify_set(out, expected_application_id=APPLICATION_ID)
    assert ok, report["errors"]
    required = report["checks"]["recovery"]["required"]
    assert required["generation"] == "P1"
    assert required["required"] >= 1
    assert required["email_jobs"] >= 1, "email jobs must be classified as database-backed"


def test_the_v1_upgrade_accepts_a_real_application_database(real_fixture, tmp_path):
    legacy = tmp_path / "legacy"
    built, _ = _legacy_from_real(real_fixture, legacy)
    out = tmp_path / "upgraded"
    code = build_v2_set.main(["--from-v1", str(built), "--output", str(out)])
    assert code == build_v2_set.EXIT_OK, "the upgrade rejected a real application fixture"
    ok, report = backup_format.verify_set(out, expected_application_id=APPLICATION_ID)
    assert ok, report["errors"]


def _legacy_from_real(real_fixture: Path, legacy: Path):
    """A v1-shaped set built from the real fixture, as create_backup.py would leave it."""
    legacy.mkdir(parents=True, exist_ok=True)
    shutil.copy2(real_fixture / "orders.db", legacy / "orders.db")
    required, _groups, errors, _stats = recovery_artifacts.required_sources(legacy / "orders.db")
    assert not errors, errors
    members = {}
    for member in required:
        source = real_fixture / "uploads" / Path(member).name
        members[member] = source.read_bytes()
    make_archive(members, legacy / "uploads.tar.gz")
    manifest = {
        "created_at": NOW.isoformat(),
        "orders_db_sha256": backup_format.sha256_file(legacy / "orders.db"),
        "uploads_tar_sha256": backup_format.sha256_file(legacy / "uploads.tar.gz"),
        "verification": "PASS",
    }
    (legacy / "backup_manifest.json").write_text(json.dumps(manifest, indent=2))
    (legacy / "manifest.sha256").write_text(
        f"{manifest['orders_db_sha256']}  orders.db\n"
        f"{manifest['uploads_tar_sha256']}  uploads.tar.gz\n", encoding="utf-8")
    return legacy, manifest


# ================================================================ HN5: no leak
def test_the_producer_leaves_no_database_copy_outside_the_set(tmp_path):
    job = file_job()
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "photo.jpg").write_bytes(GOOD)

    created: list[Path] = []
    original = __import__("tempfile").mkdtemp

    import tempfile as tempfile_module

    def tracking(*args, **kwargs):
        directory = Path(original(*args, **kwargs))
        created.append(directory)
        return str(directory)

    tempfile_module.mkdtemp = tracking
    try:
        code = build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                                  "--output", str(tmp_path / "out")])
    finally:
        tempfile_module.mkdtemp = original
    assert code == build_v2_set.EXIT_OK
    leaked = [d for d in created if any(d.rglob("*.db"))]
    assert not leaked, f"a database copy was left in a temporary directory: {leaked}"


def test_a_failed_production_removes_its_staging_copy(tmp_path):
    job = file_job()
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    uploads = tmp_path / "uploads"
    uploads.mkdir()   # the required file is missing, so production fails
    code = build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(tmp_path / "out")])
    assert code == build_v2_set.EXIT_FAILED
    leftovers = list(tmp_path.glob(f"{backup_format.TMP_PREFIX}*"))
    assert not leftovers, f"staging directories were left behind: {leftovers}"


# ============================================================== kinds and policy
def test_email_jobs_are_database_backed(tmp_path):
    jobs = (
        {"id": "m1", "order_id": 1, "kind": "admin_email", "local_path": None,
         "content_sha256": None, "content_size": None, "state": "pending",
         "cleanup_state": "not_applicable", "cleanup_phase": "none"},
        {"id": "m2", "order_id": 1, "kind": "paid_email", "local_path": None,
         "content_sha256": None, "content_size": None, "state": "pending",
         "cleanup_state": "not_applicable", "cleanup_phase": "none"},
    )
    db = p1_db(tmp_path / "db.db", jobs=jobs)
    required, _groups, errors, stats = recovery_artifacts.required_sources(db)
    assert not errors
    assert not required
    assert stats["email_jobs"] == 2


def test_an_email_job_with_a_file_is_contradictory(tmp_path):
    job = {"id": "m1", "order_id": 1, "kind": "admin_email",
           "local_path": "/app/uploads/photo.jpg", "content_sha256": None,
           "content_size": None, "state": "pending", "cleanup_state": "pending",
           "cleanup_phase": "none"}
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    _required, _groups, errors, _stats = recovery_artifacts.required_sources(db)
    assert any("database-backed" in e for e in errors)


def test_an_unknown_job_kind_is_refused(tmp_path):
    job = {"id": "x", "order_id": 1, "kind": "mystery", "local_path": None,
           "content_sha256": None, "content_size": None, "state": "pending",
           "cleanup_state": "pending", "cleanup_phase": "none"}
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    _required, _groups, errors, _stats = recovery_artifacts.required_sources(db)
    assert any("unknown kind" in e for e in errors)


def test_info_jobs_are_upload_backed(tmp_path):
    data = OTHER
    job = file_job(name="/app/uploads/info.pdf", data=data)
    job["kind"] = "info"
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    required, _groups, errors, _stats = recovery_artifacts.required_sources(db)
    assert not errors
    assert "uploads/info.pdf" in required


# ============================================== unexpected contents / private files
def test_an_extra_archived_file_is_refused(tmp_path):
    job = file_job()
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    setdir = build_set(db, tmp_path / "set",
                       {"photo.jpg": GOOD, "deleted-private-photo.jpg": b"private"})
    ok, report = backup_format.verify_set(setdir, expected_application_id=APPLICATION_ID)
    assert not ok
    assert report["checks"]["recovery"]["unexpected"] == ["uploads/deleted-private-photo.jpg"]
    assert any("must not retain" in e for e in report["errors"])


def test_a_set_with_exactly_the_required_files_passes(tmp_path):
    job = file_job()
    db = p1_db(tmp_path / "db.db", jobs=(job,))
    setdir = build_set(db, tmp_path / "set", {"photo.jpg": GOOD})
    ok, report = backup_format.verify_set(setdir, expected_application_id=APPLICATION_ID)
    assert ok, report["errors"]


# ============================================================ manifest validation
def _simple_set(tmp_path):
    db = p1_db(tmp_path / "db.db")
    return db, build_set(db, tmp_path / "set", {})


@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m.update({"set_id": "NOT-A-UUID"}), "set_id"),
    (lambda m: m["database_identity"].update({"schema_digest": None}), "schema_digest"),
    (lambda m: m["database_identity"].update({"logical_digest": None}), "logical_digest"),
    (lambda m: m["uploads"].update({"injected": 1}), "unknown fields"),
    (lambda m: m.update({"evidence_format": 999}), "evidence_format"),
    (lambda m: m.update({"trust_note": "no boundary"}), "trust_note"),
])
def test_manifest_claims_are_validated(tmp_path, mutate, needle):
    _db, setdir = _simple_set(tmp_path)
    manifest = json.loads((setdir / "backup_manifest.json").read_text())
    mutate(manifest)
    (setdir / "backup_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    rehash(setdir)
    ok, report = backup_format.verify_set(setdir, expected_application_id=APPLICATION_ID)
    assert not ok, f"{needle} was accepted"
    assert any(needle in e for e in report["errors"]), report["errors"]


# ============================================================ atomic publication
def test_publication_aborts_on_a_file_fsync_failure(tmp_path, monkeypatch):
    _db, built = _simple_set(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    for name in backup_format.REQUIRED_FILES:
        (staging / name).write_bytes((built / name).read_bytes())

    def failing(path):
        raise backup_format.FormatError(f"simulated I/O failure flushing {path}")

    monkeypatch.setattr(backup_format, "fsync_file", failing)
    with pytest.raises(backup_format.FormatError):
        backup_format.publish_set(staging, tmp_path / "published")
    assert not (tmp_path / "published").exists()
    assert staging.is_dir(), "the staging set must survive for inspection"


def test_publication_reports_an_unsupported_platform(tmp_path, monkeypatch):
    _db, built = _simple_set(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    for name in backup_format.REQUIRED_FILES:
        (staging / name).write_bytes((built / name).read_bytes())
    monkeypatch.setattr(backup_format, "fsync_file", lambda path: "unsupported")
    report = backup_format.publish_set(staging, tmp_path / "published")
    assert report["fsync_supported"] is False
    assert (tmp_path / "published").is_dir(), "an unsupported platform may still publish"


def test_publication_refuses_to_overwrite(tmp_path):
    _db, built = _simple_set(tmp_path)
    target = tmp_path / "published"
    staging = tmp_path / "s1"
    staging.mkdir()
    for name in backup_format.REQUIRED_FILES:
        (staging / name).write_bytes((built / name).read_bytes())
    backup_format.publish_set(staging, target)
    staging2 = tmp_path / "s2"
    staging2.mkdir()
    for name in backup_format.REQUIRED_FILES:
        (staging2 / name).write_bytes((built / name).read_bytes())
    with pytest.raises(backup_format.FormatError):
        backup_format.publish_set(staging2, target)


# ================================================================ gzip integrity
@pytest.mark.parametrize("drop", [1, 4, 8, 16, 32])
def test_a_truncated_gzip_trailer_is_refused(tmp_path, drop):
    _db, setdir = _simple_set(tmp_path)
    archive = setdir / "uploads.tar.gz"
    data = archive.read_bytes()
    archive.write_bytes(data[:-drop])
    _, errors = backup_format.inspect_archive(archive)
    assert errors, f"{drop} bytes removed from the trailer was accepted"


# ============================================= producer → verifier → restore rehearsal
def test_producer_verifier_restore_rehearsal(tmp_path):
    """Build a set from a realistic database, verify it, restore it, and check the
    restored database and the restored files — not only the manifest."""
    uploads = tmp_path / "uploads"
    db = build_delivery_database(tmp_path / "live.db", uploads)
    out = tmp_path / "backups" / "rehearsal"
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out)]) == build_v2_set.EXIT_OK

    ok, report = backup_format.verify_set(out, expected_application_id=APPLICATION_ID)
    assert ok, report["errors"]

    # ---- restore ----
    restore = tmp_path / "restore"
    restore_uploads = restore / "uploads"
    restore_uploads.mkdir(parents=True)
    shutil.copy2(out / "orders.db", restore / "orders.db")
    with tarfile.open(out / "uploads.tar.gz", "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            assert member.name.startswith("uploads/")
            target = restore_uploads / member.name.split("/", 1)[1]
            with tar.extractfile(member) as handle:
                target.write_bytes(handle.read())

    restored_db = restore / "orders.db"
    conn = sqlite3.connect(restored_db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        conn.row_factory = sqlite3.Row
        jobs = conn.execute(
            "SELECT id, kind, state, local_path, content_sha256 FROM delivery_jobs").fetchall()
    finally:
        conn.close()

    restored_schema = db_evidence.canonical_schema(db_evidence.open_readonly(restored_db))
    manifest = json.loads((out / "backup_manifest.json").read_text())
    assert restored_schema["digest"] == manifest["schema_digest"]

    required, _groups, errors, stats = recovery_artifacts.required_sources(restored_db)
    assert not errors
    for member, spec in required.items():
        restored_file = restore_uploads / Path(member).name
        assert restored_file.is_file(), f"{member} was not restored"
        assert hashlib.sha256(restored_file.read_bytes()).hexdigest() == spec["sha256"]
        assert restored_file.stat().st_size == spec["size"]

    file_backed = [job for job in jobs if job["kind"] in ("file", "info")
                   and job["local_path"]]
    assert file_backed, "the rehearsal needs at least one file-backed job"
    for job in file_backed:
        name = Path(job["local_path"]).name
        assert (restore_uploads / name).exists() or job["state"] == "delivered"
    assert stats["required"] == len(required) >= 1
